import os
import sys
import time
import shutil
import logging
import threading
import datetime
from pathlib import Path
from gi.repository import Gtk, Gdk, GLib, Gio, GObject, Pango
from ui.compat import Adw, HAS_ADW

class HistoryDialog:
    """Save State & Battery Save File History Dialog Controller"""

    def __init__(self, parent_window, library_section=None):
        self.parent = parent_window
        self.library = library_section

    def show_state_history(self, game, name):
        """Open save state history browser"""
        self._history_game = game
        rom_id = game.get('rom_id')
        self._history_rom_id = rom_id
        self.parent.log_message(f"📜 Loading save state history for {name}…")

        def worker():
            saves, states = self.parent.romm_client.get_save_history(rom_id)
            GLib.idle_add(self._show_history_dialog, game, name, saves, states, 'states')

        threading.Thread(target=worker, daemon=True).start()

    def show_save_file_history(self, game, name):
        """Open save file history browser"""
        self._history_game = game
        rom_id = game.get('rom_id')
        self._history_rom_id = rom_id
        self.parent.log_message(f"📜 Loading save file history for {name}…")

        def worker():
            saves, states = self.parent.romm_client.get_save_history(rom_id)
            GLib.idle_add(self._show_history_dialog, game, name, saves, states, 'saves')

        threading.Thread(target=worker, daemon=True).start()

    def _fetch_save_history(self, rom_id):
        """Fetch all server saves/states for a ROM (delegates to sync_core)."""
        return self.parent.romm_client.get_save_history(rom_id)

    def _fmt_ts(self, iso):
        if not iso:
            return "Unknown time"
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
            return dt.astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:
            return str(iso)

    def _fmt_size(self, n):
        try:
            n = float(n)
        except Exception:
            return ""
        for unit in ('B', 'KB', 'MB', 'GB'):
            if n < 1024:
                return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    def _entry_device(self, e):
        ds = e.get('device_syncs') or e.get('deviceSyncs')
        if isinstance(ds, list) and ds and isinstance(ds[0], dict):
            return ds[0].get('device_name') or ds[0].get('name')
        return None

    def _get_target_state_directory(self, states_path, game):
        """Find the exact state directory used by RetroArch for this game/core.
        Handles case-insensitivity, RetroArch core directory maps (e.g. Snes9x, mGBA, Beetle PSX HW),
        and prevents fallback to 'Unknown'.
        """
        states_path = Path(states_path)
        if not states_path.exists():
            states_path.mkdir(parents=True, exist_ok=True)

        # 1. Safely extract platform_name and platform_slug from game dictionary
        platform_val = game.get('platform')
        if isinstance(platform_val, dict):
            platform_name = platform_val.get('name', '') or platform_val.get('slug', '')
            platform_slug = platform_val.get('slug', '') or platform_val.get('name', '')
        elif isinstance(platform_val, str):
            platform_name = platform_val
            platform_slug = game.get('platform_slug', '') or platform_val
        else:
            platform_name = game.get('platform_name', '') or ''
            platform_slug = game.get('platform_slug', '') or ''

        if platform_name == 'Unknown':
            platform_name = ''
        if platform_slug == 'Unknown':
            platform_slug = ''

        game_name = game.get('name', '')
        file_name = game.get('file_name', '')
        stem = (Path(file_name).stem if file_name else game_name).lower()

        # Gather existing subdirectories in states_path
        subdirs = []
        try:
            subdirs = [d for d in states_path.iterdir() if d.is_dir() and d.name != 'Unknown']
        except Exception:
            pass

        # 2. Check if an existing subdirectory ALREADY has save states for this game stem
        for d in subdirs:
            try:
                for f in d.iterdir():
                    if f.is_file() and stem in f.name.lower() and '.state' in f.name.lower():
                        return d
            except Exception:
                pass

        # Also check root states_path if it has state files matching stem
        try:
            for f in states_path.iterdir():
                if f.is_file() and stem in f.name.lower() and '.state' in f.name.lower():
                    return states_path
        except Exception:
            pass

        # 3. Gather candidate directory names from RetroArch platform/core maps
        candidate_dir_names = []

        if hasattr(self.parent, 'retroarch') and self.parent.retroarch:
            ra = self.parent.retroarch

            # Check if retroarch interface can resolve candidate cores for this platform
            cores = []
            if platform_slug and hasattr(ra, 'platform_core_map'):
                cores.extend(ra.platform_core_map.get(platform_slug, []))
            if platform_name and hasattr(ra, 'platform_core_map'):
                cores.extend(ra.platform_core_map.get(platform_name, []))

            if hasattr(ra, 'get_core_from_platform_slug') and platform_slug:
                core_hint = ra.get_core_from_platform_slug(platform_slug)
                if core_hint and core_hint not in cores:
                    cores.insert(0, core_hint)

            emu_map = getattr(ra, 'emulator_directory_map', {}) or {}
            for c in cores:
                c_clean = c.replace('_libretro', '').lower()
                mapped_name = emu_map.get(c_clean) or emu_map.get(c)
                if mapped_name and mapped_name not in candidate_dir_names:
                    candidate_dir_names.append(mapped_name)
                # Also add standard variations
                for var in (c, c_clean, f"{c}_libretro"):
                    if var and var not in candidate_dir_names:
                        candidate_dir_names.append(var)

        if platform_name and platform_name not in candidate_dir_names:
            candidate_dir_names.append(platform_name)
        if platform_slug and platform_slug not in candidate_dir_names:
            candidate_dir_names.append(platform_slug)

        # Case-insensitive check against existing subdirectories
        candidate_lowers = {c.lower(): c for c in candidate_dir_names}
        for d in subdirs:
            if d.name.lower() in candidate_lowers:
                return d

        # 4. If no existing directory matched, create the best candidate directory
        if candidate_dir_names:
            target_name = candidate_dir_names[0]
            target_dir = states_path / target_name
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir

        return states_path

    def _fetch_local_save_states(self, game):
        """Scan local states directory and return list of all save state files for this game.
        Handles case insensitivity and core subdirectories (e.g. Snes9x, snes9x, platform dirs).
        """
        local_states = []
        if not game:
            return local_states

        states_dir = None
        if hasattr(self.parent, 'retroarch'):
            save_dirs = getattr(self.parent.retroarch, 'save_dirs', {}) or {}
            states_dir = save_dirs.get('states')
            if not states_dir:
                try:
                    dirs = self.parent.retroarch.find_retroarch_dirs()
                    states_dir = dirs.get('states')
                except Exception:
                    pass

        if not states_dir or not Path(states_dir).exists():
            return local_states

        states_path = Path(states_dir)
        game_name = game.get('name', '')
        file_name = game.get('file_name', '')
        stem = (Path(file_name).stem if file_name else game_name).lower()

        # Build list of directories to scan: root states_path and ALL subdirectories
        search_dirs = [states_path]
        try:
            for sub_d in states_path.iterdir():
                if sub_d.is_dir():
                    search_dirs.append(sub_d)
        except Exception:
            pass

        found_paths = set()
        for d in search_dirs:
            if not d.exists() or not d.is_dir():
                continue
            try:
                for f in d.iterdir():
                    if f.is_file() and not f.name.endswith('.png') and not f.name.endswith('.backup'):
                        lower_name = f.name.lower()
                        if stem in lower_name and '.state' in lower_name:
                            full_str = str(f.resolve())
                            if full_str in found_paths:
                                continue
                            found_paths.add(full_str)

                            # Match slot pattern
                            idx = lower_name.find('.state')
                            slot_str = lower_name[idx:] if idx != -1 else ''

                            if slot_str == '.state':
                                slot_name = "Slot 0 (Default)"
                                slot_code = ".state"
                            elif slot_str in ('.state.auto', 'auto'):
                                slot_name = "Auto Save"
                                slot_code = ".state.auto"
                            elif slot_str in ('.state.qsv', '.qsv'):
                                slot_name = "Quicksave"
                                slot_code = ".state.qsv"
                            else:
                                clean_num = slot_str.replace('.state', '').lstrip('.')
                                slot_name = f"Slot {clean_num}" if clean_num else "Slot 0 (Default)"
                                slot_code = f".state{clean_num}" if clean_num else ".state"

                            mtime = f.stat().st_mtime
                            dt_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                            file_size = f.stat().st_size

                            # Look for matching PNG screenshot
                            png_path = f.with_name(f.name + '.png')
                            if not png_path.exists():
                                png_path = f.with_suffix('.png')
                            has_thumb = png_path.exists() and png_path.stat().st_size > 0

                            local_states.append({
                                'file_path': str(f),
                                'file_name': f.name,
                                'slot_name': slot_name,
                                'slot_code': slot_code,
                                'timestamp': dt_str,
                                'mtime': mtime,
                                'size_bytes': file_size,
                                'png_path': str(png_path) if has_thumb else None,
                                'is_synced': False
                            })
            except Exception as scan_e:
                print(f"Error scanning directory {d}: {scan_e}")

        local_states.sort(key=lambda x: x['mtime'], reverse=True)
        return local_states

    def _get_target_save_directory(self, saves_path, game):
        """Find the exact battery saves directory used by RetroArch for this game/core."""
        saves_path = Path(saves_path)
        if not saves_path.exists():
            saves_path.mkdir(parents=True, exist_ok=True)

        platform_val = game.get('platform')
        if isinstance(platform_val, dict):
            platform_name = platform_val.get('name', '') or platform_val.get('slug', '')
            platform_slug = platform_val.get('slug', '') or platform_val.get('name', '')
        elif isinstance(platform_val, str):
            platform_name = platform_val
            platform_slug = game.get('platform_slug', '') or platform_val
        else:
            platform_name = game.get('platform_name', '') or ''
            platform_slug = game.get('platform_slug', '') or ''

        if platform_name == 'Unknown': platform_name = ''
        if platform_slug == 'Unknown': platform_slug = ''

        game_name = game.get('name', '')
        file_name = game.get('file_name', '')
        stem = (Path(file_name).stem if file_name else game_name).lower()

        subdirs = []
        try:
            subdirs = [d for d in saves_path.iterdir() if d.is_dir() and d.name != 'Unknown']
        except Exception:
            pass

        for d in subdirs:
            try:
                for f in d.iterdir():
                    if f.is_file() and stem in f.name.lower() and not f.name.endswith('.png') and not f.name.endswith('.backup') and '.state' not in f.name.lower():
                        return d
            except Exception:
                pass

        try:
            for f in saves_path.iterdir():
                if f.is_file() and stem in f.name.lower() and not f.name.endswith('.png') and not f.name.endswith('.backup') and '.state' not in f.name.lower():
                    return saves_path
        except Exception:
            pass

        candidate_dir_names = []
        if hasattr(self.parent, 'retroarch') and self.parent.retroarch:
            ra = self.parent.retroarch
            cores = []
            if platform_slug and hasattr(ra, 'platform_core_map'):
                cores.extend(ra.platform_core_map.get(platform_slug, []))
            if platform_name and hasattr(ra, 'platform_core_map'):
                cores.extend(ra.platform_core_map.get(platform_name, []))

            if hasattr(ra, 'get_core_from_platform_slug') and platform_slug:
                core_hint = ra.get_core_from_platform_slug(platform_slug)
                if core_hint and core_hint not in cores:
                    cores.insert(0, core_hint)

            emu_map = getattr(ra, 'emulator_directory_map', {}) or {}
            for c in cores:
                c_clean = c.replace('_libretro', '').lower()
                mapped_name = emu_map.get(c_clean) or emu_map.get(c)
                if mapped_name and mapped_name not in candidate_dir_names:
                    candidate_dir_names.append(mapped_name)
                for var in (c, c_clean, f"{c}_libretro"):
                    if var and var not in candidate_dir_names:
                        candidate_dir_names.append(var)

        if platform_name and platform_name not in candidate_dir_names:
            candidate_dir_names.append(platform_name)
        if platform_slug and platform_slug not in candidate_dir_names:
            candidate_dir_names.append(platform_slug)

        candidate_lowers = {c.lower(): c for c in candidate_dir_names}
        for d in subdirs:
            if d.name.lower() in candidate_lowers:
                return d

        if candidate_dir_names:
            target_name = candidate_dir_names[0]
            target_dir = saves_path / target_name
            target_dir.mkdir(parents=True, exist_ok=True)
            return target_dir

        return saves_path

    def _fetch_local_save_files(self, game):
        """Scan local saves directory and return list of all battery save files for this game."""
        local_saves = []
        if not game:
            return local_saves

        saves_dir = None
        if hasattr(self.parent, 'retroarch'):
            save_dirs = getattr(self.parent.retroarch, 'save_dirs', {}) or {}
            saves_dir = save_dirs.get('saves')
            if not saves_dir:
                try:
                    dirs = self.parent.retroarch.find_retroarch_dirs()
                    saves_dir = dirs.get('saves')
                except Exception:
                    pass

        if not saves_dir or not Path(saves_dir).exists():
            return local_saves

        saves_path = Path(saves_dir)
        game_name = game.get('name', '')
        file_name = game.get('file_name', '')
        stem = (Path(file_name).stem if file_name else game_name).lower()

        search_dirs = [saves_path]
        try:
            for sub_d in saves_path.iterdir():
                if sub_d.is_dir():
                    search_dirs.append(sub_d)
        except Exception:
            pass

        found_paths = set()
        for d in search_dirs:
            if not d.exists() or not d.is_dir():
                continue
            try:
                for f in d.iterdir():
                    if f.is_file() and not f.name.endswith('.png') and not f.name.endswith('.backup') and '.state' not in f.name.lower():
                        lower_name = f.name.lower()
                        if stem in lower_name:
                            full_str = str(f.resolve())
                            if full_str in found_paths:
                                continue
                            found_paths.add(full_str)

                            mtime = f.stat().st_mtime
                            dt_str = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
                            file_size = f.stat().st_size

                            png_path = f.with_name(f.name + '.png')
                            if not png_path.exists():
                                png_path = f.with_suffix('.png')
                            has_thumb = png_path.exists() and png_path.stat().st_size > 0

                            local_saves.append({
                                'file_path': str(f),
                                'file_name': f.name,
                                'slot_name': f.name,
                                'slot_code': f.suffix,
                                'timestamp': dt_str,
                                'mtime': mtime,
                                'size_bytes': file_size,
                                'png_path': str(png_path) if has_thumb else None,
                                'is_synced': False
                            })
            except Exception as scan_e:
                print(f"Error scanning directory {d}: {scan_e}")

        local_saves.sort(key=lambda x: x['mtime'], reverse=True)
        return local_saves

    def _cross_reference_synced_states(self, local_states, server_states):
        """Mark local save states as synced if matching entry exists on server."""
        for loc in local_states:
            loc_sz = loc.get('size_bytes', 0)
            loc_slot = loc.get('slot_code', '')
            for srv in server_states:
                if not isinstance(srv, dict):
                    continue
                srv_sz = srv.get('size_bytes') or srv.get('file_size_bytes', 0)
                srv_fn = (srv.get('file_name') or '').lower()
                srv_slot = srv.get('slot') or ''
                if (srv_sz > 0 and abs(srv_sz - loc_sz) < 512) or (srv_slot and srv_slot in loc_slot) or (loc_slot and loc_slot in srv_fn):
                    loc['is_synced'] = True
                    break

    def _show_history_dialog(self, game, name, saves, states, mode='states'):
        """Three-pane Save State / Save File Browser:
        Top Left: Local Save States/Files (synced green icon & Upload button)
        Top Right: Server Save States/Files (timestamp & Restore/Replace action)
        Bottom Center: Screenshot Preview & Details
        """
        self._shot_cache = {}
        self._current_entry = None
        self._current_type = None
        self._history_mode = mode
        self._history_game = game
        self._history_saves = saves
        self._history_states = states

        win = Adw.Window()
        self._history_win = win
        win_title = f"Save File History — {name}" if mode == 'saves' else f"Save State History — {name}"
        win.set_title(win_title)
        win.set_modal(True)
        win.set_transient_for(self.parent)
        win.set_default_size(920, 440 if mode == 'saves' else 680)

        toolbar_view = Adw.ToolbarView()
        header_bar = Adw.HeaderBar()

        # Refresh button & busy indicator
        self._history_refresh_btn = Gtk.Button()
        self._history_refresh_btn.add_css_class('image-button')
        self._history_refresh_btn.set_tooltip_text("Refresh history from server and local disk")
        self._history_refresh_btn.connect('clicked', lambda b: self._refresh_history())

        self._rb_icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        self._rb_icon.set_pixel_size(16)
        self._rb_spinner = Gtk.Spinner()
        self._rb_check = Gtk.Label()
        self._rb_check.set_markup('<span foreground="#4ade80">✓</span>')
        self._rb_stack = Gtk.Stack()
        self._rb_stack.set_hhomogeneous(True)
        self._rb_stack.set_vhomogeneous(True)
        for nm, w in (('idle', self._rb_icon), ('busy', self._rb_spinner), ('done', self._rb_check)):
            w.set_halign(Gtk.Align.CENTER)
            w.set_valign(Gtk.Align.CENTER)
            w.set_size_request(16, 16)
            self._rb_stack.add_named(w, nm)
        self._history_refresh_btn.set_child(self._rb_stack)

        self._history_busy_label = Gtk.Label()
        self._history_busy_label.add_css_class('dim-label')
        self._history_busy_label.set_xalign(0)
        self._history_busy_label.set_visible(False)
        self._set_refresh_btn_state('idle')

        status_box = Gtk.Box(spacing=6)
        status_box.append(self._history_refresh_btn)
        status_box.append(self._history_busy_label)
        header_bar.pack_start(status_box)
        toolbar_view.add_top_bar(header_bar)

        # MAIN VBOX
        main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        main_vbox.set_margin_top(8); main_vbox.set_margin_bottom(8)
        main_vbox.set_margin_start(12); main_vbox.set_margin_end(12)

        # TOP SPLIT PANE (HORIZONTAL)
        top_split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        if mode == 'saves':
            top_split.set_vexpand(True)
        else:
            top_split.set_size_request(-1, 320)

        # TOP LEFT PANE
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        left_box.set_hexpand(True)
        left_title = Gtk.Label()
        left_heading = "<b>Local Save Files (On Device)</b>" if mode == 'saves' else "<b>Local Save States (On Device)</b>"
        left_title.set_markup(left_heading)
        left_title.set_halign(Gtk.Align.START)
        left_box.append(left_title)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        left_scroll.set_vexpand(True)
        left_scroll.add_css_class("data-table")

        self._local_history_listbox = Gtk.ListBox()
        self._local_history_listbox.add_css_class('navigation-sidebar')
        self._local_history_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._local_history_listbox.connect('row-selected', self._on_local_row_selected)
        left_scroll.set_child(self._local_history_listbox)
        left_box.append(left_scroll)

        # TOP RIGHT PANE
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right_box.set_hexpand(True)
        right_title = Gtk.Label()
        right_heading = "<b>Server Save Files (RomM)</b>" if mode == 'saves' else "<b>Server Save States (RomM)</b>"
        right_title.set_markup(right_heading)
        right_title.set_halign(Gtk.Align.START)
        right_box.append(right_title)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        right_scroll.set_vexpand(True)
        right_scroll.add_css_class("data-table")

        self._server_history_listbox = Gtk.ListBox()
        self._server_history_listbox.add_css_class('navigation-sidebar')
        self._server_history_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._server_history_listbox.connect('row-selected', self._on_server_row_selected)
        right_scroll.set_child(self._server_history_listbox)
        right_box.append(right_scroll)

        top_split.append(left_box)
        top_split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        top_split.append(right_box)
        main_vbox.append(top_split)

        if mode != 'saves':
            main_vbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            # BOTTOM CENTER PANE: Screenshot Preview
            bottom_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            bottom_box.set_vexpand(True)

            bottom_title = Gtk.Label()
            bottom_title.set_markup("<b>Save State Screenshot Preview</b>")
            bottom_title.set_halign(Gtk.Align.CENTER)
            bottom_box.append(bottom_title)

            self._preview_picture = Gtk.Picture()
            self._preview_picture.set_size_request(340, 200)
            self._preview_picture.set_halign(Gtk.Align.CENTER)
            if hasattr(self._preview_picture, 'set_content_fit') and hasattr(Gtk, 'ContentFit'):
                self._preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)

            self._preview_status = Gtk.Label(label="Select a local or server item above to view preview")
            self._preview_status.add_css_class('dim-label')
            self._preview_status.set_wrap(True)
            self._preview_status.set_justify(Gtk.Justification.CENTER)
            self._preview_status.set_halign(Gtk.Align.CENTER)
            self._preview_status.set_valign(Gtk.Align.CENTER)

            preview_overlay = Gtk.Overlay()
            preview_overlay.set_child(self._preview_picture)
            preview_overlay.add_overlay(self._preview_status)
            preview_overlay.set_halign(Gtk.Align.CENTER)

            pic_frame = Gtk.Frame()
            pic_frame.set_child(preview_overlay)
            pic_frame.set_halign(Gtk.Align.CENTER)
            bottom_box.append(pic_frame)

            self._preview_info = Gtk.Label(label="")
            self._preview_info.set_halign(Gtk.Align.CENTER)
            self._preview_info.set_wrap(True)
            bottom_box.append(self._preview_info)

            main_vbox.append(bottom_box)
        else:
            self._preview_picture = None
            self._preview_status = None
            self._preview_info = None

        toolbar_view.set_content(main_vbox)
        win.set_content(toolbar_view)

        # Initial population
        if mode == 'saves':
            local_entries = self._fetch_local_save_files(game)
            server_entries = saves
        else:
            local_entries = self._fetch_local_save_states(game)
            server_entries = states

        self._cross_reference_synced_states(local_entries, server_entries)
        self._fill_local_history_list(local_entries, mode=mode)
        self._fill_server_history_list(saves, states, mode=mode)

        win.present()
        self._select_first_history_row()

    def _fill_local_history_list(self, local_entries, mode='states'):
        """Populate Top Left pane with local save state or save file rows."""
        listbox = getattr(self, '_local_history_listbox', None)
        if listbox is None:
            return
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt

        if not local_entries:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            msg = "No local save files found on device" if mode == 'saves' else "No local save states found on device"
            lbl = Gtk.Label(label=msg)
            lbl.add_css_class('dim-label')
            lbl.set_margin_top(12); lbl.set_margin_bottom(12)
            row.set_child(lbl)
            listbox.append(row)
            return

        for loc in local_entries:
            row = Gtk.ListBoxRow()
            row._entry = loc
            row._source = 'local'

            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hb.set_margin_top(6); hb.set_margin_bottom(6)
            hb.set_margin_start(8); hb.set_margin_end(8)

            dot = Gtk.Label()
            if loc.get('is_synced'):
                dot.set_markup('<span foreground="#4ade80">●</span>')
                dot.set_tooltip_text("Synced with RomM server")
            else:
                dot.set_markup('<span foreground="#6b7280">○</span>')
                dot.set_tooltip_text("Local only (not on server)")
            hb.append(dot)

            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            vb.set_hexpand(True)
            t = Gtk.Label()
            t.set_xalign(0)
            t.set_markup(f"<b>{GLib.markup_escape_text(loc.get('slot_name', 'Save File'))}</b> — {GLib.markup_escape_text(loc.get('timestamp', ''))}")
            vb.append(t)

            sz_str = self._fmt_size(loc.get('size_bytes', 0))
            s = Gtk.Label()
            s.set_xalign(0)
            s.add_css_class('dim-label')
            s.set_markup(f"<small>{GLib.markup_escape_text(sz_str)} · {GLib.markup_escape_text(loc.get('file_name', ''))}</small>")
            vb.append(s)
            hb.append(vb)

            upload_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
            upload_btn.set_tooltip_text("Upload to RomM Server")
            upload_btn.add_css_class("flat")
            if mode == 'saves':
                upload_btn.connect('clicked', lambda b, entry=loc: self._upload_local_save_file(entry))
            else:
                upload_btn.connect('clicked', lambda b, entry=loc: self._upload_local_state(entry))
            hb.append(upload_btn)

            row.set_child(hb)
            listbox.append(row)

    def _fill_server_history_list(self, saves, states, mode='states'):
        """Populate Top Right pane with server save state or save file rows."""
        listbox = getattr(self, '_server_history_listbox', None)
        if listbox is None:
            return
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt

        raw_list = saves if mode == 'saves' else states
        server_entries = [e for e in raw_list if isinstance(e, dict)]
        server_entries.sort(key=lambda x: x.get('updated_at') or x.get('created_at') or '', reverse=True)

        if not server_entries:
            row = Gtk.ListBoxRow()
            row.set_selectable(False)
            row.set_activatable(False)
            msg = "No save files found on RomM server" if mode == 'saves' else "No save states found on RomM server"
            lbl = Gtk.Label(label=msg)
            lbl.add_css_class('dim-label')
            lbl.set_margin_top(12); lbl.set_margin_bottom(12)
            row.set_child(lbl)
            listbox.append(row)
            return

        for e in server_entries:
            row = Gtk.ListBoxRow()
            row._entry = e
            row._source = 'server'

            hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hb.set_margin_top(6); hb.set_margin_bottom(6)
            hb.set_margin_start(8); hb.set_margin_end(8)

            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            vb.set_hexpand(True)
            ts = self._fmt_ts(e.get('updated_at') or e.get('created_at') or '')
            t = Gtk.Label()
            t.set_xalign(0)
            t.set_markup(f"<b>{GLib.markup_escape_text(ts)}</b>")
            vb.append(t)

            sub = []
            sz = e.get('size_bytes') or e.get('file_size_bytes')
            if sz:
                sub.append(self._fmt_size(sz))
            dev = self._entry_device(e)
            if dev:
                sub.append(dev)
            slot = e.get('slot')
            if slot:
                sub.append(f"Slot {slot}")
            elif e.get('file_name'):
                sub.append(e.get('file_name'))

            s = Gtk.Label()
            s.set_xalign(0)
            s.add_css_class('dim-label')
            s.set_markup(f"<small>{GLib.markup_escape_text(' · '.join(sub))}</small>")
            vb.append(s)
            hb.append(vb)

            if mode == 'saves':
                replace_btn = Gtk.Button(label="Replace Save")
                replace_btn.set_tooltip_text("Replace local save file with this server version")
                replace_btn.connect('clicked', lambda btn, entry=e: self._confirm_replace_server_save(entry))
                hb.append(replace_btn)
            else:
                popover = Gtk.Popover()
                p_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                p_box.set_margin_top(6); p_box.set_margin_bottom(6)
                p_box.set_margin_start(6); p_box.set_margin_end(6)

                p_title = Gtk.Label()
                p_title.set_markup("<b>Select target slot to restore to:</b>")
                p_title.set_margin_bottom(4)
                p_box.append(p_title)

                slots_options = [
                    ("Slot 0 (Default)", ".state"),
                    ("Slot 1", ".state1"),
                    ("Slot 2", ".state2"),
                    ("Slot 3", ".state3"),
                    ("Slot 4", ".state4"),
                    ("Slot 5", ".state5"),
                    ("Quicksave", ".state.qsv"),
                    ("Auto Save", ".state.auto"),
                ]

                for opt_label, opt_code in slots_options:
                    b = Gtk.Button(label=opt_label)
                    b.add_css_class("flat")
                    b.connect('clicked', lambda btn, entry=e, code=opt_code, pop=popover: (pop.popdown(), self._restore_server_state_to_slot(entry, code)))
                    p_box.append(b)

                popover.set_child(p_box)

                menu_btn = Gtk.MenuButton()
                menu_btn.set_label("Restore to Slot ▾")
                menu_btn.set_popover(popover)
                hb.append(menu_btn)

            row.set_child(hb)
            listbox.append(row)

    def _set_refresh_btn_state(self, state):
        """Morph the header refresh button by flipping the indicator Stack page:
        'idle' (refresh icon) | 'busy' (spinner) | 'done' (green ✓). The Stack is
        homogeneous so the button keeps a constant size in every state."""
        btn = getattr(self, '_history_refresh_btn', None)
        stack = getattr(self, '_rb_stack', None)
        if btn is None or stack is None:
            return
        btn.set_opacity(1)
        sp = getattr(self, '_rb_spinner', None)
        if state == 'busy':
            if sp is not None:
                sp.start()
            stack.set_visible_child_name('busy')
            btn.set_sensitive(False)
        elif state == 'done':
            if sp is not None:
                sp.stop()
            stack.set_visible_child_name('done')
            btn.set_sensitive(False)
        else:  # idle
            if sp is not None:
                sp.stop()
            stack.set_visible_child_name('idle')
            btn.set_sensitive(True)

    def _set_history_busy(self, busy, text=""):
        if busy:
            self._history_fade_token = None  # cancel any in-flight success fade
            self._set_refresh_btn_state('busy')
            lb = getattr(self, '_history_busy_label', None)
            if lb is not None:
                lb.set_opacity(1)
                lb.set_text(text)
                lb.set_visible(bool(text))
        else:
            self._set_refresh_btn_state('idle')
            lb = getattr(self, '_history_busy_label', None)
            if lb is not None:
                lb.set_visible(False)
        return False

    def _history_done(self, text="Up to date"):
        """Turn the refresh button into a ✓ with a message, then fade back to idle."""
        win = getattr(self, '_history_win', None)
        if win is None or not win.get_visible():
            return
        self._set_refresh_btn_state('done')
        lb = getattr(self, '_history_busy_label', None)
        if lb is not None:
            lb.set_opacity(1)
            lb.set_text(text)
            lb.set_visible(bool(text))
        token = object()
        self._history_fade_token = token
        GLib.timeout_add(1600, lambda: self._history_fade_step(token, 1.0))

    def _history_fade_step(self, token, opacity):
        if getattr(self, '_history_fade_token', None) is not token:
            return False  # superseded by a newer operation
        win = getattr(self, '_history_win', None)
        btn = getattr(self, '_history_refresh_btn', None)
        lb = getattr(self, '_history_busy_label', None)
        if win is None or not win.get_visible() or btn is None:
            return False
        opacity -= 0.08
        if opacity <= 0:
            if lb is not None:
                lb.set_visible(False)
                lb.set_opacity(1)
            self._history_fade_token = None
            self._set_refresh_btn_state('idle')  # revert ✓ → refresh icon
            return False
        btn.set_opacity(opacity)
        if lb is not None:
            lb.set_opacity(opacity)
        GLib.timeout_add(40, lambda: self._history_fade_step(token, opacity))
        return False

    def _on_local_row_selected(self, listbox, row):
        if row is None or not hasattr(row, '_entry'):
            return
        server_lb = getattr(self, '_server_history_listbox', None)
        if server_lb and server_lb.get_selected_row():
            server_lb.unselect_all()
        self._update_preview(row._entry, 'local')

    def _on_server_row_selected(self, listbox, row):
        if row is None or not hasattr(row, '_entry'):
            return
        local_lb = getattr(self, '_local_history_listbox', None)
        if local_lb and local_lb.get_selected_row():
            local_lb.unselect_all()
        self._update_preview(row._entry, 'server')

    def _update_preview(self, entry, source):
        self._current_entry = entry
        self._current_source = source
        if not hasattr(self, '_preview_picture') or self._preview_picture is None:
            return
        if not entry:
            self._preview_picture.set_paintable(None)
            self._preview_status.set_text("Select a local or server item above to view preview")
            self._preview_status.set_visible(True)
            self._preview_info.set_text("")
            return

        mode = getattr(self, '_history_mode', 'states')
        if source == 'local':
            ts = entry.get('timestamp', '')
            slot_name = entry.get('slot_name', 'Local Item')
            sz = self._fmt_size(entry.get('size_bytes', 0))
            is_synced = entry.get('is_synced', False)
            synced_str = "Synced with server" if is_synced else "Local only"
            self._preview_info.set_markup(f"<b>{GLib.markup_escape_text(slot_name)}</b> — {GLib.markup_escape_text(ts)} ({GLib.markup_escape_text(sz)}) · <i>{GLib.markup_escape_text(synced_str)}</i>")

            png_path = entry.get('png_path')
            if png_path and Path(png_path).exists():
                try:
                    from gi.repository import Gdk
                    tex = Gdk.Texture.new_from_filename(png_path)
                    self._preview_picture.set_paintable(tex)
                    self._preview_status.set_visible(False)
                    return
                except Exception:
                    pass
            self._preview_picture.set_paintable(None)
            self._preview_status.set_text("No preview screenshot available for this local item")
            self._preview_status.set_visible(True)

        elif source == 'server':
            ts = self._fmt_ts(entry.get('updated_at') or entry.get('created_at') or '')
            sz = self._fmt_size(entry.get('size_bytes') or entry.get('file_size_bytes') or 0)
            dev = self._entry_device(entry) or "RomM Server"
            header_text = "RomM Server Save File" if mode == 'saves' else "RomM Server Save State"
            self._preview_info.set_markup(f"<b>{header_text}</b> — {GLib.markup_escape_text(ts)} ({GLib.markup_escape_text(sz)}) · {GLib.markup_escape_text(dev)}")

            sid = entry.get('id')
            if sid in self._shot_cache:
                tex = self._shot_cache[sid]
                self._preview_picture.set_paintable(tex)
                self._preview_status.set_visible(tex is None)
                if tex is None:
                    self._preview_status.set_text("No screenshot available for this server version")
                return

            self._preview_picture.set_paintable(None)
            self._preview_status.set_visible(True)
            self._preview_status.set_text("Loading preview…")

            def worker():
                save_type = 'saves' if mode == 'saves' else 'states'
                data = self._fetch_screenshot_bytes(entry, save_type)
                GLib.idle_add(self._apply_screenshot, sid, data)
            threading.Thread(target=worker, daemon=True).start()

    def _fetch_screenshot_bytes(self, entry, save_type='states'):
        """Fetch screenshot image bytes for a save/state entry from RomM client."""
        if not hasattr(self.parent, 'romm_client') or not self.parent.romm_client:
            return None
        try:
            return self.parent.romm_client.fetch_screenshot_bytes(entry, save_type)
        except Exception as e:
            print(f"Error fetching screenshot bytes: {e}")
            return None

    def _apply_screenshot(self, sid, data):
        """Decode screenshot bytes and apply texture to preview picture."""
        from gi.repository import Gdk
        tex = None
        if data:
            try:
                tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))
            except Exception:
                try:
                    from gi.repository import GdkPixbuf
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    if pixbuf is not None:
                        tex = Gdk.Texture.new_for_pixbuf(pixbuf)
                except Exception as e:
                    print(f"Screenshot decode failed: {e}")
        self._shot_cache[sid] = tex
        if self._current_entry and self._current_entry.get('id') == sid:
            self._preview_picture.set_paintable(tex)
            if tex is None:
                self._preview_status.set_text("No screenshot available for this server version")
                self._preview_status.set_visible(True)
            else:
                self._preview_status.set_visible(False)
        return False

    def _upload_local_state(self, local_entry):
        """Upload a specific local save state file to the RomM server."""
        game = getattr(self, '_history_game', None)
        if not game or not local_entry:
            return

        file_path = local_entry.get('file_path')
        if not file_path or not Path(file_path).exists():
            return

        self._set_history_busy(True, "Uploading to server…")

        def worker():
            try:
                rom_id = getattr(self, '_history_rom_id', None)
                thumbnail_path = self.parent.retroarch.find_thumbnail_for_save_state(file_path) if hasattr(self.parent.retroarch, 'find_thumbnail_for_save_state') else None
                slot, autocleanup, autocleanup_limit = RomMClient.get_slot_info(file_path)

                result = self.parent.romm_client.upload_save_with_thumbnail(
                    rom_id, 'states', file_path, thumbnail_path, None, self.parent.device_id,
                    slot=slot, autocleanup=autocleanup, autocleanup_limit=autocleanup_limit
                )

                if result:
                    GLib.idle_add(self.parent.log_message, f"✅ Uploaded {local_entry.get('slot_name')} to server")
                else:
                    GLib.idle_add(self.parent.log_message, f"⚠️ Upload of {local_entry.get('slot_name')} failed")

            except Exception as e:
                GLib.idle_add(self.parent.log_message, f"❌ Save state upload failed: {e}")

            GLib.idle_add(self._refresh_history)

        threading.Thread(target=worker, daemon=True).start()

    def _upload_local_save_file(self, local_entry):
        """Upload a specific local battery save file to the RomM server."""
        game = getattr(self, '_history_game', None)
        if not game or not local_entry:
            return

        file_path = local_entry.get('file_path')
        if not file_path or not Path(file_path).exists():
            return

        self._set_history_busy(True, "Uploading save file to server…")

        def worker():
            try:
                rom_id = getattr(self, '_history_rom_id', None)
                thumbnail_path = local_entry.get('png_path')
                if not thumbnail_path or not Path(thumbnail_path).exists():
                    thumbnail_path = self.parent.retroarch.find_thumbnail_for_save_state(file_path) if hasattr(self.parent.retroarch, 'find_thumbnail_for_save_state') else None

                result = self.parent.romm_client.upload_save_with_thumbnail(
                    rom_id, 'saves', file_path, thumbnail_path, None, self.parent.device_id
                )

                if result:
                    GLib.idle_add(self.parent.log_message, f"✅ Uploaded save file {local_entry.get('file_name')} to server")
                else:
                    GLib.idle_add(self.parent.log_message, f"⚠️ Upload of {local_entry.get('file_name')} failed")

            except Exception as e:
                GLib.idle_add(self.parent.log_message, f"❌ Save file upload failed: {e}")

            GLib.idle_add(self._refresh_history)

        threading.Thread(target=worker, daemon=True).start()

    def _confirm_replace_server_save(self, server_entry):
        """Prompt confirmation before overwriting local save file with server version."""
        game = getattr(self, '_history_game', None)
        game_name = (game or {}).get('name', 'selected game')

        msg = f"Are you sure you want to replace your local save file for '{game_name}' with this server version?\n\nYour current local save will be backed up with a .backup extension."

        if hasattr(Adw, 'AlertDialog'):
            dialog = Adw.AlertDialog.new("Replace Local Save File?", msg)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("replace", "Replace Save")
            if hasattr(Adw.ResponseAppearance, 'DESTRUCTIVE'):
                dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_response(d, resp):
                if resp == "replace":
                    self._restore_server_save_file(server_entry)

            dialog.connect('response', on_response)
            dialog.present(getattr(self, '_history_win', self.parent))
        else:
            dialog = Gtk.MessageDialog(
                transient_for=getattr(self, '_history_win', self.parent),
                modal=True,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text="Replace Local Save File?"
            )
            dialog.format_secondary_text(msg)
            def on_response(d, resp):
                d.destroy()
                if resp == Gtk.ResponseType.OK:
                    self._restore_server_save_file(server_entry)
            dialog.connect('response', on_response)
            dialog.present()

    def _restore_server_save_file(self, server_entry):
        """Download & replace local battery save file with server version."""
        game = getattr(self, '_history_game', None)
        if not game or not server_entry:
            return

        self._set_history_busy(True, "Replacing local save file…")

        def worker():
            try:
                import shutil
                save_id = server_entry.get('id')

                save_dirs = getattr(self.parent.retroarch, 'save_dirs', {}) or {}
                saves_dir = save_dirs.get('saves')
                if not saves_dir:
                    dirs = self.parent.retroarch.find_retroarch_dirs()
                    saves_dir = dirs.get('saves')

                if not saves_dir:
                    GLib.idle_add(self.parent.log_message, "❌ Local saves directory not found")
                    GLib.idle_add(self._set_history_busy, False)
                    return

                game_name = game.get('name', '')
                file_name = game.get('file_name', '')
                stem = Path(file_name).stem if file_name else game_name

                target_dir = self._get_target_save_directory(saves_dir, game)
                target_dir.mkdir(parents=True, exist_ok=True)

                ext = '.srm'
                srv_fn = server_entry.get('file_name') or ''
                if srv_fn and '.' in srv_fn:
                    ext = Path(srv_fn).suffix

                try:
                    for f in target_dir.iterdir():
                        if f.is_file() and stem.lower() in f.name.lower() and not f.name.endswith('.png') and not f.name.endswith('.backup'):
                            ext = f.suffix
                            break
                except Exception:
                    pass

                target_path = target_dir / f"{stem}{ext}"

                if target_path.exists():
                    backup_path = target_path.with_suffix(target_path.suffix + '.backup')
                    shutil.copy2(target_path, backup_path)

                fallback_url = server_entry.get('download_path') or server_entry.get('path')
                success = self.parent.romm_client.download_save_by_id(
                    save_id=save_id,
                    save_type='saves',
                    download_path=target_path,
                    device_id=getattr(self.parent, 'device_id', None),
                    fallback_url=fallback_url
                )

                if not success:
                    GLib.idle_add(self.parent.log_message, f"❌ Failed to download save file {save_id} from server")
                    GLib.idle_add(self._set_history_busy, False)
                    return

                ts = self._parse_entry_mtime(server_entry)
                if ts and target_path.exists():
                    try:
                        os.utime(target_path, (ts, ts))
                    except Exception as utime_err:
                        print(f"Error setting mtime on {target_path}: {utime_err}")

                shot_bytes = self.parent.romm_client.fetch_screenshot_bytes(server_entry, 'saves')
                if shot_bytes:
                    png_path = target_path.with_name(target_path.name + '.png')
                    png_path.write_bytes(shot_bytes)

                GLib.idle_add(self.parent.log_message, f"✅ Replaced save file with {target_path.name}")

            except Exception as e:
                GLib.idle_add(self.parent.log_message, f"❌ Save file restore failed: {e}")

            GLib.idle_add(self._refresh_history)

        threading.Thread(target=worker, daemon=True).start()

    def _parse_entry_mtime(self, server_entry):
        """Extract Unix epoch timestamp float from server entry metadata."""
        if not isinstance(server_entry, dict):
            return None
        iso = server_entry.get('updated_at') or server_entry.get('created_at') or server_entry.get('timestamp')
        if iso:
            try:
                dt = datetime.datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
                return dt.timestamp()
            except Exception as e:
                print(f"Error parsing entry timestamp '{iso}': {e}")
        mtime = server_entry.get('mtime')
        if mtime:
            try:
                return float(mtime)
            except Exception:
                pass
        return None

    def _restore_server_state_to_slot(self, server_entry, target_slot_code):
        """Download & restore a server save state directly to a specific local slot."""
        game = getattr(self, '_history_game', None)
        if not game or not server_entry:
            return

        self._set_history_busy(True, f"Restoring into {target_slot_code}…")

        def worker():
            try:
                import shutil
                save_id = server_entry.get('id')

                save_dirs = getattr(self.parent.retroarch, 'save_dirs', {}) or {}
                states_dir = save_dirs.get('states')
                if not states_dir:
                    dirs = self.parent.retroarch.find_retroarch_dirs()
                    states_dir = dirs.get('states')

                if not states_dir:
                    GLib.idle_add(self.parent.log_message, "❌ Local states directory not found")
                    GLib.idle_add(self._set_history_busy, False)
                    return

                game_name = game.get('name', '')
                file_name = game.get('file_name', '')
                stem = Path(file_name).stem if file_name else game_name

                target_dir = self._get_target_state_directory(states_dir, game)
                target_dir.mkdir(parents=True, exist_ok=True)
                target_path = target_dir / f"{stem}{target_slot_code}"

                if target_path.exists():
                    backup_path = target_path.with_suffix(target_path.suffix + '.backup')
                    shutil.copy2(target_path, backup_path)

                fallback_url = server_entry.get('download_path') or server_entry.get('path')
                success = self.parent.romm_client.download_save_by_id(
                    save_id=save_id,
                    save_type='states',
                    download_path=target_path,
                    device_id=getattr(self.parent, 'device_id', None),
                    fallback_url=fallback_url
                )

                if not success:
                    GLib.idle_add(self.parent.log_message, f"❌ Failed to download save state {save_id} from server")
                    GLib.idle_add(self._set_history_busy, False)
                    return

                ts = self._parse_entry_mtime(server_entry)
                if ts and target_path.exists():
                    try:
                        os.utime(target_path, (ts, ts))
                    except Exception as utime_err:
                        print(f"Error setting mtime on {target_path}: {utime_err}")

                # Fetch screenshot
                shot_bytes = self.parent.romm_client.fetch_screenshot_bytes(server_entry, 'states')
                if shot_bytes:
                    png_path = target_path.with_name(target_path.name + '.png')
                    png_path.write_bytes(shot_bytes)

                GLib.idle_add(self.parent.log_message, f"✅ Restored save state into {target_path.name}")

            except Exception as e:
                GLib.idle_add(self.parent.log_message, f"❌ Save state restore failed: {e}")

            GLib.idle_add(self._refresh_history)

        threading.Thread(target=worker, daemon=True).start()

    def _select_first_history_row(self):
        local_lb = getattr(self, '_local_history_listbox', None)
        if local_lb and local_lb.get_first_child():
            child = local_lb.get_first_child()
            if hasattr(child, '_entry'):
                local_lb.select_row(child)
                return
        server_lb = getattr(self, '_server_history_listbox', None)
        if server_lb and server_lb.get_first_child():
            child = server_lb.get_first_child()
            if hasattr(child, '_entry'):
                server_lb.select_row(child)

    def _refresh_history(self):
        """Re-fetch both server and local save history and repopulate open dialog."""
        rom_id = getattr(self, '_history_rom_id', None)
        game = getattr(self, '_history_game', None)
        mode = getattr(self, '_history_mode', 'states')
        win = getattr(self, '_history_win', None)
        if not rom_id or win is None or not win.get_visible():
            return

        self._set_history_busy(True, "Refreshing…")

        def worker():
            saves, states = self._fetch_save_history(rom_id)
            if mode == 'saves':
                local_entries = self._fetch_local_save_files(game)
                server_entries = saves
            else:
                local_entries = self._fetch_local_save_states(game)
                server_entries = states

            self._cross_reference_synced_states(local_entries, server_entries)
            GLib.idle_add(self._finish_refresh_all, local_entries, saves, states, mode)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_refresh_all(self, local_entries, saves, states, mode='states', done_text="Up to date"):
        win = getattr(self, '_history_win', None)
        if win is None or not win.get_visible():
            return False
        self._fill_local_history_list(local_entries, mode=mode)
        self._fill_server_history_list(saves, states, mode=mode)
        self._set_history_busy(False)
        self._history_done(done_text)
        return False

