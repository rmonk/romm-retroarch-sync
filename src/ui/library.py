import os
import sys
import time
import math
import cairo
import shutil
import logging
import threading
import webbrowser
from pathlib import Path
from datetime import datetime

import urllib.request
import urllib.parse
from PIL import Image

from gi.repository import Gtk, Gdk, GLib, Gio, GObject, Pango
from ui.compat import Adw, HAS_ADW
from ui.models import GameItem, DiscItem, PlatformItem, LibraryTreeModel
from romm_sync_engine.sync_core import cache_dir, RomMClient, PerformanceTimer

class EnhancedLibrarySection:
    """Enhanced library section with tree view"""
    
    def __init__(self, parent_window):
        self.parent = parent_window
        self.library_model = LibraryTreeModel()
        self.selected_game = None
        self.selected_disc = None  # Track selected disc for launching
        self.selected_collection = None  # Track selected collection for deletion
        self.selected_checkboxes = set()  # Keep this for compatibility
        self.selected_rom_ids = set()     # Add this new tracking
        self.selected_game_keys = set()   # Add this for non-ROM ID games
        self.is_flat_view = self.parent.settings.get('UI', 'flat_view_enabled', 'false') == 'true'
        self.library_model.is_flat_view = self.is_flat_view
        self.show_downloaded_only = self.parent.settings.get('UI', 'show_downloaded_only', 'false') == 'true'
        self.sort_downloaded_first = False  # Sort mode state
        self.filtered_games = []
        self.search_text = ""
        self.game_progress = {}  # rom_id -> progress_info
        self.current_view_mode = 'platform'
        self.collections_games = []
        self.collections_cache_time = 0
        self.collections_cache_duration = 300
        self.view_mode_generation = 0  # Track view mode switches to prevent race conditions
        self.platform_view_selection = set()  # Store platform view row selections
        self.collection_view_selection = set()  # Store collection view row selections
        self.setup_library_ui()
        # Store checkbox and game selections for each view mode
        self.platform_view_checkboxes = set()
        self.platform_view_rom_ids = set()
        self.platform_view_game_keys = set()
        self.platform_view_selected_game = None
        self.platform_view_expanded = set()  # Store expanded platform names
        self.collection_view_checkboxes = set()
        self.collection_view_rom_ids = set()
        self.collection_view_game_keys = set()
        self.collection_view_selected_game = None
        self.collection_view_expanded = set()  # Store expanded collection names
        # Collection auto-sync attributes
        self.selected_collections_for_sync = set()  # UI selection state
        self.actively_syncing_collections = set()   # Auto-sync enabled state (persistent)
        self.currently_downloading_collections = set()  # Currently downloading state (temporary)
        self.completed_sync_collections = set()  # Collections that have completed sync (shows green immediately)
        self.collection_auto_sync_enabled = False
        self.collection_sync_thread = None
        self.collection_sync_interval = 30
        self.load_selected_collections()

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

    def get_collections_for_autosync(self):
        """Get collections selected for auto-sync (either checked or row-selected)"""
        collections_for_sync = set()
        
        # Add checkbox-selected collections
        collections_for_sync.update(self.selected_collections_for_sync)
        
        # Add row-selected collections (if in collections view)
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            selection_model = self.column_view.get_model()
            if selection_model:
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        tree_item = selection_model.get_item(i)
                        if tree_item and tree_item.get_depth() == 0:  # Collection level
                            item = tree_item.get_item()
                            if isinstance(item, PlatformItem):
                                collections_for_sync.add(item.platform_name)
        
        return collections_for_sync

    def remove_orphaned_games_on_startup(self):
        """Remove games that are no longer in any synced collections"""
        if not self.actively_syncing_collections:
            return
        
        def check_and_remove():
            try:
                # Get current collection contents
                all_collections = self.parent.romm_client.get_collections()
                all_synced_rom_ids = set()
                
                for collection in all_collections:
                    if collection.get('name') in self.actively_syncing_collections:
                        collection_id = collection.get('id')
                        collection_name = collection.get('name', '')

                        # Use collections cache if available to avoid full fetch
                        cache_key = f"{collection_id}:{collection_name}"
                        if hasattr(self, '_collections_rom_cache') and cache_key in self._collections_rom_cache:
                            collection_roms = self._collections_rom_cache[cache_key]
                        else:
                            collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                        all_synced_rom_ids.update(rom.get('id') for rom in collection_roms if rom.get('id'))
                
                # Check local games - ONLY check games that were previously in synced collections
                # Don't touch games downloaded outside of collection sync!
                removed_count = 0
                for game in self.parent.available_games:
                    if (game.get('is_downloaded') and
                        game.get('rom_id') and
                        game.get('rom_id') not in all_synced_rom_ids):

                        # Check if this game was actually part of a synced collection before
                        # by checking if it has collection metadata
                        if game.get('collection'):
                            # This game WAS in a synced collection but is no longer
                            GLib.idle_add(lambda g=game: self.parent.delete_game_file(g, is_bulk_operation=True))
                            removed_count += 1
                
                if removed_count > 0:
                    GLib.idle_add(lambda count=removed_count: 
                                self.parent.log_message(f"🗑️ Auto-sync startup: removed {count} orphaned games"))
                
            except Exception as e:
                GLib.idle_add(lambda err=str(e): 
                            self.parent.log_message(f"❌ Startup cleanup error: {err}"))
        
        threading.Thread(target=check_and_remove, daemon=True).start()

    def download_all_actively_syncing_games(self):
        """Download all non-downloaded games in actively syncing collections"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def download_all():
            try:
                all_collections = self.parent.romm_client.get_collections()
                total_to_download = 0
                collections_with_downloads = []

                for collection in all_collections:
                    collection_name = collection.get('name', '')
                    if collection_name not in self.actively_syncing_collections:
                        continue

                    collection_id = collection.get('id')

                    # Always fetch fresh data for auto-sync downloads so that
                    # _sibling_files grouping is current (disk cache may predate
                    # the siblings fix and have ungrouped data).
                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    download_dir = Path(self.parent.rom_dir_row.get_text())
                    games_to_download = []

                    # Build parent-folder lookup (same logic as load_collections).
                    _parent_by_filename = {}
                    for _r in collection_roms:
                        if not _r.get('fs_extension', '') and _r.get('files', []):
                            for _f in _r.get('files', []):
                                _fname = _f.get('filename') or _f.get('file_name', '')
                                if _fname:
                                    _parent_by_filename[_fname] = _r

                    for rom in collection_roms:
                        # Skip folder-container ROMs (no file extension, has child files).
                        # These are never directly downloadable — their content is
                        # populated implicitly when the variant files inside them are
                        # downloaded via the parent-ROM fallback path.
                        if not rom.get('fs_extension', '') and rom.get('files', []):
                            continue

                        processed_game = self.parent.process_single_rom(rom, download_dir)

                        # Inject parent-ROM reference for 404-fallback downloads.
                        _rom_fs_name = rom.get('fs_name', '')
                        if _rom_fs_name and _rom_fs_name in _parent_by_filename:
                            processed_game['_parent_rom'] = _parent_by_filename[_rom_fs_name]
                            processed_game['_fs_extension'] = rom.get('fs_extension', '')

                        if not processed_game.get('is_downloaded'):
                            games_to_download.append(processed_game)

                    if games_to_download:
                        total_to_download += len(games_to_download)
                        collections_with_downloads.append(collection_name)

                        # Mark this collection as currently downloading
                        self.currently_downloading_collections.add(collection_name)

                        GLib.idle_add(lambda name=collection_name, count=len(games_to_download):
                                    self.parent.log_message(f"⬇️ Auto-sync: downloading {count} games from '{name}'"))

                        # Update UI to show orange indicator
                        GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                        for game in games_to_download:
                            GLib.idle_add(lambda g=game:
                                        self.parent.download_game(g, is_bulk_operation=True))

                if total_to_download > 0:
                    GLib.idle_add(lambda count=total_to_download:
                                self.parent.log_message(f"🎯 Auto-sync restored: started download of {count} total games"))

                    # Wait for downloads to complete, then update UI
                    def wait_and_update():
                        time.sleep(5)  # Wait for downloads to start
                        while self.parent.download_progress:
                            time.sleep(2)  # Check every 2 seconds

                        # All downloads complete - remove downloading status
                        for collection_name in collections_with_downloads:
                            self.currently_downloading_collections.discard(collection_name)
                            GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                    threading.Thread(target=wait_and_update, daemon=True).start()
                else:
                    GLib.idle_add(lambda:
                                self.parent.log_message(f"✅ Auto-sync restored: all collections already complete"))

            except Exception as e:
                GLib.idle_add(lambda err=str(e):
                            self.parent.log_message(f"❌ Auto-sync restore error: {err}"))

        threading.Thread(target=download_all, daemon=True).start()

    def update_collection_sync_status(self, collection_name):
        """Update the visual indicator for a collection's sync status"""
        import time
        entry_time = time.time()
        self.parent.log_message(f"[DEBUG] update_collection_sync_status called for {collection_name}")

        if not hasattr(self, 'library_model') or not self.library_model.tree_model:
            self.parent.log_message(f"[DEBUG] No library_model, returning")
            return

        def update_ui():
            update_start = time.time()
            self.parent.log_message(f"[DEBUG] update_ui callback started ({update_start - entry_time:.3f}s after call)")
            model = self.library_model.tree_model
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Collection/Platform level
                    item = tree_item.get_item()
                    if isinstance(item, PlatformItem) and item.platform_name == collection_name:
                        # Determine the new sync status
                        # First check if collection is marked as completed
                        if hasattr(self, 'completed_sync_collections') and collection_name in self.completed_sync_collections:
                            new_status = 'synced'  # Green dot - collection sync complete
                        elif collection_name in self.currently_downloading_collections:
                            new_status = 'syncing'  # Orange dot - currently downloading
                        elif collection_name in self.actively_syncing_collections:
                            # Re-check download state from disk.  item.games may have
                            # stale is_downloaded=False flags if files were downloaded
                            # during this session (process_single_rom isn't re-run).
                            all_downloaded = True
                            for g in item.games:
                                local_path_str = g.get('local_path', '')
                                if not local_path_str:
                                    all_downloaded = False
                                    break
                                lp = Path(local_path_str)
                                if not self.is_path_validly_downloaded(lp):
                                    # Variant files land inside a parent-named subdir;
                                    # scan one level of subdirectories as a fallback.
                                    found_in_sub = False
                                    parent_dir = lp.parent
                                    fname = lp.name
                                    if parent_dir.exists():
                                        try:
                                            for sub in parent_dir.iterdir():
                                                if sub.is_dir() and self.is_path_validly_downloaded(sub / fname):
                                                    found_in_sub = True
                                                    break
                                        except (OSError, PermissionError):
                                            pass
                                    if not found_in_sub:
                                        all_downloaded = False
                                        break
                            new_status = 'synced' if all_downloaded else 'disabled'  # Green if synced, grey if not
                        else:
                            new_status = 'disabled'  # Grey dot (not enabled)

                        self.parent.log_message(f"[DEBUG] Setting status to {new_status} for {collection_name}")
                        # Update the sync_status and notify
                        old_status = item.sync_status
                        item.sync_status = new_status
                        item.notify('sync-status-text')
                        item.notify('name')
                        self.parent.log_message(f"[DEBUG] Status changed from {old_status} to {new_status} ({time.time() - entry_time:.3f}s total)")
                        break
            return False

        # Use HIGH priority to execute ASAP
        GLib.idle_add(update_ui, priority=GLib.PRIORITY_HIGH)

    def download_game_directly(self, game):
        """Download game directly WITH progress tracking"""
        try:
            rom_id = game['rom_id']
            rom_name = game['name']
            platform_slug = game.get('platform_slug', game.get('platform', 'Unknown'))
            file_name = game['file_name']

            # Skip if another download path is already handling this ROM
            if self.parent.download_progress.get(rom_id, {}).get('downloading'):
                return True

            # Get download directory
            download_dir = Path(self.parent.rom_dir_row.get_text())

            # Use platform slug directly (RomM and RetroDECK now use the same slugs)
            platform_dir = download_dir / platform_slug
            platform_dir.mkdir(parents=True, exist_ok=True)
            download_path = platform_dir / file_name

            # Skip if already downloaded (handles both files and folders)
            if self.parent.is_path_validly_downloaded(download_path):
                return True

            # Initialize progress tracking
            self.parent.download_progress[rom_id] = {
                'progress': 0.0,
                'downloading': True,
                'filename': rom_name,
                'speed': 0,
                'downloaded': 0,
                'total': 0
            }

            # Update UI to show download starting
            GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))

            # Download using RomM client with progress callback
            def progress_callback(progress_info):
                # Update progress tracking
                self.parent.download_progress[rom_id].update({
                    'progress': progress_info.get('progress', 0),
                    'speed': progress_info.get('speed', 0),
                    'downloaded': progress_info.get('downloaded', 0),
                    'total': progress_info.get('total', 0),
                    'downloading': True
                })
                # Update UI
                GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))

            # Download using RomM client
            success, message = self.parent.romm_client.download_rom(
                rom_id, rom_name, download_path, progress_callback
            )

            # Child-file variants (e.g. regional ROMs stored inside a parent folder)
            # cannot be downloaded via their own ROM ID — the API returns 404.
            # Fall back to downloading via the parent folder ROM + file_id.
            if not success and 'HTTP 404' in (message or '') and game.get('_fs_extension') and (game.get('_parent_rom') or game.get('_siblings')):
                self.parent.log_message(f"  ↩ Direct download 404; trying via parent folder ROM...")
                parent_success, parent_message, parent_path = self.parent._download_via_parent_rom(
                    game, file_name, platform_dir, progress_callback, lambda: False
                )
                if parent_success:
                    success = True
                    if parent_path:
                        download_path = parent_path

            # Mark as completed or failed
            if success:
                self.parent.download_progress[rom_id] = {
                    'progress': 1.0,
                    'downloading': False,
                    'completed': True,
                    'filename': rom_name
                }
            else:
                self.parent.download_progress[rom_id] = {
                    'progress': 0.0,
                    'downloading': False,
                    'failed': True,
                    'filename': rom_name
                }

            # Final UI update
            GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))

            # Clean up progress after a delay
            def cleanup():
                import time
                time.sleep(2)
                if rom_id in self.parent.download_progress:
                    del self.parent.download_progress[rom_id]
            threading.Thread(target=cleanup, daemon=True).start()

            return success

        except Exception as e:
            print(f"❌ Direct download error: {e}")
            # Mark as failed
            if rom_id in self.parent.download_progress:
                self.parent.download_progress[rom_id] = {
                    'progress': 0.0,
                    'downloading': False,
                    'failed': True,
                    'filename': rom_name
                }
                GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))
            return False

    def restore_collection_auto_sync_on_connect(self):
        """Restore collection auto-sync when connection is established"""
        try:
            if not self.actively_syncing_collections or not self.collection_auto_sync_enabled:
                return

            count = len(self.actively_syncing_collections)
            plural = "collection" if count == 1 else "collections"
            self.parent.log_message(f"🔄 Restoring collection auto-sync for {count} {plural}")

            # Download missing games
            self.download_all_actively_syncing_games()
            
            # Remove orphaned games (if auto-delete enabled)
            self.remove_orphaned_games_on_startup()
            
            # Start background monitoring
            self.start_collection_auto_sync()
            self.update_sync_button_state()

        except Exception as e:
            self.parent.log_message(f"⚠️ Failed to restore collection auto-sync: {e}")

    def get_collection_sync_status(self, collection_name, games):
        """Determine sync status of a collection"""
        if not games:
            return 'empty'
        
        downloaded_count = sum(1 for game in games if game.get('is_downloaded', False))
        total_count = len(games)
        
        if downloaded_count == 0:
            return 'none'
        elif downloaded_count == total_count:
            return 'complete'
        else:
            return 'partial'
        
    def refresh_collection_checkboxes(self, specific_collection=None):
        """Refresh collection display after auto-sync state changes
        
        Args:
            specific_collection: If provided, only refresh this specific collection.
                                  Otherwise, refresh all collections.
        """
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            model = self.library_model.tree_model
            if model:
                # Only trigger property notifications, not items_changed
                # This prevents affecting other collections' expansion states
                for i in range(model.get_n_items()):
                    tree_item = model.get_item(i)
                    if tree_item and tree_item.get_depth() == 0:
                        platform = tree_item.get_item()
                        if isinstance(platform, PlatformItem):
                            # If specific_collection is provided, only update that one
                            if specific_collection is None or platform.platform_name == specific_collection:
                                # Trigger property notifications to refresh the UI
                                # without affecting the tree structure or other collections
                                platform.notify('name')
                                platform.notify('sync-status-text')

    def save_selected_collections(self):
        """Save both UI selection and active sync states"""
        if hasattr(self.parent, 'settings'):
            # UI selection state
            collections_str = '|'.join(self.selected_collections_for_sync)
            self.parent.settings.set('Collections', 'selected_for_sync', collections_str)
            
            # Active sync state (what's actually running)
            active_str = '|'.join(self.actively_syncing_collections)
            self.parent.settings.set('Collections', 'actively_syncing', active_str)
            
            self.parent.settings.set('Collections', 'auto_sync_enabled', str(self.collection_auto_sync_enabled).lower())

    def load_selected_collections(self):
        """Load settings and restore actively syncing collections"""
        if hasattr(self.parent, 'settings'):
            # Restore actively syncing collections (not UI selections)
            actively_syncing_str = self.parent.settings.get('Collections', 'actively_syncing', '')
            if actively_syncing_str:
                self.actively_syncing_collections = set(actively_syncing_str.split('|'))
            
            # Keep UI selections empty on startup
            self.selected_collections_for_sync = set()
            
            # Load other settings
            interval = int(self.parent.settings.get('Collections', 'sync_interval', '30'))
            self.collection_sync_interval = interval
            
            auto_sync_enabled = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            self.collection_auto_sync_enabled = auto_sync_enabled

    def start_collection_auto_sync(self):
        """Start background collection sync and download all non-downloaded games"""
        if not self.actively_syncing_collections:
            self.parent.log_message(f"🚫 No collections selected for sync")
            return
        
        # Don't exit if thread exists - restart it instead
        if self.collection_sync_thread and self.collection_sync_thread.is_alive():
            self.parent.log_message(f"🔄 Collection sync already running")
        else:
            count = len(self.actively_syncing_collections)
            plural = "collection" if count == 1 else "collections"
            self.parent.log_message(f"🎯 Starting collection sync for {count} {plural}...")

            # Download all existing games in selected collections first
            # Don't send notifications during startup - only for user-initiated actions
            self.download_all_collection_games(send_notifications=False)

            # Initialize ROM caches for selected collections
            self.initialize_collection_caches()
            
            self.collection_auto_sync_enabled = True
            
            def sync_worker():
                self.parent.log_message(f"🚀 Collection sync worker thread started")
                while self.collection_auto_sync_enabled:
                    try:
                        self.check_actively_syncing_collections()
                        time.sleep(self.collection_sync_interval)
                    except Exception as e:
                        self.parent.log_message(f"❌ Collection sync error: {e}")
                        time.sleep(60)
                self.parent.log_message(f"🛑 Collection sync worker stopped")
            
            self.collection_sync_thread = threading.Thread(target=sync_worker, daemon=True)
            self.collection_sync_thread.start()
            
        self.refresh_collection_checkboxes()

    def download_all_collection_games(self, send_notifications=True):
        """Download all non-downloaded games in selected collections (respecting concurrency limit)"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def download_all():
            try:
                all_collections = self.parent.romm_client.get_collections()
                total_to_download = 0
                all_games_to_download = []
                collections_data = {}  # Track per-collection data

                for collection in all_collections:
                    collection_name = collection.get('name', '')
                    if (collection_name not in self.selected_collections_for_sync and
                        collection_name not in getattr(self, 'actively_syncing_collections', set())):
                        continue

                    collection_id = collection.get('id')
                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    download_dir = Path(self.parent.rom_dir_row.get_text())

                    # Build parent-folder lookup for 404-fallback downloads.
                    _parent_by_filename = {}
                    for _r in collection_roms:
                        if not _r.get('fs_extension', '') and _r.get('files', []):
                            for _f in _r.get('files', []):
                                _fname = _f.get('filename') or _f.get('file_name', '')
                                if _fname:
                                    _parent_by_filename[_fname] = _r

                    # Track collection-specific data
                    collections_data[collection_name] = {
                        'total': len(collection_roms),
                        'to_download': 0,
                        'already_downloaded': 0
                    }

                    for rom in collection_roms:
                        if not rom.get('fs_extension', '') and rom.get('files', []):
                            continue  # Skip folder-container ROMs
                        processed_game = self.parent.process_single_rom(rom, download_dir)

                        _rom_fs_name = rom.get('fs_name', '')
                        if _rom_fs_name and _rom_fs_name in _parent_by_filename:
                            processed_game['_parent_rom'] = _parent_by_filename[_rom_fs_name]
                            processed_game['_fs_extension'] = rom.get('fs_extension', '')

                        if not processed_game.get('is_downloaded'):
                            # Tag game with collection name for tracking
                            processed_game['_sync_collection'] = collection_name
                            all_games_to_download.append(processed_game)
                            total_to_download += 1
                            collections_data[collection_name]['to_download'] += 1
                        else:
                            collections_data[collection_name]['already_downloaded'] += 1

                if total_to_download > 0:
                    # Use bulk download method with collection tracking
                    GLib.idle_add(lambda games=all_games_to_download, cdata=collections_data:
                                self.parent.download_multiple_games_with_collection_tracking(games, cdata))
                else:
                    # All collections are already synced - update their status to green
                    def update_all_collection_statuses():
                        for collection_name in collections_data.keys():
                            self.update_collection_sync_status(collection_name)
                        return False
                    GLib.idle_add(update_all_collection_statuses)

                    # Send per-collection notifications (only if requested)
                    if send_notifications:
                        for collection_name, data in collections_data.items():
                            def send_collection_synced_notification(name=collection_name, total=data['total']):
                                self.parent.send_desktop_notification(
                                    f"✅ {name} - Sync Complete",
                                    f"{total}/{total} ROMs synced"
                                )
                                return False
                            GLib.idle_add(send_collection_synced_notification)

            except Exception as e:
                GLib.idle_add(lambda err=str(e):
                            self.parent.log_message(f"❌ Collection download error: {err}"))

        threading.Thread(target=download_all, daemon=True).start()

    def initialize_collection_caches(self):
        """Initialize ROM ID caches for actively syncing collections"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        # Set a flag to indicate caches are being initialized
        self._cache_initialization_complete = False

        def init_caches():
            try:
                all_collections = self.parent.romm_client.get_collections()
                for collection in all_collections:
                    collection_name = collection.get('name', '')
                    # Only cache actively syncing collections
                    if collection_name in self.actively_syncing_collections:
                        collection_id = collection.get('id')
                        collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                        # Store current ROM IDs
                        current_rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                        cache_key = f'_collection_roms_{collection_name}'
                        setattr(self, cache_key, current_rom_ids)

                        GLib.idle_add(lambda name=collection_name, count=len(current_rom_ids):
                                    self.parent.log_message(f"🔋 Initialized cache for '{name}': {count} games"))

                # Mark initialization as complete
                self._cache_initialization_complete = True
                GLib.idle_add(lambda: self.parent.log_message(f"✅ Collection cache initialization complete"))

            except Exception as e:
                self._cache_initialization_complete = True  # Set to True even on error to avoid blocking
                GLib.idle_add(lambda err=str(e):
                            self.parent.log_message(f"❌ Cache initialization error: {err}"))

        threading.Thread(target=init_caches, daemon=True).start()

    def stop_collection_auto_sync(self):
        """Stop background collection sync"""
        self.collection_auto_sync_enabled = False
        self.collection_sync_thread = None
        self.refresh_collection_checkboxes()

    def check_actively_syncing_collections(self):
        """Check actively syncing collections for changes"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        # Wait for cache initialization to complete before checking
        if not getattr(self, '_cache_initialization_complete', False):
            self.parent.log_message(f"⏳ Waiting for cache initialization to complete...")
            return

        # ADD THIS LOGGING
        if hasattr(self, '_sync_check_count'):
            self._sync_check_count += 1
        else:
            self._sync_check_count = 1

        # ADD THIS - ALWAYS LOG
        count = len(self.actively_syncing_collections)
        plural = "collection" if count == 1 else "collections"
        self.parent.log_message(f"🔄 Collection autosync running: checking {count} {plural}...")

        try:
            all_collections = self.parent.romm_client.get_collections()
            changes_detected = False
            
            for collection in all_collections:
                collection_name = collection.get('name', '')
                # Check actively syncing collections, not UI selected ones
                if collection_name not in self.actively_syncing_collections:
                    continue
                    
                collection_id = collection.get('id')
                collection_roms = self.parent.romm_client.get_collection_roms(collection_id)
                
                # Get current ROM IDs in this collection
                current_rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                
                # Get previously stored ROM IDs
                cache_key = f'_collection_roms_{collection_name}'
                previous_rom_ids = getattr(self, cache_key, set())
                
                if previous_rom_ids != current_rom_ids:
                    changes_detected = True

                    # Find added and removed games
                    added_rom_ids = current_rom_ids - previous_rom_ids
                    removed_rom_ids = previous_rom_ids - current_rom_ids

                    if added_rom_ids:
                        # Don't log here - let handle_added_games decide if logging is appropriate
                        self.handle_added_games(collection_roms, added_rom_ids, collection_name)

                    if removed_rom_ids:
                        GLib.idle_add(lambda name=collection_name, count=len(removed_rom_ids):
                            self.parent.log_message(f"🗑️ Collection '{name}': {count} games removed"))
                        self.handle_removed_games(removed_rom_ids, collection_name)

                    # Sync Steam shortcuts if enabled for this collection
                    steam = self.parent.steam_manager
                    if steam and steam.is_available():
                        steam_collections = steam.get_steam_sync_collections()
                        if collection_name in steam_collections:
                            download_dir = self.parent.settings.get('Download', 'rom_directory')
                            try:
                                added_sc, removed_sc = steam.sync_collection_shortcuts(
                                    collection_name, collection_roms, download_dir)
                                if added_sc or removed_sc:
                                    GLib.idle_add(self.parent.log_message,
                                                  f"🎮 Steam shortcuts for '{collection_name}': +{added_sc} -{removed_sc}")
                            except Exception as e:
                                logging.debug(f"Steam sync error for '{collection_name}': {e}")
                    
                # MAKE SURE THIS LINE IS OUTSIDE THE IF BLOCKS AND ALWAYS EXECUTES:
                setattr(self, cache_key, current_rom_ids)  # This must happen after handling changes
            
            # At the end of the method, after the for loop
            if not changes_detected and len(self.actively_syncing_collections) > 0:
                count = len(self.actively_syncing_collections)
                plural = "collection" if count == 1 else "collections"
                self.parent.log_message(f"✅ Collection check complete: no changes detected in {count} {plural}")

            # Note: We no longer reload the entire collections view after changes
            # because handle_added_games and handle_removed_games now do in-place updates
                    
        except Exception as e:
            print(f"Collection change check error: {e}")

    def handle_added_games(self, collection_roms, added_rom_ids, collection_name):
        """Automatically download newly added games"""
        def download_new_games():
            try:
                download_dir = Path(self.parent.rom_dir_row.get_text())
                downloaded_count = 0
                already_downloaded_count = 0

                # Create a stable reference to collection name
                current_collection = str(collection_name)

                # Build parent-folder lookup for 404-fallback downloads.
                _parent_by_filename = {}
                for _r in collection_roms:
                    if not _r.get('fs_extension', '') and _r.get('files', []):
                        for _f in _r.get('files', []):
                            _fname = _f.get('filename') or _f.get('file_name', '')
                            if _fname:
                                _parent_by_filename[_fname] = _r

                # First pass: check how many are already downloaded
                for rom in collection_roms:
                    if rom.get('id') not in added_rom_ids:
                        continue
                    processed_game = self.parent.process_single_rom(rom, download_dir)
                    if processed_game.get('is_downloaded'):
                        already_downloaded_count += 1

                # Only log the "games added" message if not all are already downloaded
                total_added = len(added_rom_ids)
                if already_downloaded_count < total_added:
                    GLib.idle_add(lambda name=current_collection, count=total_added:
                        self.parent.log_message(f"�� Collection '{name}': {count} games added"))

                for rom in collection_roms:
                    if rom.get('id') not in added_rom_ids:
                        continue

                    # Process the new ROM
                    processed_game = self.parent.process_single_rom(rom, download_dir)

                    # Inject parent-ROM reference for 404-fallback downloads.
                    _rom_fs_name = rom.get('fs_name', '')
                    if _rom_fs_name and _rom_fs_name in _parent_by_filename:
                        processed_game['_parent_rom'] = _parent_by_filename[_rom_fs_name]
                        processed_game['_fs_extension'] = rom.get('fs_extension', '')

                    # Skip if already downloaded (already counted in first pass)
                    if processed_game.get('is_downloaded'):
                        continue

                    # Log with stable collection reference
                    game_name = processed_game.get('name')
                    current_rom_id = rom.get('id')
                    processed_game['collection'] = current_collection

                    # ADD GAME TO TREE BEFORE DOWNLOADING so user can see it appear
                    def add_game_to_tree():
                        # Update available_games.
                        # Regional variant files (has _parent_rom) belong inside the parent
                        # ROM's _sibling_files — adding them as standalone entries would
                        # create duplicate rows in the platform view.  Only update/append
                        # if there is no parent-folder ROM already tracked.
                        _parent_rom_data = processed_game.get('_parent_rom')
                        _parent_already_tracked = (
                            _parent_rom_data and
                            any(g.get('rom_id') == _parent_rom_data.get('id')
                                for g in self.parent.available_games)
                        )
                        if not _parent_already_tracked:
                            for i, game in enumerate(self.parent.available_games):
                                if game.get('rom_id') == current_rom_id:
                                    self.parent.available_games[i] = processed_game
                                    break
                            else:
                                self.parent.available_games.append(processed_game)

                        # Update collections_games cache
                        if hasattr(self, 'collections_games'):
                            found = False
                            for i, collection_game in enumerate(self.collections_games):
                                if collection_game.get('rom_id') == current_rom_id:
                                    self.collections_games[i] = processed_game
                                    found = True
                                    break
                            if not found:
                                self.collections_games.append(processed_game)

                        # Add to tree view
                        if self.current_view_mode == 'collection':
                            for i in range(self.library_model.root_store.get_n_items()):
                                platform_item = self.library_model.root_store.get_item(i)
                                if platform_item.platform_name == current_collection:
                                    game_exists = any(g.get('rom_id') == current_rom_id for g in platform_item.games)
                                    if not game_exists:
                                        platform_item.games.append(processed_game)
                                        # Sort games alphabetically
                                        if self.sort_downloaded_first:
                                            platform_item.games.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                                        else:
                                            platform_item.games.sort(key=lambda g: g.get('name', '').lower())
                                        platform_item.rebuild_children()
                                        platform_item.notify('status-text')
                                        platform_item.notify('size-text')
                                    break
                        return False

                    GLib.idle_add(add_game_to_tree)

                    self.parent.log_message(f"  ⬇️ Auto-downloading {game_name} from '{current_collection}'...")

                    # Respect concurrent download limit for auto-sync
                    max_concurrent = int(self.parent.settings.get('Download', 'max_concurrent', '3'))
                    # Snapshot: a download worker thread may add/remove keys
                    # concurrently, which would crash a live iteration.
                    active_downloads = sum(1 for p in list(self.parent.download_progress.values())
                                        if p.get('downloading', False))

                    if active_downloads < max_concurrent:
                        # Use direct download if under limit
                        if self.download_game_directly(processed_game):
                            downloaded_count += 1
                            self.parent.log_message(f"  ✅ {game_name} downloaded from '{current_collection}'")

                            # CRITICAL: Update the game's download status IMMEDIATELY after download
                            platform_slug = processed_game.get('platform_slug', 'Unknown')
                            file_name = processed_game.get('file_name', '')
                            download_dir = Path(self.parent.rom_dir_row.get_text())

                            # Use platform slug directly (RomM and RetroDECK now use the same slugs)
                            local_path = download_dir / platform_slug / file_name

                            # Check download status (handle both files and folders)
                            is_valid_download = False
                            if local_path.is_dir():
                                # For folders, check if directory exists and has content
                                is_valid_download = any(local_path.iterdir())
                            elif local_path.is_file():
                                # For files, check if file exists and has reasonable size
                                is_valid_download = local_path.stat().st_size > 1024

                            if is_valid_download:
                                processed_game['is_downloaded'] = True
                                processed_game['local_path'] = str(local_path)
                                processed_game['local_size'] = self.parent.get_actual_file_size(local_path)

                            # Update available_games
                            for i, game in enumerate(self.parent.available_games):
                                if game.get('rom_id') == current_rom_id:
                                    self.parent.available_games[i] = processed_game
                                    break

                            # If this is a regional variant file (has a parent folder ROM),
                            # also update the parent ROM's download status in available_games
                            # so the platform view reflects the download correctly.
                            _parent_rom_data = processed_game.get('_parent_rom')
                            if _parent_rom_data and is_valid_download:
                                _parent_rom_id = _parent_rom_data.get('id')
                                _parent_slug = _parent_rom_data.get('platform_slug', platform_slug)
                                _parent_folder = _parent_rom_data.get('fs_name') or _parent_rom_data.get('name', '')
                                _parent_local = download_dir / _parent_slug / _parent_folder
                                if _parent_local.is_dir():
                                    try:
                                        _parent_has_files = any(_parent_local.iterdir())
                                    except (OSError, PermissionError):
                                        _parent_has_files = False
                                    if _parent_has_files:
                                        for i, game in enumerate(self.parent.available_games):
                                            if game.get('rom_id') == _parent_rom_id:
                                                self.parent.available_games[i]['is_downloaded'] = True
                                                self.parent.available_games[i]['local_path'] = str(_parent_local)
                                                self.parent.available_games[i]['local_size'] = self.parent.get_actual_file_size(_parent_local)
                                                break

                            # Update collections_games cache
                            if hasattr(self, 'collections_games'):
                                for i, collection_game in enumerate(self.collections_games):
                                    if collection_game.get('rom_id') == current_rom_id:
                                        self.collections_games[i] = processed_game
                                        break

                            # Update UI to show download completed (game already exists in tree)
                            def update_download_status():
                                self.update_single_game(processed_game)
                                # Also update the collection's sync status to reflect the new download
                                self.update_collection_sync_status(current_collection)
                                return False

                            GLib.idle_add(update_download_status)
                    else:
                        self.parent.log_message(f"  ❌ Failed to download {game_name} from '{current_collection}'")
                
                # Send notifications about collection changes
                total_added = len(added_rom_ids)
                if total_added > 0:
                    if downloaded_count > 0:
                        self.parent.log_message(f"🎯 Auto-downloaded {downloaded_count} new games from '{current_collection}'")

                        # Send RetroArch notification
                        if self.parent.retroarch:
                            if downloaded_count == total_added:
                                self.parent.retroarch.send_notification(f"'{current_collection}': Downloaded {downloaded_count} new game{'s' if downloaded_count != 1 else ''}")
                            else:
                                self.parent.retroarch.send_notification(f"'{current_collection}': {total_added} added ({downloaded_count} downloaded)")

                        # Send desktop notification
                        def send_desktop_notif():
                            if downloaded_count == total_added:
                                self.parent.send_desktop_notification(
                                    "Collection Synced",
                                    f"'{current_collection}': Downloaded {downloaded_count} new game{'s' if downloaded_count != 1 else ''}"
                                )
                            else:
                                self.parent.send_desktop_notification(
                                    "Collection Synced",
                                    f"'{current_collection}': {total_added} game{'s' if total_added != 1 else ''} added, {downloaded_count} downloaded"
                                )
                            return False
                        GLib.idle_add(send_desktop_notif)
                    elif already_downloaded_count > 0 and already_downloaded_count < total_added:
                        # Some games were already downloaded, but not all
                        self.parent.log_message(f"  ℹ️ {already_downloaded_count} of {total_added} games already downloaded")

                        if self.parent.retroarch:
                            self.parent.retroarch.send_notification(f"'{current_collection}': {total_added} game{'s' if total_added != 1 else ''} added ({already_downloaded_count} already downloaded)")

                        def send_desktop_notif_partial():
                            self.parent.send_desktop_notification(
                                "Collection Updated",
                                f"'{current_collection}': {total_added} game{'s' if total_added != 1 else ''} added ({already_downloaded_count} already downloaded)"
                            )
                            return False
                        GLib.idle_add(send_desktop_notif_partial)
                    # If all games were already downloaded (already_downloaded_count == total_added),
                    # don't send any notification - this is likely cache initialization

            except Exception as e:
                self.parent.log_message(f"❌ Auto-download error: {e}")
        
        # Run downloads in background
        threading.Thread(target=download_new_games, daemon=True).start()

    def handle_removed_games(self, removed_rom_ids, collection_name):
        """Handle removed games - always delete if not in other synced collections"""
        download_dir = Path(self.parent.rom_dir_row.get_text())
        deleted_count = 0
        
        # Find and delete removed games
        for game in self.parent.available_games:
            if game.get('rom_id') in removed_rom_ids and game.get('is_downloaded'):
                # Check if game exists in other synced collections
                found_in_other = False
                for other_collection in self.actively_syncing_collections:
                    if other_collection != collection_name:
                        # Check if ROM ID exists in other collection's cache
                        other_cache = getattr(self, f'_collection_roms_{other_collection}', set())
                        if game.get('rom_id') in other_cache:
                            found_in_other = True
                            break
                
                if not found_in_other:
                    local_path = Path(game.get('local_path', ''))
                    if local_path.exists():
                        try:
                            local_path.unlink()
                            self.parent.log_message(f"  🗑️ Deleted {game.get('name')}")
                            deleted_count += 1
                        except Exception as e:
                            self.parent.log_message(f"  ❌ Failed to delete {game.get('name')}: {e}")
        
        if deleted_count > 0:
            self.parent.log_message(f"Auto-deleted {deleted_count} games removed from '{collection_name}'")

            # Send RetroArch notification
            if self.parent.retroarch:
                self.parent.retroarch.send_notification(f"Collection '{collection_name}': Removed {deleted_count} game{'s' if deleted_count != 1 else ''}")

            # Send desktop notification
            def send_removal_notification():
                self.parent.send_desktop_notification(
                    "Collection Synced",
                    f"'{collection_name}': Removed {deleted_count} game{'s' if deleted_count != 1 else ''}"
                )
                return False
            GLib.idle_add(send_removal_notification)

            def update_ui_after_deletion():
                # Update master games list - mark as not downloaded
                for game in self.parent.available_games:
                    if game.get('rom_id') in removed_rom_ids:
                        game['is_downloaded'] = False
                        game['local_path'] = None
                        game['local_size'] = 0
                
                # Force tree view refresh regardless of view mode
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    # Remove games entirely from collections cache
                    if hasattr(self, 'collections_games'):
                        self.collections_games = [
                            game for game in self.collections_games
                            if game.get('rom_id') not in removed_rom_ids
                        ]

                    # Update the view directly WITHOUT invalidating cache
                    # This prevents showing "Loading..." placeholder
                    self.library_model.update_library(self.collections_games, group_by='collection')
                else:
                    # Platform view - full refresh
                    self.update_games_library(self.parent.available_games)
                
                return False
            
            GLib.idle_add(update_ui_after_deletion)

    def on_collection_checkbox_changed(self, checkbox, collection_name):
        """Handle collection selection (visual state only)"""
        if checkbox.get_active():
            self.selected_collections_for_sync.add(collection_name)
        else:
            self.selected_collections_for_sync.discard(collection_name)
        
        self.save_selected_collections()
        self.update_sync_button_state()

    def download_single_collection_games(self, collection_name):
        """Queue all non-downloaded games from a collection using bulk download"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def queue_downloads():
            try:
                all_collections = self.parent.romm_client.get_collections()

                for collection in all_collections:
                    if collection.get('name', '') != collection_name:
                        continue

                    collection_id = collection.get('id')
                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    download_dir = Path(self.parent.rom_dir_row.get_text())
                    games_to_download = []

                    # Build parent-folder lookup for 404-fallback downloads.
                    _parent_by_filename = {}
                    for _r in collection_roms:
                        if not _r.get('fs_extension', '') and _r.get('files', []):
                            for _f in _r.get('files', []):
                                _fname = _f.get('filename') or _f.get('file_name', '')
                                if _fname:
                                    _parent_by_filename[_fname] = _r

                    for rom in collection_roms:
                        if not rom.get('fs_extension', '') and rom.get('files', []):
                            continue  # Skip folder-container ROMs
                        processed_game = self.parent.process_single_rom(rom, download_dir)
                        _rom_fs_name = rom.get('fs_name', '')
                        if _rom_fs_name and _rom_fs_name in _parent_by_filename:
                            processed_game['_parent_rom'] = _parent_by_filename[_rom_fs_name]
                            processed_game['_fs_extension'] = rom.get('fs_extension', '')
                        if not processed_game.get('is_downloaded'):
                            processed_game['collection'] = collection_name
                            games_to_download.append(processed_game)

                    if games_to_download:
                        # Mark this collection as currently downloading
                        self.currently_downloading_collections.add(collection_name)

                        GLib.idle_add(lambda: self.parent.log_message(
                            f"📥 Starting download of {len(games_to_download)} games from '{collection_name}'"))

                        # Update UI to show orange indicator
                        GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                        # Tag games with collection name for tracking
                        for game in games_to_download:
                            game['_sync_collection'] = collection_name

                        # Prepare collections_data for tracking
                        collections_data = {
                            collection_name: {
                                'total': len(collection_roms),
                                'to_download': len(games_to_download),
                                'already_downloaded': len(collection_roms) - len(games_to_download)
                            }
                        }

                        # Use bulk download method with collection tracking
                        GLib.idle_add(lambda games=games_to_download, cdata=collections_data:
                                    self.parent.download_multiple_games_with_collection_tracking(games, cdata))

                        # Wait for downloads to complete, then update UI
                        def wait_and_update():
                            time.sleep(5)  # Wait for downloads to start
                            while self.parent.download_progress:
                                time.sleep(2)  # Check every 2 seconds

                            # All downloads complete - remove downloading status
                            self.currently_downloading_collections.discard(collection_name)
                            GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                        threading.Thread(target=wait_and_update, daemon=True).start()
                    else:
                        # Remove from currently_downloading since no downloads are needed
                        self.currently_downloading_collections.discard(collection_name)
                        
                        GLib.idle_add(lambda: self.parent.log_message(
                            f"✅ Collection '{collection_name}': all games already downloaded"))
                        # Update status to 'synced' (green) since all games are already downloaded
                        GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name) or False)
                        # Send notification that collection is already synced
                        total_games = len(collection_roms)
                        def send_sync_complete_notif(name=collection_name, total=total_games):
                            self.parent.send_desktop_notification(
                                f"✅ {name} - Sync Complete",
                                f"{total}/{total} ROMs synced"
                            )
                            return False
                        GLib.idle_add(send_sync_complete_notif)
                    break

            except Exception as e:
                GLib.idle_add(lambda: self.parent.log_message(f"❌ Error: {e}"))

        threading.Thread(target=queue_downloads, daemon=True).start()

    def on_toggle_collection_auto_sync(self, toggle_button):
        """Toggle collection auto-sync on/off"""
        import time
        start_time = time.time()
        self.parent.log_message(f"[DEBUG] Toggle activated at {start_time}")

        if toggle_button.get_active():
            # Check both checkbox selections AND row selection
            selected_collections = self.selected_collections_for_sync.copy()
            self.parent.log_message(f"[DEBUG] Got selected collections ({time.time() - start_time:.3f}s)")

            # Add currently selected row if in collections view
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                selection_model = self.column_view.get_model()
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        tree_item = selection_model.get_item(i)
                        item = tree_item.get_item()
                        if isinstance(item, PlatformItem):
                            selected_collections.add(item.platform_name)
            self.parent.log_message(f"[DEBUG] Checked row selection ({time.time() - start_time:.3f}s)")

            if selected_collections:
                self.actively_syncing_collections = selected_collections
                self.parent.log_message(f"[DEBUG] Set actively_syncing_collections ({time.time() - start_time:.3f}s)")

                # Update collection labels to show sync status BEFORE starting sync thread
                for collection_name in selected_collections:
                    # Add to currently_downloading_collections to show orange status immediately
                    self.currently_downloading_collections.add(collection_name)
                    self.parent.log_message(f"[DEBUG] Added {collection_name} to currently_downloading ({time.time() - start_time:.3f}s)")
                    self.update_collection_sync_status(collection_name)
                    self.parent.log_message(f"[DEBUG] Updated status for {collection_name} ({time.time() - start_time:.3f}s)")

                self.start_collection_auto_sync()
                self.parent.log_message(f"[DEBUG] Started auto sync ({time.time() - start_time:.3f}s)")
                toggle_button.set_label("Auto-Sync: ON")
                self.parent.log_message(f"🟡 Collection auto-sync enabled for {len(selected_collections)} collections")
                self.parent.log_message(f"[DEBUG] TOTAL TIME: {time.time() - start_time:.3f}s")

                # Clear UI selections after enabling
                self.selected_collections_for_sync.clear()
                self.save_selected_collections()
                self.refresh_collection_checkboxes()
                
        else:
            # Save collections to update before clearing
            collections_to_update = self.actively_syncing_collections.copy()

            self.stop_collection_auto_sync()
            self.actively_syncing_collections.clear()
            self.currently_downloading_collections.clear()

            # Update collection labels to remove sync indicators
            for collection_name in collections_to_update:
                self.update_collection_sync_status(collection_name)

            # Clear UI selections after disabling
            self.selected_collections_for_sync.clear()
            self.save_selected_collections()
            self.refresh_collection_checkboxes()

            toggle_button.set_label("Auto-Sync: OFF")
            self.parent.log_message("🔴 Collection auto-sync disabled")

            # Save the disabled state
            self.save_selected_collections()

    def bind_checkbox_cell_with_sync_status(self, factory, list_item):
        """Enhanced checkbox binding with visual sync status indicators"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        checkbox = list_item.get_child()
        
        if isinstance(item, GameItem):
            # Game-level checkboxes (existing logic)
            checkbox.set_visible(True)
            checkbox.game_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = False
            
        elif isinstance(item, PlatformItem):
            checkbox.set_visible(True)
            checkbox.platform_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = True

            # In collections view, show sync selection (no status colors)
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Show different visual states:
                collection_name = item.platform_name
                is_selected = collection_name in self.selected_collections_for_sync
                is_syncing = is_selected and self.collection_auto_sync_enabled

                if is_syncing:
                    tooltip = f"{collection_name} - Selected & Auto-syncing"
                elif is_selected:
                    tooltip = f"{collection_name} - Selected (click button to start sync)"
                else:
                    tooltip = f"{collection_name} - Not selected"

                checkbox.set_tooltip_text(tooltip)
                
                # Connect handler
                def on_collection_sync_toggle(cb):
                    if not getattr(cb, '_updating', False):
                        self.on_collection_checkbox_changed(cb, collection_name)
                
                if not hasattr(checkbox, '_sync_handler_connected'):
                    checkbox.connect('toggled', on_collection_sync_toggle)
                    checkbox._sync_handler_connected = True
            else:
                # Platform view - existing logic
                pass

    def get_collection_sync_status(self, collection_name, games):
        """Determine sync status of a collection"""
        if not games:
            return 'empty'
        
        downloaded_count = sum(1 for game in games if game.get('is_downloaded', False))
        total_count = len(games)
        
        if downloaded_count == 0:
            return 'none'
        elif downloaded_count == total_count:
            return 'complete'
        else:
            return 'partial'

    def update_sync_button_state(self):
        """Update sync state - now using toggle switches instead of button"""
        # Also restore UI state on collections view load
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            # Check if auto-sync should be restored
            saved_state = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            if saved_state and self.selected_collections_for_sync and not self.collection_auto_sync_enabled:
                # Trigger restore if not already running
                GLib.timeout_add(1000, self.restore_auto_sync_state)

    def restore_auto_sync_state(self):
        """Restore auto-sync state after app restart"""
        try:
            if (self.parent.romm_client and 
                self.parent.romm_client.authenticated and 
                self.selected_collections_for_sync and
                not self.collection_auto_sync_enabled):

                # 🚫 Clear previous selections
                self.selected_collections_for_sync.clear()

                self.parent.log_message("⚠️ Skipping collection selection restore (auto-sync only)")

                # Just enable the global auto-sync flag
                self.collection_auto_sync_enabled = True

                self.parent.log_message("✅ Auto-sync restored without restoring selections")

        except Exception as e:
            self.parent.log_message(f"⚠️ Failed to restore auto-sync: {e}")

        return False

    def apply_filters(self, games):
        """Apply both platform and search filters to games list"""
        # Debug: count multi-disc games before filtering
        multi_before = sum(1 for g in games if g.get('is_multi_disc', False))

        filtered_games = games

        # Apply platform filter
        selected_index = self.platform_filter.get_selected()
        if selected_index != Gtk.INVALID_LIST_POSITION:
            string_list = self.platform_filter.get_model()
            if string_list:
                selected_platform = string_list.get_string(selected_index)
                if selected_platform != "All Platforms":
                    filtered_games = [game for game in filtered_games
                                    if game.get('platform', 'Unknown') == selected_platform]

        # Apply search filter
        if self.search_text:
            filtered_games = [game for game in filtered_games
                            if self.search_text in game.get('name', '').lower() or
                                self.search_text in game.get('platform', '').lower()]

        # Apply downloaded filter
        if self.show_downloaded_only:
            filtered_games = [game for game in filtered_games if game.get('is_downloaded', False)]

        # Debug: count multi-disc games after filtering
        multi_after = sum(1 for g in filtered_games if g.get('is_multi_disc', False))

        return filtered_games

    def on_toggle_selected_collection_auto_sync(self, button):
        """Toggle auto-sync and then clear selections"""
        current_selections = self.get_collections_for_autosync()
        
        if not current_selections:
            self.parent.log_message("Please select collections first")
            return
        
        # Check if selected collections are actively syncing
        actively_syncing = current_selections.intersection(self.actively_syncing_collections)
        
        if actively_syncing:
            # STOP: Remove selected collections from active sync
            self.actively_syncing_collections -= current_selections

            # Stop global sync if no collections left
            if not self.actively_syncing_collections:
                self.stop_collection_auto_sync()

            # Update collection labels to remove sync indicators
            for collection_name in actively_syncing:
                self.update_collection_sync_status(collection_name)

            self.parent.log_message(f"Stopped auto-sync for {len(actively_syncing)} collections")
        else:
            # START: Add selected collections to active sync
            self.actively_syncing_collections.update(current_selections)
            
            # Update selected_collections_for_sync to persist the selection
            self.selected_collections_for_sync.update(current_selections)
            
            # Download missing games for newly selected collections immediately
            for collection_name in current_selections:
                self.download_single_collection_games(collection_name)
                self.parent.log_message(f"📥 Downloading missing games for '{collection_name}'")
            
            # Initialize collection caches for new collections
            def init_new_collections():
                try:
                    all_collections = self.parent.romm_client.get_collections()
                    for collection in all_collections:
                        if collection.get('name') in current_selections:
                            collection_id = collection.get('id')
                            collection_roms = self.parent.romm_client.get_collection_roms(collection_id)
                            cache_key = f'_collection_roms_{collection.get("name")}'
                            setattr(self, cache_key, {rom.get('id') for rom in collection_roms if rom.get('id')})
                except Exception as e:
                    print(f"Error initializing collection cache: {e}")
            
            threading.Thread(target=init_new_collections, daemon=True).start()
            
            # Start global sync if not already running
            if not self.collection_auto_sync_enabled or not self.collection_sync_thread:
                self.start_collection_auto_sync()
            else:
                # Just log that we added to existing sync
                self.collection_auto_sync_enabled = True

            self.parent.log_message(f"Started auto-sync for {len(current_selections)} collections")

            # Update collection status indicators with a delay to ensure data is loaded
            def update_new_collection_statuses():
                for collection_name in current_selections:
                    self.update_collection_sync_status(collection_name)
                return False
            GLib.timeout_add(1000, update_new_collection_statuses)
        
        # Save persistent state and clear UI selections
        self.save_selected_collections()

        # Clear UI selections after any toggle operation
        self.selected_collections_for_sync.clear()
        self.refresh_collection_checkboxes()
        self.update_sync_button_state()

    def disable_autosync_for_collections(self, collections_to_disable):
        """Disable autosync for specific collections (used when deleting ROMs)"""
        if not collections_to_disable:
            return

        # Remove from actively syncing collections
        self.actively_syncing_collections -= collections_to_disable

        # Update collection labels to remove sync indicators
        for collection_name in collections_to_disable:
            self.update_collection_sync_status(collection_name)

        # Stop global sync if no collections left
        if not self.actively_syncing_collections:
            self.stop_collection_auto_sync()

        # Save persistent state
        self.save_selected_collections()

        # Refresh UI
        self.refresh_collection_checkboxes()
        self.update_sync_button_state()

    def on_toggle_sort(self, button):
        """Toggle between alphabetical and download-status sorting"""
        # 1. Save the current UI state before making changes
        scroll_position = 0
        if hasattr(self, 'column_view'):
            scrolled_window = self.column_view.get_parent()
            if scrolled_window:
                vadj = scrolled_window.get_vadjustment()
                if vadj:
                    scroll_position = vadj.get_value()

        # Save the expansion state of the tree
        expansion_state = self.library_model._get_current_expansion_state()

        # 2. Freeze the UI to prevent intermediate redraws
        self.library_model.root_store.freeze_notify()
        if hasattr(self, 'column_view'):
            self.column_view.freeze_notify()

        try:
            # 3. Toggle sort mode
            self.sort_downloaded_first = not self.sort_downloaded_first

            if self.sort_downloaded_first:
                button.set_icon_name("view-sort-descending-symbolic")
                button.set_tooltip_text("Sort: Alphabetical")
            else:
                button.set_icon_name("view-sort-ascending-symbolic")
                button.set_tooltip_text("Sort: Downloaded")

            # 4. Apply sorting to all platform items
            for i in range(self.library_model.root_store.get_n_items()):
                platform_item = self.library_model.root_store.get_item(i)
                if isinstance(platform_item, PlatformItem):
                    # Get filtered games (respect current filter state)
                    if self.show_downloaded_only:
                        filtered_games = [g for g in platform_item.games if g.get('is_downloaded', False)]
                    else:
                        filtered_games = platform_item.games.copy()  # Make a copy to avoid modifying original

                    # Sort the filtered games
                    if self.sort_downloaded_first:
                        filtered_games.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                    else:
                        filtered_games.sort(key=lambda g: g.get('name', '').lower())

                    # Replace all items in the child store
                    platform_item.child_store.remove_all()
                    for game in filtered_games:
                        platform_item.child_store.append(GameItem(game))

            # Update filtered_games
            self.filtered_games = []
            for i in range(self.library_model.root_store.get_n_items()):
                platform_item = self.library_model.root_store.get_item(i)
                if isinstance(platform_item, PlatformItem):
                    if self.show_downloaded_only:
                        self.filtered_games.extend([g for g in platform_item.games if g.get('is_downloaded', False)])
                    else:
                        self.filtered_games.extend(platform_item.games)

        finally:
            # 5. Thaw notifications - triggers a single batched UI update
            self.library_model.root_store.thaw_notify()
            if hasattr(self, 'column_view'):
                self.column_view.thaw_notify()

        # 6. Restore the UI state after the update
        def restore_state():
            self.library_model._restore_expansion_from_state(expansion_state)

            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        vadj.set_value(scroll_position)
            return False

        GLib.timeout_add(50, restore_state)

    def has_active_downloads(self):
        """Check if any downloads are currently in progress"""
        if not hasattr(self.parent, 'download_progress'):
            return False
        # Snapshot: a download worker thread may mutate the dict concurrently.
        return any(
            progress.get('downloading', False)
            for progress in list(self.parent.download_progress.values())
        )

    def should_cache_collections_at_startup(self):
        """Determine if collections should be cached at startup based on usage patterns"""
        # Only cache if user has used collections view recently
        try:
            last_collection_use = self.parent.settings.get('Collections', 'last_used', '0')
            last_use_time = float(last_collection_use)
            current_time = time.time()
            
            # Cache if used in last 7 days, or if auto-sync is enabled
            recent_use = (current_time - last_use_time) < (7 * 24 * 60 * 60)
            auto_sync_enabled = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            
            return recent_use or auto_sync_enabled
        except Exception:
            return False

    def cache_collections_data(self, force_refresh=False):
        """Cache collections with full processed game caching"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def load_collections_optimized():
            try:
                import time, json
                start_time = time.time()

                # Cache file paths
                cache_dir = Path.home() / '.cache' / 'romm_launcher'
                cache_dir.mkdir(parents=True, exist_ok=True)
                server_hash = self.parent.romm_client.base_url.replace('http://', '').replace('https://', '').replace(':', '_').replace('/', '_')

                roms_cache_file = cache_dir / f'collections_{server_hash}.json'
                games_cache_file = cache_dir / f'games_{server_hash}.json'
                collections_meta_file = cache_dir / f'collections_meta_{server_hash}.json'

                # Try to load processed games cache first (fastest path)
                if not force_refresh and games_cache_file.exists():
                    try:
                        cache_age = time.time() - games_cache_file.stat().st_mtime
                        if cache_age < 3600:  # 1 hour
                            # Check if collection list has changed before using cache
                            collections_changed = False
                            try:
                                all_collections = self.parent.romm_client.get_collections()
                                custom_collections = [c for c in all_collections if not c.get('is_auto_generated', False)]
                                current_collection_ids = set(str(c.get('id')) for c in custom_collections)
                                
                                if collections_meta_file.exists():
                                    with open(collections_meta_file, 'r') as f:
                                        cached_meta = json.load(f)
                                        cached_collection_ids = set(cached_meta.get('collection_ids', []))
                                        if current_collection_ids != cached_collection_ids:
                                            collections_changed = True
                                            print(f"🔄 Collection list changed, invalidating cache (cached: {len(cached_collection_ids)}, current: {len(current_collection_ids)})")
                                else:
                                    # No meta file exists, need to check collections
                                    collections_changed = True
                                    print(f"🔄 No collection metadata found, checking collections")
                            except Exception as e:
                                print(f"⚠️ Could not check collection changes: {e}")
                                collections_changed = False
                            
                            if not collections_changed:
                                with open(games_cache_file, 'r') as f:
                                    cache_data = json.load(f)

                                # Version check: old cache is a plain list; new cache
                                # is {"v": 2, "games": [...]} with folder ROMs excluded.
                                # Reject old format so stale entries are never shown.
                                if not isinstance(cache_data, dict) or cache_data.get('v') != 4:
                                    print("🔄 Stale games cache format, rebuilding...")
                                    raise ValueError("stale cache version")

                                cached_games = cache_data['games']

                                # Update download status by checking filesystem.
                                # Variant files land inside a parent-named subdirectory,
                                # so scan one level deep when the flat path misses.
                                download_dir = Path(self.parent.rom_dir_row.get_text())
                                downloaded_count = 0
                                for game in cached_games:
                                    platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')
                                    file_name = game.get('file_name')
                                    if file_name:
                                        platform_dir = download_dir / platform_slug
                                        local_path = platform_dir / file_name
                                        is_downloaded = self.is_path_validly_downloaded(local_path)
                                        if not is_downloaded and platform_dir.exists():
                                            try:
                                                for _sub in platform_dir.iterdir():
                                                    if _sub.is_dir() and self.is_path_validly_downloaded(_sub / file_name):
                                                        local_path = _sub / file_name
                                                        is_downloaded = True
                                                        break
                                            except (OSError, PermissionError):
                                                pass
                                        game['is_downloaded'] = is_downloaded
                                        game['local_path'] = str(local_path) if is_downloaded else None
                                        if is_downloaded:
                                            game['local_size'] = self.get_actual_file_size(local_path)
                                            downloaded_count += 1
                                        elif 'local_size' not in game:
                                            game['local_size'] = 0
                                        if 'romm_data' not in game:
                                            game['romm_data'] = {'fs_size_bytes': game.get('local_size', 0)}
                                    else:
                                        game['is_downloaded'] = False
                                        game['local_path'] = None
                                        if 'local_size' not in game:
                                            game['local_size'] = 0
                                        if 'romm_data' not in game:
                                            game['romm_data'] = {'fs_size_bytes': 0}

                                print(f"📊 Updated download status: {downloaded_count}/{len(cached_games)} games downloaded")
                                self.collections_games = cached_games
                                self.collections_cache_time = time.time()
                                print(f"⚡ Loaded {len(self.collections_games)} games from cache in {time.time()-start_time:.2f}s")
                                print(f"✅ Collections ready for instant loading (cache valid for {self.collections_cache_duration}s)")
                                return
                            else:
                                print(f"🔄 Collections changed, fetching fresh data from server")
                    except Exception:
                        pass
                
                # Load ROM cache if no games cache
                if force_refresh:
                    self._collections_rom_cache = {}
                    print(f"🔄 Force refresh: bypassing ROM cache")
                elif roms_cache_file.exists():
                    try:
                        with open(roms_cache_file, 'r') as f:
                            self._collections_rom_cache = json.load(f)
                        print(f"📁 Loaded {len(self._collections_rom_cache)} collections from disk")
                    except Exception:
                        self._collections_rom_cache = {}
                else:
                    self._collections_rom_cache = {}

                # Get current collections and fetch any new ones
                all_collections = self.parent.romm_client.get_collections()
                custom_collections = [c for c in all_collections if not c.get('is_auto_generated', False)]
                
                # Always re-fetch all collection ROM lists when rebuilding the games
                # cache.  The per-collection ROM cache cannot detect membership changes
                # (e.g. ROMs added to a collection after the cache was last saved), so
                # relying on it produces stale results.  The ROM cache is still written
                # after a fresh fetch so future no-op runs are fast.
                collections_to_fetch = list(custom_collections)
                
                if collections_to_fetch:
                    print(f"⚡ Fetching {len(collections_to_fetch)} new collections")
                    for collection in collections_to_fetch:
                        roms = self.parent.romm_client.get_collection_roms(collection.get('id'))
                        cache_key = f"{collection.get('id')}:{collection.get('name')}"
                        self._collections_rom_cache[cache_key] = roms
                    
                    # Save ROM cache
                    with open(roms_cache_file, 'w') as f:
                        json.dump(self._collections_rom_cache, f)
                
                # Build games list with download status check
                all_collection_games = []
                download_dir = Path(self.parent.rom_dir_row.get_text())

                for collection in custom_collections:
                    cache_key = f"{collection.get('id')}:{collection.get('name')}"
                    collection_roms = self._collections_rom_cache.get(cache_key, [])

                    # Build parent-folder lookup so child ROMs get _parent_rom set.
                    _parent_by_filename = {}
                    for _r in collection_roms:
                        if not _r.get('fs_extension', '') and _r.get('files', []):
                            for _f in _r.get('files', []):
                                _fname = _f.get('filename') or _f.get('file_name', '')
                                if _fname:
                                    _parent_by_filename[_fname] = _r

                    for rom in collection_roms:
                        # Skip folder-container ROMs — not directly playable/downloadable.
                        if not rom.get('fs_extension', '') and rom.get('files', []):
                            continue

                        # Check if file is actually downloaded
                        platform_slug = rom.get('platform_slug') or rom.get('platform_name', 'Unknown')
                        file_name = rom.get('fs_name')
                        platform_dir = download_dir / platform_slug
                        local_path = platform_dir / file_name if file_name else None
                        is_downloaded = local_path and self.is_path_validly_downloaded(local_path)

                        # Variant files land in a parent-named subdirectory; scan one level.
                        if not is_downloaded and file_name and platform_dir.exists():
                            try:
                                for _sub in platform_dir.iterdir():
                                    if _sub.is_dir() and self.is_path_validly_downloaded(_sub / file_name):
                                        local_path = _sub / file_name
                                        is_downloaded = True
                                        break
                            except (OSError, PermissionError):
                                pass

                        # Get file size from local file if downloaded, otherwise from ROM metadata
                        local_size = 0
                        if is_downloaded and local_path:
                            local_size = self.get_actual_file_size(local_path)
                        elif rom.get('fs_size_bytes'):
                            local_size = rom.get('fs_size_bytes')

                        # Store romm_data for total size calculation (used by size_text property)
                        romm_data = {
                            'fs_size_bytes': rom.get('fs_size_bytes', 0) or local_size
                        }

                        game = {
                            'name': Path(rom.get('fs_name', 'unknown')).stem,
                            'rom_id': rom.get('id'),
                            'platform': rom.get('platform_name', 'Unknown'),
                            'platform_slug': platform_slug,
                            'file_name': file_name,
                            'is_downloaded': is_downloaded,
                            'local_path': str(local_path) if is_downloaded else None,
                            'local_size': local_size,
                            'romm_data': romm_data,
                            'collection': collection.get('name')
                        }

                        # Inject parent-ROM reference for 404-fallback downloads.
                        if file_name and file_name in _parent_by_filename:
                            game['_parent_rom'] = _parent_by_filename[file_name]
                            game['_fs_extension'] = rom.get('fs_extension', '')

                        all_collection_games.append(game)
                
                # Save processed games cache (versioned format — v3 excludes folder ROMs,
                # has _parent_rom on child variants, and was built from ungrouped ROM data)
                try:
                    with open(games_cache_file, 'w') as f:
                        json.dump({'v': 4, 'games': all_collection_games}, f)
                    # Save collection metadata for cache validation
                    collection_ids = [str(c.get('id')) for c in custom_collections]
                    with open(collections_meta_file, 'w') as f:
                        json.dump({'collection_ids': collection_ids}, f)
                except Exception:
                    pass
                
                self.collections_games = all_collection_games
                self.collections_cache_time = time.time()  # Mark cache as valid
                print(f"✅ Collections ready: {len(all_collection_games)} games loaded in {time.time()-start_time:.2f}s (cache valid for {self.collections_cache_duration}s)")

            except Exception as e:
                print(f"Error: {e}")

        threading.Thread(target=load_collections_optimized, daemon=True).start()

    def on_toggle_filter(self, button):
        """Toggle between showing all games and only downloaded games."""
        self.show_downloaded_only = not self.show_downloaded_only
        self.parent.settings.set('UI', 'show_downloaded_only', str(self.show_downloaded_only).lower())

        if self.show_downloaded_only:
            button.set_icon_name("starred-symbolic")
            button.set_tooltip_text("Show all games")
        else:
            button.set_icon_name("folder-symbolic")
            button.set_tooltip_text("Show downloaded only")

        games_source = getattr(self.parent, 'available_games', []) or []
        self.update_games_library(games_source)

    def sort_games_consistently(self, games):
        """Lightning-fast sorting with key pre-computation"""
        if not games:
            return games

        # Check if we should sort by download status first
        sort_downloaded_first = getattr(self, 'sort_downloaded_first', False)

        game_count = len(games)

        # For small lists, use simple sorting
        if game_count < 200:
            start_time = time.time()
            if sort_downloaded_first:
                result = sorted(games, key=lambda game: (
                    game.get('platform', 'ZZZ_Unknown'),
                    not game.get('is_downloaded', False),  # Downloaded first (False sorts before True)
                    game.get('name', '').lower()
                ))
            else:
                result = sorted(games, key=lambda game: (
                    game.get('platform', 'ZZZ_Unknown'),
                    game.get('name', '').lower()
                ))
            return result

        # For large lists, use optimized sorting with download status
        start_time = time.time()

        keyed_games = []
        for game in games:
            platform = game.get('platform', 'ZZZ_Unknown')
            name = game.get('name', '')
            name_lower = name.lower() if name else ''

            if sort_downloaded_first:
                is_downloaded = game.get('is_downloaded', False)
                sort_key = (platform, not is_downloaded, name_lower)  # Downloaded first
            else:
                sort_key = (platform, name_lower)

            keyed_games.append((sort_key, game))

        # Sort using pre-computed keys
        keyed_games.sort(key=lambda x: x[0])
        sorted_games = [game for sort_key, game in keyed_games]

        elapsed = time.time() - start_time

        return sorted_games

    def update_game_progress(self, rom_id, progress_info):
        """Update progress for a specific game"""
        if progress_info:
            self.game_progress[rom_id] = progress_info
        elif rom_id in self.game_progress:
            del self.game_progress[rom_id]
        
        # Find and update the specific game item
        self._update_game_status_display(rom_id)
        
    def _update_game_status_display(self, rom_id):
        """Update game status display by directly updating cells"""

        # Find and update the GameItem cells directly
        def update_cells():
            model = self.library_model.tree_model
            updated_any = False
            selected_collection = None

            # In collections view, try to determine which collection is currently selected
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                if self.selected_game and self.selected_game.get('rom_id') == rom_id:
                    selected_collection = self.selected_game.get('collection')

            games_found = 0
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 1:  # Game level
                    item = tree_item.get_item()
                    if isinstance(item, GameItem):
                        games_found += 1
                        if item.game_data.get('rom_id') == rom_id:
                            # In collections view, prioritize the selected collection
                            if selected_collection and item.game_data.get('collection') != selected_collection:
                                continue

                            item.notify('is-downloaded')
                            item.notify('status-text')
                            item.notify('size-text')
                            item.notify('name')
                            updated_any = True

                            # If we found the selected collection, stop here
                            if selected_collection:
                                break

            # If no selected collection or not found, update all instances
            if not updated_any:
                for i in range(model.get_n_items() if model else 0):
                    tree_item = model.get_item(i)
                    if tree_item and tree_item.get_depth() == 1:
                        item = tree_item.get_item()
                        if isinstance(item, GameItem) and item.game_data.get('rom_id') == rom_id:
                            item.notify('is-downloaded')
                            item.notify('status-text')
                            item.notify('size-text')
                            item.notify('name')

            # Also check child items (regional variants) for matching rom_id
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Platform level
                    platform_item = tree_item.get_item()
                    if isinstance(platform_item, PlatformItem):
                        # Look through games
                        for j in range(platform_item.child_store.get_n_items()):
                            game_item = platform_item.child_store.get_item(j)
                            if isinstance(game_item, GameItem):
                                # Check if game has regional variants
                                if hasattr(game_item, 'child_store') and game_item.child_store:
                                    for k in range(game_item.child_store.get_n_items()):
                                        child_item = game_item.child_store.get_item(k)
                                        if isinstance(child_item, DiscItem):
                                            child_rom_id = child_item.disc_data.get('rom_id')
                                            # Check if this child's ROM ID matches
                                            if child_rom_id == rom_id:
                                                child_item.notify('is-downloaded')
                                                child_item.notify('size-text')
                                                child_item.notify('name')
                                                updated_any = True

            return False

        GLib.idle_add(update_cells)

    def update_disc_progress(self, rom_id, disc_name, progress_info):
        """Update progress for a specific disc in a multi-disc game"""
        disc_key = f"{rom_id}:{disc_name}"

        if progress_info:
            # Store disc progress separately
            if not hasattr(self, 'disc_progress'):
                self.disc_progress = {}
            self.disc_progress[disc_key] = progress_info
        elif hasattr(self, 'disc_progress') and disc_key in self.disc_progress:
            del self.disc_progress[disc_key]

        # Find and update the specific disc item
        self._update_disc_status_display(rom_id, disc_name)

    def _update_disc_status_display(self, rom_id, disc_name):
        """Update disc status display by directly updating cells"""
        def update_cells():
            model = self.library_model.tree_model

            # Find the game item first
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Platform level
                    platform_item = tree_item.get_item()
                    if isinstance(platform_item, PlatformItem):
                        # Look through child items (games)
                        for j in range(platform_item.child_store.get_n_items()):
                            game_item = platform_item.child_store.get_item(j)
                            if isinstance(game_item, GameItem) and game_item.game_data.get('rom_id') == rom_id:
                                # Found the game, now look through its discs
                                if hasattr(game_item, 'child_store') and game_item.child_store:
                                    for k in range(game_item.child_store.get_n_items()):
                                        disc_item = game_item.child_store.get_item(k)
                                        if isinstance(disc_item, DiscItem) and disc_item.disc_data.get('name') == disc_name:
                                            # Trigger property notifications to update UI
                                            # This will call the update functions in bind_size_cell and bind_status_cell
                                            disc_item.notify('is-downloaded')
                                            disc_item.notify('size-text')

                                            # Also queue a redraw to ensure visual updates
                                            if hasattr(self, 'column_view'):
                                                self.column_view.queue_draw()
                                            return False
            return False

        GLib.idle_add(update_cells)

    def on_open_in_romm_clicked(self, button):
        """Opens the selected game or platform page in the default web browser."""
        
        # Check if RomM client is connected
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return
        
        base_url = self.parent.romm_client.base_url
        
        # Check for single row selection first
        if self.selected_game:
            # Individual game selected
            rom_id = self.selected_game.get('rom_id')
            if rom_id:
                game_url = f"{base_url}/rom/{rom_id}"
                try:
                    webbrowser.open(game_url)
                    self.parent.log_message(f"🌐 Opened {self.selected_game.get('name')} in browser.")
                except Exception as e:
                    self.parent.log_message(f"❌ Could not open web page: {e}")
        else:
            # Check if platform is selected via tree selection
            selection_model = self.column_view.get_model()
            selected_positions = []
            for i in range(selection_model.get_n_items()):
                if selection_model.is_selected(i):
                    selected_positions.append(i)
            
            if len(selected_positions) == 1:
                tree_item = selection_model.get_item(selected_positions[0])
                item = tree_item.get_item()
                
                if isinstance(item, PlatformItem):
                    # Platform selected - get platform ID from first game in platform
                    platform_name = item.platform_name
                    platform_id = None
                    
                    # Get platform ID from any game in this platform
                    if item.games:
                        for game in item.games:
                            romm_data = game.get('romm_data')
                            if romm_data and romm_data.get('platform_id'):
                                platform_id = romm_data['platform_id']
                                break
                    
                    if platform_id:
                        platform_url = f"{base_url}/platform/{platform_id}"
                    else:
                        # Fallback to generic platforms page
                        platform_url = f"{base_url}/platforms"
                    
                    try:
                        webbrowser.open(platform_url)
                        self.parent.log_message(f"🌐 Opened {platform_name} platform in browser.")
                    except Exception as e:
                        self.parent.log_message(f"❌ Could not open platform page: {e}")

    # ------------------------------------------------------------------ #
    # Save / state history browser (restore older versions)
    # ------------------------------------------------------------------ #
    def on_save_history_clicked(self, button):
        """Open the save state history browser for the selected game."""
        game = self.selected_game
        rom_id = None
        if self.selected_disc:
            game = self.selected_disc
            rom_id = self.selected_disc.get('rom_id')
        elif game:
            rom_id = game.get('rom_id')
        if not rom_id or not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return
        name = (game or {}).get('name', 'Game')
        self._history_rom_id = rom_id
        self._history_game = game
        self.parent.log_message(f"📜 Loading save state history for {name}…")

        def worker():
            saves, states = self._fetch_save_history(rom_id)
            GLib.idle_add(self._show_history_dialog, game, name, saves, states, 'states')

        threading.Thread(target=worker, daemon=True).start()

    def on_save_file_history_clicked(self, button):
        """Open the battery save file history browser for the selected game."""
        game = self.selected_game
        rom_id = None
        if self.selected_disc:
            game = self.selected_disc
            rom_id = self.selected_disc.get('rom_id')
        elif game:
            rom_id = game.get('rom_id')
        if not rom_id or not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return
        name = (game or {}).get('name', 'Game')
        self._history_rom_id = rom_id
        self._history_game = game
        self.parent.log_message(f"📜 Loading save file history for {name}…")

        def worker():
            saves, states = self._fetch_save_history(rom_id)
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

    def auto_expand_platforms_with_results(self, filtered_games):
        """Automatically expand platforms that contain search results"""
        if not self.search_text:  # No search active, don't auto-expand
            return
            
        # Get platforms that have results
        platforms_with_results = set()
        for game in filtered_games:
            platforms_with_results.add(game.get('platform', 'Unknown'))
        
        def expand_matching_platforms():
            model = self.library_model.tree_model
            if not model:
                return False
                
            for i in range(model.get_n_items()):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Platform level
                    platform_item = tree_item.get_item()
                    if isinstance(platform_item, PlatformItem):
                        if platform_item.platform_name in platforms_with_results:
                            tree_item.set_expanded(True)
            
            return False
        
        # Expand after a small delay to ensure tree is updated
        GLib.timeout_add(100, expand_matching_platforms)

    def update_games_library(self, games):
        """Update the tree view with enhanced stable expansion preservation"""
        multi_count = sum(1 for g in games if g.get('is_multi_disc', False))

        # Debug: show stack trace to see who's calling this
        if len(games) < 20 or multi_count == 0:
            import traceback
            for line in traceback.format_stack()[-5:-1]:
                pass

        with PerformanceTimer(f"update_games_library called with {len(games)} games") as timer:
            if getattr(self.parent, '_dialog_open', False):
                return

            current_mode = getattr(self, 'current_view_mode', 'platform')

            # Apply current filters (platform + search)
            filter_start = time.time()
            games = self.apply_filters(games)
            timer.checkpoint(f"apply_filters: {time.time() - filter_start:.2f}s")
            self.filtered_games = games

            # Apply current platform filter
            plat_filter_start = time.time()
            selected_index = self.platform_filter.get_selected()
            if selected_index != Gtk.INVALID_LIST_POSITION:
                string_list = self.platform_filter.get_model()
                if string_list:
                    selected_platform = string_list.get_string(selected_index)
                    if selected_platform != "All Platforms":
                        games = [game for game in games if game.get('platform', 'Unknown') == selected_platform]
            timer.checkpoint(f"platform filter: {time.time() - plat_filter_start:.2f}s")

            self.filtered_games = games

            # Save scroll position
            scroll_position = 0
            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        scroll_position = vadj.get_value()

            def do_update():
                update_start = time.time()
                self.library_model.update_library(games, flat=self.is_flat_view)

                group_filter_start = time.time()
                self.update_group_filter(games)  # Use filtered games, not all games

                if games:
                    downloaded_count = sum(1 for g in games if g.get('is_downloaded'))
                    total_count = len(games)

            # Update with selection preservation
            preserve_start = time.time()
            self.preserve_selections_during_update(do_update)
            timer.checkpoint(f"preserve_selections_during_update: {time.time() - preserve_start:.2f}s")

            # Restore scroll position
            def restore_scroll():
                if hasattr(self, 'column_view'):
                    scrolled_window = self.column_view.get_parent()
                    if scrolled_window:
                        vadj = scrolled_window.get_vadjustment()
                        if vadj:
                            vadj.set_value(scroll_position)
                return False

            GLib.timeout_add(400, restore_scroll)

    def refresh_all_platform_checkboxes(self):
        """Force refresh all platform checkbox states to match current selections"""
        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 0:  # Platform level
                item = tree_item.get_item()
                if isinstance(item, PlatformItem):
                    self.update_platform_checkbox_for_game({'platform': item.platform_name})

    def _restore_tree_state_immediate(self, tree_state):
        """Restore tree state immediately for smoother transitions"""
        try:
            # Restore expansion first (immediately)
            expansion_state = tree_state.get('expansion_state', {})
            self.library_model._restore_expansion_immediate(expansion_state)
            
            # Then restore scroll position with minimal delay
            GLib.timeout_add(50, lambda: self._restore_scroll_position(tree_state))
            
        except Exception as e:
            print(f"Error in immediate tree state restore: {e}")
    
    def _restore_scroll_position(self, tree_state):
        """Restore scroll position"""
        try:
            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        vadj.set_value(tree_state.get('scroll_position', 0))
            return False
        except Exception as e:
            print(f"Error restoring scroll position: {e}")
            return False

    def get_selected_games(self):
        selected_games = []
        selection_model = self.column_view.get_model()

        # Get row selections
        for i in range(selection_model.get_n_items()):
            if selection_model.is_selected(i):
                tree_item = selection_model.get_item(i)
                if tree_item and tree_item.get_depth() == 1:
                    item = tree_item.get_item()
                    if isinstance(item, GameItem):
                        selected_games.append(item.game_data)

        # Add checkbox selections
        for rom_id in self.selected_rom_ids:
            for game in self.parent.available_games:
                if game.get('rom_id') == rom_id and game not in selected_games:
                    selected_games.append(game)
                    break

        return selected_games

    def get_selected_discs(self):
        """Get selected discs from multi-disc games and regional variants"""
        selected_discs = []
        for key in self.selected_game_keys:
            if key.startswith('disc:'):
                # Parse disc key: disc:{rom_id}:{disc_name}
                parts = key.split(':', 2)
                if len(parts) == 3:
                    rom_id = int(parts[1])
                    disc_name = parts[2]

                    # Find the game and disc (multi-disc games)
                    for game in self.parent.available_games:
                        if game.get('rom_id') == rom_id and game.get('is_multi_disc'):
                            for disc in game.get('discs', []):
                                if disc.get('name') == disc_name:
                                    selected_discs.append({
                                        'game': game,
                                        'disc': disc
                                    })
                                    break
                            break
                        # Also check for regional variants
                        elif game.get('rom_id') == rom_id and game.get('_sibling_files'):
                            # Build regional variant items to match against
                            from pathlib import Path
                            for sibling in game.get('_sibling_files', []):
                                full_fs_name = sibling.get('fs_name') or sibling.get('name', 'Unknown')
                                variant_name = Path(full_fs_name).stem if full_fs_name != 'Unknown' else 'Unknown'
                                if variant_name == disc_name:
                                    # Check if this variant is downloaded
                                    parent_local_path = game.get('local_path')
                                    parent_is_downloaded = game.get('is_downloaded', False)
                                    variant_is_downloaded = False
                                    if parent_is_downloaded and parent_local_path:
                                        parent_path = Path(parent_local_path)
                                        if parent_path.is_dir():
                                            variant_file_path = parent_path / full_fs_name
                                            variant_is_downloaded = variant_file_path.exists()

                                    variant_data = {
                                        'name': variant_name,
                                        'full_fs_name': full_fs_name,
                                        'rom_id': sibling.get('id'),
                                        'is_downloaded': variant_is_downloaded,
                                        'size': sibling.get('fs_size_bytes', 0),
                                        'is_regional_variant': True
                                    }
                                    selected_discs.append({
                                        'game': game,
                                        'disc': variant_data
                                    })
                                    break
                            break
        return selected_discs

    def get_game_identifier(self, game_data):
        """Get unique identifier for a game (ROM ID if available, otherwise name+platform)"""
        rom_id = game_data.get('rom_id')
        if rom_id:
            return ('rom_id', rom_id)
        else:
            name = game_data.get('name', '')
            platform = game_data.get('platform', '')
            return ('game_key', f"{name}|{platform}")

    def is_game_in_autosync_collection(self, game_data):
        """Check if a game is in any collection that has autosync enabled"""
        rom_id = game_data.get('rom_id')

        # If no rom_id, check the current collection only
        if not rom_id:
            collection_name = game_data.get('collection', '')
            return collection_name in self.actively_syncing_collections

        # Check all collections that contain this rom_id
        if hasattr(self, 'collections_games'):
            for collection_game in self.collections_games:
                if collection_game.get('rom_id') == rom_id:
                    collection_name = collection_game.get('collection', '')
                    if collection_name in self.actively_syncing_collections:
                        return True

        return False

    def _block_selection_updates(self, block=True):
        """Temporarily block selection updates during dialogs"""
        self._selection_blocked = block

    def update_platform_checkbox_states(self):
        """Update platform checkbox states based on their games' selection"""
        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 0:  # Platform level items
                item = tree_item.get_item()
                if isinstance(item, PlatformItem):
                    # Check how many games in this platform are selected
                    selected_games_in_platform = [
                        game_item for game_item in self.selected_checkboxes 
                        if game_item.game_data in item.games
                    ]
                    
                    # Platform should be checked if all games are selected
                    should_be_checked = len(selected_games_in_platform) == len(item.games) and len(item.games) > 0
                    
                    # This will trigger a UI refresh for the platform checkbox
                    # The bind_checkbox_cell method will handle the visual update

    def update_bulk_action_buttons(self):
        """Update action button states based on selection (no separate bulk buttons)"""
        # SKIP UPDATES DURING DIALOG  
        if getattr(self, '_selection_blocked', False):
            return

        # Count selected games using the dual tracking system
        selected_count = 0
        
        for game in self.parent.available_games:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                selected_count += 1
            elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                selected_count += 1
        
        # Update selection label
        if selected_count > 0:
            self.selection_label.set_text(f"{selected_count} selected")
        else:
            self.selection_label.set_text("No selection")

    def on_bulk_delete(self, button):
        """Delete all selected downloaded games"""
        selected_games = self.get_selected_games()
        
        # Filter to only downloaded games
        downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]
        
        if downloaded_games and hasattr(self.parent, 'delete_multiple_games'):
            self.parent.delete_multiple_games(downloaded_games)

    def on_select_all(self, button):
        """Select all game items (not platforms)"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        
        # Add all games to selection tracking
        for game in self.parent.available_games:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id':
                self.selected_rom_ids.add(identifier_value)
            elif identifier_type == 'game_key':
                self.selected_game_keys.add(identifier_value)
        
        self.sync_selected_checkboxes()
        self.update_action_buttons()
        self.update_selection_label()
        # Force immediate checkbox sync instead of full refresh
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)

    def on_select_downloaded(self, button):
        """Select only downloaded games"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        
        # Add only downloaded games to selection tracking
        for game in self.parent.available_games:
            if game.get('is_downloaded', False):
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self.selected_rom_ids.add(identifier_value)
                elif identifier_type == 'game_key':
                    self.selected_game_keys.add(identifier_value)
        
        self.sync_selected_checkboxes()
        self.update_action_buttons()
        self.update_selection_label()
        # Force immediate checkbox sync instead of full refresh
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)

    def on_select_none(self, button):
        """Clear all selections"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()
        # Force immediate checkbox sync instead of full refresh
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)

    def setup_library_ui(self):
        """Create the enhanced library UI with tree view"""
        # Create library group
        self.library_group = Adw.PreferencesGroup()
        self.library_group.set_title("Game Library")
        # Don't set vexpand - let scrolled window handle height constraints

        # Create main container
        library_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        library_container.set_margin_top(12)
        library_container.set_margin_bottom(12)
        library_container.set_margin_start(12)
        library_container.set_margin_end(12)
        # Don't set vexpand - let scrolled window handle vertical expansion

        # Store reference for setup_ui to extract
        self.library_container = library_container

        # Toolbar with actions
        toolbar = self.create_toolbar()
        library_container.append(toolbar)

        # Tree view container
        tree_container = self.create_tree_view()
        library_container.append(tree_container)

        # Action buttons
        action_bar = self.create_action_bar()
        library_container.append(action_bar)

        # Wrap in ActionRow for proper styling
        library_row = Adw.ActionRow()
        library_row.set_child(library_container)
        self.library_group.add(library_row)
    
    def create_toolbar(self):
        """Create toolbar with search and filters"""
        toolbar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Search entry
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search games...")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect('search-changed', self.on_search_changed)
        toolbar_box.append(self.search_entry)
        
        # Platform filter dropdown
        self.platform_filter = Gtk.DropDown()
        self.platform_filter.set_tooltip_text("Filter by platform")
        self.platform_filter.connect('notify::selected-item', self.on_platform_filter_changed)
        toolbar_box.append(self.platform_filter)

        # Collection/Platform toggle - round toggle group
        toggle_group_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        toggle_group_box.add_css_class('linked')
        toggle_group_box.add_css_class('pill')

        self.platforms_toggle_btn = Gtk.ToggleButton(label="Platforms")
        self.platforms_toggle_btn.set_active(True)  # Start with Platforms view
        self.platforms_toggle_btn.connect('toggled', self.on_platforms_toggle)
        toggle_group_box.append(self.platforms_toggle_btn)

        self.collections_toggle_btn = Gtk.ToggleButton(label="Collections")
        self.collections_toggle_btn.set_group(self.platforms_toggle_btn)  # Link them as a group
        self.collections_toggle_btn.connect('toggled', self.on_collections_toggle)
        toggle_group_box.append(self.collections_toggle_btn)

        toolbar_box.append(toggle_group_box)

        # Store reference for backward compatibility
        self.view_mode_toggle = self.collections_toggle_btn
        
        # View options
        view_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        view_box.add_css_class('linked')
        
        # Flat View toggle button
        self.flat_view_btn = Gtk.ToggleButton()
        flat_icon = "view-list-symbolic" if self.is_flat_view else "view-list-tree-symbolic"
        self.flat_view_btn.set_icon_name(flat_icon)
        self.flat_view_btn.set_tooltip_text("Toggle Flat View / Tree View")
        self.flat_view_btn.set_active(self.is_flat_view)
        self.flat_view_btn.connect('toggled', self.on_flat_view_toggle)
        view_box.append(self.flat_view_btn)

        # Expand all button (down chevron icon)
        self.expand_btn = Gtk.Button.new_from_icon_name("pan-down-symbolic")
        self.expand_btn.set_tooltip_text("Expand all platforms")
        self.expand_btn.connect('clicked', self.on_expand_all)
        self.expand_btn.set_sensitive(not self.is_flat_view)
        view_box.append(self.expand_btn)
        
        # Collapse all button (up chevron icon)
        self.collapse_btn = Gtk.Button.new_from_icon_name("pan-up-symbolic")
        self.collapse_btn.set_tooltip_text("Collapse all platforms")
        self.collapse_btn.connect('clicked', self.on_collapse_all)
        self.collapse_btn.set_sensitive(not self.is_flat_view)
        view_box.append(self.collapse_btn)

        # Filter toggle button
        filter_icon = "starred-symbolic" if self.show_downloaded_only else "folder-symbolic"
        filter_tooltip = "Show all games" if self.show_downloaded_only else "Show downloaded only"
        self.filter_btn = Gtk.Button.new_from_icon_name(filter_icon)
        self.filter_btn.set_tooltip_text(filter_tooltip)
        self.filter_btn.connect('clicked', self.on_toggle_filter)
        view_box.append(self.filter_btn)
                
        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh library from server")
        refresh_btn.connect('clicked', self.on_refresh_library)
        view_box.append(refresh_btn)
        
        toolbar_box.append(view_box)
        return toolbar_box
    
    def create_tree_view(self):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)

        # Set min/max heights for adaptive sizing with controlled bounds
        scrolled.set_min_content_height(250)  # Minimum to keep it usable
        scrolled.set_max_content_height(600)  # Maximum height before its own scrollbar appears
        scrolled.set_propagate_natural_height(False)  # Don't propagate beyond max
        scrolled.set_vexpand(True)   # Expand to fill available space in window
        scrolled.set_hexpand(True)   # Allow horizontal expansion

        scrolled.add_css_class('data-table')

        self.column_view = Gtk.ColumnView()
        self.column_view.add_css_class('data-table')

        # Make sure the ColumnView can actually be selected
        self.column_view.set_can_focus(True)
        self.column_view.set_focusable(True)

        # Add row activation (double-click)
        self.column_view.connect('activate', self.on_row_activated)

        # Add checkbox column (first column)
        checkbox_factory = Gtk.SignalListItemFactory()
        checkbox_factory.connect('setup', self.setup_checkbox_cell)
        checkbox_factory.connect('bind', self.bind_checkbox_cell)
        self.checkbox_column = Gtk.ColumnViewColumn.new("", checkbox_factory)
        self.checkbox_column.set_fixed_width(75)  # Increased width to accommodate both switch and steam button
        self.column_view.append_column(self.checkbox_column)
        
        # Name column with TreeExpander
        name_factory = Gtk.SignalListItemFactory()
        name_factory.connect('setup', self.setup_name_cell)
        name_factory.connect('bind', self.bind_name_cell)
        self.name_column = Gtk.ColumnViewColumn.new("Name", name_factory)
        self.name_column.set_expand(True)
        self.column_view.append_column(self.name_column)

        # Console / Platform column (placed between Name and Status columns)
        platform_factory = Gtk.SignalListItemFactory()
        platform_factory.connect('setup', self.setup_platform_cell)
        platform_factory.connect('bind', self.bind_platform_cell)
        self.platform_column = Gtk.ColumnViewColumn.new("Platform", platform_factory)
        self.platform_column.set_fixed_width(140)
        self.column_view.append_column(self.platform_column)
        
        # Status column
        status_factory = Gtk.SignalListItemFactory()
        status_factory.connect('setup', self.setup_status_cell)
        status_factory.connect('bind', self.bind_status_cell)
        self.status_column = Gtk.ColumnViewColumn.new("Status", status_factory)
        self.status_column.set_fixed_width(80)
        self.column_view.append_column(self.status_column)

        # Sync Status column (for collections only)
        sync_status_factory = Gtk.SignalListItemFactory()
        sync_status_factory.connect('setup', self.setup_sync_status_cell)
        sync_status_factory.connect('bind', self.bind_sync_status_cell)
        self.sync_status_column = Gtk.ColumnViewColumn.new("Sync", sync_status_factory)
        self.sync_status_column.set_fixed_width(50)
        self.sync_status_column.set_visible(False)  # Hidden by default (platform view)
        self.column_view.append_column(self.sync_status_column)

        # Size column
        size_factory = Gtk.SignalListItemFactory()
        size_factory.connect('setup', self.setup_size_cell)
        size_factory.connect('bind', self.bind_size_cell)
        self.size_column = Gtk.ColumnViewColumn.new("Size", size_factory)
        self.size_column.set_fixed_width(150)
        self.column_view.append_column(self.size_column)

        # Setup click-to-sort sorters on column headers
        def _get_item_obj(row):
            if not row: return None
            if hasattr(row, 'get_item'):
                sub = row.get_item()
                if hasattr(sub, 'get_item'): return sub.get_item()
                return sub
            return row

        def _sort_name(a, b, u=None):
            obj_a, obj_b = _get_item_obj(a), _get_item_obj(b)
            n_a = (obj_a.name if isinstance(obj_a, GameItem) else getattr(obj_a, 'platform_name', '')) or ''
            n_b = (obj_b.name if isinstance(obj_b, GameItem) else getattr(obj_b, 'platform_name', '')) or ''
            n_a, n_b = n_a.lower(), n_b.lower()
            return -1 if n_a < n_b else (1 if n_a > n_b else 0)

        def _sort_platform(a, b, u=None):
            obj_a, obj_b = _get_item_obj(a), _get_item_obj(b)
            p_a = (getattr(obj_a, 'platform_name', '') or (obj_a.game_data.get('platform_name') if hasattr(obj_a, 'game_data') else '') or '').lower()
            p_b = (getattr(obj_b, 'platform_name', '') or (obj_b.game_data.get('platform_name') if hasattr(obj_b, 'game_data') else '') or '').lower()
            return -1 if p_a < p_b else (1 if p_a > p_b else 0)

        def _sort_status(a, b, u=None):
            obj_a, obj_b = _get_item_obj(a), _get_item_obj(b)
            d_a = 1 if getattr(obj_a, 'is_downloaded', False) else 0
            d_b = 1 if getattr(obj_b, 'is_downloaded', False) else 0
            return d_b - d_a

        def _sort_size(a, b, u=None):
            obj_a, obj_b = _get_item_obj(a), _get_item_obj(b)
            s_a = getattr(obj_a, 'size', 0) or 0
            s_b = getattr(obj_b, 'size', 0) or 0
            return -1 if s_a < s_b else (1 if s_a > s_b else 0)

        self.name_column.set_sorter(Gtk.CustomSorter.new(_sort_name))
        self.platform_column.set_sorter(Gtk.CustomSorter.new(_sort_platform))
        self.status_column.set_sorter(Gtk.CustomSorter.new(_sort_status))
        self.size_column.set_sorter(Gtk.CustomSorter.new(_sort_size))

        self.sort_model = Gtk.SortListModel.new(self.library_model.tree_model, self.column_view.get_sorter())
        selection_model = Gtk.MultiSelection.new(self.sort_model)
        selection_model.connect('selection-changed', self.on_selection_changed)
        self.column_view.set_model(selection_model)

        # Setup gear MenuButton for Column Chooser
        self.column_chooser_btn = Gtk.MenuButton()
        gear_img = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        self.column_chooser_btn.set_child(gear_img)
        self.column_chooser_btn.add_css_class("flat")
        self.column_chooser_btn.add_css_class("column-gear-btn")
        self.column_chooser_btn.set_tooltip_text("Customize displayed columns")

        # Setup Popover for Column Chooser menu button
        self.setup_column_chooser_popover()

        # Controller to trigger popover directly on click even inside ColumnView header button
        gesture = Gtk.GestureClick.new()
        gesture.connect("pressed", lambda g, n, x, y: self.column_chooser_btn.get_popover().popup() if self.column_chooser_btn.get_popover() else None)
        self.column_chooser_btn.add_controller(gesture)

        # Attach gear button to rightmost visible column header after realization
        GLib.idle_add(self.attach_gear_to_rightmost_header)

        scrolled.set_child(self.column_view)
        return scrolled

    def setup_platform_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        list_item.set_child(label)

    def bind_platform_cell(self, factory, list_item):
        tree_item = list_item.get_item()
        label = list_item.get_child()
        if tree_item:
            item = tree_item.get_item()
            if isinstance(item, GameItem):
                plat = item.game_data.get('platform_name') or item.game_data.get('platform') or item.game_data.get('platform_slug', '')
                label.set_text(plat)
            elif isinstance(item, PlatformItem):
                label.set_text(item.platform_name)
            else:
                label.set_text("")

    def attach_gear_to_rightmost_header(self, retries=3):
        """Place gear menu button on the right side of the rightmost visible column header"""
        if not hasattr(self, 'column_view') or not hasattr(self, 'column_chooser_btn'):
            return False

        all_cols = [self.name_column, self.platform_column, self.status_column, self.sync_status_column, self.size_column]
        visible_cols = [c for c in all_cols if c and c.get_visible()]
        if not visible_cols:
            return False

        rightmost_col = visible_cols[-1]
        title = rightmost_col.get_title()

        # Ensure rightmost column expands so there is ample room for title + gear icon
        rightmost_col.set_resizable(True)
        rightmost_col.set_expand(True)

        def _find_header_label_box(container, target_title):
            def _search(w):
                # Ignore CheckButtons and Popovers so we never match popover checkboxes
                if isinstance(w, Gtk.CheckButton) or isinstance(w, Gtk.Popover):
                    return None, None
                if isinstance(w, Gtk.Label) and w.get_text() == target_title:
                    p = w.get_parent()
                    if isinstance(p, Gtk.Box):
                        return w, p
                child = w.get_first_child()
                while child:
                    lbl, res = _search(child)
                    if res: return lbl, res
                    child = child.get_next_sibling()
                return None, None
            return _search(container)

        target_label, target_box = _find_header_label_box(self.column_view, title)
        if target_box:
            # 1. Left justify column title label and fill horizontal space to push gear to far right
            if target_label:
                target_label.set_halign(Gtk.Align.START)
                target_label.set_xalign(0.0)
                target_label.set_hexpand(True)

                # Ensure rightmost column width is expanded if needed to fit title text + gear icon + padding
                try:
                    layout = target_label.create_pango_layout(target_label.get_text())
                    text_width = layout.get_pixel_extents()[1].width
                    needed_width = max(90, text_width + 16 + 32)
                    if rightmost_col.get_fixed_width() < needed_width:
                        rightmost_col.set_fixed_width(needed_width)
                except Exception:
                    rightmost_col.set_fixed_width(max(90, rightmost_col.get_fixed_width()))

            # 2. Always refresh child GtkImage so icon is never lost when re-attached
            gear_img = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
            gear_img.set_pixel_size(16)
            self.column_chooser_btn.set_child(gear_img)

            parent = self.column_chooser_btn.get_parent()
            if parent != target_box:
                if parent:
                    parent.remove(self.column_chooser_btn)
                self.column_chooser_btn.set_valign(Gtk.Align.CENTER)
                self.column_chooser_btn.set_halign(Gtk.Align.END)
                self.column_chooser_btn.set_hexpand(False)
                self.column_chooser_btn.add_css_class("flat")
                self.column_chooser_btn.add_css_class("column-gear-btn")
                target_box.append(self.column_chooser_btn)
            return False

        # If header layout is still rendering, retry shortly
        if retries > 0:
            GLib.timeout_add(100, lambda: self.attach_gear_to_rightmost_header(retries - 1))

        return False

    def update_column_visibilities_for_mode(self):
        """Update column visibility and popover checkbuttons based on active view mode (Flat vs Tree)"""
        section = 'Columns_Flat' if self.is_flat_view else 'Columns_Tree'
        defaults = {
            'col_platform': 'true' if self.is_flat_view else 'false',
            'col_status': 'true',
            'col_sync': 'true',
            'col_size': 'true'
        }
        cols_mapping = [
            (self.platform_column, 'col_platform'),
            (self.status_column, 'col_status'),
            (self.sync_status_column, 'col_sync'),
            (self.size_column, 'col_size')
        ]
        for col_obj, setting_key in cols_mapping:
            def_val = defaults[setting_key]
            is_vis = self.parent.settings.get(section, setting_key, def_val) == 'true'
            col_obj.set_visible(is_vis)

        if hasattr(self, 'popover_checkbuttons'):
            for setting_key, chk in self.popover_checkbuttons.items():
                def_val = defaults[setting_key]
                saved_val = self.parent.settings.get(section, setting_key, def_val) == 'true'
                chk.set_active(saved_val)

        GLib.idle_add(self.attach_gear_to_rightmost_header)

    def setup_column_chooser_popover(self):
        """Build popover menu for column chooser button"""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label()
        title.set_markup("<b>Displayed Columns</b>")
        title.set_halign(Gtk.Align.START)
        box.append(title)

        section = 'Columns_Flat' if self.is_flat_view else 'Columns_Tree'
        default_plat = 'true' if self.is_flat_view else 'false'
        cols_config = [
            ("Platform", self.platform_column, 'col_platform', default_plat),
            ("Status", self.status_column, 'col_status', 'true'),
            ("Sync Status", self.sync_status_column, 'col_sync', 'true'),
            ("Size", self.size_column, 'col_size', 'true')
        ]

        self.popover_checkbuttons = {}
        for label_text, col_obj, setting_key, default_val in cols_config:
            chk = Gtk.CheckButton(label=label_text)
            saved_val = self.parent.settings.get(section, setting_key, default_val) == 'true'
            chk.set_active(saved_val)
            col_obj.set_visible(saved_val)
            chk.connect('toggled', self.on_column_visibility_toggled, col_obj, setting_key)
            box.append(chk)
            self.popover_checkbuttons[setting_key] = chk

        popover.set_child(box)
        self.column_chooser_btn.set_popover(popover)

    def on_column_visibility_toggled(self, check_button, col_obj, setting_key):
        """Toggle column visibility and save preference for active mode (Flat vs Tree)"""
        section = 'Columns_Flat' if self.is_flat_view else 'Columns_Tree'
        is_visible = check_button.get_active()
        col_obj.set_visible(is_visible)
        self.parent.settings.set(section, setting_key, str(is_visible).lower())
        GLib.idle_add(self.attach_gear_to_rightmost_header)

    def on_flat_view_toggle(self, button):
        """Toggle between Flat View and Tree View"""
        self.is_flat_view = button.get_active()
        self.library_model.is_flat_view = self.is_flat_view
        self.parent.settings.set('UI', 'flat_view_enabled', str(self.is_flat_view).lower())
        button.set_icon_name("view-list-symbolic" if self.is_flat_view else "view-list-tree-symbolic")

        self.expand_btn.set_sensitive(not self.is_flat_view)
        self.collapse_btn.set_sensitive(not self.is_flat_view)

        # Apply distinct column visibilities for active view mode (Flat vs Tree)
        self.update_column_visibilities_for_mode()

        games_to_show = self.filtered_games if self.filtered_games else self.parent.available_games
        self.update_games_library(games_to_show)

    def on_row_activated(self, column_view, position):
        """Handle row activation (double-click): launch if downloaded, download if not downloaded"""
        selection_model = column_view.get_model()
        tree_item = selection_model.get_item(position)
        
        if tree_item:
            item = tree_item.get_item()
            if isinstance(item, GameItem):
                game_dict = getattr(item, 'game_data', None) or getattr(item, 'game', None)
                if game_dict:
                    self.selected_game = game_dict
                    if hasattr(self.parent, 'selected_game'):
                        self.parent.selected_game = game_dict

                    is_downloaded = game_dict.get('is_downloaded', False)
                    if is_downloaded:
                        # Launch downloaded game
                        if hasattr(self.parent, 'launch_game'):
                            self.parent.launch_game(game_dict)
                    else:
                        # Download game
                        if hasattr(self, 'download_game_directly'):
                            self.download_game_directly(game_dict)
                        elif hasattr(self.parent, 'download_game'):
                            self.parent.download_game(game_dict)
            elif isinstance(item, DiscItem):
                # Double-click on disc: launch specific disc file
                disc_path = getattr(item, 'disc_path', None)
                platform_name = getattr(item, 'platform_name', None)
                if disc_path and platform_name and hasattr(self.parent, 'retroarch'):
                    self.parent.retroarch.launch_game(disc_path, platform_name)
            elif isinstance(item, PlatformItem):
                # Double-click on platform: toggle expansion
                tree_item.set_expanded(not tree_item.get_expanded())

    def setup_name_cell(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        
        expander = Gtk.TreeExpander()
        icon = Gtk.Image()
        icon.set_pixel_size(16)
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3)
        
        expander.set_child(icon)
        box.append(expander)
        box.append(label)
        list_item.set_child(box)

    def save_current_selection(self):
        """Save current row selections, checkbox states, and expanded states based on view mode"""
        selection_model = self.column_view.get_model()
        if not selection_model:
            return

        selected_indices = set()
        expanded_items = set()

        # Save both selections and expanded states
        for i in range(selection_model.get_n_items()):
            tree_item = selection_model.get_item(i)
            if tree_item:
                # Save selection
                if selection_model.is_selected(i):
                    selected_indices.add(i)
                # Save expanded state for top-level items (platforms/collections)
                if tree_item.get_depth() == 0 and tree_item.get_expanded():
                    item = tree_item.get_item()
                    if isinstance(item, PlatformItem):
                        expanded_items.add(item.platform_name)

        if self.current_view_mode == 'platform':
            self.platform_view_selection = selected_indices
            self.platform_view_checkboxes = self.selected_checkboxes.copy()
            self.platform_view_rom_ids = self.selected_rom_ids.copy()
            self.platform_view_game_keys = self.selected_game_keys.copy()
            self.platform_view_selected_game = self.selected_game
            self.platform_view_expanded = expanded_items
        else:
            self.collection_view_selection = selected_indices
            self.collection_view_checkboxes = self.selected_checkboxes.copy()
            self.collection_view_rom_ids = self.selected_rom_ids.copy()
            self.collection_view_game_keys = self.selected_game_keys.copy()
            self.collection_view_selected_game = self.selected_game
            self.collection_view_expanded = expanded_items

    def restore_saved_selection(self):
        """Restore saved row selections, checkbox states, and expanded states based on view mode"""
        selection_model = self.column_view.get_model()
        if not selection_model:
            return

        if self.current_view_mode == 'platform':
            saved_selection = self.platform_view_selection
            saved_expanded = self.platform_view_expanded
            self.selected_checkboxes = self.platform_view_checkboxes.copy()
            self.selected_rom_ids = self.platform_view_rom_ids.copy()
            self.selected_game_keys = self.platform_view_game_keys.copy()
            self.selected_game = self.platform_view_selected_game
        else:
            saved_selection = self.collection_view_selection
            saved_expanded = self.collection_view_expanded
            self.selected_checkboxes = self.collection_view_checkboxes.copy()
            self.selected_rom_ids = self.collection_view_rom_ids.copy()
            self.selected_game_keys = self.collection_view_game_keys.copy()
            self.selected_game = self.collection_view_selected_game

        # Restore expanded state for platforms/collections ONLY if they were expanded before
        if saved_expanded:
            n_items = selection_model.get_n_items()
            for i in range(n_items):
                tree_item = selection_model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:
                    item = tree_item.get_item()
                    if isinstance(item, PlatformItem):
                        if item.platform_name in saved_expanded:
                            tree_item.set_expanded(True)

        # Restore row selection for saved indices
        if saved_selection:
            n_items = selection_model.get_n_items()
            for index in saved_selection:
                if index < n_items:
                    selection_model.select_item(index, False)  # False = don't unselect others

        # Update UI to reflect restored selections
        self.update_selection_label()
        self.update_action_buttons()

    def on_platforms_toggle(self, toggle_button):
        """Handle Platforms button toggle"""
        if toggle_button.get_active():
            self.switch_to_platform_view()

    def on_collections_toggle(self, toggle_button):
        """Handle Collections button toggle"""
        if toggle_button.get_active():
            self.switch_to_collection_view()

    def switch_to_collection_view(self):
        """Switch to collection view"""
        # Save current selection before switching
        self.save_current_selection()

        # Increment generation counter to invalidate any pending background loads
        self.view_mode_generation += 1

        self.current_view_mode = 'collection'

        # Hide platform filter in collections view
        if hasattr(self, 'platform_filter'):
            self.platform_filter.set_visible(False)

        # IMMEDIATELY clear the tree to remove platform view data
        self.library_model.root_store.remove_all()

        # Clear all selections when switching views
        selection_model = self.column_view.get_model()
        if selection_model:
            selection_model.unselect_all()
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.selected_game = None
        self.selected_disc = None
        self.selected_collection = None
        # Update UI immediately to reflect cleared selections
        self.update_selection_label()
        self.update_action_buttons()

        # Force GTK to render the empty tree NOW
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

        # Collection sync controls removed - using toggle switches now

        # Show sync status column (only for collections)
        if hasattr(self, 'sync_status_column'):
            self.sync_status_column.set_visible(True)

        self.load_collections_view()

        # Restore saved selection and refresh checkboxes after the view is loaded
        def restore_and_refresh():
            self.restore_saved_selection()
            # Sync currently-realized checkboxes after the selection state is
            # restored. Rows realized later are handled at bind time
            # (bind_checkbox_cell already sets their state), so a single
            # idle-time pass suffices — no staggered retry timers needed.
            GLib.idle_add(self.force_checkbox_sync)
            return False
        GLib.idle_add(restore_and_refresh)

    def switch_to_platform_view(self):
        """Switch to platform view"""
        # Save current selection before switching
        self.save_current_selection()

        # Increment generation counter to invalidate any pending background loads
        self.view_mode_generation += 1

        self.current_view_mode = 'platform'

        # Show platform filter in platform view
        if hasattr(self, 'platform_filter'):
            self.platform_filter.set_visible(True)

        # Collection sync controls removed - using toggle switches now

        # Hide sync status column (not needed for platforms)
        if hasattr(self, 'sync_status_column'):
            self.sync_status_column.set_visible(False)

        # Clear all selections when switching views
        selection_model = self.column_view.get_model()
        if selection_model:
            selection_model.unselect_all()
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.selected_game = None
        self.selected_collection = None
        # Update UI immediately to reflect cleared selections
        self.update_selection_label()
        self.update_action_buttons()

        original_games = self.parent.available_games.copy()
        self.library_model.update_library(original_games, group_by='platform')

        # Restore saved selection and refresh checkboxes after the view is loaded
        def restore_and_refresh():
            self.restore_saved_selection()
            # Sync currently-realized checkboxes after the selection state is
            # restored. Rows realized later are handled at bind time
            # (bind_checkbox_cell already sets their state), so a single
            # idle-time pass suffices — no staggered retry timers needed.
            GLib.idle_add(self.force_checkbox_sync)
            return False
        GLib.idle_add(restore_and_refresh)

    def on_view_mode_toggled(self, toggle_button):
        """Switch between platform and collection view"""
        # Save current selection before switching
        self.save_current_selection()

        # Increment generation counter to invalidate any pending background loads
        self.view_mode_generation += 1

        if toggle_button.get_active():
            # Collections view
            self.current_view_mode = 'collection'

            # IMMEDIATELY clear the tree to remove platform view data
            self.library_model.root_store.remove_all()

            # Clear all selections when switching views
            selection_model = self.column_view.get_model()
            if selection_model:
                selection_model.unselect_all()
            self.selected_checkboxes.clear()
            self.selected_rom_ids.clear()
            self.selected_game_keys.clear()
            self.selected_game = None
            self.selected_collection = None
            # Update UI immediately to reflect cleared selections
            self.update_selection_label()
            self.update_action_buttons()

            # Force GTK to render the empty tree NOW
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)

            # Collection sync controls removed - using toggle switches now

            # Show sync status column (only for collections)
            if hasattr(self, 'sync_status_column'):
                self.sync_status_column.set_visible(True)

            self.load_collections_view()

            # Restore saved selection and refresh checkboxes after the view is loaded
            def restore_and_refresh():
                self.restore_saved_selection()
                # Sync currently-realized checkboxes after the selection state is
                # restored. Rows realized later are handled at bind time
                # (bind_checkbox_cell already sets their state), so a single
                # idle-time pass suffices — no staggered retry timers needed.
                GLib.idle_add(self.force_checkbox_sync)
                return False
            GLib.idle_add(restore_and_refresh)
        else:
            # Platform view
            toggle_button.set_label("Collections")
            self.current_view_mode = 'platform'

            # Collection sync controls removed - using toggle switches now

            # Hide sync status column (not needed for platforms)
            if hasattr(self, 'sync_status_column'):
                self.sync_status_column.set_visible(False)

            # Clear all selections when switching views
            selection_model = self.column_view.get_model()
            if selection_model:
                selection_model.unselect_all()
            self.selected_checkboxes.clear()
            self.selected_rom_ids.clear()
            self.selected_game_keys.clear()
            self.selected_game = None
            self.selected_collection = None
            # Update UI immediately to reflect cleared selections
            self.update_selection_label()
            self.update_action_buttons()

            original_games = self.parent.available_games.copy()
            self.library_model.update_library(original_games, group_by='platform')

            # Restore saved selection and refresh checkboxes after the view is loaded
            def restore_and_refresh():
                self.restore_saved_selection()
                # Sync currently-realized checkboxes after the selection state is
                # restored. Rows realized later are handled at bind time
                # (bind_checkbox_cell already sets their state), so a single
                # idle-time pass suffices — no staggered retry timers needed.
                GLib.idle_add(self.force_checkbox_sync)
                return False
            GLib.idle_add(restore_and_refresh)

    def load_collections_view(self):
        """Load and display custom collections only"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            self.parent.log_message("Please connect to RomM to view collections")
            return

        # FIXED: More robust cache check
        import time
        current_time = time.time()

        # Initialize cache attributes if missing
        if not hasattr(self, 'collections_games'):
            self.collections_games = []
        if not hasattr(self, 'collections_cache_time'):
            self.collections_cache_time = 0
        if not hasattr(self, 'collections_cache_duration'):
            self.collections_cache_duration = 300

        cache_valid = (
            self.collections_games and  # Has cached data
            current_time - self.collections_cache_time < self.collections_cache_duration
        )

        # If cache is valid, show it immediately without placeholder (no flicker)
        if cache_valid:
            # Build sync status map for cached data
            cached_sync_status = {}
            collection_games_map = {}  # Group games by collection

            # Group games by collection
            for game in self.collections_games:
                collection_name = game.get('collection', 'Unknown')
                if collection_name not in collection_games_map:
                    collection_games_map[collection_name] = []
                collection_games_map[collection_name].append(game)

            # Calculate sync status for each collection
            for collection_name, games in collection_games_map.items():
                is_syncing = collection_name in self.actively_syncing_collections
                if is_syncing:
                    # Check if all games are downloaded
                    downloaded_count = sum(1 for game in games if game.get('is_downloaded', False))
                    total_count = len(games)
                    all_downloaded = downloaded_count == total_count
                    cached_sync_status[collection_name] = 'synced' if all_downloaded else 'syncing'
                else:
                    cached_sync_status[collection_name] = 'disabled'

            self.library_model.update_library(self.collections_games, group_by='collection', sync_status_map=cached_sync_status)
            return

        # Show loading placeholder for cases where we need to load data
        self.parent.log_message("Loading collections...")
        placeholder_games = [{
            'name': 'Loading...',
            'rom_id': 'placeholder_loading',
            'collection': 'Loading collections...',
            'is_downloaded': False,
            'platform': '',
            'file_name': ''
        }]
        self.library_model.update_library(placeholder_games, group_by='collection', loading=True)

        # CRITICAL: Force GTK to process pending events and render the placeholder
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

        # Capture the current generation to check if this load is still valid when it completes
        expected_generation = self.view_mode_generation

        def load_collections():
            try:
                # Get collection list first
                all_collections = self.parent.romm_client.get_collections()

                # Filter to only custom collections
                custom_collections = []
                for collection in all_collections:
                    is_custom = (
                        not collection.get('is_auto_generated', False) and
                        collection.get('type') != 'auto' and
                        'auto' not in collection.get('name', '').lower()
                    )
                    if is_custom:
                        custom_collections.append(collection)

                if not custom_collections:
                    GLib.idle_add(lambda: self.parent.log_message("No custom collections found"))
                    GLib.idle_add(lambda: self.library_model.update_library([], group_by='collection'))
                    return

                # Update placeholders with actual collection names (still loading games)
                def show_collection_placeholders():
                    # Create placeholder for each collection with real name
                    placeholder_games = []
                    for collection in custom_collections:
                        placeholder_game = {
                            'name': 'Loading...',
                            'rom_id': f'placeholder_{collection.get("id")}',
                            'collection': collection.get('name', 'Unknown Collection'),
                            'is_downloaded': False,
                            'platform': '',
                            'file_name': ''
                        }
                        placeholder_games.append(placeholder_game)

                    # Update tree with named placeholders (shows "Loading..." in status/size)
                    self.library_model.update_library(placeholder_games, group_by='collection', loading=True)
                    return False

                GLib.idle_add(show_collection_placeholders)

                # Create lookup map of existing games by ROM ID for download status
                existing_games_map = {}
                for game in self.parent.available_games:
                    rom_id = game.get('rom_id')
                    if rom_id:
                        existing_games_map[rom_id] = game
                
                all_collection_games = []
                collection_sync_status = {}  # Map collection name to sync status

                for collection in custom_collections:
                    collection_id = collection.get('id')
                    collection_name = collection.get('name', 'Unknown Collection')

                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    # Determine sync status for this collection
                    is_syncing = collection_name in self.actively_syncing_collections
                    if is_syncing:
                        # Check if fully synced
                        downloaded_count = 0
                        download_dir = Path(self.parent.rom_dir_row.get_text())

                        for rom in collection_roms:
                            platform_slug = rom.get('platform_slug', 'Unknown')
                            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                            platform_dir = download_dir / platform_slug
                            local_path = platform_dir / file_name
                            if self.parent.is_path_validly_downloaded(local_path):
                                downloaded_count += 1

                        if downloaded_count == len(collection_roms) and len(collection_roms) > 0:
                            collection_sync_status[collection_name] = 'synced'
                        else:
                            collection_sync_status[collection_name] = 'syncing'
                    else:
                        collection_sync_status[collection_name] = 'disabled'

                    # Build a lookup of parent folder ROMs by child filename so that
                    # download_game can find the parent without extra API calls.
                    # RomM's siblings[] field lists peer variants, NOT the parent folder,
                    # so we derive the relationship from the folder ROM's files[] here.
                    _parent_by_filename = {}
                    for _r in collection_roms:
                        if not _r.get('fs_extension', '') and _r.get('files', []):
                            for _f in _r.get('files', []):
                                _fname = _f.get('filename') or _f.get('file_name', '')
                                if _fname:
                                    _parent_by_filename[_fname] = _r

                    for rom in collection_roms:
                        # Folder-container ROMs are not playable games; they are
                        # populated implicitly when their variant files are downloaded.
                        if not rom.get('fs_extension', '') and rom.get('files', []):
                            continue

                        # First process the ROM normally
                        processed_game = self.parent.process_single_rom(rom, Path(self.parent.rom_dir_row.get_text()))

                        # Inject parent-ROM reference so the 404 fallback can find
                        # the folder ROM without scanning siblings at download time.
                        _rom_fs_name = rom.get('fs_name', '')
                        if _rom_fs_name and _rom_fs_name in _parent_by_filename:
                            processed_game['_parent_rom'] = _parent_by_filename[_rom_fs_name]
                            processed_game['_fs_extension'] = rom.get('fs_extension', '')

                        # Then merge with existing game data to preserve download status
                        rom_id = rom.get('id')
                        if rom_id and rom_id in existing_games_map:
                            existing_game = existing_games_map[rom_id]
                            # Preserve critical download info from existing game
                            processed_game['is_downloaded'] = existing_game.get('is_downloaded', False)
                            processed_game['local_path'] = existing_game.get('local_path')
                            processed_game['local_size'] = existing_game.get('local_size', 0)

                        # Add collection info
                        processed_game['collection'] = collection_name
                        all_collection_games.append(processed_game)
                
                # Store collections games separately AND update the instance variable
                all_collection_games_copy = []
                for game in all_collection_games:
                    all_collection_games_copy.append(game.copy())

                def update_collections_data():
                    # Check if this load is still valid (view mode hasn't changed)
                    if self.view_mode_generation != expected_generation:
                        print(f"⚠️ Discarding stale collections load (generation mismatch: {expected_generation} vs {self.view_mode_generation})")
                        return False

                    # Only update if we're still in collections view
                    if self.current_view_mode != 'collection':
                        print(f"⚠️ Discarding collections load (no longer in collections view)")
                        return False

                    import time
                    self.collections_games = all_collection_games_copy
                    self.collections_cache_time = time.time()  # Update cache timestamp
                    self.library_model.update_library(self.collections_games, group_by='collection', sync_status_map=collection_sync_status)
                    self.parent.log_message(f"Loaded {len(custom_collections)} custom collections with {len(all_collection_games)} games")
                    return False

                GLib.idle_add(update_collections_data)

            except Exception as e:
                def log_error():
                    # Only log error if still in the same view generation
                    if self.view_mode_generation == expected_generation:
                        self.parent.log_message(f"Failed to load collections: {e}")
                    return False
                GLib.idle_add(log_error)
        
        threading.Thread(target=load_collections, daemon=True).start()

        # Check if auto-sync should be restored when switching to collections view
        self.update_sync_button_state()        

    def bind_name_cell(self, factory, list_item):
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()
        expander = box.get_first_child()
        label = box.get_last_child()
        icon = expander.get_child()
        
        expander.set_list_row(tree_item)
        depth = tree_item.get_depth()
        box.set_margin_start(depth * 0)
        
        if isinstance(item, PlatformItem):
            icon.set_from_icon_name("folder-symbolic")

            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Dynamic label update for collections - just show the name, status dots will show the state
                def update_collection_label(*args):
                    collection_name = item.platform_name
                    label.set_text(collection_name)

                # Connect to name property changes to trigger label updates
                item.connect('notify::name', update_collection_label)
                update_collection_label()  # Initial update
            else:
                # For platforms view, use simple binding
                item.bind_property('name', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, DiscItem):
            # For disc items, show media-optical icon
            icon.set_from_icon_name("media-optical-symbolic")

            def update_disc_label(*args):
                label.set_text(item.name)

            item.connect('notify::name', update_disc_label)
            update_disc_label()
        else:
            # For games (GameItem), set up dynamic icon updates
            def update_icon_and_name(*args):
                # Update icon based on download status or multi-disc
                if item.game_data.get('is_multi_disc', False):
                    # Multi-disc game - show optical disc icon
                    icon.set_from_icon_name("media-optical-symbolic")
                elif item.game_data.get('is_downloaded', False):
                    icon.set_from_icon_name("object-select-symbolic")
                else:
                    icon.set_from_icon_name("folder-download-symbolic")

                # Update label
                label.set_text(item.name)

            # Connect to property changes that might affect the icon
            item.connect('notify::name', update_icon_and_name)
            item.connect('notify::is-downloaded', update_icon_and_name)

            # Initial update
            update_icon_and_name()

    def bind_status_cell(self, factory, list_item):
        """Show percentage/icons using Cairo drawing"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()

        # Get the drawing area and label from the box
        drawing_area = box.get_first_child()
        label = drawing_area.get_next_sibling()

        if isinstance(item, PlatformItem):
            # For platforms, show text status
            drawing_area.set_visible(False)
            label.set_visible(True)
            item.bind_property('status-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, DiscItem):
            # For discs, show download status with progress support
            label.set_visible(False)
            drawing_area.set_visible(True)

            def update_disc_status(*args):
                # Check for disc download progress
                rom_id = item.parent_game.get('rom_id') if item.parent_game else None
                disc_name = item.disc_data.get('name')
                disc_key = f"{rom_id}:{disc_name}" if rom_id and disc_name else None

                progress_info = None
                # First check disc_progress (for multi-disc game downloads)
                if disc_key and hasattr(self, 'disc_progress'):
                    progress_info = self.disc_progress.get(disc_key)

                # Also check game_progress for regional variants using their own ROM ID
                if not progress_info:
                    disc_rom_id = item.disc_data.get('rom_id')
                    if disc_rom_id:
                        progress_info = self.parent.download_progress.get(disc_rom_id)

                if progress_info and progress_info.get('downloading'):
                    # Show percentage using Cairo (orange)
                    progress = progress_info.get('progress', 0.0)
                    self.parent.draw_download_status_icon(drawing_area, 'downloading', progress)
                elif progress_info and progress_info.get('completed'):
                    # Show green checkmark icon
                    self.parent.draw_download_status_icon(drawing_area, 'completed')
                elif item.is_downloaded:
                    # Downloaded disc
                    self.parent.draw_download_status_icon(drawing_area, 'downloaded')
                else:
                    # Not downloaded
                    self.parent.draw_download_status_icon(drawing_area, 'not_downloaded')

            # Connect to property changes
            item.connect('notify::is-downloaded', update_disc_status)
            update_disc_status()
        elif isinstance(item, GameItem):
            sibling_files = item.game_data.get('_sibling_files', [])
            has_regional_variants = bool(sibling_files)

            if has_regional_variants:
                drawing_area.set_visible(False)
                label.set_visible(True)
                item.bind_property('status-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
            else:
                def update_status(*args):
                    rom_id = item.game_data.get('rom_id')
                    progress_info = self.parent.download_progress.get(rom_id) if rom_id else None

                    label.set_visible(False)
                    drawing_area.set_visible(True)

                    if progress_info and progress_info.get('downloading'):
                        progress = progress_info.get('progress', 0.0)
                        self.parent.draw_download_status_icon(drawing_area, 'downloading', progress)
                    elif progress_info and progress_info.get('completed'):
                        self.parent.draw_download_status_icon(drawing_area, 'completed')
                    elif progress_info and progress_info.get('failed'):
                        self.parent.draw_download_status_icon(drawing_area, 'failed')
                    else:
                        status_type = 'downloaded' if item.is_downloaded else 'not_downloaded'
                        self.parent.draw_download_status_icon(drawing_area, status_type)

                item.connect('notify::name', update_status)
                update_status()

    def bind_size_cell(self, factory, list_item):
        """Show download info with compact format"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        label = list_item.get_child()
        
        if isinstance(item, PlatformItem):
            item.bind_property('size-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, DiscItem):
            # For discs, show size with progress support
            def update_disc_size(*args):
                rom_id = item.parent_game.get('rom_id') if item.parent_game else None
                disc_name = item.disc_data.get('name')
                disc_key = f"{rom_id}:{disc_name}" if rom_id and disc_name else None

                progress_info = None
                # First check disc_progress (for multi-disc game downloads)
                if disc_key and hasattr(self, 'disc_progress'):
                    progress_info = self.disc_progress.get(disc_key)

                # Also check game_progress for regional variants using their own ROM ID
                if not progress_info:
                    disc_rom_id = item.disc_data.get('rom_id')
                    if disc_rom_id:
                        progress_info = self.parent.download_progress.get(disc_rom_id)

                if progress_info and progress_info.get('downloading'):
                    downloaded = progress_info.get('downloaded', 0)
                    total = progress_info.get('total', 0)
                    speed = progress_info.get('speed', 0)

                    def format_size_compact(bytes_val):
                        if bytes_val >= 1000**3:
                            return f"{bytes_val / (1000**3):.1f}G"
                        elif bytes_val >= 1000**2:
                            return f"{bytes_val / (1000**2):.0f}M"
                        else:
                            return f"{bytes_val / 1000:.0f}K"

                    if total > 0:
                        size_text = f"{format_size_compact(downloaded)}/{format_size_compact(total)}"
                    else:
                        size_text = format_size_compact(downloaded)

                    if speed > 0:
                        speed_str = format_size_compact(speed)
                        final_text = f"{size_text} @{speed_str}/s"
                    else:
                        final_text = f"{size_text} ..."

                    label.set_text(final_text)
                else:
                    size_text = item.size_text
                    label.set_text(size_text)

            item.connect('notify::size-text', update_disc_size)
            item.connect('notify::is-downloaded', update_disc_size)
            update_disc_size()  # Initial update
        elif isinstance(item, GameItem):
            def update_size(*args):
                rom_id = item.game_data.get('rom_id')
                progress_info = self.parent.download_progress.get(rom_id) if rom_id else None
                
                if progress_info and progress_info.get('downloading'):
                    downloaded = progress_info.get('downloaded', 0)
                    total = progress_info.get('total', 0)
                    speed = progress_info.get('speed', 0)
                    
                    def format_size_compact(bytes_val):
                        if bytes_val >= 1000**3:
                            return f"{bytes_val / (1000**3):.1f}G"
                        elif bytes_val >= 1000**2:
                            return f"{bytes_val / (1000**2):.0f}M"
                        else:
                            return f"{bytes_val / 1000:.0f}K"
                    
                    if total > 0:
                        size_text = f"{format_size_compact(downloaded)}/{format_size_compact(total)}"
                    else:
                        size_text = format_size_compact(downloaded)
                    
                    if speed > 0:
                        speed_str = format_size_compact(speed)
                        final_text = f"{size_text} @{speed_str}/s"
                    else:
                        final_text = f"{size_text} ..."
                        
                    label.set_text(final_text)
                else:
                    size_text = item.size_text
                    label.set_text(size_text)
            
            item.connect('notify::name', update_size)
            update_size()  # Initial update

    def setup_status_cell(self, factory, list_item):
        """Cairo-drawn status icons for download status"""
        # Create a box to hold either a drawing area or label (for percentage)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # Create drawing area for icons and text (wider to accommodate percentage)
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_size_request(50, 16)
        drawing_area.set_halign(Gtk.Align.CENTER)
        drawing_area.set_valign(Gtk.Align.CENTER)
        box.append(drawing_area)

        # Create label for percentage text (hidden by default)
        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.add_css_class('numeric')
        label.set_visible(False)
        box.append(label)

        list_item.set_child(box)


    def setup_size_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.END)
        label.add_css_class('numeric')
        list_item.set_child(label)

    def setup_sync_status_cell(self, factory, list_item):
        """Setup sync status indicator for collections"""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # Create a drawing area for the colored dot
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_size_request(10, 10)
        drawing_area.set_halign(Gtk.Align.CENTER)
        drawing_area.set_valign(Gtk.Align.CENTER)

        box.append(drawing_area)
        list_item.set_child(box)

    def bind_sync_status_cell(self, factory, list_item):
        """Bind sync status for collections"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()
        drawing_area = box.get_first_child()

        if isinstance(item, PlatformItem):
            def update_status(*args):
                status = item.sync_status_text

                def draw_func(area, cr, width, height):
                    # Determine color based on status
                    if status == 'synced':
                        cr.set_source_rgb(0.29, 0.86, 0.50)  # Green (#4ade80)
                    elif status == 'syncing':
                        cr.set_source_rgb(0.98, 0.57, 0.24)  # Orange (#fb923c)
                    elif status == 'disabled':
                        cr.set_source_rgb(0.42, 0.45, 0.50)  # Grey (#6b7280)
                    elif status == 'loading':
                        cr.set_source_rgb(0.6, 0.6, 0.6)  # Light grey
                    else:
                        return  # Don't draw anything for empty status

                    # Draw a filled circle
                    radius = min(width, height) / 2.0
                    cr.arc(width / 2.0, height / 2.0, radius - 1, 0, 2 * 3.14159)
                    cr.fill()

                drawing_area.set_draw_func(draw_func)
                drawing_area.queue_draw()

            item.connect('notify::sync-status-text', update_status)
            update_status()  # Initial draw
        elif isinstance(item, GameItem):
            # Games don't have sync status - don't draw anything
            drawing_area.set_draw_func(lambda *args: None)

    def create_action_bar(self):
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_box.set_margin_top(6)
        
        # Original single-item action buttons (left side) - now work on multiple items too
        single_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        single_actions.add_css_class('linked')
        
        # Download/Launch button - now handles multiple selections
        self.action_button = Gtk.Button(label="Download")
        self.action_button.add_css_class('warning')  # Start with warning style for Download
        self.action_button.set_sensitive(False)
        self.action_button.set_size_request(125, -1)  # Fixed width for text
        self.action_button.set_hexpand(False)
        self.action_button.set_halign(Gtk.Align.START)
        self._action_button_handler_id = self.action_button.connect('clicked', self.on_action_clicked)
        single_actions.append(self.action_button)
        
        # Delete button - now handles multiple selections
        self.delete_button = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.delete_button.set_tooltip_text("Delete selected ROM(s)")
        self.delete_button.add_css_class('destructive-action')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_clicked)
        single_actions.append(self.delete_button)

        # --- Create button with RomM Logo ---
        self.open_in_romm_button = Gtk.Button()
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Try multiple icon locations for AppImage compatibility
        icon_locations = [
            os.path.join(script_dir, 'romm_icon.png'),  # AppImage location
            os.path.join(script_dir, '..', 'assets', 'icons', 'romm_icon.png'),  # Regular install
            'romm_icon.png'  # Fallback
        ]

        romm_icon_path = None
        for location in icon_locations:
            if os.path.exists(location):
                romm_icon_path = location
                break

        if romm_icon_path:
            image = Gtk.Image.new_from_file(romm_icon_path)
            image.set_pixel_size(16)
            self.open_in_romm_button.set_child(image)
        else:
            # Fallback to text if icon not found
            self.open_in_romm_button.set_label("RomM")

        self.open_in_romm_button.set_tooltip_text("Open game/platform page in RomM")
        self.open_in_romm_button.set_sensitive(False)
        self.open_in_romm_button.connect('clicked', self.on_open_in_romm_clicked)
        single_actions.append(self.open_in_romm_button)

        # Save state history button - browse/restore older save state versions
        self.history_button = Gtk.Button.new_from_icon_name("document-open-recent-symbolic")
        self.history_button.set_tooltip_text("Browse and restore save state versions")
        self.history_button.set_sensitive(False)
        self.history_button.connect('clicked', self.on_save_history_clicked)
        single_actions.append(self.history_button)

        # Save file history button - browse/restore battery save versions (floppy disk icon)
        self.save_file_history_button = Gtk.Button.new_from_icon_name("media-floppy-symbolic")
        self.save_file_history_button.set_tooltip_text("Browse and restore save file versions")
        self.save_file_history_button.set_sensitive(False)
        self.save_file_history_button.connect('clicked', self.on_save_file_history_clicked)
        single_actions.append(self.save_file_history_button)

        
        action_box.append(single_actions)
        
        # Separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_start(6)
        separator.set_margin_end(6)
        action_box.append(separator)
        
        # Bulk selection controls
        bulk_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        bulk_box.add_css_class('linked')
        
        # Select all buttons
        select_all_btn = Gtk.Button(label="All")
        select_all_btn.connect('clicked', self.on_select_all)
        select_all_btn.set_tooltip_text("Select all games")
        bulk_box.append(select_all_btn)
        
        select_downloaded_btn = Gtk.Button(label="Downloaded")
        select_downloaded_btn.connect('clicked', self.on_select_downloaded)
        select_downloaded_btn.set_tooltip_text("Select downloaded games")
        bulk_box.append(select_downloaded_btn)
        
        select_none_btn = Gtk.Button(label="None")
        select_none_btn.connect('clicked', self.on_select_none)
        select_none_btn.set_tooltip_text("Clear selection")
        bulk_box.append(select_none_btn)
        
        action_box.append(bulk_box)
        
        # Selection info (right side)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        spacer.set_size_request(20, -1)  # Minimum width to prevent UI jumping
        action_box.append(spacer)
        
        # Selection label with ellipsization but flexible width
        self.selection_label = Gtk.Label()
        self.selection_label.set_text("No selection")
        self.selection_label.add_css_class('dim-label')
        self.selection_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.selection_label.set_xalign(1.0)  # Right-align text
        self.selection_label.set_size_request(200, -1)  # Give it minimum 200px width
        action_box.append(self.selection_label)
        
        return action_box
    
    def _update_history_button(self):
        """Enable Save history only for a single downloaded game while connected.

        Set unconditionally at the top of update_action_buttons so it stays correct
        across that method's many early returns.
        """
        if not hasattr(self, 'history_button'):
            return
        connected = bool(self.parent.romm_client and self.parent.romm_client.authenticated)
        game = self.selected_game
        rom_id = None
        if self.selected_disc:
            rom_id = self.selected_disc.get('rom_id')
            downloaded = self.selected_disc.get('is_downloaded', False)
        elif game:
            rom_id = game.get('rom_id')
            downloaded = game.get('is_downloaded', False)
        else:
            downloaded = False
        # Single selection only (no bulk), connected, downloaded, with a rom_id
        single = len(getattr(self, 'selected_game_keys', set())) <= 1
        is_sensitive = bool(connected and rom_id and downloaded and single)
        self.history_button.set_sensitive(is_sensitive)
        if hasattr(self, 'save_file_history_button'):
            self.save_file_history_button.set_sensitive(is_sensitive)

    def update_action_buttons(self):
        """Update action buttons based on selected game(s) or platform"""
        # Allow button updates during bulk downloads to show Cancel state
        # but still block during other dialogs
        is_bulk_download = self.parent._bulk_download_in_progress if hasattr(self, 'parent') else False
        if getattr(self, '_selection_blocked', False) and not is_bulk_download:
            return

        self._update_history_button()

        # Priority 0: Check for selected discs first
        selected_discs = self.get_selected_discs()
        if selected_discs:
            # Check if any disc is not downloaded
            not_downloaded_discs = [d for d in selected_discs if not d['disc'].get('is_downloaded', False)]
            is_connected = self.parent.romm_client and self.parent.romm_client.authenticated

            # Clear all button style classes first
            self.action_button.remove_css_class('warning')
            self.action_button.remove_css_class('suggested-action')
            self.action_button.remove_css_class('destructive-action')

            if not_downloaded_discs and is_connected:
                self.action_button.set_label(f"Download ({len(not_downloaded_discs)})")
                self.action_button.add_css_class('warning')
                self.action_button.set_sensitive(True)
            else:
                self.action_button.set_label("Download")
                self.action_button.set_sensitive(False)

            # Enable delete if any disc is downloaded
            downloaded_discs = [d for d in selected_discs if d['disc'].get('is_downloaded', False)]
            self.delete_button.set_sensitive(len(downloaded_discs) > 0)
            self.open_in_romm_button.set_sensitive(False)
            return  # Exit early

        # ADD THIS BLOCK HERE:
        # Priority 1: Check for single row selection first
        if self.selected_game:
            # Check if a specific disc is selected
            if self.selected_disc:
                # Individual disc selected - show Launch button if downloaded
                is_disc_downloaded = self.selected_disc.get('is_downloaded', False)
                is_connected = self.parent.romm_client and self.parent.romm_client.authenticated
                is_regional_variant = self.selected_disc.get('is_regional_variant', False)

                # Clear all button style classes first
                self.action_button.remove_css_class('warning')
                self.action_button.remove_css_class('suggested-action')
                self.action_button.remove_css_class('destructive-action')

                if is_disc_downloaded:
                    self.action_button.set_label("Launch")
                    self.action_button.add_css_class('suggested-action')
                    self.action_button.set_sensitive(True)
                else:
                    # Regional variants CAN be downloaded individually, multi-disc games cannot
                    if is_regional_variant and is_connected:
                        self.action_button.set_label("Download")
                        self.action_button.add_css_class('warning')
                        self.action_button.set_sensitive(True)
                    else:
                        # Multi-disc game - cannot download individual discs
                        self.action_button.set_label("Download")
                        self.action_button.set_sensitive(False)

                # Enable delete for downloaded regional variants, disable for multi-disc games
                if is_regional_variant and is_disc_downloaded:
                    self.delete_button.set_sensitive(True)
                else:
                    self.delete_button.set_sensitive(False)
                self.open_in_romm_button.set_sensitive(False)
                return  # Exit early

            # Game selected (not a disc)
            is_downloaded = self.selected_game.get('is_downloaded', False)
            is_connected = self.parent.romm_client and self.parent.romm_client.authenticated
            rom_id = self.selected_game.get('rom_id')

            # Check if download is in progress FOR THIS SPECIFIC GAME
            is_downloading = (rom_id and rom_id in self.parent.download_progress and
                            self.parent.download_progress[rom_id].get('downloading', False))

            # Check if this is part of a bulk download
            is_bulk_download = self.parent._bulk_download_in_progress

            # Clear all button style classes first
            self.action_button.remove_css_class('warning')
            self.action_button.remove_css_class('suggested-action')
            self.action_button.remove_css_class('destructive-action')

            if is_downloading:
                # Always show "Cancel" for single row selection
                # (bulk downloads are handled in the multiple checkbox selection case)
                self.action_button.set_label("Cancel")
                self.action_button.add_css_class('destructive-action')
            elif is_downloaded:
                self.action_button.set_label("Launch")
                self.action_button.add_css_class('suggested-action')
            else:
                self.action_button.set_label("Download")
                self.action_button.add_css_class('warning')

            self.action_button.set_sensitive(True)
            # Check if game is in autosync collection - disable delete if so
            is_in_autosync = self.is_game_in_autosync_collection(self.selected_game)
            self.delete_button.set_sensitive(is_downloaded and not is_in_autosync)
            self.open_in_romm_button.set_sensitive(is_connected and self.selected_game.get('rom_id'))
            return  # Exit early, don't check other selections
        
        selected_games = self.get_selected_games()
        
        is_connected = self.parent.romm_client and self.parent.romm_client.authenticated
        
        # Priority 2: Check for checkbox selections first (to determine priority)
        selected_games = []

        # FIX: Use correct games source based on view mode
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            games_to_check = getattr(self, 'collections_games', [])
        else:
            games_to_check = self.parent.available_games

        for game in games_to_check:  # CHANGED: was self.parent.available_games
            # Handle collection mode differently
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                rom_id = game.get('rom_id')
                collection_name = game.get('collection', '')
                if rom_id and collection_name:
                    collection_key = f"collection:{rom_id}:{collection_name}"
                    if collection_key in self.selected_game_keys:
                        selected_games.append(game)
            else:
                # Standard platform mode logic
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                    selected_games.append(game)
                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                    selected_games.append(game)
        
        # Priority 2: Handle checkbox selections (takes precedence when present)
        if selected_games:
            downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]
            # Exclude games that are currently downloading from not_downloaded list
            # Snapshot items: a worker thread may add/remove keys concurrently,
            # so iterate a copy and read the value from it (no re-indexing).
            downloading_rom_ids = set(rid for rid, p in dict(self.parent.download_progress).items()
                                     if p.get('downloading', False))
            not_downloaded_games = [g for g in selected_games
                                   if not g.get('is_downloaded', False)
                                   and g.get('rom_id') not in downloading_rom_ids]

            if len(selected_games) == 1:
                # Single checkbox selection
                game = selected_games[0]
                is_downloaded = game.get('is_downloaded', False)
                rom_id = game.get('rom_id')

                # ADD THIS CHECK for collections view:
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    # In collections, ensure we check the actual download status
                    if rom_id:
                        # Cross-reference with main games list for accurate download status
                        for main_game in self.parent.available_games:
                            if main_game.get('rom_id') == rom_id:
                                is_downloaded = main_game.get('is_downloaded', False)
                                break

                # Check if download is in progress FOR THIS SPECIFIC GAME
                is_downloading = (rom_id and rom_id in self.parent.download_progress and
                                self.parent.download_progress[rom_id].get('downloading', False))

                # Check if this is part of a bulk download
                is_bulk_download = self.parent._bulk_download_in_progress

                # Clear all button style classes first
                self.action_button.remove_css_class('warning')
                self.action_button.remove_css_class('suggested-action')
                self.action_button.remove_css_class('destructive-action')

                if is_downloading:
                    # Always show "Cancel" for single selection
                    # (bulk downloads are handled in the multiple selection case)
                    self.action_button.set_label("Cancel")
                    self.action_button.add_css_class('destructive-action')
                elif is_downloaded:
                    self.action_button.set_label("Launch")
                    self.action_button.add_css_class('suggested-action')
                else:
                    self.action_button.set_label("Download")
                    self.action_button.add_css_class('warning')

                self.action_button.set_sensitive(True)
                # Check if game is in autosync collection - disable delete if so
                is_in_autosync = self.is_game_in_autosync_collection(game)
                self.delete_button.set_sensitive(is_downloaded and not is_in_autosync)
                self.open_in_romm_button.set_sensitive(is_connected and game.get('rom_id'))
            else:
                # Multiple checkbox selections
                # Check if this is part of a bulk download
                is_bulk_download = self.parent._bulk_download_in_progress

                # Check if any selected games are currently downloading
                downloading_games = [g for g in selected_games
                                   if g.get('rom_id') and g.get('rom_id') in self.parent.download_progress
                                   and self.parent.download_progress[g.get('rom_id')].get('downloading', False)]

                # Clear all button style classes first
                self.action_button.remove_css_class('warning')
                self.action_button.remove_css_class('suggested-action')
                self.action_button.remove_css_class('destructive-action')

                # Prioritize bulk download state - show Cancel All even if individual downloads haven't started yet
                if is_bulk_download:
                    self.action_button.set_label("Cancel All")
                    self.action_button.add_css_class('destructive-action')
                    self.action_button.set_sensitive(True)
                elif downloading_games:
                    # Multiple individual downloads (not part of bulk)
                    self.action_button.set_label(f"Cancel ({len(downloading_games)})")
                    self.action_button.add_css_class('destructive-action')
                    self.action_button.set_sensitive(True)
                elif not_downloaded_games:
                    self.action_button.set_label(f"Download ({len(not_downloaded_games)})")
                    self.action_button.add_css_class('warning')
                    self.action_button.set_sensitive(True)
                elif downloaded_games:
                    self.action_button.set_label("Launch")
                    self.action_button.set_sensitive(False)

                # Check if any selected games are in autosync collections
                has_autosync_game = any(self.is_game_in_autosync_collection(g) for g in selected_games)
                self.delete_button.set_sensitive(len(downloaded_games) > 0 and not has_autosync_game)
                self.open_in_romm_button.set_sensitive(False)  # Disable for multi-selection
            return

        # Priority 3: Check for single platform row selection (only if no checkboxes and no game row selected)
        selection_model = self.column_view.get_model()
        selected_positions = []
        for i in range(selection_model.get_n_items()):
            if selection_model.is_selected(i):
                selected_positions.append(i)
        
        if len(selected_positions) == 1:
            tree_item = selection_model.get_item(selected_positions[0])
            item = tree_item.get_item()

            if isinstance(item, PlatformItem):
                # Check if this is a collection (not a platform)
                is_collection_view = hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection'

                if is_collection_view:
                    # Collection selected - enable delete button
                    collection_name = item.platform_name
                    has_downloaded_games = any(g.get('is_downloaded', False) for g in item.games)
                    self.action_button.set_sensitive(False)
                    self.delete_button.set_sensitive(has_downloaded_games)
                    self.delete_button.set_tooltip_text(f"Delete downloaded games from '{collection_name}'")
                    self.open_in_romm_button.set_sensitive(is_connected)
                    # Store the selected collection for delete handler
                    self.selected_collection = collection_name
                    return
                else:
                    # Platform selected - disable delete
                    self.action_button.set_sensitive(False)
                    self.delete_button.set_sensitive(False)
                    self.open_in_romm_button.set_sensitive(is_connected)
                    self.selected_collection = None
                    return

        # No selections - disable all buttons
        # Clear all button style classes first
        self.action_button.remove_css_class('warning')
        self.action_button.remove_css_class('suggested-action')
        self.action_button.remove_css_class('destructive-action')
        self.action_button.set_sensitive(False)
        self.action_button.set_label("Download")
        self.delete_button.set_sensitive(False)
        self.open_in_romm_button.set_sensitive(False)

    def update_group_filter(self, games, group_by='platform'):
        """Update filter dropdown for platforms or collections"""
        groups = set()
        group_key = 'collection' if group_by == 'collection' else 'platform'

        for game in games:
            groups.add(game.get(group_key, 'Unknown'))

        prefix = "All Collections" if group_by == 'collection' else "All Platforms"
        group_list = [prefix] + sorted(groups)

        string_list = Gtk.StringList()
        for group in group_list:
            string_list.append(group)

        # Block the notify::selected-item signal while updating to prevent duplicate update_library calls
        try:
            # Use a flag to prevent recursive calls
            if not hasattr(self, '_updating_filter'):
                self._updating_filter = False

            if self._updating_filter:
                return  # Already updating, skip

            self._updating_filter = True
            self.platform_filter.set_model(string_list)
            self.platform_filter.set_selected(0)
            self._updating_filter = False
        except Exception as e:
            self._updating_filter = False
            print(f"Error updating group filter: {e}")
    
    def on_selection_changed(self, selection_model, position, n_items):
        """Handle selection changes for both single and multi-selection"""
        # Find selected positions
        selected_positions = []
        for i in range(selection_model.get_n_items()):
            if selection_model.is_selected(i):
                selected_positions.append(i)

        if len(selected_positions) == 1:
            # Single item selected
            tree_item = selection_model.get_item(selected_positions[0])
            item = tree_item.get_item()

            if isinstance(item, GameItem):
                self.selected_game = item.game_data
                self.selected_disc = None  # Clear disc selection
                self.selected_collection = None  # Clear collection selection
                # Clear checkbox selections without full refresh
                if self.selected_checkboxes or self.selected_rom_ids or self.selected_game_keys:
                    self.selected_checkboxes.clear()
                    self.selected_rom_ids.clear()
                    self.selected_game_keys.clear()
                    GLib.idle_add(self.force_checkbox_sync)
                    GLib.idle_add(self.refresh_all_platform_checkboxes)
            elif isinstance(item, DiscItem):
                # Disc selected - store both the disc and its parent game
                self.selected_disc = item.disc_data
                self.selected_game = item.parent_game  # Store parent game for context
                self.selected_collection = None  # Clear collection selection
                # Clear checkbox selections without full refresh
                if self.selected_checkboxes or self.selected_rom_ids or self.selected_game_keys:
                    self.selected_checkboxes.clear()
                    self.selected_rom_ids.clear()
                    self.selected_game_keys.clear()
                    GLib.idle_add(self.force_checkbox_sync)
                    GLib.idle_add(self.refresh_all_platform_checkboxes)
            elif isinstance(item, PlatformItem):
                self.selected_game = None
                self.selected_disc = None  # Clear disc selection
                # Note: selected_collection is set in update_action_buttons for collection rows
            # Update button states for both game and platform selections
            self.update_action_buttons()
        else:
            # Multiple or no row selection
            self.selected_game = None
            self.selected_disc = None  # Clear disc selection
            self.selected_collection = None  # Clear collection selection
            # Update button states
            self.update_action_buttons()

        # Update selection label
        self.update_selection_label()

        # Update collection auto-sync button if in collections view
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            self.update_sync_button_state()

    def update_selection_label(self):
        """Update the selection label text"""
        # SKIP UPDATES DURING DIALOG
        if getattr(self, '_selection_blocked', False):
            return

        # Count selected discs
        disc_count = len(self.get_selected_discs())

        # Count selected games using the same logic as action buttons
        selected_count = 0

        # FIX: Use correct games source based on view mode
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            games_to_check = getattr(self, 'collections_games', [])
        else:
            games_to_check = self.parent.available_games

        for game in games_to_check:  # CHANGED: was self.parent.available_games
            # Handle collection mode differently
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                rom_id = game.get('rom_id')
                collection_name = game.get('collection', '')
                if rom_id and collection_name:
                    collection_key = f"collection:{rom_id}:{collection_name}"
                    if collection_key in self.selected_game_keys:
                        selected_count += 1
            else:
                # Standard platform mode logic
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                    selected_count += 1
                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                    selected_count += 1

        # Rest of the method unchanged...
        if disc_count > 0:
            self.selection_label.set_text(f"{disc_count} disc{'s' if disc_count != 1 else ''} checked")
        elif self.selected_disc and self.selected_game:
            # Show disc name when a disc is selected
            game_name = self.selected_game.get('name', 'Unknown')
            disc_name = self.selected_disc.get('name', 'Unknown Disc')
            self.selection_label.set_text(f"{game_name} - {disc_name}")
        elif self.selected_game and selected_count == 0:
            game_name = self.selected_game.get('name', 'Unknown')
            self.selection_label.set_text(f"{game_name}")
        elif selected_count > 0:
            if self.selected_game:
                game_name = self.selected_game.get('name', 'Unknown')
                self.selection_label.set_text(f"Row: {game_name} | {selected_count} checked")
            else:
                self.selection_label.set_text(f"{selected_count} games checked")
        else:
            self.selection_label.set_text("No selection")

    def on_search_changed(self, search_entry):
        """Handle search text changes"""
        self.search_text = search_entry.get_text().lower().strip()
        
        filtered_games = self.apply_filters(self.parent.available_games)
        sorted_games = self.sort_games_consistently(filtered_games)
        self.library_model.update_library(sorted_games)
        self.filtered_games = sorted_games
        
        self.auto_expand_platforms_with_results(sorted_games)
    
    def on_platform_filter_changed(self, dropdown, pspec):
        """Handle platform/collection filter changes"""
        # Skip if we're programmatically updating the filter
        if getattr(self, '_updating_filter', False):
            return

        # Apply combined filters
        filtered_games = self.apply_filters(self.parent.available_games)

        # Determine current view mode
        group_by = 'collection' if hasattr(self, 'view_mode_toggle') and self.view_mode_toggle.get_active() else 'platform'

        self.library_model.update_library(filtered_games, group_by=group_by)
        self.filtered_games = filtered_games
        
    def on_expand_all(self, button):
        """Expand all tree items - simple and direct approach"""
        def expand_all_platforms():
            model = self.library_model.tree_model
            if not model:
                return False
            
            # Simple approach: just expand everything, multiple times if needed
            expanded_any = False
            
            # Do this multiple times to catch any lazy-loaded items
            for attempt in range(3):  # Try up to 3 times
                current_expanded = 0
                total_items = model.get_n_items()
                
                for i in range(total_items):
                    try:
                        tree_item = model.get_item(i)
                        if tree_item and tree_item.get_depth() == 0:  # Platform items
                            if not tree_item.get_expanded():
                                tree_item.set_expanded(True)
                                expanded_any = True
                                current_expanded += 1
                    except Exception as e:
                        print(f"Error expanding item {i}: {e}")
                        continue
                
                print(f"Expand attempt {attempt + 1}: expanded {current_expanded} platforms")
                
                # If we didn't expand anything this round, we're probably done
                if current_expanded == 0:
                    break
            
            if expanded_any:
                print(f"✅ Expand All completed")
            else:
                print(f"⚠️ No platforms found to expand")
            
            return False
        
        # Run immediately
        GLib.idle_add(expand_all_platforms)

    def on_collapse_all(self, button):
        """Collapse all tree items with proper state saving"""
        model = self.library_model.tree_model
        collapsed_count = 0
        
        for i in range(model.get_n_items()):
            item = model.get_item(i)
            if item and item.get_depth() == 0:  # Top level platform items
                if item.get_expanded():
                    item.set_expanded(False)
                    collapsed_count += 1
        
        print(f"👆 Collapse All: collapsed {collapsed_count} platforms")
        # The expansion tracking will automatically save the state
    
    def on_refresh_library(self, button):
        """Refresh library data based on current view mode"""
        if self.current_view_mode == 'collection':
            # Clear collections cache and reload
            self.collections_cache_time = 0
            self.load_collections_view()
        else:
            # Regular platform refresh
            if hasattr(self.parent, 'refresh_games_list'):
                self.parent.refresh_games_list()
    
    def on_action_clicked(self, button):
        """Handle main action button (download/launch/cancel) for single or multiple items"""

        # Check if bulk download is in progress first - this takes priority
        if self.parent._bulk_download_in_progress:
            # Cancel the bulk download by passing any rom_id
            # Get the first available rom_id from selected games or download progress
            rom_id = None
            if self.selected_game:
                rom_id = self.selected_game.get('rom_id')
            if not rom_id and self.parent.download_progress:
                # Snapshot keys: worker threads may mutate the dict concurrently.
                rom_id = next(iter(list(self.parent.download_progress.keys())), None)

            if rom_id and hasattr(self.parent, 'cancel_download'):
                self.parent.cancel_download(rom_id)
            return

        # Check for selected discs first
        selected_discs = self.get_selected_discs()
        if selected_discs:
            # Check if any of the selected discs are actually regional variants
            regional_variants = [d for d in selected_discs if d['disc'].get('is_regional_variant', False)]

            if regional_variants and len(regional_variants) == len(selected_discs):
                # All selected items are regional variants - allow individual download
                self.parent.download_regional_variants(regional_variants)
                return

            # Individual disc downloads are disabled due to RomM API limitations
            # RomM always downloads all discs when requesting any disc from a multi-disc game
            game_name = selected_discs[0]['game'].get('name', 'this game')
            self.parent.log_message(
                f"⚠️ Cannot download individual discs. Please select and download '{game_name}' "
                f"to get all discs at once. (RomM API limitation)"
            )
            # Clear selections
            GLib.timeout_add(100, self.clear_checkbox_selections_smooth)
            return

        # ADD THIS BLOCK FIRST:
        # Priority: Handle single row selection directly
        if self.selected_game:
            game = self.selected_game
            rom_id = game.get('rom_id')

            # Check if a specific disc is selected
            if self.selected_disc:
                is_downloaded = self.selected_disc.get('is_downloaded', False)
                is_regional_variant = self.selected_disc.get('is_regional_variant', False)

                if is_downloaded:
                    # Launch the specific disc/variant
                    if hasattr(self.parent, 'launch_disc'):
                        self.parent.launch_disc(game, self.selected_disc)
                    else:
                        self.parent.log_message("⚠️ Disc launching not implemented")
                elif is_regional_variant:
                    # Download the regional variant
                    variant_info = {
                        'disc': self.selected_disc,
                        'game': game
                    }
                    self.parent.download_regional_variants([variant_info])
                # For multi-disc games, do nothing (cannot download individual discs)
                return  # Exit early

            # Check if download is in progress - if so, cancel it
            is_downloading = (rom_id and rom_id in self.parent.download_progress and
                            self.parent.download_progress[rom_id].get('downloading', False))

            if is_downloading:
                # Cancel the download
                if hasattr(self.parent, 'cancel_download'):
                    self.parent.cancel_download(rom_id)
            elif game.get('is_downloaded', False):
                # Launch the game
                if hasattr(self.parent, 'launch_game'):
                    self.parent.launch_game(game)
            else:
                # Download the game
                if hasattr(self.parent, 'download_game'):
                    self.parent.download_game(game)
            return  # Exit early, don't process checkbox logic
        selected_games = []
        
        # Priority 1: If there's a row selection (single game clicked), use that exclusively
        if self.selected_game:
            selected_games = [self.selected_game]
        else:
            # FIX: Use correct games source based on view mode
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                games_to_check = getattr(self, 'collections_games', [])
            else:
                games_to_check = self.parent.available_games
            
            for game in games_to_check:  # CHANGED: was self.parent.available_games
                # Handle collection mode differently
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    rom_id = game.get('rom_id')
                    collection_name = game.get('collection', '')
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        if collection_key in self.selected_game_keys:
                            selected_games.append(game)
                else:
                    # Standard platform mode logic
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        selected_games.append(game)
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        selected_games.append(game)
            
            if not selected_games:
                self.parent.log_message("No games selected")
                return
            
            if len(selected_games) == 1:
                # Single selection - use existing logic
                game = selected_games[0]
                rom_id = game.get('rom_id')

                # Check if download is in progress - if so, cancel it
                is_downloading = (rom_id and rom_id in self.parent.download_progress and
                                self.parent.download_progress[rom_id].get('downloading', False))

                if is_downloading:
                    # Cancel the download
                    if hasattr(self.parent, 'cancel_download'):
                        self.parent.cancel_download(rom_id)
                elif game.get('is_downloaded', False):
                    # Launch the game
                    if hasattr(self.parent, 'launch_game'):
                        self.parent.launch_game(game)
                else:
                    # Download the game
                    if hasattr(self.parent, 'download_game'):
                        self.parent.download_game(game)
            else:
                # Multiple selection
                # Check if this is a bulk download that should be cancelled
                is_bulk_download = self.parent._bulk_download_in_progress
                if is_bulk_download:
                    # Cancel the bulk download - pick any downloading game and cancel it
                    # This will trigger bulk cancellation
                    for game in selected_games:
                        rom_id = game.get('rom_id')
                        if rom_id and rom_id in self.parent.download_progress:
                            if self.parent.download_progress[rom_id].get('downloading', False):
                                if hasattr(self.parent, 'cancel_download'):
                                    self.parent.cancel_download(rom_id)
                                break
                else:
                    # Not a bulk download - check for download action
                    not_downloaded_games = [g for g in selected_games if not g.get('is_downloaded', False)]

                    if not_downloaded_games:
                        # Download multiple games immediately without confirmation
                        if hasattr(self.parent, 'download_multiple_games'):
                            self.parent.download_multiple_games(not_downloaded_games)

            # Clear checkbox selections after operation
            # Don't clear if bulk download started - selections will be cleared when bulk completes
            if len(selected_games) > 1 and not self.parent._bulk_download_in_progress:
                GLib.timeout_add(500, self.clear_checkbox_selection)  # Small delay for UI feedback

    def on_delete_clicked(self, button):
        """Handle delete button for single or multiple items, including collections"""
        # Check if a collection row is selected (not individual games)
        if hasattr(self, 'selected_collection') and self.selected_collection:
            # Collection deletion
            self.delete_collection(self.selected_collection)
            return

        # Check for selected discs first (checkbox selections)
        selected_discs = self.get_selected_discs()
        if selected_discs:
            # Delete selected discs
            for item in selected_discs:
                self.parent.delete_disc(item['game'], item['disc'])
            # Clear selections
            GLib.timeout_add(500, self.clear_checkbox_selection)
            return

        # Check for single disc/variant row selection (not checkbox)
        if self.selected_game and self.selected_disc:
            # Delete the specific disc/variant that's selected
            self.parent.delete_disc(self.selected_game, self.selected_disc)
            return

        selected_games = []

        # Priority 1: If there's a row selection (single game clicked), use that exclusively
        if self.selected_game:
            selected_games = [self.selected_game]
        else:
            # FIX: Use correct games source based on view mode
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                games_to_check = getattr(self, 'collections_games', [])
            else:
                games_to_check = self.parent.available_games

            for game in games_to_check:  # CHANGED: was self.parent.available_games
                # Handle collection mode differently
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    rom_id = game.get('rom_id')
                    collection_name = game.get('collection', '')
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        if collection_key in self.selected_game_keys:
                            selected_games.append(game)
                else:
                    # Standard platform mode logic
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        selected_games.append(game)
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        selected_games.append(game)

        downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]

        if not downloaded_games:
            return

        if len(downloaded_games) == 1:
            # Single deletion - use existing logic
            if hasattr(self.parent, 'delete_game_file'):
                self.parent.delete_game_file(downloaded_games[0])
        else:
            # Multiple deletion
            if hasattr(self.parent, 'delete_multiple_games'):
                self.parent.delete_multiple_games(downloaded_games)

        # Clear checkbox selections after operation
        if len(downloaded_games) > 1:  # Only clear for multi-selection operations
            GLib.timeout_add(500, self.clear_checkbox_selection)  # Small delay for UI feedback

    def delete_collection(self, collection_name):
        """Delete downloaded games from a collection with safety checks"""
        # Get all games in this collection
        collection_games = [g for g in self.collections_games if g.get('collection') == collection_name]
        downloaded_games = [g for g in collection_games if g.get('is_downloaded', False)]

        if not downloaded_games:
            self.parent.log_message(f"No downloaded games in '{collection_name}' to delete")
            return

        # Determine which games can be safely deleted
        # (only delete games that are NOT in other autosync-enabled collections)
        games_to_delete = []
        games_protected = []

        for game in downloaded_games:
            rom_id = game.get('rom_id')
            if not rom_id:
                # No ROM ID, can't check other collections, so include for deletion
                games_to_delete.append(game)
                continue

            # Check if this game exists in other autosync collections
            found_in_other_autosync = False
            for other_collection_game in self.collections_games:
                other_collection = other_collection_game.get('collection', '')
                # Skip if it's the same collection we're deleting from
                if other_collection == collection_name:
                    continue
                # Check if this is the same game and the other collection has autosync
                if other_collection_game.get('rom_id') == rom_id:
                    if other_collection in self.actively_syncing_collections:
                        found_in_other_autosync = True
                        games_protected.append((game, other_collection))
                        break

            if not found_in_other_autosync:
                games_to_delete.append(game)

        # Build confirmation message
        total_count = len(downloaded_games)
        delete_count = len(games_to_delete)
        protected_count = len(games_protected)

        # Prepare dialog message
        if protected_count > 0:
            # Some games are protected
            protected_list = []
            # Group by collection for cleaner message
            protected_by_collection = {}
            for game, other_coll in games_protected:
                if other_coll not in protected_by_collection:
                    protected_by_collection[other_coll] = []
                protected_by_collection[other_coll].append(game.get('name', 'Unknown'))

            protected_details = "\n".join(
                f"  • {coll}: {len(games)} game(s)"
                for coll, games in protected_by_collection.items()
            )

            message_body = (
                f"Found {total_count} downloaded game(s) in '{collection_name}':\n\n"
                f"  • {delete_count} will be deleted\n"
                f"  • {protected_count} will be kept (in other autosync collections)\n\n"
                f"Protected games are in:\n{protected_details}\n\n"
                f"Auto-sync for '{collection_name}' will be disabled."
            )
        else:
            # All games will be deleted
            message_body = (
                f"This will delete all {delete_count} downloaded game(s) from '{collection_name}'.\n\n"
                f"Auto-sync for this collection will be disabled."
            )

        # Show confirmation dialog
        dialog = Adw.AlertDialog.new(f"Delete Collection: {collection_name}?", message_body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", f"Delete {delete_count} Game(s)")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            if response == "cancel":
                # User cancelled - clear selection to avoid stale state
                self.selected_collection = None
                # Clear the row selection
                def clear_selection():
                    try:
                        selection_model = self.column_view.get_model()
                        if selection_model:
                            selection_model.unselect_all()
                        self.update_action_buttons()
                    except Exception:
                        pass
                    return False
                GLib.idle_add(clear_selection)
            elif response == "delete":
                # Disable autosync for this collection first
                if collection_name in self.actively_syncing_collections:
                    self.actively_syncing_collections.discard(collection_name)
                    self.selected_collections_for_sync.discard(collection_name)

                    # Stop global sync if no collections left
                    if not self.actively_syncing_collections:
                        self.stop_collection_auto_sync()

                    # Save the updated settings
                    self.save_selected_collections()
                    self.parent.log_message(f"🔄 Auto-sync disabled for '{collection_name}'")

                # Delete the games
                if games_to_delete:
                    self.parent.log_message(f"🗑️ Deleting {len(games_to_delete)} game(s) from '{collection_name}'...")
                    for game in games_to_delete:
                        self.parent.delete_game_file(game, is_bulk_operation=True)

                    # Refresh the collections view after deletion
                    GLib.timeout_add(1000, lambda: self.load_collections_view() or False)

                    if protected_count > 0:
                        self.parent.log_message(
                            f"✅ Deleted {delete_count} game(s), kept {protected_count} game(s) "
                            f"(protected by other autosync collections)"
                        )
                    else:
                        self.parent.log_message(f"✅ Deleted {delete_count} game(s) from '{collection_name}'")
                else:
                    self.parent.log_message(f"All games in '{collection_name}' are protected by other autosync collections")

                # Clear the selected collection and row selection
                self.selected_collection = None

                # Clear the row selection to prevent lingering UI state
                def clear_selection():
                    try:
                        selection_model = self.column_view.get_model()
                        if selection_model:
                            selection_model.unselect_all()
                        # Also update button states
                        self.update_action_buttons()
                    except Exception:
                        pass
                    return False

                GLib.idle_add(clear_selection)

        dialog.connect('response', on_response)
        dialog.present()

    def update_single_game(self, updated_game_data, skip_platform_update=False):
        """Update a single game in the tree without rebuilding - preserves expansion state"""
        rom_id = updated_game_data.get('rom_id')

        # Update master list
        for i, game in enumerate(self.parent.available_games):
            if game.get('rom_id') == rom_id:
                self.parent.available_games[i] = updated_game_data
                break

        # Try to update in-place first, avoiding full rebuild
        updated = False

        # Always keep collections_games cache in sync regardless of current view,
        # so switching from platform view to collection view reflects deletions/updates.
        if hasattr(self, 'collections_games'):
            for i, collection_game in enumerate(self.collections_games):
                if collection_game.get('rom_id') == rom_id:
                    # Preserve the collection field when updating
                    updated_collection_game = updated_game_data.copy()
                    updated_collection_game['collection'] = collection_game.get('collection')
                    self.collections_games[i] = updated_collection_game

            # Collections stores regional variants as individual rows using their own
            # rom_ids (not the parent's).  When a parent game with _sibling_files is
            # updated (downloaded or deleted from platform view), propagate the new
            # download state to each sibling's entry in collections_games.
            sibling_files = updated_game_data.get('_sibling_files', [])
            if sibling_files:
                parent_is_downloaded = updated_game_data.get('is_downloaded', False)
                parent_local_path_str = updated_game_data.get('local_path')
                parent_local_path = Path(parent_local_path_str) if parent_local_path_str else None

                # Build map: sibling rom_id → full filename (for filesystem check)
                sibling_id_to_name = {}
                for sib in sibling_files:
                    sib_id = sib.get('id')
                    fs_name = sib.get('fs_name', '')
                    fs_ext = sib.get('fs_extension', '')
                    if fs_ext and fs_name and not fs_name.lower().endswith(f'.{fs_ext.lower()}'):
                        full_name = f"{fs_name}.{fs_ext}"
                    else:
                        full_name = fs_name
                    if sib_id and full_name:
                        sibling_id_to_name[sib_id] = full_name

                for i, collection_game in enumerate(self.collections_games):
                    cg_rom_id = collection_game.get('rom_id')
                    if cg_rom_id not in sibling_id_to_name:
                        continue
                    full_name = sibling_id_to_name[cg_rom_id]
                    variant_is_downloaded = False
                    variant_local_path = None
                    if parent_is_downloaded and parent_local_path and parent_local_path.is_dir():
                        candidate = parent_local_path / full_name
                        if candidate.exists():
                            variant_is_downloaded = True
                            variant_local_path = candidate
                    updated_variant = collection_game.copy()
                    updated_variant['is_downloaded'] = variant_is_downloaded
                    updated_variant['local_path'] = str(variant_local_path) if variant_local_path else None
                    if variant_local_path:
                        updated_variant['local_size'] = self.parent.get_actual_file_size(variant_local_path)
                    else:
                        updated_variant['local_size'] = 0
                    self.collections_games[i] = updated_variant

        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 0:  # Platform/Collection level
                platform_item = tree_item.get_item()
                if isinstance(platform_item, PlatformItem):
                    # Update the game data in platform's games list
                    for j, game in enumerate(platform_item.games):
                        if game.get('rom_id') == rom_id:
                            # Preserve collection field if in collection view
                            if self.current_view_mode == 'collection':
                                updated_game_with_collection = updated_game_data.copy()
                                updated_game_with_collection['collection'] = game.get('collection')
                                platform_item.games[j] = updated_game_with_collection
                            else:
                                platform_item.games[j] = updated_game_data

                            # Update the corresponding GameItem in child_store
                            for k in range(platform_item.child_store.get_n_items()):
                                game_item = platform_item.child_store.get_item(k)
                                if isinstance(game_item, GameItem) and game_item.game_data.get('rom_id') == rom_id:
                                    # Deep copy game data to avoid reference issues with disc arrays
                                    import copy
                                    if self.current_view_mode == 'collection':
                                        updated_game_with_collection = copy.deepcopy(updated_game_data)
                                        updated_game_with_collection['collection'] = game_item.game_data.get('collection')
                                        game_item.game_data = updated_game_with_collection
                                    else:
                                        game_item.game_data = copy.deepcopy(updated_game_data)
                                    if game_item.game_data.get('is_multi_disc', False) or game_item.game_data.get('_sibling_files'):
                                        game_item.rebuild_children()
                                    game_item.notify('name')
                                    game_item.notify('is-downloaded')
                                    game_item.notify('status-text')
                                    game_item.notify('size-text')
                                    break

                            # Update platform properties (status and size text)
                            platform_item.notify('status-text')
                            platform_item.notify('size-text')
                            updated = True
                    # Don't break - continue to update all collections containing this game

        # If in-place update failed, fall back to full refresh
        if not updated:
            if self.current_view_mode == 'collection':
                self.load_collections_view()
            else:
                self.update_games_library(self.parent.available_games)

    def setup_checkbox_cell(self, factory, list_item):
        # Create a box to hold either a checkbox or a switch
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_spacing(4)  # Add spacing between switch and steam button to prevent truncation

        # We'll add the checkbox/switch dynamically in bind_checkbox_cell
        # since we need to know if it's a collection or a game
        list_item.set_child(box)

    def bind_checkbox_cell(self, factory, list_item):
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()

        # Clear the box first
        while box.get_first_child():
            box.remove(box.get_first_child())

        if isinstance(item, DiscItem):
            # For individual discs/regional variants, use checkboxes
            checkbox = Gtk.CheckButton()
            checkbox.connect('toggled', self.on_checkbox_toggled)
            box.append(checkbox)

            checkbox.set_visible(True)
            checkbox.disc_item = item
            checkbox.tree_item = tree_item
            checkbox.is_disc = True

            # Regional variants can be selected individually, multi-disc game discs cannot
            is_regional_variant = item.disc_data.get('is_regional_variant', False)
            if is_regional_variant:
                checkbox.set_sensitive(True)
                checkbox.set_tooltip_text("Select to download this regional variant")
            else:
                # Multi-disc game - disable checkbox
                checkbox.set_sensitive(False)
                checkbox.set_tooltip_text("Individual discs cannot be selected. Select the parent game instead.")

            # Check if this disc is selected
            disc_key = f"disc:{item.parent_game.get('rom_id')}:{item.disc_data.get('name')}"
            should_be_active = disc_key in self.selected_game_keys
            checkbox.set_active(should_be_active)

        elif isinstance(item, GameItem):
            # For games, use checkboxes
            checkbox = Gtk.CheckButton()
            checkbox.connect('toggled', self.on_checkbox_toggled)
            box.append(checkbox)

            checkbox.set_visible(True)
            checkbox.game_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = False

            # Check if this game is selected using collection-aware tracking
            game_data = item.game_data

            should_be_active = False
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Collections view: use collection-aware identifier
                rom_id = game_data.get('rom_id')
                collection_name = game_data.get('collection', '')
                game_name = game_data.get('name', 'NO_NAME')

                # Check if collection has autosync enabled - disable checkbox if so
                if collection_name in self.actively_syncing_collections:
                    checkbox.set_sensitive(False)
                    checkbox.set_tooltip_text(f"Cannot select - '{collection_name}' has auto-sync enabled")
                else:
                    checkbox.set_sensitive(True)
                    checkbox.set_tooltip_text("")

                if rom_id and collection_name:
                    collection_key = f"collection:{rom_id}:{collection_name}"
                    should_be_active = collection_key in self.selected_game_keys
                else:
                    # Fallback for games without ROM ID
                    name_key = f"collection:{game_data.get('name', '')}:{game_data.get('platform', '')}:{collection_name}"
                    should_be_active = name_key in self.selected_game_keys
            else:
                # Platform view: use standard identifier
                identifier_type, identifier_value = self.get_game_identifier(game_data)
                if identifier_type == 'rom_id':
                    should_be_active = identifier_value in self.selected_rom_ids
                elif identifier_type == 'game_key':
                    should_be_active = identifier_value in self.selected_game_keys

            checkbox.set_active(should_be_active)

            # Force immediate visual update for collections
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                def force_checkbox_update():
                    try:
                        checkbox._updating = True
                        checkbox.set_active(should_be_active)
                        checkbox._updating = False
                        return False
                    except Exception:
                        return False

                GLib.idle_add(force_checkbox_update)

        elif isinstance(item, PlatformItem):
            # In collections view, use a switch for collections
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                switch = Gtk.Switch()
                switch.set_valign(Gtk.Align.CENTER)
                switch.set_halign(Gtk.Align.CENTER)
                # Make switch smaller
                switch.add_css_class('compact-switch')
                box.append(switch)

                switch.set_visible(True)
                switch.platform_item = item
                switch.tree_item = tree_item
                switch.is_platform = True

                collection_name = item.platform_name

                # Check if this collection has autosync enabled (persistent state)
                is_actively_syncing = collection_name in self.actively_syncing_collections

                # Apply color and icon based on auto-sync status
                if is_actively_syncing and self.collection_auto_sync_enabled:
                    switch.add_css_class('collection-synced')  # Green
                    status_text = "Auto-sync active"
                elif is_actively_syncing:
                    switch.add_css_class('collection-partial-sync')  # Orange
                    status_text = "Selected (auto-sync paused)"
                else:
                    switch.add_css_class('collection-not-synced')  # Red
                    status_text = "Not selected"

                # Switch shows persistent autosync state (ON if collection has autosync enabled)
                switch._updating = True
                switch.set_active(is_actively_syncing)
                switch._updating = False

                # Build tooltip with Steam integration info if enabled
                tooltip = f"{collection_name} - {status_text}"
                if (self.parent.settings.get('Steam', 'enabled', 'false') == 'true'
                        and self.parent.steam_manager.is_available()):
                    tooltip += "\n🎮 Steam shortcuts sync included"
                switch.set_tooltip_text(tooltip)

                # Connect handler
                def on_collection_sync_toggle(sw, pspec):
                    if not getattr(sw, '_updating', False):
                        self.on_switch_toggled(sw, collection_name)

                if not hasattr(switch, '_sync_handler_connected'):
                    switch.connect('notify::active', on_collection_sync_toggle)
                    switch._sync_handler_connected = True

                return
            else:
                # For platform view, use checkboxes
                checkbox = Gtk.CheckButton()
                checkbox.connect('toggled', self.on_checkbox_toggled)
                box.append(checkbox)

                checkbox.set_visible(True)
                checkbox.platform_item = item
                checkbox.tree_item = tree_item
                checkbox.is_platform = True

                # Normal platform view logic (existing game selection)
                # Count selected games using the dual tracking system
                total_games = len(item.games)
                selected_games = 0

                for game_data in item.games:  # Fixed: use game_data instead of game
                    identifier_type, identifier_value = self.get_game_identifier(game_data)
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        selected_games += 1
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        selected_games += 1

                # Set platform checkbox state
                if selected_games == 0:
                    checkbox.set_active(False)
                    checkbox.set_inconsistent(False)
                elif selected_games == total_games and total_games > 0:
                    checkbox.set_active(True)
                    checkbox.set_inconsistent(False)
                else:
                    checkbox.set_active(False)
                    checkbox.set_inconsistent(True)

                pass

    def on_checkbox_toggled(self, checkbox):
        """Handle checkbox toggle with debugging"""
        if hasattr(checkbox, 'is_disc') and checkbox.is_disc:
            # Disc checkbox toggled
            disc_item = checkbox.disc_item
            disc_key = f"disc:{disc_item.parent_game.get('rom_id')}:{disc_item.disc_data.get('name')}"

            if checkbox.get_active():
                self.selected_game_keys.add(disc_key)
            else:
                self.selected_game_keys.discard(disc_key)

            # Update UI
            self.update_action_buttons()
            self.update_selection_label()

        elif hasattr(checkbox, 'is_platform') and checkbox.is_platform:
            platform_name = checkbox.platform_item.platform_name
            platform_item = checkbox.platform_item

            # Check if this is collections view
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Handle collection selection - select/unselect all games in collection
                should_select = checkbox.get_active()
                
                if should_select:
                    for game in platform_item.games:
                        rom_id = game.get('rom_id')
                        collection_name = game.get('collection', platform_name)
                        if rom_id:
                            collection_key = f"collection:{rom_id}:{collection_name}"
                            self.selected_game_keys.add(collection_key)
                else:
                    for game in platform_item.games:
                        rom_id = game.get('rom_id')
                        collection_name = game.get('collection', platform_name)
                        if rom_id:
                            collection_key = f"collection:{rom_id}:{collection_name}"
                            self.selected_game_keys.discard(collection_key)
                
                # Update UI
                self.sync_selected_checkboxes()
                self.update_action_buttons()
                self.update_selection_label()
                GLib.idle_add(self.force_checkbox_sync)
                return
            
            # Platform view logic (restore original logic here)
            # Determine what the user wants based on current state
            if checkbox.get_inconsistent():
                should_select = True
                checkbox.set_inconsistent(False)
                checkbox.set_active(True)
            else:
                should_select = checkbox.get_active()
            
            # Add/remove games for platform view
            if should_select:
                for game in platform_item.games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.add(identifier_value)
                    else:
                        self.selected_game_keys.add(identifier_value)
            else:
                for game in platform_item.games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.discard(identifier_value)
                    else:
                        self.selected_game_keys.discard(identifier_value)
            
            # Update UI
            self.sync_selected_checkboxes()
            self.update_action_buttons()
            self.update_selection_label()
            GLib.idle_add(self.force_checkbox_sync)
            GLib.idle_add(self.refresh_all_platform_checkboxes)
                            
        elif hasattr(checkbox, 'game_item'):
            # Game checkbox toggled
            game_data = checkbox.game_item.game_data
            
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Collections view: use collection-aware identifier
                rom_id = game_data.get('rom_id')
                collection_name = game_data.get('collection', '')
                
                if checkbox.get_active():
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        self.selected_game_keys.add(collection_key)
                    else:
                        name_key = f"collection:{game_data.get('name', '')}:{game_data.get('platform', '')}:{collection_name}"
                        self.selected_game_keys.add(name_key)
                    self.selected_checkboxes.add(checkbox.game_item)
                else:
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        self.selected_game_keys.discard(collection_key)
                    else:
                        name_key = f"collection:{game_data.get('name', '')}:{game_data.get('platform', '')}:{collection_name}"
                        self.selected_game_keys.discard(name_key)
                    self.selected_checkboxes.discard(checkbox.game_item)
            else:
                # Platform view: use standard identifier
                identifier_type, identifier_value = self.get_game_identifier(game_data)
                
                if checkbox.get_active():
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.add(identifier_value)
                    else:
                        self.selected_game_keys.add(identifier_value)
                    self.selected_checkboxes.add(checkbox.game_item)
                else:
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.discard(identifier_value)
                    else:
                        self.selected_game_keys.discard(identifier_value)
                    self.selected_checkboxes.discard(checkbox.game_item)
            
            # Update parent platform checkbox state
            self.update_platform_checkbox_for_game(checkbox.game_item.game_data)
        
        # IMPORTANT: Clear row selection when any checkbox is toggled
        # This ensures checkbox selections take priority over row selections
        if self.selected_game:
            self.selected_game = None
        
        # Clear visual row selection more aggressively
        selection_model = self.column_view.get_model()
        if selection_model:
            try:
                # Force clear all row selections
                selection_model.unselect_all()
                
                # Double-check by manually clearing any remaining selections
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        selection_model.unselect_item(i)
            except Exception as e:
                print(f"Error clearing row selection: {e}")
        
        # Update button states and selection label
        self.update_action_buttons()
        self.update_selection_label()

    def on_switch_toggled(self, switch, collection_name):
        """Handle switch toggle for collection auto-sync"""
        import time
        toggle_start = time.time()
        self.parent.log_message(f"[DEBUG] on_switch_toggled called for {collection_name}, active={switch.get_active()}")

        if switch.get_active():
            # ENABLE: Add collection to sync
            self.selected_collections_for_sync.add(collection_name)
            self.actively_syncing_collections.add(collection_name)
            # Remove from completed (if it was there) so it shows correct status
            if hasattr(self, 'completed_sync_collections'):
                self.completed_sync_collections.discard(collection_name)
            self.parent.log_message(f"[DEBUG] Added to collections ({time.time() - toggle_start:.3f}s)")

            # Download missing games for this collection immediately
            self.download_single_collection_games(collection_name)
            self.parent.log_message(f"📥 Enabling auto-sync for '{collection_name}'")
            self.parent.log_message(f"[DEBUG] Download queued ({time.time() - toggle_start:.3f}s)")

            # Initialize collection cache
            def init_collection_cache():
                try:
                    all_collections = self.parent.romm_client.get_collections()
                    for collection in all_collections:
                        if collection.get('name') == collection_name:
                            collection_id = collection.get('id')
                            collection_roms = self.parent.romm_client.get_collection_roms(collection_id)
                            cache_key = f'_collection_roms_{collection_name}'
                            setattr(self, cache_key, {rom.get('id') for rom in collection_roms if rom.get('id')})
                except Exception as e:
                    print(f"Error initializing collection cache: {e}")

            threading.Thread(target=init_collection_cache, daemon=True).start()

            # Start global sync if not already running
            if not self.collection_auto_sync_enabled or not self.collection_sync_thread:
                self.start_collection_auto_sync()
            else:
                self.collection_auto_sync_enabled = True
            self.parent.log_message(f"[DEBUG] Sync started ({time.time() - toggle_start:.3f}s)")

            # Update status indicator IMMEDIATELY (no delay)
            self.update_collection_sync_status(collection_name)
            self.parent.log_message(f"[DEBUG] Status updated ({time.time() - toggle_start:.3f}s)")

            # Enable Steam sync if Steam integration is available
            if (self.parent.settings.get('Steam', 'enabled', 'false') == 'true'
                    and self.parent.steam_manager.is_available()):
                self._toggle_steam_sync(collection_name, True)
                self.parent.log_message(f"[DEBUG] Steam sync enabled ({time.time() - toggle_start:.3f}s)")

            self.parent.log_message(f"[DEBUG] TOTAL on_switch_toggled time: {time.time() - toggle_start:.3f}s")

        else:
            # DISABLE: Remove collection from sync
            self.selected_collections_for_sync.discard(collection_name)
            self.actively_syncing_collections.discard(collection_name)
            # Also remove from completed collections
            if hasattr(self, 'completed_sync_collections'):
                self.completed_sync_collections.discard(collection_name)

            # Disable Steam sync if Steam integration is available
            if (self.parent.settings.get('Steam', 'enabled', 'false') == 'true'
                    and self.parent.steam_manager.is_available()):
                self._toggle_steam_sync(collection_name, False)

            # Stop global sync if no collections left
            if not self.actively_syncing_collections:
                self.stop_collection_auto_sync()

            # Update status indicator
            self.update_collection_sync_status(collection_name)

            self.parent.log_message(f"Stopped auto-sync for '{collection_name}'")

        # Save the selection
        self.save_selected_collections()

        # Refresh the checkbox display for this specific collection only
        self.refresh_collection_checkboxes(specific_collection=collection_name)

    def _toggle_steam_sync(self, collection_name, enabled):
        """Toggle Steam shortcut sync for a collection (runs in background thread)."""
        steam = self.parent.steam_manager
        if not steam or not steam.is_available():
            self.parent.log_message("Steam userdata not found")
            return

        def do_toggle():
            try:
                steam_collections = steam.get_steam_sync_collections()
                if enabled:
                    steam_collections.add(collection_name)
                else:
                    steam_collections.discard(collection_name)
                steam.set_steam_sync_collections(steam_collections)

                if enabled:
                    # Fetch collection ROMs and create shortcuts
                    all_collections = self.parent.romm_client.get_collections()
                    collection_id = None
                    for col in all_collections:
                        if col.get('name') == collection_name:
                            collection_id = col.get('id')
                            break
                    if collection_id is None:
                        GLib.idle_add(self.parent.log_message,
                                      f"Collection '{collection_name}' not found")
                        return
                    roms = self.parent.romm_client.get_collection_roms(collection_id)
                    download_dir = self.parent.settings.get('Download', 'rom_directory')
                    added, msg = steam.add_collection_shortcuts(collection_name, roms, download_dir)
                    GLib.idle_add(self.parent.log_message,
                                  f"🎮 {msg} — restart Steam to see changes")
                else:
                    removed, msg = steam.remove_collection_shortcuts(collection_name)
                    GLib.idle_add(self.parent.log_message, f"🎮 {msg}")
            except Exception as e:
                GLib.idle_add(self.parent.log_message, f"Steam sync error: {e}")

        threading.Thread(target=do_toggle, daemon=True).start()

    def preserve_selections_during_update(self, update_func):
        """Wrapper to preserve selections during tree updates"""
        # Save current selections
        saved_rom_ids = self.selected_rom_ids.copy()
        saved_game_keys = self.selected_game_keys.copy()
        saved_checkboxes = self.selected_checkboxes.copy()
        
        # Perform the update
        result = update_func()
        
        # Restore selections
        self.selected_rom_ids = saved_rom_ids
        self.selected_game_keys = saved_game_keys
        self.selected_checkboxes = saved_checkboxes
        
        return result

    def update_platform_checkbox_for_game(self, game_data):
        """Update platform checkbox state when an individual game selection changes"""
        platform_name = game_data.get('platform', '')
        
        # Find the platform checkbox widget directly
        def find_platform_checkbox(widget, target_platform):
            """Recursively find platform checkbox widget"""
            if isinstance(widget, Gtk.CheckButton):
                if (hasattr(widget, 'is_platform') and widget.is_platform and 
                    hasattr(widget, 'platform_item') and 
                    widget.platform_item.platform_name == target_platform):
                    return widget
            
            # Continue searching children
            if hasattr(widget, 'get_first_child'):
                child = widget.get_first_child()
                while child:
                    result = find_platform_checkbox(child, target_platform)
                    if result:
                        return result
                    child = child.get_next_sibling()
            return None
        
        # Find and update the platform checkbox
        platform_checkbox = find_platform_checkbox(self.column_view, platform_name)
        if platform_checkbox and hasattr(platform_checkbox, 'platform_item'):
            platform_item = platform_checkbox.platform_item
            
            # Count selected games in this platform (handle both ROM ID and non-ROM ID)
            total_games = len(platform_item.games)
            selected_games = 0

            for game in platform_item.games:
                identifier_type, identifier_value = self.get_game_identifier(game)
                is_selected = False
                
                if identifier_type == 'rom_id':
                    is_selected = identifier_value in self.selected_rom_ids
                elif identifier_type == 'game_key':
                    is_selected = identifier_value in self.selected_game_keys
                
                if is_selected:
                    selected_games += 1
            
            # Update platform checkbox state
            platform_checkbox._updating = True  # Prevent recursion
            
            if selected_games == 0:
                # No games selected
                platform_checkbox.set_active(False)
                platform_checkbox.set_inconsistent(False)
            elif selected_games == total_games and total_games > 0:
                # All games selected
                platform_checkbox.set_active(True)
                platform_checkbox.set_inconsistent(False)
            else:
                # Some games selected (partial)
                platform_checkbox.set_active(False)
                platform_checkbox.set_inconsistent(True)
            
            platform_checkbox._updating = False

    def _update_visible_game_checkboxes(self, platform_game_keys, should_select):
        """Directly update visible game checkboxes by finding and updating them"""
        updated_count = 0
        
        # Walk through all widgets to find game checkboxes
        def find_and_update_checkboxes(widget):
            nonlocal updated_count
            
            if isinstance(widget, Gtk.CheckButton):
                if (hasattr(widget, 'game_item') and hasattr(widget, 'is_platform') and 
                    not widget.is_platform):  # It's a game checkbox
                    
                    game = widget.game_item.game_data
                    game_key = f"{game.get('name', '')}|{game.get('platform', '')}"
                    
                    if game_key in platform_game_keys:
                        widget._updating = True
                        widget.set_active(should_select)
                        widget._updating = False
                        updated_count += 1
            
            # Continue walking the widget tree
            if hasattr(widget, 'get_first_child'):
                child = widget.get_first_child()
                while child:
                    find_and_update_checkboxes(child)
                    child = child.get_next_sibling()
        
        # Start the search from the column view
        find_and_update_checkboxes(self.column_view)
        
        # If we couldn't find checkboxes (maybe they're not created yet), 
        # force them to be updated when they are created
        if updated_count == 0:
            pass

    def force_checkbox_sync(self):
        """Force all visible checkboxes to match current selection state"""
        def sync_checkboxes(widget):
            if isinstance(widget, Gtk.CheckButton):
                if hasattr(widget, 'is_platform'):
                    if widget.is_platform:  # Platform checkbox
                        # Check if all games in this platform are selected
                        if hasattr(widget, 'platform_item'):
                            platform_item = widget.platform_item
                            games = platform_item.games
                            total_games = len(games)
                            selected_games = 0

                            for game in games:
                                identifier_type, identifier_value = self.get_game_identifier(game)
                                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                                    selected_games += 1
                                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                                    selected_games += 1

                            widget._updating = True
                            if selected_games == total_games and total_games > 0:
                                widget.set_active(True)
                                widget.set_inconsistent(False)
                            elif selected_games > 0:
                                widget.set_active(False)
                                widget.set_inconsistent(True)
                            else:
                                widget.set_active(False)
                                widget.set_inconsistent(False)
                            widget._updating = False
                    elif hasattr(widget, 'game_item'):  # Game checkbox
                        game_data = widget.game_item.game_data

                        should_be_active = False
                        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                            # Collections view: use collection-aware identifier
                            rom_id = game_data.get('rom_id')
                            collection_name = game_data.get('collection', '')
                            if rom_id and collection_name:
                                collection_key = f"collection:{rom_id}:{collection_name}"
                                should_be_active = collection_key in self.selected_game_keys
                        else:
                            # Platform view: use standard identifier
                            identifier_type, identifier_value = self.get_game_identifier(game_data)
                            if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                                should_be_active = True
                            elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                                should_be_active = True

                        if widget.get_active() != should_be_active:
                            widget._updating = True
                            widget.set_active(should_be_active)
                            widget._updating = False

            # Continue walking
            if hasattr(widget, 'get_first_child'):
                child = widget.get_first_child()
                while child:
                    sync_checkboxes(child)
                    child = child.get_next_sibling()

        sync_checkboxes(self.column_view)

    def _find_checkbox_for_tree_item(self, target_tree_item):
        """Find the checkbox widget for a specific tree item"""
        # This is complex in GTK4, so return None for now
        # The sync_selected_checkboxes() will handle the logic correctly
        return None

    def sync_selected_checkboxes(self):
        """Sync the GameItem set with current selections"""
        self.selected_checkboxes.clear()
        
        # Find all GameItem instances that should be selected
        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 1:  # Game level items
                item = tree_item.get_item()
                if isinstance(item, GameItem):
                    # Check if this game is selected using dual tracking
                    identifier_type, identifier_value = self.get_game_identifier(item.game_data)
                    
                    is_selected = False
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        is_selected = True
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        is_selected = True
                    
                    if is_selected:
                        self.selected_checkboxes.add(item)

    def refresh_checkbox_states(self):
        """Force refresh of all checkbox states to match current selection"""
        def deferred_refresh():
            # Get the checkbox column (first column)
            checkbox_column = self.column_view.get_columns().get_item(0)
            if checkbox_column:
                # Get the factory and force it to rebind all cells
                factory = checkbox_column.get_factory()
                if factory:
                    # Emit items-changed to force rebind of just this column
                    model = self.library_model.tree_model
                    n_items = model.get_n_items()
            return False  # Don't repeat
        
        GLib.idle_add(deferred_refresh)

    def clear_checkbox_selection(self):
        """Clear all checkbox selections"""
        # Don't clear selections during bulk downloads - they'll be cleared when the bulk operation completes
        if hasattr(self.parent, '_bulk_download_in_progress') and self.parent._bulk_download_in_progress:
            return

        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()

        # Force UI refresh to update checkboxes
        GLib.idle_add(lambda: self.update_games_library(self.parent.available_games))

    def clear_checkbox_selections_smooth(self):
        """Clear checkbox selections without full tree refresh"""
        # Don't clear selections during bulk downloads - they'll be cleared when the bulk operation completes
        if hasattr(self.parent, '_bulk_download_in_progress') and self.parent._bulk_download_in_progress:
            return

        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)
