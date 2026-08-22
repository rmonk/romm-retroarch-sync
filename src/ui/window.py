import os
import sys
import re
import html
import time
import socket
import shutil
import logging
import datetime
from datetime import datetime, timezone
import threading
from pathlib import Path

import urllib.request
import urllib.parse

from gi.repository import Gtk, Gdk, GLib, Gio, GObject, Pango
from ui.compat import Adw, HAS_ADW, detect_desktop_environment, get_de_custom_css
from ui.models import GameItem, DiscItem, PlatformItem, LibraryTreeModel
from ui.library import EnhancedLibrarySection
from romm_sync_engine.sync_core import (
    RomMClient, GameDataCache, AutoSyncManager,
    SettingsManager, RetroArchInterface, SteamShortcutManager,
    CoverArtManager, cache_dir
)

class SettingsBackedEntry:
    """Simple helper that acts like an EntryRow but reads from settings"""
    def __init__(self, settings, section, key, default=''):
        self.settings = settings
        self.section = section
        self.key = key
        self.default = default

    def get_text(self):
        return self.settings.get(self.section, self.key, fallback=self.default)

class SyncWindow(Gtk.ApplicationWindow):
    """Main application window"""

    def __init__(self, cli_de=None, **kwargs):
        self.cli_de = cli_de
        super().__init__(**kwargs)

        # Set window icon directly
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))
        custom_icon_path = os.path.join(script_dir, 'romm_icon.png')
        
        if os.path.exists(custom_icon_path):
            try:
                from gi.repository import GdkPixbuf
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(custom_icon_path)
                # Try different GTK4 methods
                if hasattr(self, 'set_icon'):
                    self.set_icon(pixbuf)
                elif hasattr(self, 'set_default_icon'):
                    self.set_default_icon(pixbuf)
                print(f"Set window icon: {custom_icon_path}")
            except Exception as e:
                print(f"Failed to set window icon: {e}")

        # Auto-integrate AppImage on first run
        self.integrate_appimage()

        # Set application identity FIRST
        self.set_application_identity()

        self.romm_client = None
        self.device_id = None

        self.settings = SettingsManager()

        # Create settings-backed entry for ROM directory (used throughout the code)
        self.rom_dir_row = SettingsBackedEntry(self.settings, 'Download', 'rom_directory', '')

        self.retroarch = RetroArchInterface(self.settings)

        self.steam_manager = SteamShortcutManager(
            retroarch_interface=self.retroarch,
            settings=self.settings,
            log_callback=lambda msg: GLib.idle_add(self.log_message, msg),
            cover_manager=None  # Will be set after romm_client is created
        )

        self.game_cache = GameDataCache(self.settings)
        
        # Progress tracking
        self.download_queue = []
        self.available_games = []  # Initialize games list

        # Timestamps for efficient polling with updated_after parameter
        self._last_full_fetch_time = getattr(self.game_cache, 'last_sync_datetime', None)  # ISO 8601 datetime of last sync

        self.download_progress = {}
        self._last_progress_update = {}  # rom_id -> timestamp
        self._progress_update_interval = 0.1  # Update UI every 100ms max

        # Download cancellation infrastructure
        self._cancelled_downloads = set()  # Track rom_ids of cancelled downloads
        self._download_threads = {}  # Track active download threads by rom_id
        self._cancellation_lock = threading.Lock()  # Thread-safe access to cancellation state
        self._bulk_download_cancelled = False  # Flag to cancel entire bulk operation
        self._bulk_download_in_progress = False  # Track if bulk download is active

        self.setup_ui()
        self.connect('close-request', self.on_window_close_request)
        self.load_saved_settings()

        # Auto-update systemd service for new versions
        if self.settings.get('System', 'autostart') == 'true':
            self.update_systemd_service_if_needed()

        # Add about action
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.on_about)
        self.add_action(about_action)

        # Initialize log view early so log_message() works from the start
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)

        # Add logs action (ADD THIS)
        logs_action = Gio.SimpleAction.new("logs", None)
        logs_action.connect("activate", lambda action, param: self.on_show_logs_dialog(None))
        self.add_action(logs_action)

        # Add quit action
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda action, param: self.get_application().quit())
        self.add_action(quit_action)

        # Register window actions for traditional menu bar items
        refresh_act = Gio.SimpleAction.new("refresh", None)
        refresh_act.connect("activate", lambda a, p: self.library_section.on_refresh_library(None) if hasattr(self, 'library_section') else self.refresh_games_list())
        self.add_action(refresh_act)

        bios_act = Gio.SimpleAction.new("download_bios", None)
        bios_act.connect("activate", lambda a, p: self.on_download_all_bios(None))
        self.add_action(bios_act)

        flat_act = Gio.SimpleAction.new("toggle_flat_view", None)
        flat_act.connect("activate", lambda a, p: self.library_section.flat_view_btn.set_active(not self.library_section.flat_view_btn.get_active()) if hasattr(self, 'library_section') else None)
        self.add_action(flat_act)

        filter_act = Gio.SimpleAction.new("toggle_show_downloaded", None)
        filter_act.connect("activate", lambda a, p: self.library_section.on_toggle_filter(self.library_section.filter_btn) if hasattr(self, 'library_section') else None)
        self.add_action(filter_act)

        expand_act = Gio.SimpleAction.new("expand_all", None)
        expand_act.connect("activate", lambda a, p: self.library_section.on_expand_all(None) if hasattr(self, 'library_section') else None)
        self.add_action(expand_act)

        collapse_act = Gio.SimpleAction.new("collapse_all", None)
        collapse_act.connect("activate", lambda a, p: self.library_section.on_collapse_all(None) if hasattr(self, 'library_section') else None)
        self.add_action(collapse_act)

        self._pending_refresh = False
        
        # Initialize RetroArch info and attempt to load games list
        try:
            self.refresh_retroarch_info()
            # Try to refresh games list (will show local games if not connected to RomM)
            self.refresh_games_list()
        except Exception as e:
            print(f"Initial setup error: {e}")

        # Initialize auto-sync (add after other initializations)
        self.auto_sync = AutoSyncManager(
            romm_client=None,  # Will be set when connected
            retroarch=self.retroarch,
            settings=self.settings,
            log_callback=self.log_message,
            get_games_callback=lambda: getattr(self, 'available_games', []),
            parent_window=self
        )

        # Schedule periodic memory cleanup for large libraries
        def setup_periodic_cleanup():
            if len(getattr(self, 'available_games', [])) > 1000:
                def periodic_cleanup():
                    import gc
                    gc.collect()
                    return True  # Continue running
                
                # Clean up every 5 minutes
                GLib.timeout_add(300000, periodic_cleanup)
                print("🧹 Periodic memory cleanup scheduled (every 5 minutes)")
            return False

        # Schedule cleanup check after initial load
        GLib.timeout_add(2000, setup_periodic_cleanup)
        # ADD AUTO-CONNECT LOGIC:
        GLib.timeout_add(50, self.try_auto_connect)

    def create_status_dot(self, color='grey', size=10):
        """Create a Cairo-drawn status dot widget

        Args:
            color: 'green', 'orange', 'red', 'grey', or 'yellow'
            size: Size of the dot in pixels (default: 10)

        Returns:
            Gtk.DrawingArea with colored dot
        """
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_size_request(size, size)
        drawing_area.set_halign(Gtk.Align.CENTER)
        drawing_area.set_valign(Gtk.Align.CENTER)

        def draw_func(area, cr, width, height):
            # Determine RGB color
            if color == 'green':
                cr.set_source_rgb(0.29, 0.86, 0.50)  # #4ade80
            elif color == 'orange':
                cr.set_source_rgb(0.98, 0.57, 0.24)  # #fb923c
            elif color == 'red':
                cr.set_source_rgb(0.97, 0.44, 0.44)  # #f87171
            elif color == 'yellow':
                cr.set_source_rgb(0.98, 0.80, 0.27)  # #facc15
            else:  # grey
                cr.set_source_rgb(0.42, 0.45, 0.50)  # #6b7280

            # Draw filled circle
            radius = min(width, height) / 2.0
            cr.arc(width / 2.0, height / 2.0, radius - 1, 0, 2 * 3.14159)
            cr.fill()

        drawing_area.set_draw_func(draw_func)
        return drawing_area

    def update_status_dot(self, drawing_area, color='grey'):
        """Update an existing status dot with a new color

        Args:
            drawing_area: The Gtk.DrawingArea to update
            color: 'green', 'orange', 'red', 'grey', or 'yellow'
        """
        drawing_area._current_color = color
        def draw_func(area, cr, width, height):
            # Determine RGB color
            if color == 'green':
                cr.set_source_rgb(0.29, 0.86, 0.50)  # #4ade80
            elif color == 'orange':
                cr.set_source_rgb(0.98, 0.57, 0.24)  # #fb923c
            elif color == 'red':
                cr.set_source_rgb(0.97, 0.44, 0.44)  # #f87171
            elif color == 'yellow':
                cr.set_source_rgb(0.98, 0.80, 0.27)  # #facc15
            else:  # grey
                cr.set_source_rgb(0.42, 0.45, 0.50)  # #6b7280

            # Draw filled circle
            radius = min(width, height) / 2.0
            cr.arc(width / 2.0, height / 2.0, radius - 1, 0, 2 * 3.14159)
            cr.fill()

        drawing_area.set_draw_func(draw_func)
        drawing_area.queue_draw()

        summary_dots = (getattr(self, 'summary_romm_dot', None), getattr(self, 'summary_retroarch_dot', None), getattr(self, 'summary_autosync_dot', None))
        if hasattr(self, 'sync_summary_box') and drawing_area not in summary_dots:
            GLib.idle_add(self.update_sync_summary_dots)

    def draw_download_status_icon(self, drawing_area, status_type, progress=None):
        """Draw a download status icon or percentage using Cairo

        Args:
            drawing_area: The Gtk.DrawingArea to draw on
            status_type: 'downloaded' (green checkmark), 'not_downloaded' (blue down arrow),
                        'completed' (green checkmark), 'failed' (red X), 'downloading' (percentage)
            progress: Progress value (0.0 to 1.0) for downloading status
        """
        def draw_func(area, cr, width, height):
            center_x = width / 2.0
            center_y = height / 2.0

            if status_type in ['downloaded', 'completed']:
                # Green checkmark
                cr.set_source_rgb(0.29, 0.86, 0.50)  # Green #4ade80
                cr.set_line_width(2.0)
                cr.set_line_cap(1)  # Round caps
                cr.set_line_join(1)  # Round joins

                # Draw checkmark path
                cr.move_to(center_x - 4, center_y)
                cr.line_to(center_x - 1, center_y + 3)
                cr.line_to(center_x + 4, center_y - 3)
                cr.stroke()

            elif status_type == 'not_downloaded':
                # Blue down arrow
                cr.set_source_rgb(0.37, 0.51, 0.98)  # Blue #5e82fa
                cr.set_line_width(2.0)
                cr.set_line_cap(1)  # Round caps
                cr.set_line_join(1)  # Round joins

                # Draw arrow shaft
                cr.move_to(center_x, center_y - 4)
                cr.line_to(center_x, center_y + 3)
                cr.stroke()

                # Draw arrow head
                cr.move_to(center_x - 3, center_y)
                cr.line_to(center_x, center_y + 3)
                cr.line_to(center_x + 3, center_y)
                cr.stroke()

            elif status_type == 'failed':
                # Red X
                cr.set_source_rgb(0.97, 0.44, 0.44)  # Red #f87171
                cr.set_line_width(2.0)
                cr.set_line_cap(1)  # Round caps

                # Draw X
                cr.move_to(center_x - 4, center_y - 4)
                cr.line_to(center_x + 4, center_y + 4)
                cr.stroke()

                cr.move_to(center_x + 4, center_y - 4)
                cr.line_to(center_x - 4, center_y + 4)
                cr.stroke()

            elif status_type == 'downloading' and progress is not None:
                # Orange percentage text
                cr.set_source_rgb(0.98, 0.57, 0.24)  # Orange #fb923c

                # Draw percentage text
                percentage_text = f"{progress*100:.0f}%"
                cr.select_font_face("Sans", 0, 0)  # Normal, Non Bold
                cr.set_font_size(15)

                # Get text extents to center it
                extents = cr.text_extents(percentage_text)
                text_x = center_x - extents.width / 2 - extents.x_bearing
                text_y = center_y - extents.height / 2 - extents.y_bearing

                cr.move_to(text_x, text_y)
                cr.show_text(percentage_text)

        drawing_area.set_draw_func(draw_func)
        drawing_area.queue_draw()

    def _enable_row_subtitle_markup(self, row):
        """Enable Pango markup on an ActionRow's subtitle label and center content vertically"""
        def find_and_configure(widget):
            # Recursively find and configure widgets
            if isinstance(widget, Gtk.Label):
                # Enable markup on labels
                widget.set_use_markup(True)
            elif isinstance(widget, Gtk.Box):
                # Check if this box contains labels (title/subtitle container)
                has_labels = False
                child = widget.get_first_child()
                while child:
                    if isinstance(child, Gtk.Label):
                        has_labels = True
                        break
                    child = child.get_next_sibling()

                # If this box contains labels, center it vertically and allow expansion
                if has_labels:
                    widget.set_valign(Gtk.Align.CENTER)
                    widget.set_vexpand(True)

            # Check children recursively
            child = widget.get_first_child()
            while child:
                find_and_configure(child)
                child = child.get_next_sibling()

        find_and_configure(row)

    def format_sync_interval(self, seconds):
        """Format seconds into user-friendly string"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            if remaining_seconds == 0:
                return f"{minutes}m"
            else:
                return f"{minutes}m {remaining_seconds}s"
        else:
            hours = seconds // 3600
            remaining_minutes = (seconds % 3600) // 60
            return f"{hours}h {remaining_minutes}m"

    def debug_retroarch_status(self):
            """Debug RetroArch status"""
            print("=== RetroArch Debug Info ===")
            print(f"Executable: {getattr(self.retroarch, 'retroarch_executable', 'NOT SET')}")
            print(f"RetroArch object: {self.retroarch}")
            print(f"Save dirs: {getattr(self.retroarch, 'save_dirs', 'NOT SET')}")
            print(f"Cores dir: {getattr(self.retroarch, 'cores_dir', 'NOT SET')}")
            print(f"UI elements exist:")
            print(f"  - retroarch_info_row: {hasattr(self, 'retroarch_info_row')}")
            print(f"  - cores_info_row: {hasattr(self, 'cores_info_row')}")
            print(f"  - core_count_row: {hasattr(self, 'core_count_row')}")
            print(f"  - retroarch_connection_row: {hasattr(self, 'retroarch_connection_row')}")
            print("========================")

    def try_auto_connect(self):
        """Try to auto-connect if enabled"""
        auto_connect_enabled = self.settings.get('RomM', 'auto_connect')
        remember_enabled = self.settings.get('RomM', 'remember_credentials')
        url = self.settings.get('RomM', 'url')
        username = self.settings.get('RomM', 'username')
        password = self.settings.get('RomM', 'password')
        client_token = self.settings.get('RomM', 'client_token', '')

        # A paired Client API Token is sufficient on its own; otherwise require
        # remembered username/password.
        have_creds = bool(client_token) or (remember_enabled == 'true' and username and password)
        if auto_connect_enabled == 'true' and url and have_creds:
            self.log_message("🔄 Auto-connecting to RomM...")
            self.connection_enable_switch.set_active(True)
        elif auto_connect_enabled != 'true':
            self.log_message("⚠️ Auto-connect disabled")
        else:
            self.log_message("⚠️ Auto-connect enabled but credentials incomplete")

        return False

    def refresh_credential_fields(self):
        """Show credential entry fields only when not paired; once a Client API
        Token exists, hide them and show the compact 'Paired' status row."""
        if not hasattr(self, 'paired_status_row'):
            return
        has_token = bool(self.settings.get('RomM', 'client_token', ''))
        self.pair_code_row.set_visible(not has_token)
        self.pair_hint_row.set_visible(not has_token)
        self.password_login_expander.set_visible(not has_token)
        self.paired_status_row.set_visible(has_token)

    def on_unpair_clicked(self, button):
        """Forget the stored Client API Token, disconnect, and restore the
        credential fields."""
        # Disconnect any live session first. Flipping the switch off drives
        # on_connection_toggle -> disconnect_from_romm (and updates the switch UI).
        if self.connection_enable_switch.get_active():
            self.connection_enable_switch.set_active(False)
        elif self.romm_client:
            self.disconnect_from_romm()

        self.settings.set('RomM', 'client_token', '')
        self.log_message("🔓 Unpaired — pairing token removed")
        self.refresh_credential_fields()
        self.connection_expander.set_expanded(True)

    def on_pair_clicked(self, button):
        """Exchange the entered pairing code for a Client API Token and connect."""
        code = self.pair_code_row.get_text().strip()
        url = self.url_row.get_text().strip().rstrip('/')
        if not url:
            self.log_message("⚠️ Enter the Server URL before pairing")
            return
        if not code:
            self.log_message("⚠️ Enter a pairing code (from the RomM web UI)")
            return

        button.set_sensitive(False)
        self.log_message("🔗 Exchanging pairing code…")

        def work():
            res = RomMClient(url).exchange_pair_code(code)

            def done():
                button.set_sensitive(True)
                token = res.get('raw_token') if isinstance(res, dict) else res
                device_id = res.get('device_id') if isinstance(res, dict) else None
                if token:
                    self.settings.set('RomM', 'url', url)
                    self.settings.set('RomM', 'client_token', token)
                    self.settings.set('RomM', 'auto_connect', 'true')
                    if device_id:
                        self.settings.set('Device', 'device_id', device_id)
                        self.device_id = device_id
                    self.pair_code_row.set_text("")
                    self.log_message("✅ Paired with RomM (Client API Token)")
                    self.refresh_credential_fields()
                    if not self.connection_enable_switch.get_active():
                        self.connection_enable_switch.set_active(True)
                    else:
                        self.connect_to_romm()
                else:
                    self.log_message("❌ Pairing failed: invalid or expired code")
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def set_application_identity(self):
        """Set proper application identity for dock/taskbar"""
        try:
            # Set WM_CLASS to match desktop file
            import gi
            gi.require_version('Gdk', '4.0')
            from gi.repository import Gdk, GLib
            
            # Set application name first
            GLib.set_application_name("RomM - RetroArch Sync")
            
            # Get the surface and set WM_CLASS
            surface = self.get_surface()
            if surface and hasattr(surface, 'set_title'):
                surface.set_title("RomM - RetroArch Sync")
            
            # Set window class name
            self.set_title("RomM - RetroArch Sync")
            
            # Force the WM_CLASS for X11 systems
            display = self.get_display()
            if display and hasattr(display, 'get_name'):
                display_name = display.get_name() 
                if 'x11' in display_name.lower():
                    # For X11, we need to set the class hint
                    self.set_wmclass("romm-sync", "RomM - RetroArch Sync")

        except Exception as e:
            print(f"❌ Failed to set application identity: {e}")

    def handle_offline_mode(self):
        """Handle when not connected to RomM - show only downloaded games"""
        download_dir = Path(self.rom_dir_row.get_text())

        if self.game_cache.is_cache_valid():
            # Use cached data but FILTER to only show downloaded games
            cached_games = list(self.game_cache.cached_games)
            local_games = self.filter_to_downloaded_games_only(cached_games, download_dir)

            def update_ui():
                self.available_games = local_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(local_games)
                self.update_connection_ui("disconnected")

                if local_games:
                    self.log_message(f"📂 Offline mode: {len(local_games)} downloaded games (from cache)")
                else:
                    self.log_message(f"📂 Offline mode: No downloaded games found")

            GLib.idle_add(update_ui)
        else:
            # No cache - scan local files (platform mapping will be fetched inside scan_local_games_only)
            local_games = self.scan_local_games_only(download_dir)

            def update_ui():
                self.available_games = local_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(local_games)
                self.update_connection_ui("disconnected")
                self.log_message(f"📂 Offline mode: {len(local_games)} local games found")

            GLib.idle_add(update_ui)

    def on_autostart_changed(self, switch_row, pspec):
        """Handle autostart setting change"""
        enable = switch_row.get_active()
        
        def setup_autostart():
            try:
                if enable:
                    success = self.create_systemd_service()
                    if success:
                        GLib.idle_add(lambda: self.log_message("✅ Autostart enabled"))
                    else:
                        GLib.idle_add(lambda: self.log_message("❌ Failed to enable autostart"))
                        GLib.idle_add(lambda: switch_row.set_active(False))
                else:
                    success = self.remove_systemd_service()
                    if success:
                        GLib.idle_add(lambda: self.log_message("✅ Autostart disabled"))
                    else:
                        GLib.idle_add(lambda: self.log_message("❌ Failed to disable autostart"))
            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"❌ Autostart error: {e}"))
                GLib.idle_add(lambda: switch_row.set_active(False))
        
        threading.Thread(target=setup_autostart, daemon=True).start()

    def on_debug_mode_changed(self, switch_row, pspec):
        """Handle debug mode setting change"""
        enable = switch_row.get_active()
        self.settings.set('System', 'debug_mode', 'true' if enable else 'false')
        self.settings.save_settings()

        if enable:
            self.log_message("🔍 Debug mode enabled - detailed logs will be written to debug.log")
        else:
            self.log_message("✅ Debug mode disabled")

    def create_systemd_service(self):
        """Create systemd user service for autostart"""
        import subprocess
        import os
        import sys
        from pathlib import Path
        
        try:
            # Get current executable path
            if hasattr(sys, '_MEIPASS'):  # PyInstaller bundle
                exec_path = sys.executable
            elif os.environ.get('APPIMAGE'):  # AppImage
                exec_path = os.environ['APPIMAGE']
            else:  # Python script
                exec_path = f"python3 {os.path.abspath(__file__)}"
            
            # Create systemd user directory
            systemd_dir = Path.home() / '.config' / 'systemd' / 'user'
            systemd_dir.mkdir(parents=True, exist_ok=True)
            
            # Create service file
            service_content = f"""[Unit]
    Description=RomM RetroArch Sync
    After=multi-user.target

    [Service]
    Type=simple
    ExecStartPre=/bin/sleep 15
    ExecStart={exec_path} --minimized
    Restart=always
    RestartSec=10
    Environment=DISPLAY=:0
    KillMode=mixed
    KillSignal=SIGTERM

    [Install]
    WantedBy=default.target
    """
            
            service_file = systemd_dir / 'romm-retroarch-sync.service'
            with open(service_file, 'w') as f:
                f.write(service_content)
            
            # Enable and start service
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', 'romm-retroarch-sync.service'], check=True)
            
            # Save setting
            self.settings.set('System', 'autostart', 'true')
            
            return True
            
        except Exception as e:
            print(f"Failed to create systemd service: {e}")
            return False

    def remove_systemd_service(self):
        """Remove systemd user service"""
        import subprocess
        from pathlib import Path
        
        try:
            # Disable and stop service
            subprocess.run(['systemctl', '--user', 'disable', 'romm-retroarch-sync.service'], 
                        capture_output=True)
            subprocess.run(['systemctl', '--user', 'stop', 'romm-retroarch-sync.service'], 
                        capture_output=True)
            
            # Remove service file
            service_file = Path.home() / '.config' / 'systemd' / 'user' / 'romm-retroarch-sync.service'
            if service_file.exists():
                service_file.unlink()
            
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
            
            # Save setting
            self.settings.set('System', 'autostart', 'false')
            return True
            
        except Exception as e:
            print(f"Failed to remove systemd service: {e}")
            return False

    def update_systemd_service_if_needed(self):
        """Update systemd service if current executable differs from service file"""
        try:
            import subprocess
            import os
            import sys
            from pathlib import Path
            
            service_file = Path.home() / '.config' / 'systemd' / 'user' / 'romm-retroarch-sync.service'
            
            if not service_file.exists():
                return False
                
            # Get current executable path
            if os.environ.get('APPIMAGE'):
                current_exec = os.environ['APPIMAGE']
            elif hasattr(sys, '_MEIPASS'):
                current_exec = sys.executable
            else:
                current_exec = f"python3 {os.path.abspath(__file__)}"
            
            # Read service file
            with open(service_file, 'r') as f:
                service_content = f.read()
            
            # Check if ExecStart path is different
            if f"ExecStart={current_exec}" not in service_content:
                self.log_message("🔄 Updating autostart service for new version...")
                
                # Recreate service with new path
                success = self.create_systemd_service()
                if success:
                    self.log_message("✅ Autostart service updated")
                    return True
                else:
                    self.log_message("❌ Failed to update autostart service")
            
            return False
            
        except Exception as e:
            self.log_message(f"❌ Service update check failed: {e}")
            return False

    def check_autostart_status(self):
        """Check if autostart is currently enabled"""
        import subprocess
        try:
            result = subprocess.run(['systemctl', '--user', 'is-enabled', 'romm-retroarch-sync.service'], 
                                capture_output=True, text=True)
            return result.returncode == 0 and 'enabled' in result.stdout
        except Exception:
            return False

    def filter_to_downloaded_games_only(self, cached_games, download_dir):
        """Filter cached games to only show those that are actually downloaded"""
        downloaded_games = []

        for game in cached_games:
            # Use platform_slug instead of platform for directory name
            platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')

            # Use cached local_path if available (already correct for multi-disc), otherwise construct from file_name
            cached_path = game.get('local_path')
            if cached_path:
                local_path = Path(cached_path)
            else:
                file_name = game.get('file_name', '')
                if not file_name:
                    continue
                platform_dir = download_dir / platform_slug
                local_path = platform_dir / file_name

            # Check if file/folder exists and is valid (handles both files and folders)
            if self.is_path_validly_downloaded(local_path):
                # Update game data with current local info
                game_copy = game.copy()
                # Update platform display name from mapping if available
                if platform_slug and self.game_cache.platform_mapping:
                    game_copy['platform'] = self.game_cache.get_platform_name(platform_slug)
                game_copy['is_downloaded'] = True
                game_copy['local_path'] = str(local_path)
                game_copy['local_size'] = self.get_actual_file_size(local_path)

                # Update disc status for multi-disc games
                if game_copy.get('is_multi_disc') and game_copy.get('discs'):
                    for disc in game_copy['discs']:
                        disc['is_downloaded'] = True

                downloaded_games.append(game_copy)

        return self.library_section.sort_games_consistently(downloaded_games)

    def scan_and_merge_local_changes(self, cached_games):
        """Merge local file changes with cached RomM data - FILTER to downloaded only"""
        download_dir = Path(self.rom_dir_row.get_text())
        
        # Filter to only downloaded games instead of showing all
        downloaded_games = self.filter_to_downloaded_games_only(cached_games, download_dir)
        
        def update_ui():
            self.available_games = downloaded_games
            if hasattr(self, 'library_section'):
                self.library_section.update_games_library(downloaded_games)
            self.update_connection_ui("disconnected")
            
            if downloaded_games:
                total_cached = len(cached_games)
                self.log_message(f"📂 Offline: {len(downloaded_games)} downloaded games (of {total_cached} in cache)")
            else:
                self.log_message(f"📂 Offline: No downloaded games found")
        
        GLib.idle_add(update_ui)

    def use_cached_data_as_fallback(self):
        """Emergency fallback to cached data"""
        if self.game_cache.is_cache_valid():
            self.log_message("🛡️ Using cached data as fallback")
            self.scan_and_merge_local_changes(list(self.game_cache.cached_games))
        else:
            self.log_message("⚠️ No valid cache available")
            self.handle_offline_mode()

    def is_path_validly_downloaded(self, path):
        """Check if a path (file or folder) is validly downloaded

        Args:
            path: Path object or string to check

        Returns:
            bool: True if path is validly downloaded (folder with content or file with size > 1024)
        """
        path = Path(path)
        if not path.exists():
            return False

        if path.is_dir():
            # For folders, check if directory has content
            try:
                return any(path.iterdir())
            except (PermissionError, OSError):
                return False
        elif path.is_file():
            # For files, check if file has reasonable size
            return path.stat().st_size > 1024

        return False

    def get_actual_file_size(self, path):
        """Get actual size - sum all files for directories, file size for files"""
        path = Path(path)
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
            return size
        elif path.is_file():
            return path.stat().st_size
        else:
            return 0

    def get_disc_total_size(self, disc_path, parent_folder):
        """
        Calculate total size of all files related to a disc.
        For multi-file discs, this handles two cases:
        1. Disc is a folder (e.g., "Disc 1/") - sum all files in that folder
        2. Disc is a file (e.g., "Game (Disc 1).cue") - sum all files with same base name

        Args:
            disc_path: Path to disc (either a folder or primary disc file)
            parent_folder: Path to the parent folder

        Returns:
            Total size in bytes of all related files
        """
        disc_path = Path(disc_path)
        parent_folder = Path(parent_folder)

        if not disc_path.exists():
            return 0

        # Case 1: Disc is a folder (multi-file disc in its own folder)
        if disc_path.is_dir():
            files_in_folder = list(disc_path.rglob('*'))
            total_size = sum(f.stat().st_size for f in files_in_folder if f.is_file())
            return total_size

        # Case 2: Disc is a file (e.g., .cue or .bin file)
        # Find all files with the same base name
        base_name = disc_path.stem

        total_size = 0
        try:
            all_files = list(parent_folder.iterdir())
            for file in all_files:
                if file.is_file():
                    matches = file.stem == base_name
                    if matches:
                        file_size = file.stat().st_size
                        total_size += file_size
        except (PermissionError, OSError) as e:
            return disc_path.stat().st_size if disc_path.is_file() else 0

        fallback_size = disc_path.stat().st_size
        final_size = total_size if total_size > 0 else fallback_size
        return final_size

    def get_disc_size_from_api(self, disc_file_name, all_api_files):
        """
        Calculate total size of all API files related to a disc.
        For multi-file discs (e.g., BIN/CUE), this sums all files with the same base name.

        Args:
            disc_file_name: Name of the primary disc file (e.g., "Game (Disc 1).bin")
            all_api_files: List of all API file objects for the game

        Returns:
            Total size in bytes of all related files
        """
        # Get the base name without extension (e.g., "Game (Disc 1)" from "Game (Disc 1).bin")
        base_name = Path(disc_file_name).stem

        # Find all API files with the same base name and sum their sizes
        total_size = 0
        for api_file in all_api_files:
            api_file_name = api_file.get('file_name', '')
            api_stem = Path(api_file_name).stem
            matches = api_stem == base_name
            file_size = api_file.get('file_size_bytes', 0)
            if matches:
                total_size += file_size

        return total_size

    def process_single_rom(self, rom, download_dir):
        """Process a single ROM with short directory names but full display names"""
        rom_id = rom.get('id')
        
        # Safely extract display name from platform_display_name, platform_name, platform_custom_name, or platform object
        platform_obj = rom.get('platform')
        platform_obj_name = platform_obj.get('name') if isinstance(platform_obj, dict) else (platform_obj if isinstance(platform_obj, str) else None)
        platform_obj_slug = platform_obj.get('slug') if isinstance(platform_obj, dict) else None

        platform_display_name = (
            rom.get('platform_display_name') or
            rom.get('platform_name') or
            rom.get('platform_custom_name') or
            platform_obj_name or
            'Unknown'
        )
        platform_slug = (
            rom.get('platform_slug') or
            rom.get('platform_fs_slug') or
            platform_obj_slug or
            (platform_display_name if platform_display_name != 'Unknown' else '')
        )

        # If platform_display_name is missing or 'Unknown' but we have platform_slug, look it up in the platform mapping
        if (not platform_display_name or platform_display_name == 'Unknown') and platform_slug and platform_slug != 'Unknown':
            if hasattr(self, 'game_cache') and self.game_cache.platform_mapping:
                platform_display_name = self.game_cache.get_platform_name(platform_slug)
        
        if not platform_display_name:
            platform_display_name = 'Unknown'
        # Clean up platform slug - prefer "megadrive" over "genesis"
        if 'genesis' in platform_slug.lower() and 'megadrive' in platform_slug.lower():
            platform_slug = 'megadrive'
        elif '-slash-' in platform_slug:
            platform_slug = platform_slug.replace('-slash-', '-')
        file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"

        # Use platform slug for local directory structure (RomM and RetroDECK now use the same slugs)
        platform_dir = download_dir / platform_slug
        local_path = platform_dir / file_name

        # Check download status (handles both files and folders)
        is_downloaded = self.is_path_validly_downloaded(local_path)

        # Child-file ROMs (variants stored inside a parent folder ROM) will not be
        # found at the flat platform_dir/filename path.  If the flat check fails and
        # this ROM has siblings, scan immediate subdirectories of the platform dir.
        if not is_downloaded and rom.get('siblings') and platform_dir.exists():
            try:
                for subdir in platform_dir.iterdir():
                    if subdir.is_dir():
                        candidate = subdir / file_name
                        if self.is_path_validly_downloaded(candidate):
                            local_path = candidate
                            is_downloaded = True
                            break
            except (OSError, PermissionError):
                pass

        display_name = Path(file_name).stem if file_name else rom.get('name', 'Unknown')

        # Check for multi-disc games from API data OR local filesystem
        discs = []

        # First, check API data for multi-file games (works for both downloaded and non-downloaded)
        api_files = rom.get('files', [])
        has_multiple_files = rom.get('has_multiple_files', False) or rom.get('multi', False) or len(api_files) > 1

        if has_multiple_files and len(api_files) > 1:
            # Multi-disc game detected from API
            disc_extensions = {'.chd', '.bin', '.cue', '.iso', '.img', '.pbp'}

            # Filter for actual disc files (skip metadata files, manuals, etc.)
            api_disc_files = [f for f in api_files if Path(f.get('file_name', '')).suffix.lower() in disc_extensions]

            if len(api_disc_files) > 1:
                # Collect disc files, filtering out .cue files paired with .bin files
                filtered_disc_files = []
                for api_file in sorted(api_disc_files, key=lambda x: x.get('file_name', '')):
                    file_name = api_file.get('file_name', 'unknown')
                    # Skip .cue files if there's a corresponding .bin file
                    if file_name.lower().endswith('.cue'):
                        bin_name = file_name[:-4] + '.bin'
                        if any(f.get('file_name') == bin_name for f in api_disc_files):
                            continue
                    filtered_disc_files.append(api_file)

                # Only treat as multi-disc if we have actual separate disc indicators
                # Check for disc naming patterns: (Disc N), (Disk N), (CD N), etc.
                # Exclude Track patterns as they're part of a single disc
                disc_pattern = re.compile(r'(\(|\[|_|-|\s)(disc|disk|cd|dvd)(\s|_|-)?(\d+)(\)|\]|_|-|\s)', re.IGNORECASE)
                track_pattern = re.compile(r'(track|tr)(\s|_|-)?(\d+)', re.IGNORECASE)

                # Extract disc numbers from filenames (only count files, not tracks)
                disc_numbers = set()
                for api_file in filtered_disc_files:
                    file_name = api_file.get('file_name', 'unknown')
                    # Skip if this is a track indicator (not a disc)
                    if track_pattern.search(file_name):
                        continue
                    # Check if this file has a disc indicator
                    match = disc_pattern.search(file_name)
                    if match:
                        disc_num = match.group(4)  # The disc number
                        disc_numbers.add(disc_num)

                # Only treat as multi-disc if we have multiple different disc numbers
                actual_discs = []
                if len(disc_numbers) > 1:
                    for api_file in filtered_disc_files:
                        file_name = api_file.get('file_name', 'unknown')
                        if track_pattern.search(file_name):
                            continue
                        if disc_pattern.search(file_name):
                            actual_discs.append(api_file)

                # Only add to discs list if we found multiple actual disc files
                if len(actual_discs) > 1:
                    for api_file in actual_discs:
                        file_name = api_file.get('file_name', 'unknown')
                        # For downloaded games, use local path; for non-downloaded, use None
                        disc_path = str(local_path / file_name) if is_downloaded and local_path.is_dir() else None

                        # Calculate total size including all related files (e.g., .bin + .cue)
                        total_size = self.get_disc_size_from_api(file_name, api_disc_files)

                        discs.append({
                            'name': file_name,
                            'path': disc_path,
                            'is_downloaded': is_downloaded and local_path.is_dir(),
                            'size': total_size,
                            'file_id': api_file.get('id'),  # Store file ID for direct download
                            'full_path': api_file.get('full_path')  # Store full path from API
                        })

        # Fallback: For downloaded games without API file data, scan local filesystem
        elif is_downloaded and local_path.is_dir() and not api_files:
            disc_extensions = {'.chd', '.bin', '.cue', '.iso', '.img', '.pbp'}
            disc_files = []

            try:
                for file in sorted(local_path.iterdir()):
                    if file.is_file() and file.suffix.lower() in disc_extensions:
                        # Skip .cue files if there are .bin files (they're paired)
                        if file.suffix.lower() == '.cue':
                            bin_file = file.with_suffix('.bin')
                            if bin_file.exists():
                                continue
                        disc_files.append(file)

                # Check for actual multi-disc patterns in filenames
                if len(disc_files) > 1:
                    disc_pattern = re.compile(r'(\(|\[|_|-|\s)(disc|disk|cd|dvd)(\s|_|-)?(\d+)(\)|\]|_|-|\s)', re.IGNORECASE)
                    track_pattern = re.compile(r'(track|tr)(\s|_|-)?(\d+)', re.IGNORECASE)

                    # Extract disc numbers (only from files, not tracks)
                    disc_numbers = set()
                    for f in disc_files:
                        if track_pattern.search(f.name):
                            continue
                        match = disc_pattern.search(f.name)
                        if match:
                            disc_num = match.group(4)
                            disc_numbers.add(disc_num)

                    # Only treat as multi-disc if we have multiple different disc numbers
                    actual_disc_files = []
                    if len(disc_numbers) > 1:
                        for f in disc_files:
                            if track_pattern.search(f.name):
                                continue
                            if disc_pattern.search(f.name):
                                actual_disc_files.append(f)

                    if len(actual_disc_files) > 1:
                        for disc_file in actual_disc_files:
                            # Calculate total size including all related files (e.g., .bin + .cue)
                            total_size = self.get_disc_total_size(disc_file, local_path)
                            discs.append({
                                'name': disc_file.name,
                                'path': str(disc_file),
                                'is_downloaded': True,
                                'size': total_size
                            })
            except (PermissionError, OSError) as e:
                print(f"Warning: Could not scan directory {local_path}: {e}")

        # For multi-disc games, update local_path to point to folder instead of disc file
        if discs and len(discs) > 1:
            # Multi-disc games are stored in folders named after the game
            local_path = platform_dir / display_name
            is_downloaded = self.is_path_validly_downloaded(local_path)

        # Extract only essential data from romm_data to save memory
        essential_romm_data = {
            'fs_name': rom.get('fs_name'),
            'fs_name_no_ext': rom.get('fs_name_no_ext'),
            'fs_size_bytes': rom.get('fs_size_bytes', 0),
            'platform_id': rom.get('platform_id'),
            'platform_slug': rom.get('platform_slug')
        }

        game_data = {
            'name': display_name,
            'rom_id': rom_id,
            'platform': platform_display_name,
            'platform_slug': platform_slug,
            'file_name': file_name,
            'is_downloaded': is_downloaded,
            'local_path': str(local_path) if is_downloaded else None,
            'local_size': self.get_actual_file_size(local_path) if is_downloaded else 0,
            'romm_data': essential_romm_data  # Much smaller object
        }

        # Add discs if this is a multi-disc game (only if there are multiple discs)
        if len(discs) > 1:
            game_data['discs'] = discs
            game_data['is_multi_disc'] = True
        else:
            game_data['is_multi_disc'] = False

        # Handle regional variants (sibling ROMs) from grouped data
        sibling_files = rom.get('_sibling_files', [])
        if sibling_files:
            # Store sibling data for UI display (similar to discs)
            game_data['_sibling_files'] = sibling_files
            print(f"Preserving {len(sibling_files)} sibling(s) for '{display_name}'")

        # Store raw sibling relationship so the download path can locate the
        # parent folder ROM when this entry is a child file (collection view).
        raw_siblings = rom.get('siblings', [])
        if raw_siblings:
            game_data['_siblings'] = raw_siblings
            game_data['_fs_extension'] = rom.get('fs_extension', '')

        return game_data

    def on_auto_connect_changed(self, switch_row, pspec):
        """Handle auto-connect setting change"""
        self.settings.set('RomM', 'auto_connect', str(switch_row.get_active()).lower())

    def on_auto_refresh_changed(self, switch_row, pspec):
        """Handle auto-refresh setting change"""
        self.settings.set('RomM', 'auto_refresh', str(switch_row.get_active()).lower())    

    def on_about(self, action, param):
        """Show about dialog"""
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="RomM - RetroArch Sync",
            application_icon="com.romm.retroarch.sync",
            version="1.7",
            developer_name='Hector Eduardo "Covin" Silveri',
            copyright="© 2025-2026 Hector Eduardo Silveri",
            license_type=Gtk.License.GPL_3_0
        )
        about.set_website("https://github.com/Covin90/romm-retroarch-sync")
        about.set_issue_url("https://github.com/Covin90/romm-retroarch-sync/issues")
        about.present()

    def get_overwrite_behavior(self):
        """Get user's preferred overwrite behavior"""
        if hasattr(self, 'auto_overwrite_row'):
            selected = self.auto_overwrite_row.get_selected()
            behaviors = [
                "Smart (prefer newer)",
                "Always prefer local", 
                "Always download from server",
                "Ask each time"
            ]
            if 0 <= selected < len(behaviors):
                return behaviors[selected]
        
        return "Smart (prefer newer)"  # Default

    def on_overwrite_behavior_changed(self, combo_row, pspec):
        """Save overwrite behavior setting"""
        selected = combo_row.get_selected()
        self.settings.set('AutoSync', 'overwrite_behavior', str(selected))

    def on_retroarch_override_changed(self, entry_row):
        """Handle RetroArch path override change"""
        custom_path = entry_row.get_text().strip()

        # Handle RetroDECK config directory input
        if 'retrodeck' in custom_path.lower() and 'config/retroarch' in custom_path:
            # User entered config directory, set to RetroDECK executable instead
            custom_path = 'flatpak run net.retrodeck.retrodeck retroarch'
            entry_row.set_text(custom_path)  # Update the field

        self.settings.set('RetroArch', 'custom_path', custom_path)

        # Re-initialize RetroArch with new path
        self.retroarch = RetroArchInterface()
        self.refresh_retroarch_info()

        if custom_path:
            self.log_message(f"RetroArch path overridden: {custom_path}")
        else:
            self.log_message("RetroArch path override cleared, using auto-detection")

    def on_device_name_changed(self, entry_row):
        """Handle device name change"""
        new_name = entry_row.get_text().strip()
        if not new_name:
            # Don't allow empty names - reset to hostname
            new_name = socket.gethostname()
            entry_row.set_text(new_name)

        # Save to settings
        self.settings.set('Device', 'device_name', new_name)
        self.log_message(f"✓ Device name updated to: {new_name}")

        # Re-register device with new name if connected
        if self.romm_client and self.romm_client.authenticated:
            device_id = self.settings.get('Device', 'device_id', '')
            if device_id:
                # Re-register with new name
                self.romm_client.register_device(device_name=new_name)
                self.log_message(f"✓ Device re-registered with RomM")
                # Refresh device info display
                GLib.idle_add(self.update_device_info_display)

    def debug_icon_loading(self):
        """Set application icon for GTK4 correctly"""
        import os
        from pathlib import Path
        
        print("=== Setting GTK4 Application Icon ===")
        
        # Get the script directory (src/) and go up to project root
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        
        # Find the icon in the new structure
        icon_locations = [
            # New structure paths
            os.path.join(project_root, 'assets', 'icons', 'romm_icon.png'),
            # AppImage paths (for when running from AppImage)
            os.path.join(os.environ.get('APPDIR', ''), 'usr/bin/romm_icon.png'),
            os.path.join(os.environ.get('APPDIR', ''), 'romm-sync.png'),
            # Fallback: same directory as script
            os.path.join(script_dir, 'romm_icon.png'),
            "romm_icon.png"
        ]
        
        icon_path = None
        for location in icon_locations:
            if location and Path(location).exists():
                icon_path = location
                print(f"✓ Using icon: {icon_path}")
                break
        
        if icon_path:
            try:
                # GTK4 approach: Set via GLib and icon theme
                from gi.repository import GLib, Gtk, Gio
                import shutil
                import tempfile
                
                # Create temp icon directory
                temp_dir = Path(tempfile.gettempdir()) / 'romm-sync-icons'
                temp_dir.mkdir(exist_ok=True)
                
                # Copy with application ID as filename
                app_icon_path = temp_dir / 'com.romm.retroarch.sync.png'
                shutil.copy2(icon_path, app_icon_path)
                
                # Add to default icon theme
                icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
                icon_theme.add_search_path(str(temp_dir))
                
                # Set icon on current window
                if hasattr(self, 'set_icon_name'):
                    self.set_icon_name('com.romm.retroarch.sync')
                
                print("✅ Set application icon via GTK4 method")
                    
            except Exception as e:
                print(f"❌ Failed to set application icon: {e}")
        else:
            print("❌ No icon found in any location")
        
    print("================================")

    def integrate_appimage(self):
        """Set up icon theme for AppImage without desktop file creation"""
        try:
            import os
            import shutil
            import subprocess
            from pathlib import Path
            
            # Check if running from AppImage
            appimage_path = os.environ.get('APPIMAGE')
            if not appimage_path:
                return
            
            print("Setting up AppImage icon theme...")
            
            # Copy icon to user icon directory for proper display
            icon_source = os.path.join(os.environ.get('APPDIR', ''), 'usr/bin/romm_icon.png')
            if Path(icon_source).exists():
                # Copy to multiple icon sizes for better scaling
                icon_sizes = [16, 22, 24, 32, 48, 64, 96, 128, 256]
                for size in icon_sizes:
                    size_dir = Path.home() / '.local/share/icons/hicolor' / f'{size}x{size}' / 'apps'
                    size_dir.mkdir(parents=True, exist_ok=True)
                    icon_dest = size_dir / 'com.romm.retroarch.sync.png'
                    shutil.copy2(icon_source, icon_dest)
                
                print(f"✅ Copied icon to {len(icon_sizes)} different sizes")
                
                # Update icon cache
                subprocess.run(['gtk-update-icon-cache', str(Path.home() / '.local/share/icons/hicolor')], 
                            capture_output=True)
                
                print("✅ Icon theme updated")
            else:
                print("⚠️ Icon source not found")
            
        except Exception as e:
            print(f"Icon setup failed: {e}")

    def load_saved_settings(self):
        """Load saved settings into UI"""
        self.url_row.set_text(self.settings.get('RomM', 'url'))

        has_password_creds = False
        if self.settings.get('RomM', 'remember_credentials') == 'true':
            self.username_row.set_text(self.settings.get('RomM', 'username'))
            self.password_row.set_text(self.settings.get('RomM', 'password'))
            self.remember_switch.set_active(True)
            has_password_creds = bool(self.settings.get('RomM', 'username'))

        # First-run / unconfigured: expand the connection panel so the pairing
        # flow is visible immediately instead of hidden behind a collapsed row.
        has_token = bool(self.settings.get('RomM', 'client_token', ''))
        if not has_token and not has_password_creds:
            self.connection_expander.set_expanded(True)
        elif has_password_creds and not has_token:
            # Returning password user: surface their saved fields.
            self.password_login_expander.set_expanded(True)

        # Hide credential fields when already paired; show the 'Paired' row.
        self.refresh_credential_fields()

        if hasattr(self, 'autostart_row'):
            # Defer autostart check until UI is fully ready
            def check_autostart_when_ready():
                is_enabled = self.check_autostart_status()
                self.autostart_row.set_active(is_enabled)
                return False  # Don't repeat

            GLib.timeout_add(100, check_autostart_when_ready)

        self.auto_connect_switch.set_active(self.settings.get('RomM', 'auto_connect') == 'true')
        self.auto_refresh_switch.set_active(self.settings.get('RomM', 'auto_refresh') == 'true') 

    def setup_ui(self):
            """Set up the user interface with actually working wider layout"""
            self.set_title("RomM - RetroArch Sync")
            self.set_default_size(800, 900)  # Good default height - library will expand to fill

            # Constrain window size to prevent it from growing beyond reasonable bounds
            # Get the display to calculate max height
            try:
                display = self.get_display()
                if display:
                    monitor = display.get_monitors()[0]  # Get primary monitor
                    geometry = monitor.get_geometry()
                    max_height = int(geometry.height * 0.9)  # 90% of screen height
                    self.set_size_request(800, min(900, max_height))  # Set minimum/initial size
            except Exception:
                pass  # Fallback if display detection fails

            self.detected_de = detect_desktop_environment(manual_de=getattr(self, 'cli_de', None))
            de_css = get_de_custom_css(self.detected_de)
            css_data = f"""
            /* Column Chooser gear button compact styling */
            button.column-gear-btn {{
                min-width: 16px;
                min-height: 16px;
                padding: 0px 2px;
                margin: 0px;
                border-radius: 4px;
            }}
            button.column-gear-btn image {{
                min-width: 16px;
                min-height: 16px;
                opacity: 1.0;
            }}

            /* Mission Center-inspired styling with system font */
            .data-table {{
                background: @view_bg_color;
                font-family: -gtk-system-font;
                font-size: 1em;
            }}

            /* Target the ScrolledWindow that contains the tree view */
            scrolledwindow.data-table {{
                border: 1px solid @borders;
                border-radius: 10px;
                background: @view_bg_color;
            }}

            .data-table columnview {{
                border: none;  /* Remove border since ScrolledWindow has it now */
                border-radius: 10px;
            }}

            /* Make sure the listview inside respects the rounded corners */
            .data-table columnview > listview {{
                border-radius: 0px;
            }}

            .data-table row {{
                min-height: 36px;
                border-bottom: 1px solid alpha(@borders, 0.25);
                transition: all 150ms ease;
                background: @view_bg_color;
            }}

            /* Round the corners of first and last rows */
            .data-table row:first-child {{
                border-top-left-radius: 0px;
                border-top-right-radius: 0px;
            }}

            .data-table row:last-child {{
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                border-bottom: none;
            }}

            .data-table row:nth-child(even) {{
                background: alpha(@window_bg_color, 0.5);
            }}

            .data-table row:nth-child(odd) {{
                background: alpha(@card_bg_color, 0.4);
            }}

            .data-table row:hover {{
                background: alpha(@accent_color, 0.1);
            }}

            /* Simple selection without rounded corners */
            columnview > listview > row:selected {{
                background: alpha(@accent_bg_color, 0.3);
                color: @window_fg_color;
            }}

            columnview > listview > row:selected > cell {{
                background: alpha(@accent_bg_color, 0.3);
                color: @window_fg_color;
            }}

            .numeric {{
                font-family: -gtk-system-font;
                font-size: 1em;
                color: @dim_label_color;
            }}

            /* Much smaller toggle switches for collection view - using scale transform */
            switch.compact-switch {{
                transform: scale(0.65);
                margin: -8px;
            }}

            /* Also apply to collection-specific classes */
            switch.collection-synced,
            switch.collection-partial-sync,
            switch.collection-not-synced {{
                transform: scale(0.65);
                margin: -8px;
            }}

            /* Steam button in collection view - ensure proper padding to prevent truncation */
            button.flat.compact-switch {{
                padding: 4px;
                margin: 0;
            }}

            {de_css}
            """.encode('utf-8')

            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(css_data)
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER  # Higher priority than APPLICATION
            )

            # Main content container (simple box - no PreferencesPage width constraints)
            main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

            # Create wrapper for connection section to hold PreferencesGroup
            self.connection_wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            self.connection_wrapper.set_margin_top(12)
            main_box.append(self.connection_wrapper)

            # Header bar with menu button (Only for GNOME DE)
            if self.detected_de == 'GNOME':
                header = Adw.HeaderBar()
                self.set_titlebar(header)

                # Add menu button to header bar
                menu_button = Gtk.MenuButton()
                menu_button.set_icon_name("open-menu-symbolic")
                menu_button.set_tooltip_text("Menu")

                # Create simple menu
                menu = Gio.Menu()
                menu.append("Logs / Advanced", "win.logs")
                menu.append("About", "win.about")
                menu.append("Quit", "win.quit")
                menu_button.set_menu_model(menu)
                header.pack_end(menu_button)
            else:
                # Non-GNOME DE: Let system window manager draw native DE titlebar and window controls
                self.set_titlebar(None)

            # Wrap main_box in a scrolled window to prevent window from expanding
            # when content grows (e.g., expanding library sections)
            main_scrolled = Gtk.ScrolledWindow()
            main_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            main_scrolled.set_child(main_box)

            # For non-GNOME DEs, add traditional top MenuBar (File, View, Tools, Help)
            if self.detected_de != 'GNOME':
                menubar_model = Gio.Menu()

                # File Submenu
                file_menu = Gio.Menu()
                file_menu.append("Refresh Library", "win.refresh")
                file_menu.append("Download Missing BIOS", "win.download_bios")
                file_menu.append("Quit", "win.quit")
                menubar_model.append_submenu("File", file_menu)

                # View Submenu
                view_menu = Gio.Menu()
                view_menu.append("Toggle Flat / Tree View", "win.toggle_flat_view")
                view_menu.append("Show Downloaded Only", "win.toggle_show_downloaded")
                view_menu.append("Expand All Platforms", "win.expand_all")
                view_menu.append("Collapse All Platforms", "win.collapse_all")
                menubar_model.append_submenu("View", view_menu)

                # Tools Submenu
                tools_menu = Gio.Menu()
                tools_menu.append("Logs / Advanced", "win.logs")
                menubar_model.append_submenu("Tools", tools_menu)

                # Help Submenu
                help_menu = Gio.Menu()
                help_menu.append("About", "win.about")
                menubar_model.append_submenu("Help", help_menu)

                self.top_menubar = Gtk.PopoverMenuBar.new_from_model(menubar_model)
                self.top_menubar.add_css_class("traditional-top-menubar")

                top_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                top_container.append(self.top_menubar)
                top_container.append(main_scrolled)
                self.set_child(top_container)
            else:
                self.set_child(main_scrolled)

            # Create sections
            self.create_connection_section()  # Connection & Sync section (includes RomM, RetroArch, Auto-Sync)
            self.create_library_section()     # Game library tree view

            # Add library directly to main_box (NOT to preferences_page) so it can expand
            # Add title label
            library_title = Gtk.Label()
            library_title.set_markup("<b>Game Library</b>")
            library_title.set_halign(Gtk.Align.START)
            library_title.set_margin_top(16 if self.detected_de != 'GNOME' else 24)
            library_title.set_margin_bottom(12)
            library_title.set_margin_start(12)
            main_box.append(library_title)

            # Remove library container from its current parent (ActionRow)
            # unparent() removes the widget from whatever parent it has
            self.library_section.library_container.unparent()

            # Wrap library in a styled container to match Connection & Sync section
            library_wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            margin = 12 if self.detected_de == 'GNOME' else 6
            library_wrapper.set_margin_start(margin)
            library_wrapper.set_margin_end(margin)
            library_wrapper.set_margin_bottom(margin)
            if self.detected_de == 'GNOME':
                library_wrapper.add_css_class('card')
            else:
                library_wrapper.add_css_class('traditional-frame')

            # Add library container to wrapper
            # Don't set vexpand - scrolled window inside handles expansion within max height
            library_wrapper.append(self.library_section.library_container)

            # Add wrapper to main_box
            main_box.append(library_wrapper) 

    def on_download_all_bios(self, button):
        """Download all missing BIOS files for current game platforms"""
        if not self.retroarch.bios_manager:
            self.log_message("⚠️ BIOS manager not available")
            return
        
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("⚠️ Please connect to RomM first")
            return
        
        def download_all():
            try:
                self.retroarch.bios_manager.romm_client = self.romm_client
                
                # Get platforms from current games
                platforms_in_library = set()
                for game in self.available_games:
                    platform = game.get('platform')
                    if platform:
                        platforms_in_library.add(platform)
                
                GLib.idle_add(lambda: self.log_message(f"📥 Downloading BIOS for {len(platforms_in_library)} platforms..."))
                
                total_downloaded = 0
                for platform in platforms_in_library:
                    normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                    if self.retroarch.bios_manager.auto_download_missing_bios(normalized):
                        total_downloaded += 1
                
                GLib.idle_add(lambda: self.log_message(f"✅ BIOS download complete for {total_downloaded} platforms"))
                
            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"❌ BIOS download error: {e}"))
        
        threading.Thread(target=download_all, daemon=True).start()
    
    def download_missing_bios_files(self, platforms_needing_bios):
        """Download missing BIOS files for multiple platforms"""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("⚠️ Please connect to RomM first")
            return
        
        def download_all():
            try:
                self.retroarch.bios_manager.romm_client = self.romm_client
                total_downloaded = 0
                total_failed = 0
                
                for platform_name, missing_files in platforms_needing_bios:
                    GLib.idle_add(lambda p=platform_name: 
                                self.log_message(f"📥 Downloading BIOS for {p}..."))
                    
                    for bios_info in missing_files:
                        bios_file = bios_info['file']
                        
                        # Try to download from RomM
                        if self.retroarch.bios_manager.download_bios_from_romm(platform_name, bios_file):
                            total_downloaded += 1
                            GLib.idle_add(lambda f=bios_file: 
                                        self.log_message(f"   ✅ {f}"))
                        else:
                            total_failed += 1
                            GLib.idle_add(lambda f=bios_file: 
                                        self.log_message(f"   ❌ {f} - not found on server"))
                
                # Summary
                if total_failed == 0 and total_downloaded > 0:
                    GLib.idle_add(lambda n=total_downloaded: 
                                self.log_message(f"✅ Downloaded {n} BIOS files successfully!"))
                elif total_downloaded > 0:
                    GLib.idle_add(lambda d=total_downloaded, f=total_failed: 
                                self.log_message(f"⚠️ Downloaded {d} files, {f} not found on server"))
                else:
                    GLib.idle_add(lambda: 
                                self.log_message("❌ No BIOS files could be downloaded from server"))
                
            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"❌ BIOS download error: {e}"))
        
        threading.Thread(target=download_all, daemon=True).start()

    def on_bios_override_changed(self, entry_row, pspec=None):
        """Handle BIOS path override change"""
        custom_path = entry_row.get_text().strip()
        self.settings.set('BIOS', 'custom_path', custom_path)
        
        # Force complete reinitialization of RetroArch interface
        self.retroarch = RetroArchInterface(self.settings)
        
        # Update the directory display
        self.update_bios_directory_info()
        
        if custom_path:
            self.log_message(f"BIOS path overridden: {custom_path}")
            try:
                Path(custom_path).mkdir(parents=True, exist_ok=True)
                self.log_message(f"✅ BIOS directory ready: {custom_path}")
            except Exception as e:
                self.log_message(f"❌ Could not create BIOS directory: {e}")
        else:
            self.log_message("BIOS path override cleared, reverting to auto-detection")

    def create_connection_section(self):
        """Create combined connection and sync section"""
        connection_group = Adw.PreferencesGroup()
        margin = 12 if getattr(self, 'detected_de', 'GNOME') == 'GNOME' else 6
        connection_group.set_margin_start(margin)
        connection_group.set_margin_end(margin)
        
        # Section expander row for the overall Connection & Sync section
        self.connection_sync_expander = Adw.ExpanderRow()
        self.connection_sync_expander.set_title("Connection &amp; Sync")
        self.connection_sync_expander.set_subtitle("RomM Server, RetroArch &amp; Auto-Sync Configuration")

        saved_expanded = self.settings.get('UI', 'connection_sync_expanded', 'true') == 'true'
        self.connection_sync_expander.set_expanded(saved_expanded)
        self.connection_sync_expander.connect('notify::expanded', self.on_connection_sync_expanded_changed)

        connection_group.add(self.connection_sync_expander)

        # RomM Connection expander (keep as is)
        self.connection_expander = Adw.ExpanderRow()
        self.connection_expander.set_title("RomM Connection")
        self.connection_expander.set_subtitle("Not connected - expand to configure")

        # Add status dot as prefix (15px size for better visibility)
        self.connection_status_dot = self.create_status_dot('grey', size=15)
        self.connection_status_dot.set_margin_end(8)
        self.connection_expander.add_prefix(self.connection_status_dot)

        # Add full resync button with circular arrow icon to the left of the slider
        self.resync_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.resync_btn.set_valign(Gtk.Align.CENTER)
        self.resync_btn.set_tooltip_text("Full Resync with RomM Server")
        self.resync_btn.add_css_class("flat")
        self.resync_btn.connect("clicked", lambda b: self.refresh_games_list(force_full_refresh=True))
        self.connection_expander.add_suffix(self.resync_btn)

        # Add toggle switch as suffix to enable/disable connection
        self.connection_enable_switch = Gtk.Switch()
        self.connection_enable_switch.set_valign(Gtk.Align.CENTER)
        self.connection_enable_switch.connect('notify::active', self.on_connection_toggle)
        self.connection_expander.add_suffix(self.connection_enable_switch)
        
        # RomM Connection settings inside the expander (only shown when expanded)
        
        # RomM URL entry
        self.url_row = Adw.EntryRow()
        self.url_row.set_title("Server URL")
        self.url_row.set_text("")
        self.connection_expander.add_row(self.url_row)
        
        # Pairing code (RomM Client API Token) — the recommended sign-in method.
        # Shown first and prominently (above password login) so it isn't missed
        # on first launch. Create a token in the RomM web UI, start pairing, and
        # enter the code here.
        self.pair_code_row = Adw.EntryRow()
        self.pair_code_row.set_title("Pairing code (recommended)")
        pair_button = Gtk.Button(label="Pair")
        pair_button.set_valign(Gtk.Align.CENTER)
        pair_button.add_css_class("suggested-action")
        pair_button.connect("clicked", self.on_pair_clicked)
        self.pair_code_row.add_suffix(pair_button)
        self.connection_expander.add_row(self.pair_code_row)

        # Short hint explaining where the pairing code comes from.
        self.pair_hint_row = Adw.ActionRow()
        self.pair_hint_row.set_subtitle(
            "In RomM, open Settings, generate a pairing code, then enter it above "
            "and press Pair. No username or password needed."
        )
        self.pair_hint_row.set_subtitle_lines(0)
        self.pair_hint_row.add_css_class("dim-label")
        self.connection_expander.add_row(self.pair_hint_row)

        # "Paired" status row — shown instead of the credential fields once a
        # Client API Token exists, with an Unpair action to revert.
        self.paired_status_row = Adw.ActionRow()
        self.paired_status_row.set_title("Paired with RomM")
        self.paired_status_row.set_subtitle("Signed in with a pairing token")
        unpair_button = Gtk.Button(label="Unpair")
        unpair_button.set_valign(Gtk.Align.CENTER)
        unpair_button.add_css_class("destructive-action")
        unpair_button.connect("clicked", self.on_unpair_clicked)
        self.paired_status_row.add_suffix(unpair_button)
        self.paired_status_row.set_visible(False)
        self.connection_expander.add_row(self.paired_status_row)

        # Secondary, collapsible username/password login — tucked away so the
        # pairing flow stays the primary path.
        self.password_login_expander = Adw.ExpanderRow()
        self.password_login_expander.set_title("Sign in with password instead")
        self.setup_expander_chevrons(self.password_login_expander)
        self.connection_expander.add_row(self.password_login_expander)

        # Username entry
        self.username_row = Adw.EntryRow()
        self.username_row.set_title("Username")
        self.password_login_expander.add_row(self.username_row)

        # Password entry
        self.password_row = Adw.PasswordEntryRow()
        self.password_row.set_title("Password")
        self.password_login_expander.add_row(self.password_row)

        # Remember credentials switch (applies to password login)
        self.remember_switch = Adw.SwitchRow()
        self.remember_switch.set_title("Remember credentials")
        self.remember_switch.set_subtitle("Save login details locally")
        self.password_login_expander.add_row(self.remember_switch)

        # Auto-connect switch
        self.auto_connect_switch = Adw.SwitchRow()
        self.auto_connect_switch.set_title("Auto-connect on startup")
        self.auto_connect_switch.set_subtitle("Automatically connect when app starts")
        self.auto_connect_switch.connect('notify::active', self.on_auto_connect_changed)
        self.connection_expander.add_row(self.auto_connect_switch)

        # Auto-refresh switch
        self.auto_refresh_switch = Adw.SwitchRow()
        self.auto_refresh_switch.set_title("Auto-refresh library on startup")
        self.auto_refresh_switch.set_subtitle("Automatically fetch games if cache is outdated")
        self.auto_refresh_switch.connect('notify::active', self.on_auto_refresh_changed)
        self.connection_expander.add_row(self.auto_refresh_switch)

        # Autostart setting
        self.autostart_row = Adw.SwitchRow()
        self.autostart_row.set_title("Run at Startup")
        self.autostart_row.set_subtitle("Automatically start minimized to tray on login")
        self.autostart_row.connect('notify::active', self.on_autostart_changed)
        self.connection_expander.add_row(self.autostart_row)

        # Device Information section
        device_expander = Adw.ExpanderRow()
        device_expander.set_title("Device Information")
        device_expander.set_subtitle("Registered device details")
        self.setup_expander_chevrons(device_expander)
        self.connection_expander.add_row(device_expander)

        # Device ID (read-only)
        self.device_id_row = Adw.ActionRow()
        self.device_id_row.set_title("Device ID")
        device_id_label = Gtk.Label()
        device_id_label.set_text("Not registered")
        device_id_label.add_css_class("monospace")
        self.device_id_row.add_suffix(device_id_label)
        self.device_id_label = device_id_label
        device_expander.add_row(self.device_id_row)

        # Device Name (editable)
        self.device_name_row = Adw.EntryRow()
        self.device_name_row.set_title("Device Name")
        device_name = self.settings.get('Device', 'device_name', socket.gethostname())
        self.device_name_row.set_text(device_name)
        self.device_name_row.connect('apply', self.on_device_name_changed)
        device_expander.add_row(self.device_name_row)

        # Device Platform
        self.device_platform_row = Adw.ActionRow()
        self.device_platform_row.set_title("Platform")
        device_platform_label = Gtk.Label()
        device_platform_label.set_text("-")
        self.device_platform_row.add_suffix(device_platform_label)
        self.device_platform_label = device_platform_label
        device_expander.add_row(self.device_platform_row)

        # Client Info
        self.device_client_row = Adw.ActionRow()
        self.device_client_row.set_title("Client")
        device_client_label = Gtk.Label()
        device_client_label.set_text("-")
        self.device_client_row.add_suffix(device_client_label)
        self.device_client_label = device_client_label
        device_expander.add_row(self.device_client_row)

        # Delete Device row
        delete_device_row = Adw.ActionRow()
        delete_device_row.set_title("Unregister Device")
        delete_device_row.set_subtitle("Remove this device from the server")
        delete_container = Gtk.Box()
        delete_container.set_size_request(-1, 18)
        delete_container.set_valign(Gtk.Align.CENTER)
        delete_button = Gtk.Button(label="Delete")
        delete_button.add_css_class("destructive-action")
        delete_button.connect('clicked', self.on_delete_device_clicked)
        delete_button.set_size_request(80, 18)
        delete_button.set_hexpand(False)
        delete_button.set_vexpand(False)
        delete_button.set_valign(Gtk.Align.CENTER)
        delete_container.append(delete_button)
        delete_device_row.add_suffix(delete_container)
        device_expander.add_row(delete_device_row)

        self.connection_sync_expander.add_row(self.connection_expander)
        
        # RetroArch section - simplified without status monitoring
        self.retroarch_expander = Adw.ExpanderRow()
        self.retroarch_expander.set_title("RetroArch")
        self.retroarch_expander.set_subtitle("Installation and core information")

        # Add status dot as prefix (15px size for better visibility)
        self.retroarch_status_dot = self.create_status_dot('grey', size=15)
        self.retroarch_status_dot.set_margin_end(8)
        self.retroarch_expander.add_prefix(self.retroarch_status_dot)

        # Refresh button
        refresh_container = Gtk.Box()
        refresh_container.set_size_request(-1, 18)
        refresh_container.set_valign(Gtk.Align.CENTER)

        refresh_button = Gtk.Button(label="Refresh")
        refresh_button.connect('clicked', self.on_refresh_retroarch_info)
        refresh_button.set_size_request(80, 18)
        refresh_button.set_hexpand(False)
        refresh_button.set_vexpand(False)
        refresh_button.set_valign(Gtk.Align.CENTER)
        refresh_container.append(refresh_button)

        self.retroarch_expander.add_suffix(refresh_container)

        # Installation info row
        self.retroarch_info_row = Adw.ActionRow()
        self.retroarch_info_row.set_title("Installation")
        self.retroarch_info_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.retroarch_info_row)

        # RetroArch installation override
        self.retroarch_override_row = Adw.EntryRow()
        self.retroarch_override_row.set_title("Custom Installation Path (Override auto-detection)")
        self.retroarch_override_row.set_text(self.settings.get('RetroArch', 'custom_path', ''))
        self.retroarch_override_row.connect('activate', self.on_retroarch_override_changed)
        self.retroarch_expander.add_row(self.retroarch_override_row)

        # Cores directory row
        self.cores_info_row = Adw.ActionRow()
        self.cores_info_row.set_title("Cores Directory")
        self.cores_info_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.cores_info_row)

        # Available cores count row
        self.core_count_row = Adw.ActionRow()
        self.core_count_row.set_title("Available Cores")
        self.core_count_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.core_count_row)

        # Quick access to the monitored save / save-state folders
        _save_dirs = getattr(self.retroarch, 'save_dirs', {}) or {}
        for _key, _title in (('saves', 'Saves Folder'), ('states', 'Save States Folder')):
            _dir = _save_dirs.get(_key)
            _row = Adw.ActionRow()
            _row.set_title(_title)
            _row.set_subtitle(str(_dir) if _dir else "Not detected")
            _row.set_subtitle_lines(1)
            _btn_box = Gtk.Box()
            _btn_box.set_size_request(-1, 18)
            _btn_box.set_valign(Gtk.Align.CENTER)
            _btn = Gtk.Button(label="Open")
            _btn.set_size_request(80, -1)
            _btn.set_valign(Gtk.Align.CENTER)
            _btn.set_sensitive(bool(_dir))
            _btn.connect('clicked', self.on_browse_saves if _key == 'saves' else self.on_browse_states)
            _btn_box.append(_btn)
            _row.add_suffix(_btn_box)
            self.retroarch_expander.add_row(_row)

        # Add RetroArch settings status row (auto-enabled, info-only display)
        self.retroarch_connection_row = Adw.ActionRow()
        self.retroarch_connection_row.set_title("")  # Empty title for better centering
        self.retroarch_connection_row.set_subtitle("Auto-enabling RetroArch settings...")
        # Allow subtitle to wrap to multiple lines if needed
        self.retroarch_connection_row.set_subtitle_lines(3)

        self.retroarch_expander.add_row(self.retroarch_connection_row)

        # Enable markup immediately and schedule status update
        from gi.repository import GLib
        def enable_markup_and_update():
            try:
                self._enable_row_subtitle_markup(self.retroarch_connection_row)
                # Ensure retroarch is initialized
                if not hasattr(self, 'retroarch') or self.retroarch is None:
                    self.retroarch = RetroArchInterface()
                # Trigger initial status update
                self.refresh_retroarch_info()
            except Exception as e:
                print(f"Error enabling markup and updating: {e}")
                import traceback
                traceback.print_exc()
            return False  # Don't repeat

        GLib.timeout_add(500, enable_markup_and_update)  # Increased delay to ensure everything is ready

        # Auto-Sync expander with built-in toggle switch
        self.autosync_expander = Adw.ExpanderRow()
        self.autosync_expander.set_title("Auto-Sync")
        self.autosync_expander.set_subtitle("Disabled")

        # Add status dot as prefix (15px size for better visibility)
        self.autosync_status_dot = self.create_status_dot('red', size=15)
        self.autosync_status_dot.set_margin_end(8)
        self.autosync_expander.add_prefix(self.autosync_status_dot)

        # Collection sync settings
        collection_sync_row = Adw.SpinRow()
        collection_sync_row.set_title("Collection Sync Interval")
        collection_sync_row.set_subtitle("Seconds between collection updates (minimum 30s)")
        adjustment = Gtk.Adjustment(value=30, lower=30, upper=600, step_increment=30)  # 30s to 10min
        collection_sync_row.set_adjustment(adjustment)
        collection_sync_row.set_value(int(self.settings.get('Collections', 'sync_interval', '30')))
        collection_sync_row.connect('notify::value', self.on_collection_sync_interval_changed)
        self.autosync_expander.add_row(collection_sync_row)

        # Add toggle switch as suffix to the expander
        self.autosync_enable_switch = Gtk.Switch()
        self.autosync_enable_switch.set_valign(Gtk.Align.CENTER)
        # Load saved state (default to True for new users)
        autosync_enabled = self.settings.get('AutoSync', 'enabled', 'true') == 'true'
        self.autosync_enable_switch.set_active(autosync_enabled)
        self.autosync_enable_switch.connect('notify::active', self.on_autosync_toggle)
        self.autosync_expander.add_suffix(self.autosync_enable_switch)

        # Auto-overwrite behavior setting
        self.auto_overwrite_row = Adw.ComboRow()
        self.auto_overwrite_row.set_title("Auto-Sync Behaviour")
        self.auto_overwrite_row.set_subtitle("How to handle conflicts between local and server saves")

        overwrite_options = Gtk.StringList()
        overwrite_options.append("Smart (prefer newer)")  # Default
        overwrite_options.append("Always prefer local")
        overwrite_options.append("Always download from server")
        overwrite_options.append("Ask each time")

        self.auto_overwrite_row.set_model(overwrite_options)
        self.auto_overwrite_row.set_selected(0)  # Default to "Smart"

        # Connect the setting change handler
        self.auto_overwrite_row.connect('notify::selected', self.on_overwrite_behavior_changed)

        # Load saved setting
        saved_behavior = int(self.settings.get('AutoSync', 'overwrite_behavior', '0'))
        self.auto_overwrite_row.set_selected(saved_behavior)

        self.autosync_expander.add_row(self.auto_overwrite_row)

        # Steam collections integration
        steam_enable_row = Adw.SwitchRow()
        steam_enable_row.set_title("Steam Integration")
        steam_enable_row.set_subtitle(
            "Create non-Steam game shortcuts for synced collections" if self.steam_manager.is_available()
            else "Steam userdata not found"
        )
        steam_enable_row.set_active(self.settings.get('Steam', 'enabled', 'false') == 'true')
        steam_enable_row.set_sensitive(self.steam_manager.is_available())
        steam_enable_row.connect('notify::active', self.on_steam_enable_toggle)
        self.autosync_expander.add_row(steam_enable_row)

        self.connection_sync_expander.add_row(self.retroarch_expander)
        self.connection_sync_expander.add_row(self.autosync_expander)

        # Create summary status dots container for collapsed Connection & Sync expander
        self.sync_summary_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.sync_summary_box.set_valign(Gtk.Align.CENTER)
        self.sync_summary_box.set_margin_end(6)

        self.summary_romm_dot = self.create_status_dot('grey', size=15)
        self.summary_retroarch_dot = self.create_status_dot('grey', size=15)
        self.summary_autosync_dot = self.create_status_dot('red', size=15)

        self.sync_summary_box.append(self.summary_romm_dot)
        self.sync_summary_box.append(self.summary_retroarch_dot)
        self.sync_summary_box.append(self.summary_autosync_dot)

        self.connection_sync_expander.add_suffix(self.sync_summary_box)

        def update_summary_visibility(*args):
            is_expanded = self.connection_sync_expander.get_expanded()
            self.sync_summary_box.set_visible(not is_expanded)
            if not is_expanded:
                self.update_sync_summary_dots()

        self.connection_sync_expander.connect('notify::expanded', update_summary_visibility)
        update_summary_visibility()

        for exp in (self.connection_sync_expander, self.connection_expander, self.retroarch_expander, self.autosync_expander):
            self.setup_expander_chevrons(exp)

        self.connection_wrapper.append(connection_group)

    def update_sync_summary_dots(self):
        """Update summary status dots color and tooltip on collapsed Connection & Sync bar"""
        if not hasattr(self, 'sync_summary_box'):
            return

        # RomM Status
        romm_color = getattr(self.connection_status_dot, '_current_color', 'grey')
        romm_sub = self.connection_expander.get_subtitle() or "Not connected"
        clean_romm_sub = re.sub(r'<[^>]*>', '', romm_sub).strip()
        self.update_status_dot(self.summary_romm_dot, romm_color)
        self.summary_romm_dot.set_tooltip_text(f"RomM Server: {clean_romm_sub}")

        # RetroArch Status
        ra_color = getattr(self.retroarch_status_dot, '_current_color', 'grey')
        ra_sub = self.retroarch_expander.get_subtitle() or "Installation info"
        clean_ra_sub = re.sub(r'<[^>]*>', '', ra_sub).strip()
        self.update_status_dot(self.summary_retroarch_dot, ra_color)
        self.summary_retroarch_dot.set_tooltip_text(f"RetroArch: {clean_ra_sub}")

        # Auto-Sync Status
        as_color = getattr(self.autosync_status_dot, '_current_color', 'red')
        as_sub = self.autosync_expander.get_subtitle() or "Disabled"
        clean_as_sub = re.sub(r'<[^>]*>', '', as_sub).strip()
        self.update_status_dot(self.summary_autosync_dot, as_color)
        self.summary_autosync_dot.set_tooltip_text(f"Auto-Sync: {clean_as_sub}")

    def setup_expander_chevrons(self, expander):
        """Replace missing adw-expander-arrow-symbolic with up/down chevron based on expansion state"""
        def _get_arrow_image(widget):
            if isinstance(widget, Gtk.Image):
                icon_name = widget.get_icon_name()
                if icon_name in ("adw-expander-arrow-symbolic", "pan-down-symbolic", "pan-up-symbolic", "pan-end-symbolic"):
                    return widget
            child = widget.get_first_child()
            while child:
                res = _get_arrow_image(child)
                if res: return res
                child = child.get_next_sibling()
            return None

        arrow = _get_arrow_image(expander)
        if arrow:
            arrow.remove_css_class("expander-row-arrow")
            def update_icon(*args):
                is_expanded = expander.get_expanded()
                arrow.set_from_icon_name("pan-down-symbolic" if is_expanded else "pan-up-symbolic")

            update_icon()
            expander.connect('notify::expanded', update_icon)

    def on_connection_sync_expanded_changed(self, expander, pspec):
        """Save Connection & Sync section expansion state"""
        is_expanded = expander.get_expanded()
        self.settings.set('UI', 'connection_sync_expanded', str(is_expanded).lower())

    def on_clear_cache(self, button):
        """Clear cached game data"""
        if hasattr(self, 'game_cache'):
            self.game_cache.clear_cache()
            self.log_message("🗑️ Game data cache cleared")
            self.log_message("💡 Reconnect to RomM to rebuild cache")
        else:
            self.log_message("❌ No cache to clear")

    def on_check_cache_status(self, button):
        """Check cache status and report"""
        if hasattr(self, 'game_cache'):
            cache = self.game_cache
            
            if cache.is_cache_valid():
                game_count = len(cache.cached_games)
                platform_count = len(cache.platform_mapping)
                filename_count = len(cache.filename_mapping)
                
                self.log_message(f"📂 Cache Status: VALID")
                self.log_message(f"   Games: {game_count}")
                self.log_message(f"   Platform mappings: {platform_count}")
                self.log_message(f"   Filename mappings: {filename_count}")
                
                # Show some examples
                if platform_count > 0:
                    sample_platforms = list(cache.platform_mapping.items())[:3]
                    self.log_message(f"   Platform examples:")
                    for dir_name, platform_name in sample_platforms:
                        self.log_message(f"     {dir_name} → {platform_name}")
            else:
                self.log_message(f"📭 Cache Status: EMPTY or EXPIRED")
                self.log_message(f"   Connect to RomM to populate cache")
        else:
            self.log_message(f"❌ Cache system not initialized")

    def initialize_device(self):
        """Initialize device registration with RomM on connection.

        Checks if device is already registered in config, if not registers a new one.
        Returns device_id on success, None on failure.
        """
        if not self.romm_client or not self.romm_client.authenticated:
            return None

        try:
            # Check if device is already registered
            existing_device_id = self.settings.get('Device', 'device_id', '')

            if existing_device_id:
                # Device already registered, just verify it still exists
                device_info = self.romm_client.get_device(existing_device_id)

                if device_info:
                    print(f"Device verified on server")
                    self.device_id = existing_device_id  # Cache in app instance
                    return existing_device_id
                else:
                    print(f"Device not found on server, registering new device")
                    # Fall through to register new

            # Register new device
            device_name = self.settings.get('Device', 'device_name', socket.gethostname())
            platform = self.settings.get('Device', 'device_platform', 'Linux')
            client = self.settings.get('Device', 'client', 'RomM-RetroArch-Sync')
            client_version = self.settings.get('Device', 'client_version', '1.7')

            device_id = self.romm_client.register_device(
                device_name=device_name,
                platform=platform,
                client=client,
                client_version=client_version
            )

            if device_id:
                # Store device ID in config
                self.settings.set('Device', 'device_id', device_id)
                self.device_id = device_id  # Cache in app instance
                return device_id
            else:
                return None

        except Exception as e:
            print(f"Error initializing device: {e}")
            return None

    def update_device_info_display(self, device_id):
        """Update the device information display in the preferences"""
        try:
            if not self.romm_client or not device_id:
                return

            device_info = self.romm_client.get_device(device_id)
            if device_info:
                # Update ID
                self.device_id_label.set_text(device_id)

                # Update Name
                device_name = device_info.get('name', '-')
                self.device_name_row.set_text(device_name)

                # Update Platform
                device_platform = device_info.get('platform', '-')
                self.device_platform_label.set_text(device_platform)

                # Update Client
                client = device_info.get('client', '-')
                client_version = device_info.get('client_version', '')
                if client_version:
                    client_text = f"{client} ({client_version})"
                else:
                    client_text = client
                self.device_client_label.set_text(client_text)
        except Exception as e:
            print(f"Error updating device info display: {e}")

    def on_delete_device_clicked(self, button):
        """Handle device deletion with confirmation dialog"""
        device_id = self.settings.get('Device', 'device_id', '')
        if not device_id:
            self.log_message("No device registered to delete")
            return

        dialog = Adw.AlertDialog.new(
            "Unregister Device?",
            f"This will remove device {device_id} from the server and clear the local device ID. "
            "A new device will be registered on next connect."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")

        def on_response(dialog, response):
            if response == "delete":
                def do_delete():
                    if self.romm_client and self.romm_client.delete_device(device_id):
                        self.settings.set('Device', 'device_id', '')
                        self.device_id = None
                        GLib.idle_add(lambda: self.device_id_label.set_text("Not registered"))
                        GLib.idle_add(lambda: self.device_name_row.set_text(socket.gethostname()))
                        GLib.idle_add(lambda: self.device_platform_label.set_text("-"))
                        GLib.idle_add(lambda: self.device_client_label.set_text("-"))
                        GLib.idle_add(lambda: self.log_message(f"Device {device_id} unregistered"))
                    else:
                        GLib.idle_add(lambda: self.log_message(f"Failed to delete device {device_id}"))

                import threading
                threading.Thread(target=do_delete, daemon=True).start()

        dialog.connect('response', on_response)
        dialog.present(self)

    def on_connection_toggle(self, switch_row, pspec):
            """Handle connection enable/disable toggle"""
            if switch_row.get_active():
                # User wants to connect
                url = self.url_row.get_text()
                username = self.username_row.get_text()
                password = self.password_row.get_text()
                
                if not url or not username or not password:
                    self.log_message("⚠️ Please fill in all connection details first")
                    switch_row.set_active(False)
                    return
                
                # Start connection
                self.start_connection(url, username, password)
                
            else:
                # User wants to disconnect
                self.disconnect_from_romm()

    def start_connection(self, url, username, password):
        """Simplified connection without additional testing"""
        remember = self.remember_switch.get_active()
        
        # Save settings
        self.settings.set('RomM', 'url', url)
        self.settings.set('RomM', 'remember_credentials', str(remember).lower())
        
        if remember:
            self.settings.set('RomM', 'username', username)
            self.settings.set('RomM', 'password', password)
        else:
            self.settings.set('RomM', 'username', '')
            self.settings.set('RomM', 'password', '')
        
        def connect():
            import time
            
            # START TIMING
            start_time = time.time()
            self.log_message(f"🔗 Starting RomM connection...")
            
            GLib.idle_add(lambda: self.update_connection_ui("connecting"))
            
            # STEP 1: Initialize client. Prefer a paired Client API Token (RomM's
            # recommended companion-app auth) over the stored password.
            init_start = time.time()
            client_token = self.settings.get('RomM', 'client_token', '')
            self.romm_client = RomMClient(url, username, password, client_token=client_token or None)

            # Initialize cover art manager for Steam grid images
            self.romm_client.cover_manager = CoverArtManager(self.settings, self.romm_client)

            # Update steam manager with cover manager
            self.steam_manager.cover_manager = self.romm_client.cover_manager

            init_time = time.time() - init_start
            self.log_message(f"⚡ Client initialized in {init_time:.2f}s")
            
            def update_ui():
                # STEP 2: Check authentication result
                auth_time = time.time() - start_time
                
                if self.romm_client.authenticated:
                    self.log_message(f"✅ Authentication successful in {auth_time:.2f}s")

                    # Device Registration: Initialize or retrieve device ID
                    try:
                        device_reg_start = time.time()
                        device_id = self.initialize_device()
                        if device_id:
                            device_reg_time = time.time() - device_reg_start
                            self.log_message(f"📱 Device registered: {device_id} ({device_reg_time:.2f}s)")
                            # Update the device info display in preferences
                            GLib.idle_add(lambda: self.update_device_info_display(device_id))
                        else:
                            self.log_message(f"⚠️ Device registration skipped or failed")
                    except Exception as e:
                        self.log_message(f"⚠️ Device initialization error: {e}")

                    # CRITICAL: Fetch platform mapping immediately after authentication
                    # This ensures platform names are available for all operations
                    try:
                        platforms_start = time.time()
                        platforms = self.romm_client.get_platforms()
                        if platforms:
                            self.game_cache.build_platform_mapping_from_api(platforms)
                            platforms_time = time.time() - platforms_start
                            self.log_message(f"📋 Loaded {len(platforms)} platform names in {platforms_time:.2f}s")
                    except Exception as e:
                        self.log_message(f"⚠️ Could not fetch platform names: {e}")

                    # Move this right after authentication success, before other operations
                    def preload_collections_smart():
                        if hasattr(self, 'library_section'):
                            # Don't force refresh on startup - let the cache be used if valid
                            # Force refresh only happens if freshness check detects changes
                            self.library_section.cache_collections_data(force_refresh=False)

                    # Call immediately, not as thread
                    GLib.timeout_add(100, lambda: (threading.Thread(target=preload_collections_smart, daemon=True).start(), False)[1])

                    # STEP 3: Test basic API access
                    api_test_start = time.time()
                    try:
                        test_count = self.romm_client.get_games_count_only()
                        api_test_time = time.time() - api_test_start
                        
                        if test_count is not None:
                            self.log_message(f"📊 API test successful in {api_test_time:.2f}s ({test_count:,} games)")
                        else:
                            self.log_message(f"⚠️ API test completed in {api_test_time:.2f}s (count unavailable)")
                            
                    except Exception as e:
                        api_test_time = time.time() - api_test_start
                        self.log_message(f"❌ API test failed in {api_test_time:.2f}s: {str(e)[:100]}")
                    
                    if hasattr(self, 'auto_sync'):
                        self.auto_sync.romm_client = self.romm_client
                    
                    cached_count = len(self.game_cache.cached_games) if self.game_cache.is_cache_valid() else 0
                    if cached_count > 0:
                        # Show cached games immediately first
                        download_dir = Path(self.rom_dir_row.get_text())
                        all_cached_games = []

                        for game in list(self.game_cache.cached_games):
                            platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')

                            # Use cached local_path if available, otherwise construct from file_name
                            cached_path = game.get('local_path')
                            if cached_path:
                                local_path = Path(cached_path)
                            else:
                                file_name = game.get('file_name', '')
                                if not file_name:
                                    continue
                                platform_dir = download_dir / platform_slug
                                local_path = platform_dir / file_name

                            is_downloaded = self.is_path_validly_downloaded(local_path)

                            game_copy = game.copy()
                            # Platform name already resolved by cache.load_games_cache() at line 172
                            # Just update download status
                            game_copy['is_downloaded'] = is_downloaded
                            game_copy['local_path'] = str(local_path) if is_downloaded else None
                            game_copy['local_size'] = self.get_actual_file_size(local_path) if is_downloaded else 0

                            # Update disc status for multi-disc games
                            if game_copy.get('is_multi_disc') and game_copy.get('discs'):
                                for disc in game_copy['discs']:
                                    disc['is_downloaded'] = is_downloaded

                            all_cached_games.append(game_copy)

                        # Update UI immediately with cached games
                        def update_games_ui():
                            self.available_games = all_cached_games
                            if hasattr(self, 'library_section'):
                                self.library_section.update_games_library(all_cached_games)
                        
                        GLib.idle_add(update_games_ui)
                        
                        # Define freshness check function first
                        def check_cache_freshness():
                            try:
                                self.log_message(f"🔍 Checking cache freshness...")
                                server_count = self.romm_client.get_games_count_only()
                                self.log_message(f"🔍 Server count: {server_count}")

                                if server_count is not None:
                                    # Compare using original ungrouped count (apples to apples)
                                    cache_original_total = getattr(self.game_cache, 'original_total', cached_count)
                                    self.log_message(f"🔍 Cache original_total: {cache_original_total}")
                                    count_diff = abs(server_count - cache_original_total)
                                    self.log_message(f"🔍 Count difference: {count_diff}")
                                    # Check auto-refresh setting before refreshing
                                    auto_refresh_enabled = self.settings.get('RomM', 'auto_refresh') == 'true'
                                    if auto_refresh_enabled:
                                        def auto_refresh():
                                            if count_diff > 0:
                                                self.update_connection_ui_with_message(f"⟳ Checking updates ({count_diff} count diff)...")
                                                self.log_message(f"📊 Auto-refreshing: {count_diff} games difference detected (differential sync)")
                                            else:
                                                self.update_connection_ui_with_message(f"⟳ Checking for updates from server...")
                                            self.refresh_games_list(force_full_refresh=False)
                                            # Invalidate collections cache so collection view gets fresh data
                                            if hasattr(self, 'library_section'):
                                                self.library_section.collections_cache_time = 0
                                                if self.library_section.current_view_mode == 'collection':
                                                    self.library_section.load_collections_view()
                                        GLib.idle_add(auto_refresh)
                                    else:
                                        if count_diff > 0:
                                            def show_outdated():
                                                self.update_connection_ui_with_message(f"🟡 Connected - {cached_count:,} games cached • ⚠️ {count_diff} games difference detected - Consider refreshing the library")
                                                self.log_message(f"📊 Server has {count_diff} different games - consider refreshing")
                                            GLib.idle_add(show_outdated)
                                        else:
                                            def update_status():
                                                self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                                                self.log_message(f"📊 Cache is up to date with server")
                                            GLib.idle_add(update_status)
                                else:
                                    # Server check failed, show cache info
                                    def update_status():
                                        self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                                        self.log_message(f"⚠️ Could not check server, using cached data")
                                    GLib.idle_add(update_status)
                            except Exception as e:
                                def update_status():
                                    self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                                    self.log_message(f"⚠️ Freshness check failed: {e}")
                                GLib.idle_add(update_status)
                        
                        # Check if auto-refresh is enabled
                        auto_refresh_enabled = self.settings.get('RomM', 'auto_refresh') == 'true'
                        self.log_message(f"🔍 Auto-refresh enabled: {auto_refresh_enabled}")

                        if auto_refresh_enabled:
                            self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached • checking for updates...")
                            threading.Thread(target=check_cache_freshness, daemon=True).start()
                        else:
                            # Auto-refresh disabled, show cache info
                            self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                            self.log_message(f"📂 Showing {cached_count:,} cached games (auto-refresh disabled)")
                        
                    else:
                        # Always fetch on first startup (no cached games)
                        self.update_connection_ui("loading")
                        self.log_message("🔄 Connected! Loading games list for first time...")
                        self.refresh_games_list(force_full_refresh=True)

                    # Restore collection auto-sync if it was enabled
                    if hasattr(self, 'library_section'):
                        self.library_section.restore_collection_auto_sync_on_connect()

                    # Start auto-sync if enabled (respects user's saved preference)
                    if hasattr(self, 'autosync_enable_switch') and self.autosync_enable_switch.get_active():
                        # Start auto-sync directly without triggering UI update
                        self.auto_sync.romm_client = self.romm_client
                        self.auto_sync.upload_enabled = True
                        self.auto_sync.download_enabled = True
                        self.auto_sync.upload_delay = 3
                        self.auto_sync.start_auto_sync()
                        self.autosync_expander.set_subtitle("Active - monitoring for changes")
                        self.update_status_dot(self.autosync_status_dot, 'green')
                        self.log_message("🔄 Auto-sync enabled")

                    # Library is fetched on connect and on manual refresh (the
                    # reference client's on-demand model) — no background polling.

                    total_time = time.time() - start_time
                    self.log_message(f"🎉 Total connection time: {total_time:.2f}s")
                        
                else:
                    # Authentication failed logic...
                    auth_time = time.time() - start_time
                    self.log_message(f"❌ Authentication failed after {auth_time:.2f}s")
                    self.log_message(f"🔍 Debug: Check server accessibility and credentials")
                    self.update_connection_ui("failed")
                    self.connection_enable_switch.set_active(False)

            GLib.idle_add(update_ui)

        threading.Thread(target=connect, daemon=True).start() 

    def disconnect_from_romm(self):
        """Disconnect and switch to local-only view"""
        self.romm_client = None
        
        # Clear selections when disconnecting  
        if hasattr(self, 'library_section'):
            self.library_section.clear_checkbox_selections_smooth()
            self.library_section.stop_collection_auto_sync()
        
        if hasattr(self, 'auto_sync'):
            self.auto_sync.stop_auto_sync()
            # Turn off auto-sync switch when disconnected (without saving to settings)
            self.autosync_enable_switch.handler_block_by_func(self.on_autosync_toggle)
            self.autosync_enable_switch.set_active(False)
            self.autosync_enable_switch.handler_unblock_by_func(self.on_autosync_toggle)
            self.autosync_expander.set_subtitle("Disabled - not connected to RomM")
            self.update_status_dot(self.autosync_status_dot, 'red')

        self.update_connection_ui("disconnected")
        self.log_message("Disconnected from RomM")

        # Switch to local-only view immediately
        self.handle_offline_mode()

    def update_connection_ui(self, state):
        """Update connection UI based on state"""
        if state == "connecting":
            self.connection_expander.set_subtitle("Connecting...")
            self.update_status_dot(self.connection_status_dot, 'yellow')

        elif state == "loading":
            self.connection_expander.set_subtitle("Loading games...")
            self.update_status_dot(self.connection_status_dot, 'yellow')

        elif state == "connected":
            # Add game count when connected
            game_count = len(getattr(self, 'available_games', []))
            if game_count > 0:
                subtitle = f"Connected - {game_count:,} Games"
            else:
                subtitle = "Connected"
            self.connection_expander.set_subtitle(subtitle)
            self.update_status_dot(self.connection_status_dot, 'green')

        elif state == "failed":
            self.connection_expander.set_subtitle("Connection failed")
            self.update_status_dot(self.connection_status_dot, 'red')

        elif state == "disconnected":
            self.connection_expander.set_subtitle("Disconnected")
            self.update_status_dot(self.connection_status_dot, 'red')

    def update_connection_ui_with_message(self, message):
        """Update connection UI with custom message"""
        # Remove emoji and update dot color based on message content
        clean_message = message.replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "")
        self.connection_expander.set_subtitle(clean_message)

        # Update dot color based on original message
        if "🟢" in message:
            self.update_status_dot(self.connection_status_dot, 'green')
        elif "🟡" in message:
            self.update_status_dot(self.connection_status_dot, 'yellow')
        elif "🔴" in message:
            self.update_status_dot(self.connection_status_dot, 'red')     


    def create_library_section(self):
        """Create the enhanced library section with tree view (moved from quick actions)"""
        # Create enhanced library section with tree view
        self.library_section = EnhancedLibrarySection(self)
        # Library will be added to main_box in setup_ui, not to preferences_page

    def on_autosync_toggle(self, switch_row, pspec):
        """Handle auto-sync enable/disable"""
        if switch_row.get_active():
            if self.romm_client and self.romm_client.authenticated:
                # Update auto-sync settings
                self.auto_sync.romm_client = self.romm_client
                self.auto_sync.upload_enabled = self.autoupload_row.get_active()
                self.auto_sync.download_enabled = self.autodownload_row.get_active()
                self.auto_sync.upload_delay = int(self.sync_delay_row.get_value())
                
                # Start auto-sync
                self.auto_sync.start_auto_sync()
                self.autosync_expander.set_subtitle("Active - monitoring for changes")
                self.update_status_dot(self.autosync_status_dot, 'green')
                
                self.log_message("🔄 Auto-sync enabled")
            else:
                self.log_message("⚠️ Please connect to RomM before enabling auto-sync")
                self.autosync_expander.set_subtitle("Disabled - not connected to RomM")
                self.update_status_dot(self.autosync_status_dot, 'red')
                switch_row.set_active(False)
        else:
            self.auto_sync.stop_auto_sync()
            self.autosync_expander.set_subtitle("Disabled")
            self.update_status_dot(self.autosync_status_dot, 'red')
            self.log_message("⏹️ Auto-sync disabled")

    def get_selected_game(self):
        """Get currently selected game from tree view"""
        if hasattr(self, 'library_section'):
            return self.library_section.selected_game
        return None


    def on_autosync_toggle(self, switch_row, pspec):
        """Handle auto-sync enable/disable"""
        enabled = switch_row.get_active()

        # Save state to settings
        self.settings.set('AutoSync', 'enabled', str(enabled).lower())

        if enabled:
            if self.romm_client and self.romm_client.authenticated:
                # Update auto-sync settings with defaults
                self.auto_sync.romm_client = self.romm_client
                self.auto_sync.upload_enabled = True
                self.auto_sync.download_enabled = True
                self.auto_sync.upload_delay = 3

                # Start auto-sync
                self.auto_sync.start_auto_sync()
                self.autosync_expander.set_subtitle("Active - monitoring for changes")
                self.update_status_dot(self.autosync_status_dot, 'green')

                self.log_message("🔄 Auto-sync enabled")
            else:
                self.log_message("⚠️ Please connect to RomM before enabling auto-sync")
                self.autosync_expander.set_subtitle("Disabled - not connected to RomM")
                self.update_status_dot(self.autosync_status_dot, 'red')
                switch_row.set_active(False)
        else:
            self.auto_sync.stop_auto_sync()
            self.autosync_expander.set_subtitle("Disabled")
            self.update_status_dot(self.autosync_status_dot, 'red')
            self.log_message("⏹️ Auto-sync disabled")

    def on_steam_enable_toggle(self, switch_row, pspec):
        """Handle Steam integration enable/disable"""
        enabled = switch_row.get_active()
        self.settings.set('Steam', 'enabled', str(enabled).lower())
        if enabled:
            # When enabling Steam integration, add Steam shortcuts for all currently synced collections
            if self.steam_manager and self.steam_manager.is_available():
                if hasattr(self, 'library_section'):
                    synced_collections = self.library_section.actively_syncing_collections.copy()
                    if synced_collections:
                        def add_steam_shortcuts():
                            total_added = 0
                            for collection_name in synced_collections:
                                try:
                                    # Enable Steam sync for this collection
                                    steam_collections = self.steam_manager.get_steam_sync_collections()
                                    steam_collections.add(collection_name)
                                    self.steam_manager.set_steam_sync_collections(steam_collections)

                                    # Find collection and add shortcuts
                                    all_collections = self.romm_client.get_collections()
                                    collection_id = None
                                    for col in all_collections:
                                        if col.get('name') == collection_name:
                                            collection_id = col.get('id')
                                            break

                                    if collection_id:
                                        roms = self.romm_client.get_collection_roms(collection_id)
                                        download_dir = self.settings.get('Download', 'rom_directory')
                                        added, msg = self.steam_manager.add_collection_shortcuts(
                                            collection_name, roms, download_dir)
                                        total_added += added
                                except Exception as e:
                                    GLib.idle_add(self.log_message, f"Error adding shortcuts for {collection_name}: {e}")

                            GLib.idle_add(self.log_message,
                                         f"Steam integration enabled — added {total_added} shortcuts from {len(synced_collections)} synced collections")

                        threading.Thread(target=add_steam_shortcuts, daemon=True).start()
                    else:
                        self.log_message("Steam integration enabled — no collections currently synced")
                else:
                    self.log_message("Steam integration enabled — toggle collection sync to add shortcuts")
            else:
                self.log_message("Steam integration enabled — toggle collection sync to add shortcuts")
        else:
            # When disabling Steam integration, remove all Steam shortcuts for synced collections
            if self.steam_manager and self.steam_manager.is_available():
                steam_collections = self.steam_manager.get_steam_sync_collections().copy()
                if steam_collections:
                    def cleanup_steam_shortcuts():
                        total_removed = 0
                        for collection_name in steam_collections:
                            try:
                                removed, msg = self.steam_manager.remove_collection_shortcuts(collection_name)
                                total_removed += removed
                            except Exception as e:
                                GLib.idle_add(self.log_message, f"Error removing shortcuts for {collection_name}: {e}")

                        # Clear the Steam sync collections list
                        self.steam_manager.set_steam_sync_collections(set())

                        GLib.idle_add(self.log_message,
                                     f"Steam integration disabled — removed {total_removed} shortcuts from {len(steam_collections)} collections")

                    threading.Thread(target=cleanup_steam_shortcuts, daemon=True).start()
                else:
                    self.log_message("Steam integration disabled")
            else:
                self.log_message("Steam integration disabled")

    def on_collection_sync_interval_changed(self, spin_row, pspec):
        """Save collection sync interval in seconds"""
        interval = int(spin_row.get_value())
        self.settings.set('Collections', 'sync_interval', str(interval))
        if hasattr(self, 'library_section'):
            self.library_section.collection_sync_interval = interval

    def on_show_logs_dialog(self, button):
        """Show logs and advanced tools dialog"""
        dialog = Adw.PreferencesDialog()
        dialog.set_title("Logs & Advanced Tools")
        dialog.set_content_width(600)
        dialog.set_content_height(500)
        
        # Activity Log
        log_group = Adw.PreferencesGroup()
        log_group.set_title("Activity Log")
        
        # Create dialog log view that SHARES the same buffer
        dialog_log_view = Gtk.TextView()
        dialog_log_view.set_editable(False)
        dialog_log_view.set_cursor_visible(False)
        dialog_log_view.set_buffer(self.log_view.get_buffer())  # SHARE the buffer
        
        scrolled_log = Gtk.ScrolledWindow()
        scrolled_log.set_child(dialog_log_view)
        scrolled_log.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_log.set_size_request(-1, 200)
        
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        log_box.append(scrolled_log)
        
        log_row = Adw.ActionRow()
        log_row.set_child(log_box)
        log_group.add(log_row)

        # Debug mode toggle
        debug_mode_row = Adw.SwitchRow()
        debug_mode_row.set_title("Debug Mode")
        debug_mode_row.set_subtitle("Enable detailed logging and write debug.log file")
        debug_mode_row.set_active(self.settings.get('System', 'debug_mode') == 'true')
        debug_mode_row.connect('notify::active', lambda row, _: self.on_debug_mode_changed(row, _))
        log_group.add(debug_mode_row)

        # Configuration group (Library Directory & BIOS)
        config_group = Adw.PreferencesGroup()
        config_group.set_title("Configuration")

        # Library Directory settings
        library_dir_expander = Adw.ExpanderRow()
        library_dir_expander.set_title("Library Directory")
        library_dir_expander.set_subtitle(self.settings.get('Download', 'rom_directory'))

        # Directory chooser button
        dir_button_container = Gtk.Box()
        dir_button_container.set_size_request(-1, 18)
        dir_button_container.set_valign(Gtk.Align.CENTER)
        choose_dir_button = Gtk.Button(label="Browse...")
        choose_dir_button.connect('clicked', self.on_choose_directory)
        choose_dir_button.set_size_request(100, -1)
        choose_dir_button.set_valign(Gtk.Align.CENTER)
        dir_button_container.append(choose_dir_button)
        library_dir_expander.add_suffix(dir_button_container)

        # Directory Path entry
        library_dir_path_row = Adw.EntryRow()
        library_dir_path_row.set_title("Directory Path")
        library_dir_path_row.set_text(self.settings.get('Download', 'rom_directory'))
        # Store reference for on_choose_directory to update
        self._dialog_library_dir_row = library_dir_path_row
        self._dialog_library_dir_expander = library_dir_expander
        library_dir_expander.add_row(library_dir_path_row)

        # Max concurrent downloads
        max_downloads_row = Adw.SpinRow()
        max_downloads_row.set_title("Max Concurrent Downloads")
        max_downloads_row.set_subtitle("Maximum simultaneous ROM downloads")
        downloads_adjustment = Gtk.Adjustment(value=3, lower=1, upper=10, step_increment=1)
        max_downloads_row.set_adjustment(downloads_adjustment)
        max_downloads_row.set_value(int(self.settings.get('Download', 'max_concurrent', '3')))
        max_downloads_row.connect('notify::value', self.on_max_downloads_changed)
        library_dir_expander.add_row(max_downloads_row)

        # Open Download Folder
        browse_row = Adw.ActionRow()
        browse_row.set_title("Open Download Folder")
        browse_row.set_subtitle("View downloaded files in file manager")
        browse_button_container = Gtk.Box()
        browse_button_container.set_size_request(-1, 18)
        browse_button_container.set_valign(Gtk.Align.CENTER)
        browse_button = Gtk.Button(label="Open")
        browse_button.connect('clicked', self.on_browse_downloads)
        browse_button.set_size_request(80, -1)
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button_container.append(browse_button)
        browse_row.add_suffix(browse_button_container)
        library_dir_expander.add_row(browse_row)

        config_group.add(library_dir_expander)

        # BIOS Files settings
        bios_expander = Adw.ExpanderRow()
        bios_expander.set_title("System BIOS Files")
        bios_expander.set_subtitle("Manage emulator BIOS/firmware files")

        # Download All button
        bios_download_container = Gtk.Box()
        bios_download_container.set_size_request(-1, 18)
        bios_download_container.set_valign(Gtk.Align.CENTER)
        download_all_btn = Gtk.Button(label="Download All")
        download_all_btn.connect('clicked', self.on_download_all_bios)
        download_all_btn.set_size_request(100, -1)
        download_all_btn.set_valign(Gtk.Align.CENTER)
        bios_download_container.append(download_all_btn)
        bios_expander.add_suffix(bios_download_container)

        # BIOS path override
        bios_override_row = Adw.EntryRow()
        bios_override_row.set_title("Custom BIOS Directory (Override auto-detection)")
        bios_override_row.set_text(self.settings.get('BIOS', 'custom_path', ''))
        bios_override_row.connect('entry-activated', self.on_bios_override_changed)
        bios_expander.add_row(bios_override_row)

        # BIOS directory info
        bios_dir_row = Adw.ActionRow()
        bios_dir_row.set_title("BIOS Directory")
        if self.retroarch.bios_manager and self.retroarch.bios_manager.system_dir:
            bios_dir_row.set_subtitle(str(self.retroarch.bios_manager.system_dir))
        else:
            bios_dir_row.set_subtitle("Not found")
        bios_expander.add_row(bios_dir_row)

        config_group.add(bios_expander)

        # Platform Core Overrides settings
        cores_expander = Adw.ExpanderRow()
        cores_expander.set_title("Platform Core Overrides")
        cores_expander.set_subtitle("Customize RetroArch core selection per platform")

        # Gather platform names from available games + common platforms
        platforms_to_show = {
            "Sega Saturn", "Sony PlayStation", "Sony PlayStation 2",
            "Nintendo 64", "Super Nintendo Entertainment System",
            "Nintendo Entertainment System", "Game Boy Advance",
            "Sega Genesis", "Nintendo GameCube", "Nintendo DS", "Sega Dreamcast"
        }
        if hasattr(self, 'available_games') and self.available_games:
            for g in self.available_games:
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
                        self.log_message(f"Cleared core override for {platform} (reverted to Auto: {autocore})")
                    else:
                        self.retroarch.set_core_override(platform, selected)
                        action_row.set_subtitle(f"Override: {selected}")
                        self.log_message(f"Set core override for {platform}: {selected}")
                return on_combo_changed

            combo.connect("changed", make_on_change(p_name, row))
            row.add_suffix(combo)
            cores_expander.add_row(row)

        config_group.add(cores_expander)


        # Advanced Tools
        advanced_group = Adw.PreferencesGroup()
        advanced_group.set_title("Advanced Tools")

        # Inspect Files
        inspect_row = Adw.ActionRow()
        inspect_row.set_title("Inspect Files")
        inspect_row.set_subtitle("Check downloaded file integrity")
        inspect_btn = Gtk.Button(label="Inspect")
        inspect_btn.set_valign(Gtk.Align.CENTER)  # CHANGE: Use valign instead
        inspect_btn.set_size_request(80, -1)      # CHANGE: Only set width
        inspect_btn.connect('clicked', self.on_inspect_downloads)
        inspect_row.add_suffix(inspect_btn)
        advanced_group.add(inspect_row)

        # Cache Management
        cache_row = Adw.ActionRow()
        cache_row.set_title("Game Data Cache")
        cache_row.set_subtitle("Local storage management")

        cache_box = Gtk.Box(spacing=6)
        cache_box.set_valign(Gtk.Align.CENTER)    # CHANGE: Align the box
        check_btn = Gtk.Button(label="Check")
        check_btn.set_size_request(70, -1)        # CHANGE: Only set width
        check_btn.connect('clicked', self.on_check_cache_status)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.set_size_request(70, -1)        # CHANGE: Only set width
        clear_btn.add_css_class('destructive-action')
        clear_btn.connect('clicked', self.on_clear_cache)
        cache_box.append(check_btn)
        cache_box.append(clear_btn)
        # Full Library Resync Row
        resync_row = Adw.ActionRow()
        resync_row.set_title("Full Library Resync")
        resync_row.set_subtitle("Re-download all ROM metadata from server from scratch")
        resync_btn = Gtk.Button(label="Full Resync")
        resync_btn.set_valign(Gtk.Align.CENTER)
        resync_btn.set_size_request(100, -1)
        resync_btn.connect('clicked', lambda b: self.refresh_games_list(force_full_refresh=True))
        resync_row.add_suffix(resync_btn)
        advanced_group.add(resync_row)

        # Create page and add groups
        page = Adw.PreferencesPage()
        page.add(log_group)
        page.add(config_group)
        page.add(advanced_group)
        dialog.add(page)

        dialog.present(self)

    def log_message(self, message):
        """Add message to log view with buffer limit"""

        # Always write all log messages (including TRAY DEBUG) to debug.log file
        try:
            log_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'debug.log'
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, 'a', encoding='utf-8') as f:
                import datetime
                timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                f.write(f"[{timestamp}] {message}\n")
        except Exception:
            pass

        # Check if debug mode is enabled for UI buffer
        debug_mode = self.settings.get('System', 'debug_mode') == 'true'
        if message.startswith('[DEBUG]') and not debug_mode:
            return
        def update_ui():
            try:
                buffer = self.log_view.get_buffer()
                
                # Limit buffer to last 1000 lines
                line_count = buffer.get_line_count()
                if line_count > 1000:
                    start = buffer.get_start_iter()
                    # Delete first 200 lines to avoid frequent trimming
                    line_iter = buffer.get_iter_at_line(200)
                    buffer.delete(start, line_iter)
                
                end_iter = buffer.get_end_iter()
                buffer.insert(end_iter, f"{message}\n")
                
                end_mark = buffer.get_insert()
                buffer.place_cursor(buffer.get_end_iter())
                self.log_view.scroll_to_mark(end_mark, 0.0, False, 0.0, 0.0)
            except Exception:
                pass
        
        GLib.idle_add(update_ui)

    def send_desktop_notification(self, title, body):
        """Send a desktop notification (GNOME/KDE/etc)"""
        import subprocess
        import os

        try:
            # Method 1: Direct D-Bus call (most reliable for GNOME)
            try:
                # Use gdbus to send notification directly to the notification daemon
                # This bypasses GTK/Gio and talks directly to org.freedesktop.Notifications
                result = subprocess.run([
                    'gdbus', 'call', '--session',
                    '--dest=org.freedesktop.Notifications',
                    '--object-path=/org/freedesktop/Notifications',
                    '--method=org.freedesktop.Notifications.Notify',
                    'RomM Sync',  # app_name
                    '0',  # replaces_id
                    'folder-download',  # app_icon
                    title,  # summary
                    body,  # body
                    '[]',  # actions
                    '{}',  # hints
                    '5000'  # timeout (5 seconds)
                ], timeout=5, capture_output=True, text=True)

                if result.returncode == 0:
                    print(f"✅ Desktop notification sent (gdbus): {title}")
                    return True
                else:
                    print(f"⚠️ gdbus notification failed: {result.stderr}")

            except FileNotFoundError:
                print("⚠️ gdbus not found, trying notify-send...")
            except Exception as e:
                print(f"⚠️ gdbus error: {e}")

            # Method 2: notify-send (standard tool)
            try:
                result = subprocess.run(
                    ['notify-send',
                     '--app-name=RomM Sync',
                     '--icon=folder-download',
                     '--urgency=normal',
                     title,
                     body],
                    timeout=5,
                    capture_output=True,
                    text=True
                )

                if result.returncode == 0:
                    print(f"✅ Desktop notification sent (notify-send): {title}")
                    return True
                else:
                    print(f"⚠️ notify-send failed: {result.stderr}")

            except FileNotFoundError:
                print("❌ notify-send not found, trying Gio.Notification...")
            except Exception as e:
                print(f"⚠️ notify-send error: {e}")

            # Method 3: Fallback to Gio.Notification
            app = self.get_application()
            if app and hasattr(app, 'send_notification'):
                try:
                    notification = Gio.Notification.new(title)
                    notification.set_body(body)
                    icon = Gio.ThemedIcon.new("folder-download-symbolic")
                    notification.set_icon(icon)
                    notification_id = f"collection-sync-{hash(title) % 10000}"
                    app.send_notification(notification_id, notification)
                    print(f"✅ Sent Gio notification: {title}")
                    return True
                except Exception as e:
                    print(f"❌ Gio.Notification failed: {e}")

            print(f"❌ All notification methods failed for: {title}")
            return False

        except Exception as e:
            print(f"❌ Desktop notification error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def on_choose_directory(self, button):
        """Choose download directory"""
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose ROM Download Directory")

        def on_response(source, result):
            try:
                file = dialog.select_folder_finish(result)
                if file:
                    path = file.get_path()
                    # Update settings
                    self.settings.set('Download', 'rom_directory', path)
                    # Update dialog UI if it exists
                    if hasattr(self, '_dialog_library_dir_row'):
                        self._dialog_library_dir_row.set_text(path)
                    if hasattr(self, '_dialog_library_dir_expander'):
                        self._dialog_library_dir_expander.set_subtitle(path)
                    self.log_message(f"Download directory set to: {path}")
            except Exception as e:
                # User cancelled or error occurred
                pass

        dialog.select_folder(self, None, on_response)

    def on_max_downloads_changed(self, spin_row, pspec):
        """Save max concurrent downloads setting"""
        self.settings.set('Download', 'max_concurrent', str(int(spin_row.get_value())))

    def update_download_progress(self, progress_info, rom_id=None):
        """Update progress for specific game in tree view only"""
        if not rom_id:
            rom_id = getattr(self, '_current_download_rom_id', None)
        if not rom_id:
            return
        
        # Only update tree view progress data
        current_time = time.time()
        last_update = self._last_progress_update.get(rom_id, 0)
        
        if rom_id in self.download_progress:
            # ADD THIS: Validate progress only increases
            current_progress = progress_info.get('progress', 0)
            last_progress = self.download_progress[rom_id].get('progress', 0)
            
            # Skip if progress goes backwards (unless it's a restart from 0)
            if current_progress < last_progress and current_progress > 0.01:
                return
            
            self.download_progress[rom_id].update({
                'progress': progress_info['progress'],
                'speed': progress_info['speed'],
                'downloaded': progress_info['downloaded'],
                'total': progress_info['total'],
                'downloading': True
            })
        
        # Throttled tree view updates only
        if (current_time - last_update >= self._progress_update_interval or
            progress_info.get('progress', 0) >= 1.0):
            self._last_progress_update[rom_id] = current_time
            
            if hasattr(self, 'library_section'):
                GLib.idle_add(lambda: self._safe_progress_update(rom_id))

    def _safe_progress_update(self, rom_id):
        """Safely update progress in main thread"""
        try:
            if (hasattr(self, 'library_section') and 
                rom_id in self.download_progress):
                self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
        except Exception as e:
            print(f"Safe progress update error: {e}")
        return False  # Don't repeat
            
    def refresh_retroarch_info(self):
        """Update RetroArch information in UI with installation type"""
        def update_info():
            try:
                # Check RetroArch executable
                if hasattr(self, 'retroarch_info_row'):
                    if self.retroarch.retroarch_executable:
                        # Determine installation type
                        if 'retrodeck' in self.retroarch.retroarch_executable.lower():
                            install_type = "RetroDECK"
                        elif 'flatpak' in self.retroarch.retroarch_executable:
                            install_type = "Flatpak"
                        elif 'steam' in self.retroarch.retroarch_executable.lower():
                            install_type = "Steam"
                        elif 'snap' in self.retroarch.retroarch_executable:
                            install_type = "Snap"
                        elif '.AppImage' in self.retroarch.retroarch_executable:
                            install_type = "AppImage"
                        else:
                            install_type = "Native"
                        
                        self.retroarch_info_row.set_subtitle(f"Found: {install_type} - {self.retroarch.retroarch_executable}")
                        self.retroarch_expander.set_subtitle(f"{install_type} RetroArch detected")
                        self.update_status_dot(self.retroarch_status_dot, 'green')
                    else:
                        self.retroarch_info_row.set_subtitle("Not found")
                        self.retroarch_expander.set_subtitle("RetroArch not found")
                        self.update_status_dot(self.retroarch_status_dot, 'red')
            except Exception as e:
                print(f"Error updating RetroArch info: {e}")
                if hasattr(self, 'retroarch_info_row'):
                    self.retroarch_info_row.set_subtitle("Error checking installation")
            
            try:
                # Check cores directory
                if hasattr(self, 'cores_info_row'):
                    if self.retroarch.cores_dir:
                        self.cores_info_row.set_subtitle(f"Found: {self.retroarch.cores_dir}")
                        
                        # Count cores
                        cores = self.retroarch.get_available_cores()
                        core_count = len(cores)
                        
                        if hasattr(self, 'core_count_row'):
                            self.core_count_row.set_subtitle(f"{core_count} cores available")
                    else:
                        self.cores_info_row.set_subtitle("Cores directory not found")
                        if hasattr(self, 'core_count_row'):
                            self.core_count_row.set_subtitle("0 cores available")
                            
                # Auto-enable network commands and save state thumbnails (always on)
                if hasattr(self, 'retroarch_connection_row'):
                    network_ok, network_status = self.retroarch.check_network_commands_config()
                    thumbnail_ok, thumbnail_status = self.retroarch.check_savestate_thumbnail_config()

                    # Auto-enable if disabled (Option B: always-on approach)
                    if not network_ok:
                        self.retroarch.enable_retroarch_setting('network_commands')
                        network_ok = True
                        network_status = "Network commands enabled (port 55355)"

                    if not thumbnail_ok:
                        self.retroarch.enable_retroarch_setting('savestate_thumbnails')
                        thumbnail_ok = True
                        thumbnail_status = "Save state thumbnails enabled"

                    # Build status message with green checkmarks (always enabled)
                    # Green: #4ade80 (same as game library)
                    status_parts = []
                    status_parts.append(f'<span foreground="#4ade80">✓</span> {network_status}')
                    status_parts.append(f'<span foreground="#4ade80">✓</span> {thumbnail_status}')

                    combined_status = " | ".join(status_parts)
                    self.retroarch_connection_row.set_subtitle(combined_status)
                        
            except Exception as e:
                print(f"Error checking RetroArch info: {e}")
                if hasattr(self, 'cores_info_row'):
                    self.cores_info_row.set_subtitle("Error checking cores")
                if hasattr(self, 'retroarch_connection_row'):
                    self.retroarch_connection_row.set_subtitle("Error checking configuration - use buttons above to enable")
        
        # Ensure UI update happens in main thread
        from gi.repository import GLib
        GLib.idle_add(update_info)
            
    def on_refresh_retroarch_info(self, button):
        """Refresh RetroArch information"""
        self.log_message("Refreshing RetroArch information...")
        
        # Re-initialize RetroArch interface
        self.retroarch = RetroArchInterface()
        self.refresh_retroarch_info()
        
        self.log_message("RetroArch information updated")

    def refresh_games_list(self, force_full_refresh=False):
        """Smart sync with comprehensive change detection

        Args:
            force_full_refresh: If True, fetch all data regardless of timestamps (default: False)
        """
        if getattr(self, '_dialog_open', False):
            return

        def smart_sync():
            if not (self.romm_client and self.romm_client.authenticated):
                self.handle_offline_mode()
                return

            try:
                download_dir = Path(self.rom_dir_row.get_text())
                server_url = self.romm_client.base_url

                # Determine whether to do incremental or full refresh
                # Incremental sync is ONLY valid if we have a last sync timestamp AND
                # an existing catalog of server games in memory to apply deltas to.
                has_existing_catalog = (
                    hasattr(self, 'available_games') and
                    isinstance(self.available_games, list) and
                    len([g for g in self.available_games if isinstance(g, dict) and g.get('rom_id')]) > 0
                )

                use_incremental = (
                    not force_full_refresh and
                    self._last_full_fetch_time is not None and
                    has_existing_catalog
                )

                if use_incremental:
                    self.log_message(f"🔄 Checking for updates from server: {server_url}")
                    self.perform_incremental_sync(download_dir, server_url)
                else:
                    self.log_message(f"🔄 Syncing with server: {server_url}")
                    self.perform_full_sync(download_dir, server_url)

            except Exception as e:
                self.log_message(f"❌ Sync error: {e}")
                self.use_cached_data_as_fallback()

        threading.Thread(target=smart_sync, daemon=True).start()

    def perform_full_sync(self, download_dir, server_url):
        """Perform full sync with live updates"""
        try:
            sync_start = time.time()

            # Preserve existing local games to keep them visible during fetch
            existing_local_games = []
            if hasattr(self, 'available_games') and self.available_games:
                # Keep all existing games for now (they'll be updated/merged with server data)
                existing_local_games = list(self.available_games)
                self.log_message(f"Preserving {len(existing_local_games)} existing games during fetch")

            # Debouncing: Track last UI update time to prevent excessive updates
            last_ui_update = [0]  # Use list to allow modification in nested function
            min_update_interval = 0.5  # Minimum 500ms between UI updates
            pending_update_source = [None]  # Track pending GLib timeout

            def progress_handler(stage, data):
                if stage in ['chunk', 'page']:
                    # Update connection status with chunk progress
                    GLib.idle_add(lambda msg=data: self.update_connection_ui_with_message(msg))
                elif stage == 'batch':
                    # Process and show games after each chunk
                    chunk_games = data.get('accumulated_games', [])
                    chunk_num = data.get('chunk_number', 0)
                    total_chunks = data.get('total_chunks', 0)

                    if chunk_games:
                        # Process games
                        process_start = time.time()

                        # Group sibling ROMs in this chunk before processing
                        if hasattr(self.romm_client, '_group_sibling_roms'):
                            chunk_games = self.romm_client._group_sibling_roms(chunk_games)

                        processed_games = []
                        for rom in chunk_games:
                            processed_game = self.process_single_rom(rom, download_dir)
                            processed_games.append(processed_game)

                        # Merge with existing local games to keep them visible
                        # Create a map to identify duplicates (use rom_id if available, otherwise use file path)
                        fetched_identifiers = set()
                        for g in processed_games:
                            rom_id = g.get('rom_id')
                            if rom_id:
                                fetched_identifiers.add(('rom_id', rom_id))
                            # Also track by file path for games without rom_id
                            local_path = g.get('local_path')
                            if local_path:
                                fetched_identifiers.add(('path', local_path))

                        # Add local games that aren't in the fetched data yet
                        added_count = 0
                        for local_game in existing_local_games:
                            local_rom_id = local_game.get('rom_id')
                            local_path = local_game.get('local_path')

                            # Check if this game is already in the fetched data
                            is_duplicate = False
                            if local_rom_id and ('rom_id', local_rom_id) in fetched_identifiers:
                                is_duplicate = True
                            elif local_path and ('path', local_path) in fetched_identifiers:
                                is_duplicate = True

                            if not is_duplicate:
                                processed_games.append(local_game)
                                added_count += 1

                        # Sort games
                        processed_games = self.library_section.sort_games_consistently(processed_games)

                        # Debounced UI update
                        current_time = time.time()
                        time_since_last_update = current_time - last_ui_update[0]

                        def do_ui_update():
                            ui_start = time.time()
                            # Update with merged games (fetched + remaining local)
                            self.available_games = processed_games
                            if hasattr(self, 'library_section'):
                                self.library_section.update_games_library(processed_games)
                            last_ui_update[0] = time.time()
                            pending_update_source[0] = None
                            return False  # Don't repeat

                        # If enough time has passed, update immediately
                        if time_since_last_update >= min_update_interval:
                            # Cancel any pending update
                            if pending_update_source[0]:
                                GLib.source_remove(pending_update_source[0])
                                pending_update_source[0] = None
                            GLib.idle_add(do_ui_update)
                        else:
                            # Schedule update for later (debounce)
                            if not pending_update_source[0]:  # Only schedule if not already pending
                                delay_ms = int((min_update_interval - time_since_last_update) * 1000)
                                pending_update_source[0] = GLib.timeout_add(delay_ms, do_ui_update)

            # Fetch with progress handler
            fetch_start = time.time()
            romm_result = self.romm_client.get_roms(progress_callback=progress_handler)

            if not romm_result or len(romm_result) != 2:
                self.log_message("Failed to fetch games from RomM")
                return

            final_games, total_count = romm_result

            # Final processing and UI update
            final_process_start = time.time()
            games = []
            for rom in final_games:
                processed_game = self.process_single_rom(rom, download_dir)
                games.append(processed_game)

            games = self.library_section.sort_games_consistently(games)

            def final_update():
                final_ui_start = time.time()
                self.available_games = games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(games)

                total_elapsed = time.time() - sync_start

                # Show completion message first
                completion_msg = f"✓ Full sync complete: {len(games):,} games in {total_elapsed:.2f}s"
                self.update_connection_ui_with_message(completion_msg)
                self.log_message(completion_msg)

                # After 3 seconds, show connected status
                def show_connected():
                    self.update_connection_ui("connected")
                    return False  # Don't repeat

                GLib.timeout_add(5000, show_connected)  # 3 second delay

            GLib.idle_add(final_update)

            # Set timestamp for future incremental updates
            sync_time = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            self._last_full_fetch_time = sync_time

            # Save cache in background with original ungrouped count
            threading.Thread(target=lambda: self.game_cache.save_games_data(games, original_total=total_count, last_sync_datetime=sync_time), daemon=True).start()

            # Clear collections cache after main library refresh
            if hasattr(self, 'library_section'):
                self.library_section.collections_cache_time = 0

        except Exception as e:
            self.log_message(f"Full sync error: {e}")

    def perform_incremental_sync(self, download_dir, server_url):
        """Perform incremental (differential) sync using updated_after parameter"""
        try:
            sync_start = time.time()

            # If in-memory library has no server games, fallback to full sync
            server_games_count = len([g for g in self.available_games if isinstance(g, dict) and g.get('rom_id')]) if (hasattr(self, 'available_games') and self.available_games) else 0
            if server_games_count == 0:
                self.log_message("Incremental sync: no server games in memory, falling back to full sync...")
                self.perform_full_sync(download_dir, server_url)
                return

            # Fetch only ROMs updated since last check
            updated_after = self._last_full_fetch_time
            self.log_message(f"Checking for updates since {updated_after}...")

            new_roms_data = self.romm_client.get_roms(
                limit=500,
                offset=0,
                updated_after=updated_after
            )

            if new_roms_data is None or len(new_roms_data) != 2:
                self.log_message("Incremental sync: no data returned, falling back to full sync...")
                self.perform_full_sync(download_dir, server_url)
                return

            new_roms, _ = new_roms_data

            if not new_roms:
                total_elapsed = time.time() - sync_start
                msg = f"✓ Library is up to date (0 changes, {total_elapsed:.2f}s)"
                self.log_message(msg)

                now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
                self._last_full_fetch_time = now_str

                def update_ui_up_to_date():
                    self.update_connection_ui_with_message(msg)
                    GLib.timeout_add(3000, lambda: self.update_connection_ui("connected") or False)
                    return False

                GLib.idle_add(update_ui_up_to_date)
                if hasattr(self, 'game_cache') and self.available_games and len(self.available_games) > 0 and server_games_count > 0:
                    orig_total = getattr(self.game_cache, 'original_total', None)
                    threading.Thread(target=lambda: self.game_cache.save_games_data(self.available_games, original_total=orig_total, last_sync_datetime=now_str), daemon=True).start()
                return

            # Group sibling ROMs in new_roms if supported
            if hasattr(self.romm_client, '_group_sibling_roms'):
                new_roms = self.romm_client._group_sibling_roms(new_roms)

            # Process new/updated ROMs
            new_count = 0
            updated_count = 0

            # Create a map for fast lookup by rom_id
            existing_games_map = {g['rom_id']: g for g in self.available_games if isinstance(g, dict) and 'rom_id' in g}

            for rom in new_roms:
                rom_id = rom.get('id')
                was_existing = rom_id in existing_games_map

                processed_game = self.process_single_rom(rom, download_dir)
                existing_games_map[rom_id] = processed_game

                if was_existing:
                    updated_count += 1
                else:
                    new_count += 1

            # Update the games list
            updated_games = list(existing_games_map.values())
            updated_games = self.library_section.sort_games_consistently(updated_games)

            now_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            self._last_full_fetch_time = now_str

            def update_ui():
                self.available_games = updated_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(updated_games)

                total_elapsed = time.time() - sync_start
                msg = f"✓ Differential sync: {new_count} new, {updated_count} updated games ({total_elapsed:.2f}s)"
                self.log_message(msg)
                self.update_connection_ui_with_message(msg)

                GLib.timeout_add(3000, lambda: self.update_connection_ui("connected") or False)

            GLib.idle_add(update_ui)

            # Save updated cache in background with updated original_total
            orig_total = getattr(self.game_cache, 'original_total', len(self.available_games))
            if orig_total is not None and new_count > 0:
                orig_total += new_count
            threading.Thread(target=lambda: self.game_cache.save_games_data(updated_games, original_total=orig_total, last_sync_datetime=now_str), daemon=True).start()

        except Exception as e:
            self.log_message(f"Incremental sync error: {e}")
            self.log_message("Falling back to full sync...")
            self.perform_full_sync(download_dir, server_url)

    def scan_local_games_only(self, download_dir):
        """Enhanced local game scanning that handles both slug and full platform names"""
        games = []

        self.log_message(f"Scanning {download_dir}")
        self.log_message(f"Directory exists: {download_dir.exists()}")

        if not download_dir.exists():
            return games

        # Ensure platform mapping is populated before scanning
        if not self.game_cache.platform_mapping and self.romm_client and self.romm_client.authenticated:
            try:
                self.log_message("📋 Fetching platform names from RomM...")
                platforms = self.romm_client.get_platforms()
                if platforms:
                    self.game_cache.build_platform_mapping_from_api(platforms)
                    self.log_message(f"✅ Loaded {len(platforms)} platform names")
            except Exception as e:
                self.log_message(f"⚠️ Could not fetch platform names: {e}")

        rom_extensions = {'.zip', '.7z', '.rar', '.bin', '.cue', '.iso', '.chd', '.sfc', '.smc', '.nes', '.gba', '.gb', '.gbc', '.md', '.gen', '.n64', '.z64'}

        for file_path in download_dir.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in rom_extensions:
                directory_name = file_path.parent.name if file_path.parent != download_dir else "Unknown"

                game_name = file_path.stem

                # Use cache to get proper platform name (handles both slug and full names)
                platform_display_name = self.game_cache.get_platform_name(directory_name)
                
                # Try to get additional ROM data from cache
                game_info = self.game_cache.get_game_info(file_path.name)
                
                if game_info:
                    platform_display_name = game_info['platform']  # Use cached full platform name
                    rom_id = game_info['rom_id']
                    romm_data = game_info['romm_data']
                else:
                    rom_id = None
                    romm_data = None
                
                games.append({
                    'name': game_name,
                    'rom_id': rom_id,
                    'platform': platform_display_name,  # Full name for tree view
                    'platform_slug': directory_name,    # Actual directory name used
                    'file_name': file_path.name,
                    'is_downloaded': True,
                    'local_path': str(file_path),
                    'local_size': file_path.stat().st_size,
                    'romm_data': romm_data
                })
        
        return self.library_section.sort_games_consistently(games)

    def on_refresh_games_list(self, button):
        """Refresh games list button handler"""
        self.log_message("Refreshing games list...")
        self.refresh_games_list()
    
    def on_delete_game_clicked(self, button):
        """Delete a downloaded game file"""
        selected_game = self.get_selected_game()
        if not selected_game:
            self.log_message("No game selected")
            return
        
        if not selected_game['is_downloaded']:
            self.log_message("Game is not downloaded")
            return
        
        # Create confirmation dialog
        def on_response(dialog, response):
            if response == "delete":
                self.delete_game_file(selected_game)
        
        dialog = Adw.AlertDialog.new("Delete Game?", f"Are you sure you want to delete '{selected_game['name']}'? This will permanently remove the ROM file from your computer.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect('response', on_response)
        dialog.present()

    def download_multiple_games(self, games):
        """Download multiple games with concurrency limit"""
        count = len(games)
        games_to_download = list(games)

        # Filter to only games that aren't already downloaded
        not_downloaded = [g for g in games_to_download if not g.get('is_downloaded', False)]

        if not not_downloaded:
            self.log_message("All selected games are already downloaded")
            return

        # Update count to reflect actual games to download
        download_count = len(not_downloaded)

        # SET BULK DOWNLOAD STATE FIRST - before anything that might trigger UI updates
        self._bulk_download_in_progress = True
        self._bulk_download_cancelled = False

        # CAPTURE SELECTION STATE BEFORE BLOCKING
        if hasattr(self, 'library_section'):
            self._downloading_rom_ids = set()
            for game in not_downloaded:
                identifier_type, identifier_value = self.library_section.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self._downloading_rom_ids.add(identifier_value)

        # BLOCK TREE REFRESHES DURING BULK OPERATION
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)

        # Update UI to show "Cancel All" button immediately
        if hasattr(self, 'library_section'):
            GLib.idle_add(lambda: self.library_section.update_action_buttons())

        # Track completion
        self._bulk_download_remaining = download_count
        
        # Get max concurrent setting and create semaphore
        max_concurrent = int(self.settings.get('Download', 'max_concurrent', '3'))
        self.log_message(f"🚀 Starting bulk download of {download_count} games (max {max_concurrent} concurrent)...")
        
        import threading
        semaphore = threading.Semaphore(max_concurrent)
        
        def controlled_download(game):
            """Download with proper semaphore control"""
            semaphore.acquire()  # Wait for slot
            try:
                # Call download_game but pass semaphore to control the actual download thread
                self.download_game_controlled(game, semaphore, is_bulk_operation=True)
            except Exception as e:
                self.log_message(f"Download error for {game.get('name')}: {e}")
                semaphore.release()  # Ensure release on error
        
        # Start all downloads (semaphore controls actual concurrency)
        for game in not_downloaded:
            threading.Thread(target=controlled_download, args=(game,), daemon=True).start()

        # Check for completion periodically
        def check_completion():
            if hasattr(self, '_bulk_download_remaining') and self._bulk_download_remaining <= 0:
                self._dialog_open = False
                self._bulk_download_in_progress = False
                if hasattr(self, 'library_section'):
                    self.library_section._block_selection_updates(False)
                    if hasattr(self, '_downloading_rom_ids'):
                        for rom_id in self._downloading_rom_ids:
                            self.library_section.selected_rom_ids.discard(rom_id)
                        self.library_section.sync_selected_checkboxes()
                        self.library_section.update_selection_label()
                        self.library_section.refresh_all_platform_checkboxes()
                        # Update visual checkbox states to match cleared selections
                        GLib.idle_add(self.library_section.force_checkbox_sync)
                        delattr(self, '_downloading_rom_ids')

                    # Update action buttons after bulk operation completes - always call this
                    # to ensure button state is refreshed (e.g., from "Cancel All" back to "Download")
                    self.library_section.update_action_buttons()

                # Check if this was a cancellation
                was_cancelled = self._bulk_download_cancelled
                self._bulk_download_cancelled = False

                if was_cancelled:
                    self.log_message(f"⊗ Bulk download cancelled")
                else:
                    self.log_message(f"✅ Bulk download complete ({download_count} games)")

                    # Send desktop notification when bulk download completes
                    self.send_desktop_notification(
                        "Downloads Complete",
                        f"Successfully downloaded {download_count} game{'s' if download_count != 1 else ''}"
                    )

                delattr(self, '_bulk_download_remaining')
                return False
            return True

        GLib.timeout_add(500, check_completion)

    def download_multiple_games_with_collection_tracking(self, games, collections_data):
        """Download multiple games with per-collection tracking and notifications"""
        count = len(games)
        games_to_download = list(games)

        # Filter to only games that aren't already downloaded
        not_downloaded = [g for g in games_to_download if not g.get('is_downloaded', False)]

        if not not_downloaded:
            self.log_message("All selected games are already downloaded")
            return

        # Update count to reflect actual games to download
        download_count = len(not_downloaded)

        # SET BULK DOWNLOAD STATE FIRST - before anything that might trigger UI updates
        self._bulk_download_in_progress = True
        self._bulk_download_cancelled = False

        # CAPTURE SELECTION STATE BEFORE BLOCKING
        if hasattr(self, 'library_section'):
            self._downloading_rom_ids = set()
            for game in not_downloaded:
                identifier_type, identifier_value = self.library_section.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self._downloading_rom_ids.add(identifier_value)

        # BLOCK TREE REFRESHES DURING BULK OPERATION
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)

        # Update UI to show "Cancel All" button immediately
        if hasattr(self, 'library_section'):
            GLib.idle_add(lambda: self.library_section.update_action_buttons())

        # Track completion per collection
        self._bulk_download_remaining = download_count
        self._collection_downloads = {}

        # Initialize per-collection counters
        for collection_name, data in collections_data.items():
            if data['to_download'] > 0:
                self._collection_downloads[collection_name] = {
                    'total': data['total'],
                    'remaining': data['to_download'],
                    'downloaded': data['already_downloaded']
                }

        # Get max concurrent setting and create semaphore
        max_concurrent = int(self.settings.get('Download', 'max_concurrent', '3'))
        self.log_message(f"🚀 Starting bulk download of {download_count} games (max {max_concurrent} concurrent)...")

        import threading
        semaphore = threading.Semaphore(max_concurrent)
        download_lock = threading.Lock()

        def controlled_download(game):
            """Download with proper semaphore control and collection tracking"""
            semaphore.acquire()  # Wait for slot
            try:
                # Call download_game but pass semaphore to control the actual download thread
                self.download_game_controlled(game, semaphore, is_bulk_operation=True,
                                            on_complete=lambda g=game: on_game_complete(g))
            except Exception as e:
                self.log_message(f"Download error for {game.get('name')}: {e}")
                semaphore.release()  # Ensure release on error

        def on_game_complete(game):
            """Called when a game download completes - track per collection"""
            collection_name = game.get('_sync_collection')
            if collection_name and hasattr(self, '_collection_downloads') and collection_name in self._collection_downloads:
                with download_lock:
                    self._collection_downloads[collection_name]['remaining'] -= 1
                    self._collection_downloads[collection_name]['downloaded'] += 1

                    # If this collection is complete, send notification
                    if self._collection_downloads[collection_name]['remaining'] == 0:
                        total = self._collection_downloads[collection_name]['total']
                        downloaded = self._collection_downloads[collection_name]['downloaded']

                        self.send_desktop_notification(
                            f"✅ {collection_name} - Sync Complete",
                            f"{downloaded}/{total} ROMs synced"
                        )
                        self.log_message(f"✅ Collection '{collection_name}' sync complete: {downloaded}/{total} ROMs")

                        # Mark collection as completed (synced) instead of just removing from downloading
                        if hasattr(self, 'library_section'):
                            # Add to a new set tracking completed collections
                            if not hasattr(self.library_section, 'completed_sync_collections'):
                                self.library_section.completed_sync_collections = set()
                            self.library_section.completed_sync_collections.add(collection_name)
                            # Remove from downloading to transition orange -> green
                            self.library_section.currently_downloading_collections.discard(collection_name)
                            # Update status immediately
                            self.library_section.update_collection_sync_status(collection_name)

        # Start all downloads (semaphore controls actual concurrency)
        for game in not_downloaded:
            threading.Thread(target=controlled_download, args=(game,), daemon=True).start()

        # Check for completion periodically
        def check_completion():
            if hasattr(self, '_bulk_download_remaining') and self._bulk_download_remaining <= 0:
                self._dialog_open = False
                self._bulk_download_in_progress = False
                if hasattr(self, 'library_section'):
                    self.library_section._block_selection_updates(False)
                    if hasattr(self, '_downloading_rom_ids'):
                        for rom_id in self._downloading_rom_ids:
                            self.library_section.selected_rom_ids.discard(rom_id)
                        self.library_section.sync_selected_checkboxes()
                        self.library_section.update_selection_label()
                        self.library_section.refresh_all_platform_checkboxes()
                        # Update visual checkbox states to match cleared selections
                        GLib.idle_add(self.library_section.force_checkbox_sync)
                        delattr(self, '_downloading_rom_ids')

                    # Update action buttons after bulk operation completes - always call this
                    # to ensure button state is refreshed (e.g., from "Cancel All" back to "Download")
                    self.library_section.update_action_buttons()

                # Check if this was a cancellation
                was_cancelled = self._bulk_download_cancelled
                self._bulk_download_cancelled = False

                if was_cancelled:
                    self.log_message(f"⊗ Bulk download cancelled")
                else:
                    self.log_message(f"✅ All downloads complete ({download_count} games)")

                # Clean up collection tracking
                if hasattr(self, '_collection_downloads'):
                    delattr(self, '_collection_downloads')

                delattr(self, '_bulk_download_remaining')
                return False
            return True

        GLib.timeout_add(500, check_completion)

    def delete_multiple_games(self, games):
        """Delete multiple games with confirmation"""
        count = len(games)
        games_to_delete = list(games)

        # SAVE SELECTION STATE BEFORE DIALOG
        if hasattr(self, 'library_section'):
            saved_rom_ids = self.library_section.selected_rom_ids.copy()
            saved_game_keys = self.library_section.selected_game_keys.copy()
            saved_checkboxes = self.library_section.selected_checkboxes.copy()
            saved_selected_game = self.library_section.selected_game

        # BLOCK ALL UPDATES
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)

        dialog = Adw.AlertDialog.new(f"Delete {count} Games?", f"Are you sure you want to delete {count} selected games?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete Selected")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            self._dialog_open = False
            if hasattr(self, 'library_section'):
                self.library_section._block_selection_updates(False)

                if response == "delete":
                    # Identify collections that contain the games being deleted
                    affected_collections = set()
                    for game in games_to_delete:
                        collection = game.get('collection')
                        if collection:
                            affected_collections.add(collection)

                    # Delete the games
                    for game in games_to_delete:
                        self.delete_game_file(game, is_bulk_operation=True)

                    # Disable autosync for affected collections that were actively syncing
                    if hasattr(self, 'library_section') and affected_collections:
                        collections_to_disable = affected_collections.intersection(
                            self.library_section.actively_syncing_collections
                        )

                        if collections_to_disable:
                            self.library_section.disable_autosync_for_collections(collections_to_disable)
                            collection_list = ", ".join(f"'{c}'" for c in collections_to_disable)
                            self.log_message(f"🔄 Auto-sync disabled for {len(collections_to_disable)} collection(s): {collection_list}")

                    self.library_section.clear_checkbox_selections_smooth()
                else:
                    # RESTORE SELECTION STATE ON CANCEL
                    self.library_section.selected_rom_ids = saved_rom_ids
                    self.library_section.selected_game_keys = saved_game_keys
                    self.library_section.selected_checkboxes = saved_checkboxes
                    self.library_section.selected_game = saved_selected_game

                    # UPDATE UI TO REFLECT RESTORED STATE
                    self.library_section.update_action_buttons()
                    self.library_section.update_selection_label()

        dialog.connect('response', on_response)
        dialog.present()

    def delete_game_file(self, game, is_bulk_operation=False):
        """Actually delete the game file"""
        def delete():
            try:
                # Get game name early for logging
                game_name = game.get('name', 'Unknown Game')
                local_path = game.get('local_path')

                if not local_path:
                    GLib.idle_add(lambda n=game_name:
                                self.log_message(f"No local path for {n}"))
                    return

                game_path = Path(local_path)

                # For multi-disc or multi-file games, ensure we're deleting the folder
                # local_path can point to either a file (when scanned from filesystem) or folder (when freshly downloaded)
                if game.get('is_multi_disc'):
                    if game_path.is_file():
                        # If it points to a file, delete the parent folder
                        game_path = game_path.parent
                elif not game_path.exists() and game_path.suffix:
                    # If path doesn't exist and looks like a file (has extension), try the parent folder
                    # This handles multi-file games where local_path might point to a non-existent file
                    parent_folder = game_path.parent
                    if parent_folder.exists() and parent_folder.is_dir():
                        game_path = parent_folder

                GLib.idle_add(lambda n=game_name:
                            self.log_message(f"Deleting {n}..."))

                # After deletion, replace complex update with:
                if game_path.exists():
                    # Handle both files and directories (for multi-disc/multi-file games)
                    if game_path.is_dir():
                        import shutil
                        shutil.rmtree(game_path)
                    else:
                        game_path.unlink()

                    game['is_downloaded'] = False
                    game['local_path'] = None
                    game['local_size'] = 0

                    # Update all discs in multi-disc games to reflect deletion
                    if game.get('is_multi_disc') and game.get('discs'):
                        for disc in game['discs']:
                            disc['is_downloaded'] = False
                            disc['size'] = 0
                    
                    def refresh_ui():
                        for i, g in enumerate(self.available_games):
                            if g.get('rom_id') == game.get('rom_id'):
                                self.available_games[i] = game
                                break

                    GLib.idle_add(refresh_ui)
                    
                    # Update based on current view mode
                    def update_after_deletion():
                        if hasattr(self, 'library_section'):
                            current_mode = getattr(self.library_section, 'current_view_mode', 'platform')
                            
                            # For offline mode (not connected to RomM), we need a full refresh to remove items
                            if not (self.romm_client and self.romm_client.authenticated):
                                # If not connected to RomM, remove the game entirely from the list
                                if hasattr(self, 'available_games') and game in self.available_games:
                                    self.available_games.remove(game)

                                # Refresh the entire library to remove the item
                                if hasattr(self, 'library_section'):
                                    self.library_section.update_games_library(self.available_games)
                            else:
                                # Connected to RomM - just update the single item (works for both platform and collection view)
                                if hasattr(self, 'library_section'):
                                    self.library_section.update_single_game(game, skip_platform_update=is_bulk_operation)
                        
                        return False
                    
                    GLib.idle_add(update_after_deletion)

                    # Only clear selections after an individual (non-bulk) deletion.
                    if not is_bulk_operation:
                        GLib.idle_add(lambda: self.library_section.clear_checkbox_selections_smooth() if hasattr(self, 'library_section') else None)
                    
                    # Try to remove empty platform directory
                    try:
                        platform_dir = game_path.parent
                        if platform_dir.exists() and not any(platform_dir.iterdir()):
                            platform_dir.rmdir()
                            GLib.idle_add(lambda d=platform_dir.name: 
                                        self.log_message(f"Removed empty directory: {d}"))
                    except Exception:
                        pass  # Directory not empty or other error, ignore
                        
                else:
                    GLib.idle_add(lambda n=game_name: 
                                self.log_message(f"File not found: {n}"))
                
            except Exception as e:
                # Make sure game_name is available here too
                name = game.get('name', 'Unknown Game')
                GLib.idle_add(lambda err=str(e), n=name: 
                            self.log_message(f"Error deleting {n}: {err}"))
        
        threading.Thread(target=delete, daemon=True).start()

    def delete_disc(self, game, disc):
        """Delete a single disc from a multi-disc game or regional variant"""
        def delete():
            try:
                disc_name = disc['name']
                is_regional_variant = disc.get('is_regional_variant', False)
                platform_slug = game.get('platform_slug', game.get('platform', 'Unknown'))

                # Get game folder path
                download_dir = Path(self.rom_dir_row.get_text())
                platform_dir = download_dir / platform_slug

                # For regional variants, use the actual folder name (fs_name) from local_path
                if is_regional_variant and game.get('local_path'):
                    game_folder = Path(game['local_path'])
                    # Use full filename with extension for regional variants
                    file_name = disc.get('full_fs_name', disc_name)
                else:
                    # For multi-disc games, use the game name
                    game_folder = platform_dir / game['name']
                    file_name = disc_name

                disc_path = game_folder / file_name

                # Log the path for debugging
                GLib.idle_add(lambda p=str(disc_path): self.log_message(f"  Attempting to delete: {p}"))

                if disc_path.exists():
                    # Verify it's a file, not a directory
                    if disc_path.is_file():
                        disc_path.unlink()
                        GLib.idle_add(lambda: self.log_message(f"✓ Deleted {disc_name}"))

                        # Update game status based on type
                        if is_regional_variant:
                            # For regional variants, check if any sibling file still exists
                            # Game is downloaded if the folder exists with at least one variant
                            game['is_downloaded'] = game_folder.exists() and any(game_folder.iterdir())

                            # Recalculate local_size after deletion
                            if game_folder.exists():
                                game['local_size'] = sum(f.stat().st_size for f in game_folder.rglob('*') if f.is_file())
                            else:
                                game['local_size'] = 0
                        else:
                            # For multi-disc games, update parent status based on remaining discs
                            for d in game.get('discs', []):
                                if d.get('name') == disc_name:
                                    d['is_downloaded'] = False
                                    d['size'] = 0
                            game['is_downloaded'] = all(d.get('is_downloaded', False) for d in game.get('discs', []))

                        # Update available_games list
                        rom_id = game.get('rom_id')
                        for i, existing_game in enumerate(self.available_games):
                            if existing_game.get('rom_id') == rom_id:
                                self.available_games[i] = game
                                break

                        # Save cache to persist deletion status
                        if hasattr(self, 'game_cache'):
                            orig_total = getattr(self.game_cache, 'original_total', None)
                            last_sync = getattr(self.game_cache, 'last_sync_datetime', self._last_full_fetch_time)
                            threading.Thread(target=lambda: self.game_cache.save_games_data(self.available_games, original_total=orig_total, last_sync_datetime=last_sync), daemon=True).start()

                        # Update UI - rebuild_children will check file existence for each variant
                        def update_ui():
                            if hasattr(self, 'library_section'):
                                # Create a fresh copy with updated data
                                import copy
                                game_copy = copy.deepcopy(game)
                                self.library_section.update_single_game(game_copy)
                            return False

                        GLib.idle_add(update_ui)
                    else:
                        GLib.idle_add(lambda: self.log_message(f"⚠️ Path is a directory, not a file: {disc_path}"))
                else:
                    GLib.idle_add(lambda: self.log_message(f"⚠️ File not found: {disc_path}"))

            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"Error deleting {disc_name}: {e}"))

        threading.Thread(target=delete, daemon=True).start()

    def on_game_action_clicked(self, button):
        """Handle download or launch action based on game status"""
        selected_game = self.get_selected_game()
        if not selected_game:
            self.log_message("No game selected")
            return

        if selected_game['is_downloaded']:
            # Launch the game
            self.launch_game(selected_game)
        else:
            # Download the game
            self.download_game(selected_game)

    def _select_file_from_folder(self, folder_path):
        """Select the appropriate file to launch from a folder using smart heuristics.

        Args:
            folder_path: Path object pointing to a folder containing game files

        Returns:
            Path object pointing to the selected file, or None if no suitable file found
        """
        if not folder_path.is_dir():
            return folder_path

        # Get all files in the folder (excluding hidden files and directories)
        files = [f for f in folder_path.iterdir() if f.is_file() and not f.name.startswith('.')]

        if not files:
            logging.warning(f"No files found in folder: {folder_path}")
            return None

        # Single file - auto-select it
        if len(files) == 1:
            logging.info(f"Auto-selected single file from folder: {files[0].name}")
            return files[0]

        # Multi-file folder - use smart selection
        logging.info(f"Multiple files found in folder ({len(files)}), using smart selection")

        # Priority 1: .m3u files (multi-disc playlists)
        m3u_files = [f for f in files if f.suffix.lower() == '.m3u']
        if m3u_files:
            logging.info(f"Selected .m3u playlist: {m3u_files[0].name}")
            return m3u_files[0]

        # Priority 2: .cue files (CD-based games - REQUIRED for CD games)
        cue_files = [f for f in files if f.suffix.lower() == '.cue']
        if cue_files:
            logging.info(f"Selected .cue file: {cue_files[0].name}")
            return cue_files[0]

        # Priority 3: .chd files (compressed CD images)
        chd_files = [f for f in files if f.suffix.lower() == '.chd']
        if chd_files:
            logging.info(f"Selected .chd file: {chd_files[0].name}")
            return chd_files[0]

        # Priority 4: Common ROM extensions
        rom_extensions = {'.iso', '.bin', '.img', '.nds', '.gba', '.gb', '.gbc',
                         '.n64', '.z64', '.v64', '.sfc', '.smc', '.nes',
                         '.md', '.gen', '.smd', '.32x', '.gg', '.pce'}
        rom_files = [f for f in files if f.suffix.lower() in rom_extensions]
        if rom_files:
            # If multiple ROMs, pick the largest one
            largest = max(rom_files, key=lambda f: f.stat().st_size)
            logging.info(f"Selected largest ROM file: {largest.name} ({largest.stat().st_size} bytes)")
            return largest

        # Fallback: Pick the largest file
        largest = max(files, key=lambda f: f.stat().st_size)
        logging.info(f"Selected largest file as fallback: {largest.name} ({largest.stat().st_size} bytes)")
        return largest

    def launch_game(self, game):
        """Launch a game using RetroArch (with BIOS verification)"""
        if not game.get('is_downloaded'):
            self.log_message("Game is not downloaded")
            return

        # Auto-download missing BIOS if manager is available
        if self.retroarch.bios_manager:
            platform = game.get('platform')
            if platform:
                # Set RomM client BEFORE checking (needed for server queries)
                self.retroarch.bios_manager.romm_client = self.romm_client

                logging.debug(f"[BIOS] Checking platform: {platform}")
                normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                logging.debug(f"[BIOS] Normalized to: {normalized}")
                present, missing = self.retroarch.bios_manager.check_platform_bios(normalized)
                logging.debug(f"[BIOS] Present: {len(present)}, Missing: {len(missing)}")
                if missing:
                    logging.debug(f"[BIOS] Missing files: {[m.get('file') for m in missing]}")
                required_missing = [b for b in missing if not b.get('optional', False)]
                logging.debug(f"[BIOS] Required missing: {len(required_missing)}")

                if required_missing:
                    self.log_message(f"📥 Downloading {len(required_missing)} missing BIOS file(s) for {platform}...")
                    success = self.retroarch.bios_manager.auto_download_missing_bios(normalized)
                    if success:
                        self.log_message(f"✅ BIOS download complete for {platform}")
                    else:
                        self.log_message(f"⚠️ Some BIOS files may not have downloaded for {platform}")
            else:
                logging.debug("[BIOS] No platform specified for game")
        else:
            logging.debug("[BIOS] No BIOS manager available")

        # Actually launch the game
        local_path = game.get('local_path')
        if not local_path or not Path(local_path).exists():
            self.log_message("Game file not found")
            return

        platform_name = game.get('platform')
        rom_path = Path(local_path)

        # Handle folder-based games by selecting the appropriate file
        if rom_path.is_dir():
            selected_file = self._select_file_from_folder(rom_path)
            if selected_file is None:
                self.log_message("❌ No launchable file found in game folder")
                return
            logging.info(f"Folder detected, selected file: {selected_file.name}")
            rom_path = selected_file

        logging.info(f"Launching game: {game.get('name')}")
        logging.debug(f"ROM path: {rom_path}, Platform: {platform_name}")

        # Pre-launch sync is handled by AutoSyncManager when RetroArch content is detected

        # Let RetroArch interface handle the actual launching
        success, message = self.retroarch.launch_game(rom_path, platform_name)

        if success:
            self.log_message(f"🚀 {message}")
            # Send notification to RetroArch if possible
            self.retroarch.send_notification(f"Launching {game.get('name', 'Unknown')}")
        else:
            self.log_message(f"❌ Launch failed: {message}")
            # Show user-friendly dialog for missing core
            if "No suitable core found" in message:
                self._show_missing_core_dialog(game.get('name', 'Unknown'), platform_name)
            elif "Core not found" in message:
                self._show_missing_core_dialog(game.get('name', 'Unknown'), platform_name)

    def launch_disc(self, game, disc):
        """Launch a specific disc from a multi-disc game using RetroArch"""
        if not disc.get('is_downloaded', False):
            self.log_message("Disc is not downloaded")
            return

        # Auto-download missing BIOS if manager is available (same as launch_game)
        if self.retroarch.bios_manager:
            platform = game.get('platform')
            if platform:
                # Set RomM client BEFORE checking (needed for server queries)
                self.retroarch.bios_manager.romm_client = self.romm_client

                normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                present, missing = self.retroarch.bios_manager.check_platform_bios(normalized)
                required_missing = [b for b in missing if not b.get('optional', False)]

                if required_missing:
                    self.log_message(f"📥 Downloading {len(required_missing)} missing BIOS file(s) for {platform}...")
                    success = self.retroarch.bios_manager.auto_download_missing_bios(normalized)
                    if success:
                        self.log_message(f"✅ BIOS download complete for {platform}")
                    else:
                        self.log_message(f"⚠️ Some BIOS files may not have downloaded for {platform}")

        # Build path to the specific disc file
        game_local_path = game.get('local_path')
        if not game_local_path:
            self.log_message("Game path not found")
            return

        # For multi-disc games, local_path points to the folder containing all discs
        game_folder = Path(game_local_path)
        
        # Use full_fs_name (with extension) for regional variants, or name for multi-disc
        disc_filename = disc.get('full_fs_name') or disc.get('name')
        disc_path = game_folder / disc_filename

        if not disc_path.exists():
            self.log_message(f"Disc file not found: {disc_filename}")
            self.log_message(f"Expected path: {disc_path}")
            return

        platform_name = game.get('platform')

        # Handle folder-based discs (rare edge case)
        if disc_path.is_dir():
            selected_file = self._select_file_from_folder(disc_path)
            if selected_file is None:
                self.log_message("❌ No launchable file found in disc folder")
                return
            logging.info(f"Disc folder detected, selected file: {selected_file.name}")
            disc_path = selected_file

        # Let RetroArch interface handle the actual launching
        success, message = self.retroarch.launch_game(disc_path, platform_name)

        if success:
            self.log_message(f"🚀 {message}")
            # Send notification to RetroArch if possible
            game_name = game.get('name', 'Unknown')
            self.retroarch.send_notification(f"Launching {game_name} - {disc_filename}")
        else:
            self.log_message(f"❌ Launch failed: {message}")
            # Show user-friendly dialog for missing core
            if "No suitable core found" in message:
                self._show_missing_core_dialog(f"{game.get('name', 'Unknown')} - {disc_filename}", platform_name)
            elif "Core not found" in message:
                self._show_missing_core_dialog(f"{game.get('name', 'Unknown')} - {disc_filename}", platform_name)

    def _show_missing_core_dialog(self, game_name, platform_name):
        """Show a dialog informing the user that no RetroArch core is installed for the platform,
        and allow selecting an installed core as an override."""
        platform_display = platform_name if platform_name else "this platform"
        available_cores = self.retroarch.get_available_cores() if hasattr(self, 'retroarch') and self.retroarch else {}

        if not available_cores:
            dialog = Adw.AlertDialog.new(
                "RetroArch Core Not Found",
                f"Cannot launch '{game_name}' because no RetroArch core is installed for {platform_display}.\n\n"
                f"Please install a compatible RetroArch core for {platform_display} and try again."
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.set_close_response("ok")
            dialog.present(self)
            return

        # We have installed cores on the system; allow user to select one to assign as override.
        dialog = Gtk.Dialog(
            title="RetroArch Core Not Found",
            transient_for=self,
            modal=True
        )
        dialog.set_default_size(480, 250)

        content_area = dialog.get_content_area()
        content_area.set_margin_top(16)
        content_area.set_margin_bottom(16)
        content_area.set_margin_start(16)
        content_area.set_margin_end(16)
        content_area.set_spacing(12)

        heading = Gtk.Label()
        heading.set_markup(f"<b>No default core matched for {html.escape(platform_display)}</b>")
        heading.set_halign(Gtk.Align.START)
        content_area.append(heading)

        desc = Gtk.Label()
        desc.set_text(f"Cannot launch '{game_name}'. Select one of your installed RetroArch cores to use for {platform_display}:")
        desc.set_wrap(True)
        desc.set_halign(Gtk.Align.START)
        content_area.append(desc)

        combo = Gtk.ComboBoxText()
        sorted_cores = sorted(available_cores.keys())
        current_override = self.retroarch.get_core_override(platform_name) if hasattr(self.retroarch, 'get_core_override') else ''

        default_idx = 0
        for i, c_name in enumerate(sorted_cores):
            combo.append_text(c_name)
            if c_name == current_override:
                default_idx = i
        combo.set_active(default_idx)
        content_area.append(combo)

        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_box.set_halign(Gtk.Align.END)
        button_box.set_margin_top(12)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda b: dialog.destroy())
        button_box.append(cancel_btn)

        assign_btn = Gtk.Button(label="Assign Core & Save")
        assign_btn.add_css_class("suggested-action")

        def on_assign_clicked(btn):
            sel_core = combo.get_active_text()
            if sel_core and platform_name:
                self.retroarch.set_core_override(platform_name, sel_core)
                self.log_message(f"✅ Set custom core override for {platform_name}: {sel_core}")
            dialog.destroy()

        assign_btn.connect("clicked", on_assign_clicked)
        button_box.append(assign_btn)
        content_area.append(button_box)

        dialog.present()

    def on_window_close_request(self, _window):
        """Quit application when window is closed via X"""
        try:
            app = self.get_application()
            if app:
                app.quit()
        except Exception:
            pass
        return False

    def cancel_download(self, rom_id):
        """Cancel an in-progress download. If part of a bulk operation, cancels all downloads.

        Args:
            rom_id: The ROM ID to cancel
        """
        with self._cancellation_lock:
            # Check if this is part of a bulk download
            if self._bulk_download_in_progress:
                # Cancel ALL downloads in the bulk operation
                self._bulk_download_cancelled = True

                # Mark all queued/downloading games for cancellation
                # Use _downloading_rom_ids which captures ALL games in the bulk operation
                if hasattr(self, '_downloading_rom_ids'):
                    for downloading_rom_id in self._downloading_rom_ids:
                        self._cancelled_downloads.add(downloading_rom_id)

                # Also mark currently active threads
                download_threads_keys = list(self._download_threads.keys())
                for downloading_rom_id in download_threads_keys:
                    self._cancelled_downloads.add(downloading_rom_id)

                self.log_message(f"Cancelling bulk download operation...")
                return True
            elif rom_id in self._download_threads:
                # Single download cancellation
                self._cancelled_downloads.add(rom_id)
                self.log_message(f"Cancelling download...")
                return True
            return False

    def download_regional_variants(self, regional_variants):
        """Download selected regional variants individually

        Args:
            regional_variants: List of dicts with 'disc' and 'game' keys
        """
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return

        self.log_message(f"Downloading {len(regional_variants)} regional variant(s)...")

        def download():
            try:
                parent_game = regional_variants[0]['game']
                parent_rom_id = parent_game.get('rom_id')
                platform_slug = parent_game.get('platform_slug', 'Unknown')
                # Use same download dir source as download_game()
                download_dir = Path(self.rom_dir_row.get_text())
                platform_dir = download_dir / platform_slug

                # Fetch parent ROM details to get the files array with file IDs
                from urllib.parse import urljoin
                parent_response = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, f'/api/roms/{parent_rom_id}'),
                    timeout=10
                )
                if parent_response.status_code != 200:
                    GLib.idle_add(lambda: self.log_message(f"⚠️ Could not fetch parent ROM details, downloading entire folder instead"))
                    # Fall back to downloading the entire parent folder
                    self.download_game(parent_game)
                    return

                parent_details = parent_response.json()
                parent_files = parent_details.get('files', [])

                # Use file_name (actual folder name on disk) matching process_single_rom() logic
                parent_folder_name = parent_game.get('file_name') or parent_game.get('name', 'unknown')
                local_folder = platform_dir / parent_folder_name
                local_folder.mkdir(parents=True, exist_ok=True)

                for variant_info in regional_variants:
                    variant = variant_info['disc']
                    child_rom_id = variant.get('rom_id')
                    rom_name = variant.get('name', 'Unknown')
                    full_fs_name = variant.get('full_fs_name', rom_name)

                    # Find the matching file in parent's files array
                    matching_file = None
                    for file_obj in parent_files:
                        # Match by filename only (rom_id in files array is parent's ID)
                        file_name = file_obj.get('filename') or file_obj.get('file_name', '')
                        if file_name == full_fs_name:
                            matching_file = file_obj
                            break

                    if not matching_file:
                        self.log_message(f"  ⚠️ Could not find file ID for {rom_name}, skipping")
                        continue

                    file_id = matching_file.get('id')
                    if not file_id:
                        self.log_message(f"  ⚠️ No file ID found for {rom_name}, skipping")
                        continue

                    self.log_message(f"  Downloading {rom_name} (file ID: {file_id})...")
                    self.log_message(f"  Target path: {local_folder / full_fs_name}")

                    # Initialize progress tracking for child only
                    self.log_message(f"  Initializing progress for child ROM ID: {child_rom_id}")

                    progress_data = {
                        'progress': 0.0,
                        'downloading': True,
                        'filename': rom_name,
                        'speed': 0,
                        'downloaded': 0,
                        'total': 0
                    }

                    # Track progress on child only
                    if child_rom_id:
                        self.download_progress[child_rom_id] = progress_data.copy()
                        self._last_progress_update[child_rom_id] = 0

                    # Update UI to show download starting on child only
                    if child_rom_id:
                        GLib.idle_add(lambda rid=child_rom_id: self.library_section.update_game_progress(rid, self.download_progress[rid])
                                    if hasattr(self, 'library_section') else None)

                    self.log_message(f"  🔄 Starting download_rom call...")

                    # Download using parent ROM ID + specific file ID
                    # Progress callback updates child only
                    def update_child_progress(progress):
                        if child_rom_id:
                            self.update_download_progress(progress, child_rom_id)

                    success, message = self.romm_client.download_rom(
                        parent_rom_id,  # Use parent ROM ID
                        full_fs_name,  # Use full filename with extension
                        local_folder,
                        progress_callback=update_child_progress,
                        file_ids=str(file_id)  # Specify which file to download
                    )

                    self.log_message(f"  ✅ download_rom returned!")

                    # Debug logging
                    self.log_message(f"  Download result: success={success}, message={message}")
                    file_path = local_folder / full_fs_name
                    self.log_message(f"  File exists after download: {file_path.exists()}")
                    if file_path.exists():
                        self.log_message(f"  File size: {file_path.stat().st_size} bytes")

                    if success:
                        self.log_message(f"  ✅ Downloaded {rom_name}")

                        # Mark download as complete for child only
                        current_progress = self.download_progress.get(child_rom_id, {})
                        file_size = current_progress.get('downloaded', 0)
                        if file_size == 0:
                            # Fallback: get actual file size
                            if file_path.exists():
                                file_size = file_path.stat().st_size

                        completion_data = {
                            'progress': 1.0,
                            'downloading': False,
                            'completed': True,
                            'filename': rom_name,
                            'downloaded': file_size,
                            'total': file_size
                        }

                        # Update child progress only
                        if child_rom_id:
                            self.download_progress[child_rom_id] = completion_data.copy()

                        # Force final UI update on child
                        if child_rom_id:
                            GLib.idle_add(lambda rid=child_rom_id: self.library_section.update_game_progress(rid, self.download_progress[rid])
                                        if hasattr(self, 'library_section') else None)

                        # Update parent game status immediately after each variant downloads
                        for i, existing_game in enumerate(self.available_games):
                            if existing_game.get('rom_id') == parent_rom_id:
                                # Update the parent's download status
                                folder_exists = local_folder.exists()
                                has_files = any(local_folder.iterdir()) if folder_exists else False
                                existing_game['is_downloaded'] = folder_exists and has_files
                                existing_game['local_path'] = str(local_folder)

                                # Calculate actual folder size
                                if local_folder.exists():
                                    existing_game['local_size'] = sum(f.stat().st_size for f in local_folder.rglob('*') if f.is_file())

                                self.available_games[i] = existing_game

                                # Debug logging

                                # Update UI with the modified game data
                                GLib.idle_add(lambda g=existing_game.copy(): self.library_section.update_single_game(g)
                                            if hasattr(self, 'library_section') else None)
                                break

                        # Clear progress after a delay for child only
                        def clear_progress():
                            import time
                            time.sleep(2)
                            if child_rom_id and child_rom_id in self.download_progress:
                                del self.download_progress[child_rom_id]

                        threading.Thread(target=clear_progress, daemon=True).start()
                    else:
                        self.log_message(f"  ❌ Failed to download {rom_name}: {message}")

                        # Mark download as failed for child only
                        if child_rom_id and child_rom_id in self.download_progress:
                            self.download_progress[child_rom_id]['downloading'] = False
                            GLib.idle_add(lambda rid=child_rom_id: self.library_section.update_game_progress(rid, self.download_progress[rid])
                                        if hasattr(self, 'library_section') else None)

            except Exception as e:
                import traceback
                self.log_message(f"⚠️ Error downloading regional variants: {e}")
                self.log_message(f"Traceback: {traceback.format_exc()}")
                self.log_message("Falling back to downloading entire folder")
                self.download_game(parent_game)
                return


            # Update parent game status in available_games
            rom_id = parent_game.get('rom_id')
            local_path = platform_dir / parent_folder_name


            # Find and update the game in available_games
            for i, existing_game in enumerate(self.available_games):
                if existing_game.get('rom_id') == rom_id:
                    # Update the parent's download status
                    is_dl = local_path.exists() and any(local_path.iterdir())
                    existing_game['is_downloaded'] = is_dl
                    existing_game['local_path'] = str(local_path)

                    # Calculate actual folder size
                    if local_path.exists():
                        existing_game['local_size'] = sum(f.stat().st_size for f in local_path.rglob('*') if f.is_file())

                    self.available_games[i] = existing_game

                    # Update UI directly with the modified game data
                    import copy
                    game_snapshot = copy.deepcopy(existing_game)

                    def update_ui(g=game_snapshot):
                        if hasattr(self, 'library_section'):
                            self.library_section.update_single_game(g)
                        return False

                    GLib.idle_add(update_ui)
                    break
        threading.Thread(target=download, daemon=True).start()


    def _download_via_parent_rom(self, game, file_name, platform_dir, progress_callback, cancellation_checker):
        """Download a child-file ROM via its parent folder ROM's endpoint + file_id.

        Used when a collection entry is a file stored inside a parent folder ROM
        (direct download via the child's own ROM ID gives HTTP 404).

        Returns (success, message, actual_path) where actual_path is the Path where
        the file was saved, or (False, None, None) if no suitable parent found.
        """
        from urllib.parse import urljoin

        def _attempt_parent(parent_data):
            """Try downloading file_name via a specific parent ROM dict."""
            parent_id = parent_data.get('id')
            if not parent_id:
                return False, None, None
            parent_files = parent_data.get('files', [])
            matching = next(
                (f for f in parent_files
                 if (f.get('filename') or f.get('file_name', '')) == file_name),
                None
            )
            if not matching or not matching.get('id'):
                return False, None, None
            file_id = matching['id']
            parent_folder_name = parent_data.get('fs_name') or parent_data.get('name', str(parent_id))
            actual_path = platform_dir / parent_folder_name / file_name
            download_path = platform_dir / file_name
            self.log_message(f"  ↩ Downloading via parent ROM {parent_id} (file_id={file_id})")
            success, message = self.romm_client.download_rom(
                parent_id, file_name, download_path,
                progress_callback=progress_callback,
                cancellation_checker=cancellation_checker,
                file_ids=str(file_id)
            )
            return success, message, actual_path

        # Fast path: use the pre-computed parent ROM stored at collection-load time.
        # This avoids extra API calls and works even when siblings[] omits the parent.
        if game.get('_parent_rom'):
            result = _attempt_parent(game['_parent_rom'])
            if result[0]:
                return result

        # Slow-path fallback: fetch each sibling and check if it is a folder ROM.
        for sib in game.get('_siblings', []):
            sib_id = sib.get('id')
            if not sib_id:
                continue
            try:
                resp = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, f'/api/roms/{sib_id}'),
                    timeout=10
                )
                if resp.status_code != 200:
                    continue
                sib_details = resp.json()
                if sib_details.get('fs_extension', ''):
                    continue  # not a folder ROM
                result = _attempt_parent(sib_details)
                if result[0]:
                    return result
            except Exception as e:
                print(f"_download_via_parent_rom: error checking sibling {sib_id}: {e}")

        return False, None, None

    def _download_folder_rom_bundle(self, rom_id, rom_name, platform_dir, parent_details,
                                    sibling_files, child_sizes, is_cancelled):
        """Download a multi-file 'folder' ROM as a single zip (one request).

        RomM serves the whole bundle — every disc/file plus a generated .m3u —
        from /api/roms/{id}/content with no file_ids. This mirrors grout's
        whole-ROM download and avoids per-file matching and manual playlist
        generation. The grouped sibling entries are this bundle's own contents,
        so they are marked complete rather than fetched again.

        Returns (success, message, local_folder).
        """
        fs_name = parent_details.get('fs_name') or rom_name
        local_folder = platform_dir / fs_name

        self.log_message(
            f"Downloading bundled ROM '{rom_name}' "
            f"({len(parent_details.get('files', []))} files) as a single archive…"
        )

        def progress_cb(progress):
            self.update_download_progress(progress, rom_id)

        # No file_ids → download_rom fetches the whole folder ROM as a zip and
        # extracts it (keeping RomM's bundled .m3u) into platform_dir / fs_name.
        success, message = self.romm_client.download_rom(
            rom_id, rom_name, local_folder,
            progress_callback=progress_cb,
            cancellation_checker=is_cancelled,
        )

        if success:
            for sib in sibling_files:
                cid = sib.get('id')
                if not cid:
                    continue
                csize = child_sizes.get(cid, 0)
                self.download_progress[cid] = {
                    'progress': 1.0, 'downloading': False, 'completed': True,
                    'filename': sib.get('name', ''), 'downloaded': csize, 'total': csize,
                }
                GLib.idle_add(lambda c=cid: self.library_section.update_game_progress(c, self.download_progress[c])
                              if hasattr(self, 'library_section') else None)

        return success, message, local_folder

    def _download_variant_roms_by_id(self, rom_id, rom_name, platform_dir, parent_details,
                                     sibling_files, child_sizes, is_cancelled):
        """Download a group of independent single-file ROMs (regional/version
        variants) by fetching each one by its own ROM id.

        Unlike a folder bundle, these siblings are NOT files inside the main ROM,
        so they cannot be resolved via the parent's file_ids. Each variant —
        including the main ROM — is a standalone ROM downloaded by id into a
        shared folder named after the game. A local .m3u is generated afterwards
        so multi-disc groups still support disc-swap.

        Returns (success, message, local_folder).
        """
        from urllib.parse import urljoin
        local_folder = platform_dir / rom_name
        local_folder.mkdir(parents=True, exist_ok=True)

        # Main ROM first, then each grouped sibling — all standalone ROMs.
        variants = [{'id': rom_id, 'name': rom_name, '_details': parent_details}]
        variants += list(sibling_files)
        total = len(variants)

        completed = 0
        for idx, variant in enumerate(variants):
            if is_cancelled():
                return False, "Download cancelled", local_folder

            vid = variant.get('id')
            vname = variant.get('name', 'Unknown')
            if not vid:
                continue

            # Resolve the on-disk filename from the variant's own metadata.
            details = variant.get('_details')
            if details is None:
                try:
                    details = self.romm_client.session.get(
                        urljoin(self.romm_client.base_url, f'/api/roms/{vid}'), timeout=10
                    ).json()
                except Exception:
                    details = {}
            file_name = details.get('fs_name') or vname
            dest = local_folder / file_name

            self.log_message(f"  [{idx+1}/{total}] Downloading {vname}…")

            def progress_cb(progress, cid=vid):
                self.update_download_progress(progress, cid)

            v_success, v_message = self.romm_client.download_rom(
                vid, file_name, dest,
                progress_callback=progress_cb,
                cancellation_checker=is_cancelled,
            )

            if not v_success:
                return False, f"Failed to download {vname}: {v_message}", local_folder

            completed += 1
            csize = child_sizes.get(vid, 0)
            self.download_progress[vid] = {
                'progress': 1.0, 'downloading': False, 'completed': True,
                'filename': vname, 'downloaded': csize, 'total': csize,
            }
            GLib.idle_add(lambda c=vid: self.library_section.update_game_progress(c, self.download_progress[c])
                          if hasattr(self, 'library_section') else None)

        if completed == 0:
            return False, "No variants could be downloaded", local_folder

        # Multi-disc groups arrive as separate files with no playlist; generate
        # one so disc-swap works (the whole-zip path gets RomM's bundled .m3u).
        try:
            m3u = self.retroarch.ensure_m3u_for_disc_folder(local_folder, rom_name)
            if m3u:
                self.log_message(f"  🎵 Created multi-disc playlist: {m3u.name}")
        except Exception as e:
            logging.warning(f"Could not generate .m3u for {local_folder}: {e}")

        return True, "Download complete", local_folder

    def download_game(self, game, is_bulk_operation=False):
        """Download a single game from RomM and its saves (with BIOS check)"""

        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return

        # Check if bulk download has been cancelled
        if is_bulk_operation and self._bulk_download_cancelled:
            return  # Don't start new downloads if bulk operation is cancelled
        
        # Check BIOS requirements first if enabled
        auto_download_setting = self.settings.get('Download', 'auto_download_bios', fallback='true')
        has_bios_manager = bool(self.retroarch.bios_manager)

        self.log_message(f"🔍 BIOS auto-download setting: {auto_download_setting}")
        self.log_message(f"🔍 BIOS manager available: {has_bios_manager}")

        # Default to enabled if not set or empty
        auto_download_enabled = auto_download_setting in ['true', '', None]
        if (auto_download_enabled and has_bios_manager):
            platform = game.get('platform')
            if platform:
                self.log_message(f"🔍 Checking BIOS for platform: {platform}")
                normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                self.log_message(f"🔍 Normalized platform: {normalized}")
                
                present, missing = self.retroarch.bios_manager.check_platform_bios(normalized)
                required_missing = [b for b in missing if not b.get('optional', False)]
                
                self.log_message(f"🔍 Required missing BIOS: {len(required_missing)}")
                
                if required_missing:
                    self.log_message(f"📋 Auto-downloading BIOS for {platform}...")
                    
                    # Set RomM client
                    self.retroarch.bios_manager.romm_client = self.romm_client
                    
                    # Download all missing BIOS for this platform
                    if self.retroarch.bios_manager.auto_download_missing_bios(normalized):
                        self.log_message(f"✅ BIOS ready for {platform}")
                    else:
                        self.log_message(f"⚠️ Some BIOS files unavailable for {platform}")
                else:
                    self.log_message(f"✅ All required BIOS already present for {platform}")
        else:
            self.log_message(f"⚠️ BIOS auto-download disabled or manager unavailable")
        
        def download():
            try:
                rom_name = game['name']
                rom_id = game['rom_id']
                platform = game['platform']
                platform_slug = game.get('platform_slug', platform)
                file_name = game['file_name']

                # Skip if another download path is already handling this ROM
                if self.download_progress.get(rom_id, {}).get('downloading'):
                    return

                # Track current download for progress updates
                self._current_download_rom_id = rom_id

                # Track this download thread
                current_thread = threading.current_thread()
                with self._cancellation_lock:
                    self._download_threads[rom_id] = current_thread
                    # Ensure this download is not marked as cancelled
                    self._cancelled_downloads.discard(rom_id)

                # Calculate file sizes for progress tracking FIRST
                child_sizes = {}
                total_children_size = 0
                if game.get('_sibling_files'):
                    for sibling in game['_sibling_files']:
                        child_id = sibling.get('id')
                        child_size = sibling.get('fs_size_bytes', 0)
                        if child_id:
                            child_sizes[child_id] = child_size
                            total_children_size += child_size

                # Initialize progress and throttling for this game
                progress_data = {
                    'progress': 0.0,
                    'downloading': True,
                    'filename': rom_name,
                    'speed': 0,
                    'downloaded': 0,
                    'total': 0
                }
                
                # For regional variants, set parent total to sum of all children
                if game.get('_sibling_files'):
                    progress_data['total'] = total_children_size
                    progress_data['filename'] = f"{rom_name} (0/{len(game['_sibling_files'])} variants)"
                
                self.download_progress[rom_id] = progress_data.copy()
                self._last_progress_update[rom_id] = 0  # Reset throttling

                # If downloading parent with regional variants, also initialize progress for children
                child_variant_ids = []
                if game.get('_sibling_files'):
                    for sibling in game['_sibling_files']:
                        child_rom_id = sibling.get('id')
                        if child_rom_id:
                            child_variant_ids.append(child_rom_id)
                            # Initialize each child with its OWN individual size, not parent's total
                            child_progress_data = progress_data.copy()
                            child_progress_data['total'] = child_sizes.get(child_rom_id, 0)
                            self.download_progress[child_rom_id] = child_progress_data
                            self._last_progress_update[child_rom_id] = 0

                # Update tree view to show download starting
                GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                            if hasattr(self, 'library_section') else None)

                # Update children progress too
                for child_id in child_variant_ids:
                    GLib.idle_add(lambda cid=child_id: self.library_section.update_game_progress(cid, self.download_progress[cid])
                                if hasattr(self, 'library_section') else None)

                # Update action buttons to show "Cancel" button
                GLib.idle_add(lambda: self.library_section.update_action_buttons()
                            if hasattr(self, 'library_section') else None)
                
                # Get download directory and create platform directory
                download_dir = Path(self.rom_dir_row.get_text())
                # Use platform slug directly (RomM and RetroDECK now use the same slugs)
                platform_dir = download_dir / platform_slug
                platform_dir.mkdir(parents=True, exist_ok=True)
                download_path = platform_dir / file_name

                # Download with throttled progress tracking and cancellation support
                def is_cancelled():
                    with self._cancellation_lock:
                        return rom_id in self._cancelled_downloads

                # Check if this is a multi-file ROM with regional variants
                sibling_local_folder = None
                if game.get('_sibling_files'):
                    # Resolve how this grouped ROM should be downloaded. A "folder"
                    # parent (multiple entries in its `files` array, or multi=true) is
                    # a single bundle: download it as one zip so we get every disc/file
                    # plus RomM's generated .m3u in one request. Otherwise the group is
                    # a set of independent single-file ROMs (regional/version variants)
                    # that must each be fetched by their own ROM id.
                    from urllib.parse import urljoin
                    try:
                        parent_details = self.romm_client.session.get(
                            urljoin(self.romm_client.base_url, f'/api/roms/{rom_id}'),
                            timeout=10
                        ).json()
                    except Exception as e:
                        parent_details = None
                        GLib.idle_add(lambda msg=str(e): self.log_message(f"⚠️ Could not fetch ROM details: {msg}"))

                    if not parent_details:
                        success = False
                        message = "Failed to fetch ROM details"
                    else:
                        parent_files = parent_details.get('files', [])
                        is_folder_rom = bool(parent_details.get('multi')) or len(parent_files) > 1

                        if is_folder_rom:
                            success, message, sibling_local_folder = self._download_folder_rom_bundle(
                                rom_id, rom_name, platform_dir, parent_details,
                                game['_sibling_files'], child_sizes, is_cancelled,
                            )
                        else:
                            success, message, sibling_local_folder = self._download_variant_roms_by_id(
                                rom_id, rom_name, platform_dir, parent_details,
                                game['_sibling_files'], child_sizes, is_cancelled,
                            )

                        if success and sibling_local_folder:
                            game['is_downloaded'] = True
                            game['local_path'] = str(sibling_local_folder)
                            if sibling_local_folder.exists():
                                game['local_size'] = sum(
                                    f.stat().st_size for f in sibling_local_folder.rglob('*') if f.is_file()
                                )
                else:
                    # Single file download - use existing logic
                    def update_all_progress(progress):
                        self.update_download_progress(progress, rom_id)

                    success, message = self.romm_client.download_rom(
                        rom_id, rom_name, download_path,
                        progress_callback=update_all_progress,
                        cancellation_checker=is_cancelled
                    )

                    # If direct download gave 404 and this ROM has siblings, it is
                    # likely a child file stored inside a parent folder ROM.  Try
                    # to locate the parent and download via parent ID + file_id.
                    if not success and 'HTTP 404' in message and game.get('_fs_extension') and (game.get('_siblings') or game.get('_parent_rom')):
                        self.log_message(f"  ↩ Direct download failed (404); trying via parent folder ROM...")
                        parent_success, parent_message, parent_path = self._download_via_parent_rom(
                            game, file_name, platform_dir, update_all_progress, is_cancelled
                        )
                        if parent_success:
                            success = True
                            message = parent_message
                            # Update download_path so success handling records the correct local_path
                            if parent_path:
                                download_path = parent_path

                if success:
                    # Mark download complete
                    if game.get('_sibling_files'):
                        # Multi-file download - use total of all children
                        file_size = total_children_size
                    else:
                        # Single file download
                        current_progress = self.download_progress.get(rom_id, {})
                        if current_progress.get('downloaded', 0) > 0:
                            file_size = current_progress['downloaded']
                        else:
                            file_size = download_path.stat().st_size if download_path.exists() else 0

                    # Mark parent as complete
                    self.download_progress[rom_id] = {
                        'progress': 1.0,
                        'downloading': False,
                        'completed': True,
                        'filename': rom_name,
                        'downloaded': file_size,
                        'total': file_size
                    }

                    # For single-file downloads, no children to update
                    # For multi-file downloads, children were already marked complete individually
                    if not game.get('_sibling_files'):
                        # Single file - no children
                        pass
                    # If multi-file, children were already marked complete in download loop

                    # Force final update for parent
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                if hasattr(self, 'library_section') else None)

                    # Update action buttons back to "Download" or "Launch"
                    # Don't update if bulk download is in progress - keep "Cancel All" button
                    if not is_bulk_operation:
                        GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                    if hasattr(self, 'library_section') else None)
                    
                    # Rest of success handling...
                    # Always update game status after successful download
                    if True:  # Changed from download_path.exists() to handle multi-disc folders
                        size_str = f"{file_size / (1024*1024*1024):.1f} GB" if file_size > 1024*1024*1024 else f"{file_size / (1024*1024):.1f} MB" if file_size > 1024*1024 else f"{file_size / 1024:.1f} KB"
                        
                        GLib.idle_add(lambda n=rom_name, s=size_str:
                                    self.log_message(f"✓ Downloaded {n} ({s})"))

                        # Update game data
                        game['is_downloaded'] = True

                        # Check if download created a folder (multi-file game)
                        actual_folder = platform_dir / rom_name

                        # Update disc status for multi-disc games
                        if game.get('is_multi_disc') and game.get('discs'):
                            # For multi-disc, RomM client creates folder with game name
                            game['local_path'] = str(actual_folder)
                            for disc in game['discs']:
                                disc['is_downloaded'] = True
                                disc_path = actual_folder / disc.get('name', '')
                                if disc_path.exists():
                                    # Calculate total size including all related files (e.g., .bin + .cue)
                                    disc['size'] = self.get_disc_total_size(disc_path, actual_folder)
                        # Update status for grouped variants / bundles
                        elif game.get('_sibling_files'):
                            # Use the actual folder the download helper wrote to: a
                            # folder bundle uses the ROM's fs_name, the by-id path uses
                            # the game name. Fall back to the name-based path.
                            game['local_path'] = str(sibling_local_folder or actual_folder)
                            # Note: We don't update _sibling_files here because they're API data
                            # The UI will check file existence in rebuild_children() when displaying
                        elif actual_folder.exists() and actual_folder.is_dir():
                            # Multi-file game (folder exists with game name)
                            game['local_path'] = str(actual_folder)
                        elif download_path.exists():
                            # Single file download
                            game['local_path'] = str(download_path)
                        else:
                            # Fallback - use folder path if download_path doesn't exist
                            game['local_path'] = str(actual_folder)

                        game['local_size'] = file_size

                        # Update UI - update both the underlying games list AND current view
                        def update_ui():
                            # ALWAYS update the underlying available_games list first
                            for i, existing_game in enumerate(self.available_games):
                                if existing_game.get('rom_id') == game.get('rom_id'):
                                    self.available_games[i] = game
                                    break

                            # Update platform item directly
                            if hasattr(self, 'library_section'):
                                for j in range(self.library_section.library_model.root_store.get_n_items()):
                                    platform_item = self.library_section.library_model.root_store.get_item(j)
                                    if isinstance(platform_item, PlatformItem):
                                        for k, platform_game in enumerate(platform_item.games):
                                            if platform_game.get('rom_id') == game.get('rom_id'):
                                                platform_item.games[k] = game
                                                platform_item.notify('status-text')
                                                platform_item.notify('size-text')
                                                break
                            
                            # Update collections view data if in collections mode
                            if (hasattr(self.library_section, 'current_view_mode') and 
                                self.library_section.current_view_mode == 'collection'):
                                
                                # Update ALL instances of this game in collections_games list
                                if hasattr(self.library_section, 'collections_games'):
                                    updated_collections = set()  # Track which collections were updated
                                    
                                    for i, collection_game in enumerate(self.library_section.collections_games):
                                        if collection_game.get('rom_id') == game.get('rom_id'):
                                            updated_collection_game = game.copy()
                                            updated_collection_game['collection'] = collection_game.get('collection')
                                            self.library_section.collections_games[i] = updated_collection_game
                                            updated_collections.add(collection_game.get('collection'))
                                    
                                    # ADD THIS: Force property updates on affected collection platform items
                                    def force_collection_updates():
                                        model = self.library_section.library_model.tree_model
                                        for i in range(model.get_n_items() if model else 0):
                                            tree_item = model.get_item(i)
                                            if tree_item and tree_item.get_depth() == 0:  # Collection level
                                                platform_item = tree_item.get_item()
                                                if isinstance(platform_item, PlatformItem):
                                                    if platform_item.platform_name in updated_collections:
                                                        # Force property notifications to update Status/Size
                                                        platform_item.notify('status-text')
                                                        platform_item.notify('size-text')
                                        return False
                                    
                                    GLib.timeout_add(150, force_collection_updates)                          

                            # Call update_single_game as fallback
                            self.library_section.update_single_game(game, skip_platform_update=is_bulk_operation)

                        GLib.idle_add(update_ui)

                        # Update the GameItem
                        def update_game_item():
                            model = self.library_section.library_model.tree_model

                            for i in range(model.get_n_items() if model else 0):
                                tree_item = model.get_item(i)
                                if tree_item and tree_item.get_depth() == 1:  # Game level
                                    item = tree_item.get_item()
                                    if isinstance(item, GameItem):
                                        if item.game_data.get('rom_id') == rom_id:
                                            # Update data
                                            item.game_data.update(game)

                                            # Rebuild children and notify UI of changes
                                            item.rebuild_children()
                                            item.notify('is-downloaded')
                                            item.notify('size-text')

                                            break

                            return False

                        GLib.idle_add(update_game_item)

                        # Bulk operation handling
                        if is_bulk_operation and hasattr(self, 'library_section'):
                            def update_bulk_progress():
                                if hasattr(self, '_bulk_download_remaining'):
                                    self._bulk_download_remaining -= 1
                                    remaining = self._bulk_download_remaining
                                    
                                    if remaining > 0:
                                        GLib.idle_add(lambda r=remaining: 
                                            self.library_section.selection_label.set_text(f"{r} downloads remaining") 
                                            if hasattr(self.library_section, 'selection_label') else None)
                                    else:
                                        GLib.idle_add(lambda: 
                                            self.library_section.selection_label.set_text("Downloads complete") 
                                            if hasattr(self.library_section, 'selection_label') else None)
                            
                            GLib.idle_add(update_bulk_progress)

                        # Clear checkbox selections for individual downloads, but preserve row selections
                        if not is_bulk_operation and hasattr(self, 'library_section'):
                            def clear_only_checkboxes():
                                # Only clear checkbox selections if there's no row selection
                                # If user clicked on a row and downloaded, they probably want to keep it selected to launch
                                if not self.library_section.selected_game:
                                    self.library_section.clear_checkbox_selections_smooth()
                                else:
                                    # Just clear checkboxes but keep the row selection
                                    self.library_section.selected_checkboxes.clear()
                                    self.library_section.selected_rom_ids.clear()
                                    self.library_section.selected_game_keys.clear()
                                    # Update UI to reflect cleared checkboxes but keep row selection
                                    self.library_section.update_action_buttons()
                                    self.library_section.update_selection_label()
                                    GLib.idle_add(self.library_section.force_checkbox_sync)
                            
                            GLib.idle_add(clear_only_checkboxes)
                        
                        if file_size >= 1024:
                            GLib.idle_add(lambda n=rom_name: self.log_message(f"✓ {n} ready to play"))
                
                else:
                    # Check if this was a cancellation
                    was_cancelled = (message == "cancelled")

                    if was_cancelled:
                        # Mark download as cancelled
                        self.download_progress[rom_id] = {
                            'progress': 0.0,
                            'downloading': False,
                            'cancelled': True,
                            'filename': rom_name
                        }

                        # Clean up partial download file
                        if download_path.exists():
                            try:
                                if download_path.is_file():
                                    download_path.unlink()
                                elif download_path.is_dir():
                                    shutil.rmtree(download_path)
                            except Exception as e:
                                print(f"Failed to clean up partial download: {e}")

                        GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                    if hasattr(self, 'library_section') else None)

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        # Decrement bulk download counter for cancelled downloads too
                        if is_bulk_operation and hasattr(self, '_bulk_download_remaining'):
                            self._bulk_download_remaining -= 1

                        GLib.idle_add(lambda n=rom_name:
                                    self.log_message(f"⊗ Cancelled download: {n}"))
                    else:
                        # Mark download failed
                        self.download_progress[rom_id] = {
                            'progress': 0.0,
                            'downloading': False,
                            'failed': True,
                            'filename': rom_name
                        }

                        GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                    if hasattr(self, 'library_section') else None)

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        GLib.idle_add(lambda n=rom_name, m=message:
                                    self.log_message(f"✗ Failed to download {n}: {m}"))
                
                # Clean up progress and throttling data
                def cleanup_progress():
                    time.sleep(3)  # Show completed/failed state for 3 seconds

                    # More thorough cleanup for parent
                    if rom_id in self.download_progress:
                        del self.download_progress[rom_id]
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]

                    # Also clean up all children (if any)
                    if 'child_variant_ids' in locals() or 'child_variant_ids' in dir():
                        for child_id in child_variant_ids:
                            if child_id in self.download_progress:
                                del self.download_progress[child_id]
                            if child_id in self._last_progress_update:
                                del self._last_progress_update[child_id]

                    # Clean up download thread tracking
                    with self._cancellation_lock:
                        if rom_id in self._download_threads:
                            del self._download_threads[rom_id]
                        self._cancelled_downloads.discard(rom_id)

                    # Clean up current download tracking
                    if hasattr(self, '_current_download_rom_id') and self._current_download_rom_id == rom_id:
                        delattr(self, '_current_download_rom_id')

                    # Clear progress for parent
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, None)
                                if hasattr(self, 'library_section') else None)

                    # Clear progress for all children (if any)
                    if 'child_variant_ids' in locals() or 'child_variant_ids' in dir():
                        for child_id in child_variant_ids:
                            GLib.idle_add(lambda cid=child_id: self.library_section.update_game_progress(cid, None)
                                        if hasattr(self, 'library_section') else None)

                    # Force garbage collection for large downloads
                    import gc
                    gc.collect()

                threading.Thread(target=cleanup_progress, daemon=True).start()

            except Exception as e:
                import traceback
                traceback.print_exc()
                # Handle error state
                if hasattr(self, '_current_download_rom_id'):
                    rom_id = self._current_download_rom_id
                    self.download_progress[rom_id] = {
                        'progress': 0.0,
                        'downloading': False,
                        'failed': True,
                        'filename': game.get('name', 'Unknown')
                    }
                    # Clean up throttling data on error
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]

                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                if hasattr(self, 'library_section') else None)

                # Schedule cleanup of download_progress so wait_and_update can unblock.
                # The normal path calls cleanup_progress() defined inside the try block,
                # but exceptions bypass that — without this the entry lingers forever.
                _exc_rom_id = locals().get('rom_id') or getattr(self, '_current_download_rom_id', None)
                if _exc_rom_id:
                    def _exc_cleanup(rid=_exc_rom_id):
                        time.sleep(3)
                        self.download_progress.pop(rid, None)
                        self._last_progress_update.pop(rid, None)
                    threading.Thread(target=_exc_cleanup, daemon=True).start()

                GLib.idle_add(lambda err=str(e), n=game['name']:
                            self.log_message(f"Download error for {n}: {err}"))
        
        threading.Thread(target=download, daemon=True).start()

    def download_game_controlled(self, game, semaphore, is_bulk_operation=False, on_complete=None):
        """Download with semaphore already acquired - releases when complete"""
        def download():
            success = False  # Track success for on_complete callback
            try:
                # Check if bulk download has been cancelled before starting
                if is_bulk_operation and self._bulk_download_cancelled:
                    semaphore.release()
                    if on_complete:
                        on_complete(False)
                    return  # Don't start if bulk operation is cancelled

                rom_name = game['name']
                rom_id = game['rom_id']
                platform = game['platform']
                platform_slug = game.get('platform_slug', platform)
                file_name = game['file_name']

                # Skip if another download path is already handling this ROM
                if self.download_progress.get(rom_id, {}).get('downloading'):
                    semaphore.release()
                    if on_complete:
                        on_complete(True)  # treat as success — already in progress
                    return

                # Track current download for progress updates
                self._current_download_rom_id = rom_id

                # Initialize progress and throttling for this game
                self.download_progress[rom_id] = {
                    'progress': 0.0,
                    'downloading': True,
                    'filename': rom_name,
                    'speed': 0,
                    'downloaded': 0,
                    'total': 0
                }
                self._last_progress_update[rom_id] = 0  # Reset throttling

                # Update tree view to show download starting
                GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                            if hasattr(self, 'library_section') else None)

                # Get download directory and create platform directory
                download_dir = Path(self.rom_dir_row.get_text())
                # Use platform slug directly (RomM and RetroDECK now use the same slugs)
                platform_dir = download_dir / platform_slug
                platform_dir.mkdir(parents=True, exist_ok=True)
                download_path = platform_dir / file_name

                # Log file size for large downloads
                try:
                    # Try to get file size from ROM data
                    romm_data = game.get('romm_data', {})
                    expected_size = romm_data.get('fs_size_bytes', 0)
                except Exception:
                    pass

                # Download with throttled progress tracking and cancellation support
                def is_cancelled():
                    with self._cancellation_lock:
                        return rom_id in self._cancelled_downloads

                download_success, message = self.romm_client.download_rom(
                    rom_id, rom_name, download_path,
                    progress_callback=lambda progress: self.update_download_progress(progress, rom_id),
                    cancellation_checker=is_cancelled
                )

                # Child-file variants cannot be downloaded via their own ROM ID (404).
                # Fall back to downloading via the parent folder ROM + file_id.
                if not download_success and 'HTTP 404' in (message or '') and game.get('_fs_extension') and (game.get('_parent_rom') or game.get('_siblings')):
                    self.log_message(f"  ↩ Direct download 404; trying via parent folder ROM...")
                    _p_success, _p_msg, _p_path = self._download_via_parent_rom(
                        game, file_name, platform_dir,
                        lambda progress: self.update_download_progress(progress, rom_id),
                        is_cancelled
                    )
                    if _p_success:
                        download_success = True
                        message = _p_msg
                        if _p_path:
                            download_path = _p_path

                if download_success:
                    success = True
                    # Mark download complete
                    current_progress = self.download_progress.get(rom_id, {})
                    if current_progress.get('downloaded', 0) > 0:
                        # Keep the original download size from the download process
                        file_size = current_progress['downloaded']
                    else:
                        # Fallback for single files
                        file_size = download_path.stat().st_size if download_path.exists() else 0

                    self.download_progress[rom_id] = {
                        'progress': 1.0,
                        'downloading': False,
                        'completed': True,
                        'filename': rom_name,
                        'downloaded': file_size,
                        'total': file_size
                    }
                    
                    # Force final update
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                if hasattr(self, 'library_section') else None)

                    # Update action buttons back to "Download" or "Launch"
                    # Don't update if bulk download is in progress - keep "Cancel All" button
                    if not is_bulk_operation:
                        GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                    if hasattr(self, 'library_section') else None)
                    
                    # Rest of success handling...
                    # Always update game status after successful download
                    if True:  # Changed from download_path.exists() to handle multi-disc folders
                        size_str = f"{file_size / (1024*1024*1024):.1f} GB" if file_size > 1024*1024*1024 else f"{file_size / (1024*1024):.1f} MB" if file_size > 1024*1024 else f"{file_size / 1024:.1f} KB"
                        
                        GLib.idle_add(lambda n=rom_name, s=size_str:
                                    self.log_message(f"✓ Downloaded {n} ({s})"))

                        # Update game data
                        game['is_downloaded'] = True

                        # Update disc status for multi-disc games
                        if game.get('is_multi_disc') and game.get('discs'):
                            # For multi-disc, RomM client creates folder with game name
                            actual_folder = platform_dir / rom_name
                            game['local_path'] = str(actual_folder)
                            for disc in game['discs']:
                                disc['is_downloaded'] = True
                                disc_path = actual_folder / disc.get('name', '')
                                if disc_path.exists():
                                    # Calculate total size including all related files (e.g., .bin + .cue)
                                    disc['size'] = self.get_disc_total_size(disc_path, actual_folder)
                        else:
                            game['local_path'] = str(download_path)

                        game['local_size'] = file_size

                        # Update UI
                        def update_ui():
                            # Update master games list
                            for i, existing_game in enumerate(self.available_games):
                                if existing_game.get('rom_id') == game.get('rom_id'):
                                    self.available_games[i] = game
                                    break

                            # Update platform item directly
                            if hasattr(self, 'library_section'):
                                for j in range(self.library_section.library_model.root_store.get_n_items()):
                                    platform_item = self.library_section.library_model.root_store.get_item(j)
                                    if isinstance(platform_item, PlatformItem):
                                        for k, platform_game in enumerate(platform_item.games):
                                            if platform_game.get('rom_id') == game.get('rom_id'):
                                                platform_item.games[k] = game
                                                platform_item.notify('status-text')
                                                platform_item.notify('size-text')
                                                break
                            
                            # Update collections cache AND platform item data
                            if (hasattr(self.library_section, 'current_view_mode') and
                                self.library_section.current_view_mode == 'collection'):

                                # Update collections_games cache
                                updated_any = False
                                if hasattr(self.library_section, 'collections_games'):
                                    for i, collection_game in enumerate(self.library_section.collections_games):
                                        if collection_game.get('rom_id') == game.get('rom_id'):
                                            updated_game = game.copy()
                                            updated_game['collection'] = collection_game.get('collection')
                                            self.library_section.collections_games[i] = updated_game
                                            updated_any = True

                                # Update the actual PlatformItem.games data in the tree model
                                def update_platform_items():
                                    model = self.library_section.library_model.tree_model
                                    if model:
                                        for i in range(model.get_n_items()):
                                            tree_item = model.get_item(i)
                                            if tree_item and tree_item.get_depth() == 0:  # Collection level
                                                platform_item = tree_item.get_item()
                                                if isinstance(platform_item, PlatformItem):
                                                    # Update games in this platform item
                                                    for j, platform_game in enumerate(platform_item.games):
                                                        if platform_game.get('rom_id') == game.get('rom_id'):
                                                            platform_item.games[j] = game.copy()
                                                            platform_item.games[j]['collection'] = platform_item.platform_name

                                                    # Force property recalculation
                                                    platform_item.notify('status-text')
                                                    platform_item.notify('size-text')
                                    return False

                                GLib.timeout_add(200, update_platform_items)
                            
                            # Update the GameItem directly with proper notifications
                            model = self.library_section.library_model.tree_model
                            for i in range(model.get_n_items() if model else 0):
                                tree_item = model.get_item(i)
                                if tree_item and tree_item.get_depth() == 1:
                                    item = tree_item.get_item()
                                    if isinstance(item, GameItem) and item.game_data.get('rom_id') == game.get('rom_id'):
                                        import copy
                                        item.game_data = copy.deepcopy(game)
                                        # Rebuild children for multi-disc games to update disc status
                                        if item.game_data.get('is_multi_disc', False):
                                            item.rebuild_children()
                                        # Trigger property notifications to refresh UI
                                        item.notify('name')
                                        item.notify('is-downloaded')
                                        item.notify('size-text')

                            # Clear selections after download completes
                            # Don't clear selections during bulk downloads - wait until all complete
                            if not is_bulk_operation and hasattr(self, 'library_section'):
                                def clear_selections():
                                    self.library_section.selected_checkboxes.clear()
                                    self.library_section.selected_rom_ids.clear()
                                    self.library_section.selected_game_keys.clear()
                                    self.library_section.selected_game = None
                                    self.library_section.update_action_buttons()
                                    self.library_section.update_selection_label()
                                    self.library_section.force_checkbox_sync()

                                # Clear selections after a short delay
                                GLib.timeout_add(1000, lambda: (clear_selections(), False)[1])

                        GLib.idle_add(update_ui)

                        # Update collection sync status if this game is part of a collection
                        if not is_bulk_operation and hasattr(self.library_section, 'current_view_mode') and self.library_section.current_view_mode == 'collection':
                            def update_collection_status():
                                # Find which collection this game belongs to
                                collection_name = game.get('collection')
                                if collection_name and hasattr(self.library_section, 'update_collection_sync_status'):
                                    self.library_section.update_collection_sync_status(collection_name)
                                return False
                            GLib.idle_add(update_collection_status)

                        # Bulk operation handling
                        if is_bulk_operation and hasattr(self, 'library_section'):
                            def update_bulk_progress():
                                if hasattr(self, '_bulk_download_remaining'):
                                    self._bulk_download_remaining -= 1
                                    remaining = self._bulk_download_remaining
                                    
                                    if remaining > 0:
                                        GLib.idle_add(lambda r=remaining: 
                                            self.library_section.selection_label.set_text(f"{r} downloads remaining") 
                                            if hasattr(self.library_section, 'selection_label') else None)
                                    else:
                                        GLib.idle_add(lambda: 
                                            self.library_section.selection_label.set_text("Downloads complete") 
                                            if hasattr(self.library_section, 'selection_label') else None)
                            
                            GLib.idle_add(update_bulk_progress)

                        # Clear checkbox selections for individual downloads, but preserve row selections
                        if not is_bulk_operation and hasattr(self, 'library_section'):
                            def clear_only_checkboxes():
                                # Only clear checkbox selections if there's no row selection
                                # If user clicked on a row and downloaded, they probably want to keep it selected to launch
                                if not self.library_section.selected_game:
                                    self.library_section.clear_checkbox_selections_smooth()
                                else:
                                    # Just clear checkboxes but keep the row selection
                                    self.library_section.selected_checkboxes.clear()
                                    self.library_section.selected_rom_ids.clear()
                                    self.library_section.selected_game_keys.clear()
                                    # Update UI to reflect cleared checkboxes but keep row selection
                                    self.library_section.update_action_buttons()
                                    self.library_section.update_selection_label()
                                    GLib.idle_add(self.library_section.force_checkbox_sync)
                            
                            GLib.idle_add(clear_only_checkboxes)
                        
                        if file_size >= 1024:
                            GLib.idle_add(lambda n=rom_name: self.log_message(f"✓ {n} ready to play"))
                
                else:
                    # Check if this was a cancellation
                    was_cancelled = (message == "cancelled")

                    if was_cancelled:
                        # Mark download as cancelled
                        self.download_progress[rom_id] = {
                            'progress': 0.0,
                            'downloading': False,
                            'cancelled': True,
                            'filename': rom_name
                        }

                        # Clean up partial download file
                        if download_path.exists():
                            try:
                                if download_path.is_file():
                                    download_path.unlink()
                                elif download_path.is_dir():
                                    shutil.rmtree(download_path)
                            except Exception as e:
                                print(f"Failed to clean up partial download: {e}")

                        GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                    if hasattr(self, 'library_section') else None)

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        # Decrement bulk download counter for cancelled downloads too
                        if is_bulk_operation and hasattr(self, '_bulk_download_remaining'):
                            self._bulk_download_remaining -= 1

                        GLib.idle_add(lambda n=rom_name:
                                    self.log_message(f"⊗ Cancelled download: {n}"))
                    else:
                        # Mark download failed
                        self.download_progress[rom_id] = {
                            'progress': 0.0,
                            'downloading': False,
                            'failed': True,
                            'filename': rom_name
                        }

                        GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                    if hasattr(self, 'library_section') else None)

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        GLib.idle_add(lambda n=rom_name, m=message:
                                    self.log_message(f"✗ Failed to download {n}: {m}"))
                
                # Clean up progress and throttling data
                def cleanup_progress():
                    time.sleep(3)  # Show completed/failed state for 3 seconds

                    # More thorough cleanup for parent
                    if rom_id in self.download_progress:
                        del self.download_progress[rom_id]
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]

                    # Also clean up all children (if any)
                    if 'child_variant_ids' in locals() or 'child_variant_ids' in dir():
                        for child_id in child_variant_ids:
                            if child_id in self.download_progress:
                                del self.download_progress[child_id]
                            if child_id in self._last_progress_update:
                                del self._last_progress_update[child_id]

                    # Clean up download thread tracking
                    with self._cancellation_lock:
                        if rom_id in self._download_threads:
                            del self._download_threads[rom_id]
                        self._cancelled_downloads.discard(rom_id)

                    # Clean up current download tracking
                    if hasattr(self, '_current_download_rom_id') and self._current_download_rom_id == rom_id:
                        delattr(self, '_current_download_rom_id')

                    # Clear progress for parent
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, None)
                                if hasattr(self, 'library_section') else None)

                    # Clear progress for all children (if any)
                    if 'child_variant_ids' in locals() or 'child_variant_ids' in dir():
                        for child_id in child_variant_ids:
                            GLib.idle_add(lambda cid=child_id: self.library_section.update_game_progress(cid, None)
                                        if hasattr(self, 'library_section') else None)

                    # Force garbage collection for large downloads
                    import gc
                    gc.collect()

                threading.Thread(target=cleanup_progress, daemon=True).start()
                
            except Exception as e:
                # Handle error state
                if hasattr(self, '_current_download_rom_id'):
                    rom_id = self._current_download_rom_id
                    self.download_progress[rom_id] = {
                        'progress': 0.0,
                        'downloading': False,
                        'failed': True,
                        'filename': game.get('name', 'Unknown')
                    }
                    # Clean up throttling data on error
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]
                        
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                if hasattr(self, 'library_section') else None)
                
                GLib.idle_add(lambda err=str(e), n=game['name']:
                            self.log_message(f"Download error for {n}: {err}"))
            finally:
                # Call on_complete callback if provided (even on failure to track completion)
                if on_complete and success:
                    try:
                        on_complete(game)
                    except Exception as e:
                        print(f"Error in on_complete callback: {e}")
                semaphore.release()  # Always release when done
        
        threading.Thread(target=download, daemon=True).start()

    def remove_game_from_selection(self, game):
        """Remove a specific game from all selection tracking structures"""
        if not hasattr(self, 'library_section'):
            return
        
        library_section = self.library_section
        
        # Get the game's identifier for tracking removal
        identifier_type, identifier_value = library_section.get_game_identifier(game)
        
        # Remove from ROM ID or game key tracking
        if identifier_type == 'rom_id':
            library_section.selected_rom_ids.discard(identifier_value)
        elif identifier_type == 'game_key':
            library_section.selected_game_keys.discard(identifier_value)
        
        # Remove from GameItem tracking (find matching GameItem)
        items_to_remove = []
        for game_item in library_section.selected_checkboxes:
            if game_item.game_data.get('rom_id') == game.get('rom_id') and game.get('rom_id'):
                items_to_remove.append(game_item)
            elif (game_item.game_data.get('name') == game.get('name') and 
                game_item.game_data.get('platform') == game.get('platform')):
                items_to_remove.append(game_item)
        
        for item in items_to_remove:
            library_section.selected_checkboxes.discard(item)
        
        # Update UI to reflect new selection state
        library_section.update_action_buttons()
        library_section.update_selection_label()
        
        # Decrement bulk download counter if it exists
        if hasattr(self, '_bulk_download_remaining'):
            self._bulk_download_remaining -= 1

    # NOTE: download_saves_for_game() removed — pre-launch sync handled by AutoSyncManager

    def on_sync_to_romm(self, button):
        """Upload local saves from RetroArch to RomM using NEW method."""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return
        
        if not self.available_games:
            self.log_message("Game library not loaded. Cannot match saves. Please refresh.")
            return

        def sync():
            try:
                GLib.idle_add(lambda: self.log_message("🚀 Starting upload using NEW thumbnail method..."))
                
                # Create mapping from 'fs_name_no_ext' to rom_id for more reliable matching.
                rom_map = {}
                for game in self.available_games:
                    if game.get('rom_id') and game.get('romm_data'):
                        basename = game['romm_data'].get('fs_name_no_ext')
                        if basename:
                            rom_map[basename] = game['rom_id']

                if not rom_map:
                    GLib.idle_add(lambda: self.log_message("Could not create a map of games from RomM library."))
                    return

                local_saves = self.retroarch.get_save_files()
                total_files = sum(len(files) for files in local_saves.values())
                
                if total_files == 0:
                    GLib.idle_add(lambda: self.log_message("No local save files found to upload."))
                    return

                GLib.idle_add(lambda: self.log_message(f"Found {total_files} local save/state files to check."))
                
                uploaded_count = 0
                unmatched_count = 0

                for save_type, files in local_saves.items(): # 'saves' or 'states'
                    for save_file in files:
                        save_name = save_file['name']
                        save_path = save_file['path']
                        emulator = save_file.get('emulator', 'unknown')
                        relative_path = save_file.get('relative_path', save_name)
                        
                        # Match by filename stem (e.g., "Test.srm" -> "Test")
                        save_basename = Path(save_name).stem
                        
                        # Try to extract a cleaner basename by removing timestamps and brackets
                        import re
                        clean_basename = re.sub(r'\s*\[.*?\]', '', save_basename)  # Remove [timestamp] parts
                        
                        rom_id = rom_map.get(save_basename) or rom_map.get(clean_basename)
                        
                        if rom_id:
                            # Look for thumbnail if it's a save state
                            thumbnail_path = None
                            if save_type == 'states':
                                thumbnail_path = self.retroarch.find_thumbnail_for_save_state(save_path)
                            
                            # Always use the new upload method (with or without thumbnail)
                            if emulator:
                                GLib.idle_add(lambda n=save_name, e=emulator: 
                                            self.log_message(f"  📤 Uploading {n} ({e}) using NEW method..."))
                            else:
                                GLib.idle_add(lambda n=save_name: 
                                            self.log_message(f"  📤 Uploading {n} using NEW method..."))
                            
                            # Use NEW method for all uploads
                            slot, autocleanup, autocleanup_limit = RomMClient.get_slot_info(save_path)
                            result = self.romm_client.upload_save_with_thumbnail(
                                rom_id, save_type, save_path, thumbnail_path, emulator, self.device_id,
                                slot=slot, autocleanup=autocleanup, autocleanup_limit=autocleanup_limit
                            )

                            if result == 'conflict':
                                GLib.idle_add(lambda n=save_name:
                                            self.log_message(f"  ⚠️ Sync conflict for {n} - server has newer version"))
                            elif result:
                                if thumbnail_path:
                                    if emulator:
                                        GLib.idle_add(lambda n=save_name, e=emulator:
                                                    self.log_message(f"  ✅ Successfully uploaded {n} with screenshot 📸 ({e})"))
                                    else:
                                        GLib.idle_add(lambda n=save_name:
                                                    self.log_message(f"  ✅ Successfully uploaded {n} with screenshot 📸"))
                                else:
                                    if emulator:
                                        GLib.idle_add(lambda n=save_name, e=emulator:
                                                    self.log_message(f"  ✅ Successfully uploaded {n} ({e})"))
                                    else:
                                        GLib.idle_add(lambda n=save_name:
                                                    self.log_message(f"  ✅ Successfully uploaded {n}"))
                                uploaded_count += 1
                            else:
                                GLib.idle_add(lambda n=save_name:
                                            self.log_message(f"  ❌ Failed to upload {n}"))
                        else:
                            unmatched_count += 1
                            location_info = f" ({relative_path})" if relative_path != save_name else ""
                            GLib.idle_add(lambda n=save_name, loc=location_info: 
                                        self.log_message(f"  - Could not match local file '{n}'{loc}, skipping."))
                
                GLib.idle_add(lambda: self.log_message("-" * 20))
                GLib.idle_add(lambda u=uploaded_count, t=total_files, m=unmatched_count:
                            self.log_message(f"Sync complete. Uploaded {u}/{t-m} matched files. ({m} unmatched)"))

            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"An error occurred during save sync: {err}"))

        threading.Thread(target=sync, daemon=True).start()

    def on_clear_cache(self, button):
        """Clear cached game data"""
        if hasattr(self, 'game_cache'):
            self.game_cache.clear_cache()
            self.log_message("🗑️ Game data cache cleared")
            self.log_message("💡 Reconnect to RomM to rebuild cache")
        else:
            self.log_message("❌ No cache to clear")

    def _open_folder_in_file_manager(self, folder, label):
        """Open a directory in the user's file manager (portal-aware on GTK4)."""
        folder = Path(folder) if folder else None
        if not folder or not folder.exists():
            self.log_message(f"{label} does not exist: {folder}")
            return
        try:
            # Gtk.FileLauncher routes through XDG portals (works native + Flatpak).
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder)))
            launcher.launch(self, None, None)
            self.log_message(f"Opened {label.lower()}: {folder}")
        except Exception as e:
            # Fall back to xdg-open if FileLauncher is unavailable.
            import subprocess
            try:
                subprocess.run(['xdg-open', str(folder)], check=True)
                self.log_message(f"Opened {label.lower()}: {folder}")
            except Exception as e2:
                self.log_message(f"Could not open {label.lower()}: {e2}")
                self.log_message(f"{label}: {folder}")

    def on_browse_downloads(self, button):
        """Open the download directory in file manager"""
        self._open_folder_in_file_manager(Path(self.rom_dir_row.get_text()), "Download directory")

    def on_browse_saves(self, button):
        """Open the RetroArch saves directory in file manager"""
        self._open_folder_in_file_manager(
            getattr(self.retroarch, 'save_dirs', {}).get('saves'), "Saves folder")

    def on_browse_states(self, button):
        """Open the RetroArch save-states directory in file manager"""
        self._open_folder_in_file_manager(
            getattr(self.retroarch, 'save_dirs', {}).get('states'), "Save states folder")
    
    def on_inspect_downloads(self, button):
        """Inspect downloaded files to check if they're legitimate"""
        download_dir = Path(self.rom_dir_row.get_text())
        
        def inspect():
            try:
                self.log_message("=== Inspecting Downloaded Files ===")
                
                if not download_dir.exists():
                    GLib.idle_add(lambda: self.log_message("Download directory does not exist"))
                    return
                
                file_count = 0
                total_size = 0
                
                # Recursively find all files
                for file_path in download_dir.rglob('*'):
                    if file_path.is_file():
                        file_count += 1
                        file_size = file_path.stat().st_size
                        total_size += file_size
                        
                        # Format size
                        if file_size > 1024 * 1024:
                            size_str = f"{file_size / (1024 * 1024):.1f} MB"
                        elif file_size > 1024:
                            size_str = f"{file_size / 1024:.1f} KB"
                        else:
                            size_str = f"{file_size} bytes"
                        
                        relative_path = file_path.relative_to(download_dir)
                        GLib.idle_add(lambda p=str(relative_path), s=size_str: 
                                     self.log_message(f"  {p} - {s}"))
                        
                        # Check if suspiciously small
                        if file_size < 1024:
                            try:
                                # Try to read as text to see if it's an error page
                                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                    content = f.read()[:200]  # First 200 chars
                                    
                                if any(keyword in content.lower() for keyword in ['html', 'error', '404', 'not found', 'unauthorized']):
                                    GLib.idle_add(lambda p=str(relative_path): 
                                                 self.log_message(f"    ⚠ {p} appears to be an error page"))
                                    GLib.idle_add(lambda c=content[:100]: 
                                                 self.log_message(f"    Content: {c}..."))
                                else:
                                    GLib.idle_add(lambda p=str(relative_path): 
                                                 self.log_message(f"    ✓ {p} appears to be binary data"))
                            except Exception:
                                GLib.idle_add(lambda p=str(relative_path): 
                                             self.log_message(f"    ✓ {p} is binary (good sign)"))
                
                # Summary
                if file_count > 0:
                    total_mb = total_size / (1024 * 1024)
                    GLib.idle_add(lambda c=file_count, s=total_mb: 
                                 self.log_message(f"Total: {c} files, {s:.1f} MB"))
                else:
                    GLib.idle_add(lambda: self.log_message("No files found in download directory"))
                
                GLib.idle_add(lambda: self.log_message("=== Inspection complete ==="))
                
            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"Inspection error: {err}"))
        
        threading.Thread(target=inspect, daemon=True).start()
