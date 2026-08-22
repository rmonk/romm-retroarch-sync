#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# Add project root and local engine to sys.path
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

_local_engine = _script_dir.parent / 'build' / 'AppDir' / 'usr' / 'bin'
if _local_engine.exists() and str(_local_engine) not in sys.path:
    sys.path.insert(0, str(_local_engine))

import logging

class _SafeStdStream:
    """Protects stdout/stderr against fatal locks during interpreter shutdown with daemon threads."""
    def __init__(self, target):
        self._target = target

    def write(self, s):
        if getattr(sys, 'is_finalizing', lambda: False)():
            return len(s) if s else 0
        try:
            return self._target.write(s)
        except Exception:
            return len(s) if s else 0

    def flush(self):
        if getattr(sys, 'is_finalizing', lambda: False)():
            return
        try:
            self._target.flush()
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._target, name)

if not isinstance(sys.stdout, _SafeStdStream):
    sys.stdout = _SafeStdStream(sys.stdout)
if not isinstance(sys.stderr, _SafeStdStream):
    sys.stderr = _SafeStdStream(sys.stderr)

def detect_desktop_environment(manual_de=None):
    """Detect current Linux desktop environment or return manual override"""
    if manual_de:
        de_map = {
            'gnome': 'GNOME',
            'kde': 'KDE',
            'steamos': 'STEAM_OS',
            'xfce': 'XFCE',
            'cinnamon': 'CINNAMON',
            'mate': 'MATE',
            'generic': 'GENERIC'
        }
        return de_map.get(manual_de.lower(), 'GENERIC')

    # Detect Steam Deck / SteamOS Game Mode or Desktop Mode
    if os.path.exists('/etc/os-release'):
        try:
            with open('/etc/os-release', 'r') as f:
                os_release_content = f.read().lower()
                if 'steamos' in os_release_content or 'steamdeck' in os_release_content:
                    return 'STEAM_OS'
        except Exception:
            pass

    xdg_current = os.environ.get('XDG_CURRENT_DESKTOP', '').upper()
    desktop_session = os.environ.get('DESKTOP_SESSION', '').upper()

    if 'GNOME' in xdg_current or 'GNOME' in desktop_session:
        return 'GNOME'
    elif 'KDE' in xdg_current or 'PLASMA' in xdg_current or 'KDE' in desktop_session:
        return 'KDE'
    elif 'XFCE' in xdg_current or 'XFCE' in desktop_session:
        return 'XFCE'
    elif 'CINNAMON' in xdg_current or 'CINNAMON' in desktop_session:
        return 'CINNAMON'
    elif 'MATE' in xdg_current or 'MATE' in desktop_session:
        return 'MATE'
    else:
        return 'GENERIC'

