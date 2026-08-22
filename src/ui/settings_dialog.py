import os
import sys
from pathlib import Path
from gi.repository import Gtk, Gdk, GLib, Gio, GObject
from ui.compat import Adw, HAS_ADW

class SettingsDialog:
    """Manages the Preferences, Logs, and Advanced Tools dialog"""

    def __init__(self, parent_window):
        self.window = parent_window
        self.settings = parent_window.settings
        self.retroarch = parent_window.retroarch

    def show(self):
        """Build and display the settings and logs dialog"""
        dialog = Adw.PreferencesDialog()
        dialog.set_title("Logs & Advanced Tools")
        dialog.set_content_width(600)
        dialog.set_content_height(500)

        # 1. Activity Log Group
        log_group = Adw.PreferencesGroup()
        log_group.set_title("Activity Log")

        dialog_log_view = Gtk.TextView()
        dialog_log_view.set_editable(False)
        dialog_log_view.set_cursor_visible(False)
        dialog_log_view.set_buffer(self.window.log_view.get_buffer())

        scrolled_log = Gtk.ScrolledWindow()
        scrolled_log.set_child(dialog_log_view)
        scrolled_log.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_log.set_size_request(-1, 200)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        log_box.append(scrolled_log)

        log_row = Adw.ActionRow()
        log_row.set_child(log_box)
        log_group.add(log_row)

        debug_mode_row = Adw.SwitchRow()
        debug_mode_row.set_title("Debug Mode")
        debug_mode_row.set_subtitle("Enable detailed logging and write debug.log file")
        debug_mode_row.set_active(self.settings.get('System', 'debug_mode') == 'true')
        debug_mode_row.connect('notify::active', lambda row, _: self.window.on_debug_mode_changed(row, _))
        log_group.add(debug_mode_row)

        # 2. Configuration Group
        config_group = Adw.PreferencesGroup()
        config_group.set_title("Configuration")

        library_dir_expander = Adw.ExpanderRow()
        library_dir_expander.set_title("Library Directory")
        library_dir_expander.set_subtitle(self.settings.get('Download', 'rom_directory'))

        dir_button_container = Gtk.Box()
        dir_button_container.set_size_request(-1, 18)
        dir_button_container.set_valign(Gtk.Align.CENTER)
        choose_dir_button = Gtk.Button(label="Browse...")
        choose_dir_button.connect('clicked', self.window.on_choose_directory)
        choose_dir_button.set_size_request(100, -1)
        choose_dir_button.set_valign(Gtk.Align.CENTER)
        dir_button_container.append(choose_dir_button)
        library_dir_expander.add_suffix(dir_button_container)

        library_dir_path_row = Adw.EntryRow()
        library_dir_path_row.set_title("Directory Path")
        library_dir_path_row.set_text(self.settings.get('Download', 'rom_directory'))
        self.window._dialog_library_dir_row = library_dir_path_row
        self.window._dialog_library_dir_expander = library_dir_expander
        library_dir_expander.add_row(library_dir_path_row)

        max_downloads_row = Adw.SpinRow()
        max_downloads_row.set_title("Max Concurrent Downloads")
        max_downloads_row.set_subtitle("Maximum simultaneous ROM downloads")
        downloads_adjustment = Gtk.Adjustment(value=3, lower=1, upper=10, step_increment=1)
        max_downloads_row.set_adjustment(downloads_adjustment)
        max_downloads_row.set_value(int(self.settings.get('Download', 'max_concurrent', '3')))
        max_downloads_row.connect('notify::value', self.window.on_max_downloads_changed)
        library_dir_expander.add_row(max_downloads_row)

        browse_row = Adw.ActionRow()
        browse_row.set_title("Open Download Folder")
        browse_row.set_subtitle("View downloaded files in file manager")
        browse_button_container = Gtk.Box()
        browse_button_container.set_size_request(-1, 18)
        browse_button_container.set_valign(Gtk.Align.CENTER)
        browse_button = Gtk.Button(label="Open")
        browse_button.connect('clicked', self.window.on_browse_downloads)
        browse_button.set_size_request(80, -1)
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button_container.append(browse_button)
        browse_row.add_suffix(browse_button_container)
        library_dir_expander.add_row(browse_row)

        config_group.add(library_dir_expander)

        # BIOS Settings
        bios_expander = Adw.ExpanderRow()
        bios_expander.set_title("System BIOS Files")
        bios_expander.set_subtitle("Manage emulator BIOS/firmware files")

        bios_download_container = Gtk.Box()
        bios_download_container.set_size_request(-1, 18)
        bios_download_container.set_valign(Gtk.Align.CENTER)
        download_all_btn = Gtk.Button(label="Download All")
        download_all_btn.connect('clicked', self.window.on_download_all_bios)
        download_all_btn.set_size_request(100, -1)
        download_all_btn.set_valign(Gtk.Align.CENTER)
        bios_download_container.append(download_all_btn)
        bios_expander.add_suffix(bios_download_container)

        bios_override_row = Adw.EntryRow()
        bios_override_row.set_title("Custom BIOS Directory (Override auto-detection)")
        bios_override_row.set_text(self.settings.get('BIOS', 'custom_path', ''))
        bios_override_row.connect('entry-activated', self.window.on_bios_override_changed)
        bios_expander.add_row(bios_override_row)

        bios_dir_row = Adw.ActionRow()
        bios_dir_row.set_title("BIOS Directory")
        if self.retroarch.bios_manager and self.retroarch.bios_manager.system_dir:
            bios_dir_row.set_subtitle(str(self.retroarch.bios_manager.system_dir))
        else:
            bios_dir_row.set_subtitle("Not found")
        bios_expander.add_row(bios_dir_row)

        config_group.add(bios_expander)

        # Platform Core Overrides
        cores_expander = Adw.ExpanderRow()
        cores_expander.set_title("Platform Core Overrides")
        cores_expander.set_subtitle("Customize RetroArch core selection per platform")

        platforms_to_show = {
            "Sega Saturn", "Sony PlayStation", "Sony PlayStation 2",
            "Nintendo 64", "Super Nintendo Entertainment System",
            "Nintendo Entertainment System", "Game Boy Advance",
            "Sega Genesis", "Nintendo GameCube", "Nintendo DS", "Sega Dreamcast"
        }
        if hasattr(self.window, 'available_games') and self.window.available_games:
            for g in self.window.available_games:
                p_name = g.get('platform')
                if p_name:
                    platforms_to_show.add(p_name)

        available_cores = self.retroarch.get_available_cores() if hasattr(self, 'retroarch') and self.retroarch else {}
        sorted_cores = sorted(available_cores.keys())

        for p_name in sorted(platforms_to_show):
            row = Adw.ActionRow()
            row.set_title(p_name)

            curr_override = self.retroarch.get_core_override(p_name) if hasattr(self, 'retroarch') else ''
            res_info = self.retroarch.describe_core_resolution(p_name) if hasattr(self, 'retroarch') and hasattr(self.retroarch, 'describe_core_resolution') else {}
            resolved_core = res_info.get('resolved_core', 'None')

            if curr_override:
                row.set_subtitle(f"Override: {curr_override}")
            elif resolved_core:
                row.set_subtitle(f"Auto: {resolved_core}")
            else:
                row.set_subtitle("Auto: None")

            combo = Gtk.ComboBoxText()
            combo.set_valign(Gtk.Align.CENTER)
            combo.append_text("Auto (Default)")

            active_idx = 0
            for idx, c_name in enumerate(sorted_cores, start=1):
                combo.append_text(c_name)
                if curr_override and c_name == curr_override:
                    active_idx = idx

            combo.set_active(active_idx)

            def make_on_change(platform=p_name, action_row=row):
                def on_combo_changed(widget):
                    selected = widget.get_active_text()
                    if selected == "Auto (Default)" or not selected:
                        self.retroarch.set_core_override(platform, None)
                        res = self.retroarch.describe_core_resolution(platform)
                        autocore = res.get('resolved_core', 'None')
                        action_row.set_subtitle(f"Auto: {autocore}")
                        self.window.log_message(f"Cleared core override for {platform} (reverted to Auto: {autocore})")
                    else:
                        self.retroarch.set_core_override(platform, selected)
                        action_row.set_subtitle(f"Override: {selected}")
                        self.window.log_message(f"Set core override for {platform}: {selected}")
                return on_combo_changed

            combo.connect("changed", make_on_change(p_name, row))
            row.add_suffix(combo)
            cores_expander.add_row(row)

        config_group.add(cores_expander)

        # 3. Advanced Tools Group
        advanced_group = Adw.PreferencesGroup()
        advanced_group.set_title("Advanced Tools")

        inspect_row = Adw.ActionRow()
        inspect_row.set_title("Inspect Files")
        inspect_row.set_subtitle("Check downloaded file integrity")
        inspect_btn = Gtk.Button(label="Inspect")
        inspect_btn.set_valign(Gtk.Align.CENTER)
        inspect_btn.set_size_request(80, -1)
        inspect_btn.connect('clicked', self.window.on_inspect_downloads)
        inspect_row.add_suffix(inspect_btn)
        advanced_group.add(inspect_row)

        cache_row = Adw.ActionRow()
        cache_row.set_title("Game Data Cache")
        cache_row.set_subtitle("Local storage management")

        cache_box = Gtk.Box(spacing=6)
        cache_box.set_valign(Gtk.Align.CENTER)
        check_btn = Gtk.Button(label="Check")
        check_btn.set_size_request(70, -1)
        check_btn.connect('clicked', self.window.on_check_cache_status)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.set_size_request(70, -1)
        clear_btn.add_css_class('destructive-action')
        clear_btn.connect('clicked', self.window.on_clear_cache)
        cache_box.append(check_btn)
        cache_box.append(clear_btn)
        cache_row.add_suffix(cache_box)
        advanced_group.add(cache_row)

        resync_row = Adw.ActionRow()
        resync_row.set_title("Full Library Resync")
        resync_row.set_subtitle("Re-download all ROM metadata from server from scratch")
        resync_btn = Gtk.Button(label="Full Resync")
        resync_btn.set_valign(Gtk.Align.CENTER)
        resync_btn.set_size_request(100, -1)
        resync_btn.connect('clicked', lambda b: self.window.refresh_games_list(force_full_refresh=True))
        resync_row.add_suffix(resync_btn)
        advanced_group.add(resync_row)

        # Add groups to page
        page = Adw.PreferencesPage()
        page.add(log_group)
        page.add(config_group)
        page.add(advanced_group)
        dialog.add(page)

        dialog.present(self.window)
