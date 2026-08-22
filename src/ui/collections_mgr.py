import os
import sys
import time
import json
import threading
from pathlib import Path
from gi.repository import GLib, Gio

class CollectionsManager:
    """Manages collection caching, polling, synchronization, and orphan cleanup."""

    def __init__(self, library_section):
        self.library = library_section
        self.parent = library_section.parent
        self.actively_syncing_collections = set()
        self.selected_collections_for_sync = set()
        self.collection_sync_thread = None
        self.collection_sync_interval = 30
        self.collection_auto_sync_enabled = False
        self._collections_rom_cache = {}

    def get_collections_for_autosync(self):
        """Get collections selected for auto-sync (either checked or row-selected)"""
        collections_for_sync = set()
        collections_for_sync.update(self.selected_collections_for_sync)

        if hasattr(self.library, 'current_view_mode') and self.library.current_view_mode == 'collection':
            selection_model = self.library.column_view.get_model()
            if selection_model:
                from ui.models import PlatformItem
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        tree_item = selection_model.get_item(i)
                        if tree_item and tree_item.get_depth() == 0:
                            item = tree_item.get_item()
                            if isinstance(item, PlatformItem):
                                collections_for_sync.add(item.platform_name)

        return collections_for_sync

    def save_selected_collections(self):
        """Save selected collections for auto-sync to settings"""
        try:
            collections_list = list(self.actively_syncing_collections)
            collections_json = json.dumps(collections_list)
            self.parent.settings.set('Collections', 'auto_sync_collections', collections_json)
            self.parent.settings.save_settings()
        except Exception as e:
            self.parent.log_message(f"⚠️ Failed to save collection selections: {e}")

    def load_selected_collections(self):
        """Load selected collections for auto-sync from settings"""
        try:
            collections_json = self.parent.settings.get('Collections', 'auto_sync_collections', '[]')
            collections_list = json.loads(collections_json)
            self.actively_syncing_collections = set(collections_list)
            self.selected_collections_for_sync = set(collections_list)

            interval = int(self.parent.settings.get('Collections', 'sync_interval', '30'))
            self.collection_sync_interval = interval

            auto_sync_enabled = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            self.collection_auto_sync_enabled = auto_sync_enabled
        except Exception as e:
            self.parent.log_message(f"⚠️ Failed to load collection selections: {e}")
            self.actively_syncing_collections = set()
            self.selected_collections_for_sync = set()

    def start_collection_auto_sync(self):
        """Start background collection sync and download all non-downloaded games"""
        if not self.actively_syncing_collections:
            self.parent.log_message("🚫 No collections selected for sync")
            return

        if self.collection_sync_thread and self.collection_sync_thread.is_alive():
            self.parent.log_message("🔄 Collection sync already running")
        else:
            count = len(self.actively_syncing_collections)
            plural = "collection" if count == 1 else "collections"
            self.parent.log_message(f"🎯 Starting collection sync for {count} {plural}...")

            self.library.download_all_collection_games(send_notifications=False)
            self.library.initialize_collection_caches()
            self.collection_auto_sync_enabled = True

            def sync_worker():
                self.parent.log_message("🚀 Collection sync worker thread started")
                while self.collection_auto_sync_enabled:
                    try:
                        self.library.check_actively_syncing_collections()
                        time.sleep(self.collection_sync_interval)
                    except Exception as e:
                        self.parent.log_message(f"❌ Collection sync error: {e}")
                        time.sleep(60)
                self.parent.log_message("🛑 Collection sync worker stopped")

            self.collection_sync_thread = threading.Thread(target=sync_worker, daemon=True)
            self.collection_sync_thread.start()

        self.library.refresh_collection_checkboxes()

    def stop_collection_auto_sync(self):
        """Stop background collection auto sync"""
        self.collection_auto_sync_enabled = False
        self.parent.log_message("🛑 Stopped collection auto-sync")
        self.library.refresh_collection_checkboxes()