def get_de_custom_css(de):
    """Generate dynamic CSS rules tailored for the detected desktop environment"""
    css_snippets = []
    if de == 'GNOME':
        css_snippets.append("""
            /* GNOME Libadwaita Card & Pill Styling */
            .card, expanderrow {
                border-radius: 12px;
            }
            scrolledwindow.data-table, .data-table columnview {
                border-radius: 12px;
            }
            .data-table row {
                min-height: 38px;
            }
        """)
    elif de == 'STEAM_OS':
        css_snippets.append("""
            /* SteamOS Game Mode & Handheld Touch Optimization */
            .card, expanderrow {
                border-radius: 8px;
                border: 1px solid alpha(@borders, 0.4);
            }
            scrolledwindow.data-table, .data-table columnview {
                border-radius: 10px;
            }
            .data-table row {
                min-height: 44px;
                font-size: 1.05em;
            }
            button.column-gear-btn {
                min-width: 24px;
                min-height: 24px;
            }
            :focus {
                outline: 2px solid @accent_bg_color;
                outline-offset: 2px;
            }
        """)
    else:
        # Non-GNOME Traditional Desktop Window Styling (KDE, XFCE, Cinnamon, MATE, Generic)
        de_label = "KDE Breeze" if de == 'KDE' else f"{de} Traditional Desktop"
        border_rad = "4px" if de == 'KDE' else "2px"
        css_snippets.append(f"""
            /* {de_label} - Traditional Window Styling (Non-GNOME Card Overrides) */
            .card {{
                background-color: @window_bg_color;
                box-shadow: none;
                border: 1px solid @borders;
                border-radius: {border_rad};
            }}
            expanderrow, preferencesgroup > list {{
                background-color: @window_bg_color;
                border: 1px solid @borders;
                border-radius: {border_rad};
                box-shadow: none;
            }}
            scrolledwindow.data-table, .data-table columnview {{
                border-radius: {border_rad};
                border: 1px solid @borders;
                background-color: @view_bg_color;
            }}
            .data-table row {{
                min-height: 32px;
                border-bottom: 1px solid alpha(@borders, 0.2);
            }}
            button {{
                border-radius: {border_rad};
            }}
            popovermenubar.traditional-top-menubar {{
                background-color: @window_bg_color;
                border-bottom: 1px solid alpha(@borders, 0.5);
                padding: 1px 4px;
                font-family: -gtk-system-font;
            }}
            popovermenubar.traditional-top-menubar item {{
                padding: 4px 8px;
                border-radius: {border_rad};
            }}
            headerbar.traditional-headerbar {{
                background-color: @window_bg_color;
                border-bottom: 1px solid alpha(@borders, 0.4);
                box-shadow: none;
            }}
        """)
    return "\n".join(css_snippets)

from ui.compat import Adw, HAS_ADW, Gtk, Gdk, GLib, Gio, GObject
from ui.window import SyncWindow
from romm_sync_engine.sync_core import *

class SyncApp(Adw.Application):
    """Main application class"""
    
    def __init__(self):
        super().__init__(application_id='com.romm.retroarch.sync')
        self.connect('activate', self.on_activate)
        self.connect('shutdown', self.on_shutdown)
    
    def on_activate(self, app):
        """Application activation handler"""
        # Only create window if it doesn't exist
        windows = self.get_windows()
        if windows:
            windows[0].present()
        else:
            cli_de = getattr(app, 'cli_de', None)
            win = SyncWindow(application=app, cli_de=cli_de)
            
            # Handle minimized startup
            if hasattr(app, 'start_minimized') and app.start_minimized:
                print("🔽 Starting minimized to tray")
                win.set_visible(False)  # Start hidden
            else:
                win.present()
    
    def on_shutdown(self, app):
        """Clean up before shutdown"""
        print("🚪 Application shutting down...")
        for window in self.get_windows():
            if hasattr(window, 'auto_sync') and window.auto_sync:
                try:
                    window.auto_sync.stop_auto_sync()
                except Exception:
                    pass
            if hasattr(window, 'library_section') and window.library_section:
                try:
                    if hasattr(window.library_section, 'stop_collection_auto_sync'):
                        window.library_section.stop_collection_auto_sync()
                except Exception:
                    pass

def main():
    """Main entry point"""
    import argparse
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='RomM-RetroArch Sync')
    parser.add_argument('--minimized', action='store_true',
                       help='Start minimized to tray')
    parser.add_argument('--de', '--desktop-environment', type=str, default=None,
                       choices=['gnome', 'kde', 'steamos', 'xfce', 'cinnamon', 'mate', 'generic'],
                       help='Manually specify desktop environment style (gnome, kde, steamos, xfce, cinnamon, mate, generic)')
    args = parser.parse_args()

    print("🚀 Starting RomM-RetroArch Sync...")

    # GUI mode continues here...
    active_de = detect_desktop_environment(manual_de=args.de)
    print(f"🖥️ Desktop environment style: {active_de} (specified: {args.de or 'Auto-detected'})")
    
    app = SyncApp()
    app.start_minimized = args.minimized  # Pass the flag to the app
    app.cli_de = args.de
    return app.run()

if __name__ == '__main__':
    main()
