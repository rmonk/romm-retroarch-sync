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

from ui.compat import (
    Adw, HAS_ADW, Gtk, Gdk, GLib, Gio, GObject,
    detect_desktop_environment, get_de_custom_css
)
from ui.window import SyncWindow

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
