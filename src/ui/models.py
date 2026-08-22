import os
import sys
from pathlib import Path
from gi.repository import GObject, Gio, GLib, Gtk


class GameItem(GObject.Object):
    def __init__(self, game_data):
        super().__init__()
        self.game_data = game_data
        # Initialize child store for multi-disc games
        self.child_store = Gio.ListStore()
        self.rebuild_children()

    def __eq__(self, other):
        """Enable proper equality comparison for GameItem objects"""
        if not isinstance(other, GameItem):
            return False
        return self.game_data.get('rom_id') == other.game_data.get('rom_id')

    @property
    def platform_name(self):
        return self.game_data.get('platform_name') or self.game_data.get('platform') or self.game_data.get('platform_slug', 'Unknown')

    @property
    def name(self):
        return self.game_data.get('name', 'Unknown')

    @property
    def is_downloaded(self):
        return self.game_data.get('is_downloaded', False)

    @property
    def size(self):
        return self.game_data.get('size') or self.game_data.get('local_size', 0)

    def __hash__(self):
        """Enable GameItem to be used in sets"""
        return hash(self.game_data.get('rom_id', id(self.game_data)))

    def rebuild_children(self):
        """Rebuild child items (discs or regional variants) if this is a multi-disc/multi-regional game"""
        old_items = []
        for i in range(self.child_store.get_n_items()):
            old_items.append(self.child_store.get_item(i))

        self.child_store.remove_all()

        new_items = []

        if self.game_data.get('is_multi_disc', False):
            discs = self.game_data.get('discs', [])
            for disc in discs:
                disc_item = DiscItem(disc, parent_game=self.game_data)
                new_items.append(disc_item)

        elif self.game_data.get('_sibling_files'):
            siblings = self.game_data.get('_sibling_files', [])

            parent_local_path = self.game_data.get('local_path')
            parent_is_downloaded = self.game_data.get('is_downloaded', False)

            for sibling in siblings:
                fs_name = sibling.get('fs_name', '')
                fs_extension = sibling.get('fs_extension', '')
                
                # Build filename - fs_name may or may not include extension
                if fs_name:
                    if fs_extension and not fs_name.lower().endswith(f'.{fs_extension.lower()}'):
                        full_fs_name = f"{fs_name}.{fs_extension}"
                    else:
                        full_fs_name = fs_name
                else:
                    full_fs_name = sibling.get('name', 'Unknown')
                
                variant_name = full_fs_name

                if variant_name != 'Unknown':
                    from pathlib import Path
                    variant_name = Path(variant_name).stem

                variant_is_downloaded = False
                if parent_is_downloaded and parent_local_path:
                    from pathlib import Path
                    parent_path = Path(parent_local_path)
                    if parent_path.is_dir():
                        variant_file_path = parent_path / full_fs_name
                        variant_is_downloaded = variant_file_path.exists()

                sibling_data = {
                    'name': variant_name,
                    'full_fs_name': full_fs_name,
                    'rom_id': sibling.get('id'),
                    'is_downloaded': variant_is_downloaded,
                    'size': sibling.get('fs_size_bytes', 0),
                    'is_regional_variant': True
                }
                variant_item = DiscItem(sibling_data, parent_game=self.game_data)
                new_items.append(variant_item)

        if new_items:
            self.child_store.splice(0, 0, new_items)

        for old_item in old_items:
            if isinstance(old_item, DiscItem):
                old_item.notify('is-downloaded')
                old_item.notify('size-text')
                old_item.notify('name')

        for new_item in new_items:
            if isinstance(new_item, DiscItem):
                new_item.notify('is-downloaded')
                new_item.notify('size-text')
                new_item.notify('name')

    @GObject.Property(type=str, default='Unknown')
    def name(self):
        return self.game_data.get('name', 'Unknown')

    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return self.game_data.get('is_downloaded', False)

    @GObject.Property(type=str, default='')
    def status_text(self):
        sibling_files = self.game_data.get('_sibling_files', [])
        if sibling_files:
            downloaded_count = 0
            local_path = self.game_data.get('local_path')
            if local_path:
                from pathlib import Path
                folder_path = Path(local_path)
                if folder_path.exists() and folder_path.is_dir():
                    for sibling in sibling_files:
                        # Construct filename properly from fs_name and fs_extension
                        fs_name = sibling.get('fs_name', '')
                        fs_extension = sibling.get('fs_extension', '')
                        
                        if fs_name:
                            if fs_extension and not fs_name.lower().endswith(f'.{fs_extension.lower()}'):
                                full_fs_name = f"{fs_name}.{fs_extension}"
                            else:
                                full_fs_name = fs_name
                        else:
                            full_fs_name = sibling.get('name', 'Unknown')
                        
                        if full_fs_name:
                            variant_file = folder_path / full_fs_name
                            if variant_file.exists():
                                downloaded_count += 1
            return f"{downloaded_count}/{len(sibling_files)}"
        return ''

    @GObject.Property(type=str, default='Not downloaded')
    def size_text(self):
        def format_size(size_bytes):
            if size_bytes > 1000**3:
                return f"{size_bytes / (1000**3):.1f} GB"
            elif size_bytes > 1000**2:
                return f"{size_bytes / (1000**2):.1f} MB"
            elif size_bytes > 1000:
                return f"{size_bytes / 1000:.1f} KB"
            return f"{size_bytes} bytes"

        # Check if this game has regional variants
        sibling_files = self.game_data.get('_sibling_files', [])
        if sibling_files:
            total_size = sum(s.get('fs_size_bytes', 0) for s in sibling_files)
            downloaded_size = 0

            local_path = self.game_data.get('local_path')

            if local_path:
                from pathlib import Path
                folder_path = Path(local_path)

                if folder_path.exists() and folder_path.is_dir():
                    for sibling in sibling_files:
                        # Construct filename properly from fs_name and fs_extension
                        fs_name = sibling.get('fs_name', '')
                        fs_extension = sibling.get('fs_extension', '')
                        
                        if fs_name:
                            if fs_extension and not fs_name.lower().endswith(f'.{fs_extension.lower()}'):
                                full_fs_name = f"{fs_name}.{fs_extension}"
                            else:
                                full_fs_name = fs_name
                        else:
                            full_fs_name = sibling.get('name', 'Unknown')
                        
                        if full_fs_name:
                            variant_file = folder_path / full_fs_name
                            if variant_file.exists():
                                downloaded_size += sibling.get('fs_size_bytes', 0)

            # Show downloaded/total format if partially downloaded
            if downloaded_size > 0 and downloaded_size < total_size:
                return f"{format_size(downloaded_size)} / {format_size(total_size)}"
            elif downloaded_size >= total_size and total_size > 0:
                # All downloaded
                return format_size(total_size)
            else:
                # File scanning didn't find files - check is_downloaded as fallback
                if self.game_data.get('is_downloaded'):
                    # Use local_size and show downloaded / total format
                    local_size = self.game_data.get('local_size', 0)
                    if local_size > 0 and total_size > 0:
                        # Show downloaded / total format
                        if local_size < total_size:
                            return f"{format_size(local_size)} / {format_size(total_size)}"
                        else:
                            # All downloaded
                            return format_size(total_size)
                    elif local_size > 0:
                        # No total available, just show downloaded
                        return format_size(local_size)
                return "Not downloaded"

        # Single file or multi-disc game (existing logic)
        if self.game_data.get('is_downloaded'):
            size = self.game_data.get('local_size', 0)
            return format_size(size)
        return "Not downloaded"

class DiscItem(GObject.Object):
    """Represents an individual disc in a multi-disc game"""
    def __init__(self, disc_data, parent_game=None):
        super().__init__()
        self.disc_data = disc_data
        self.parent_game = parent_game  # Reference to parent game data

    @GObject.Property(type=str, default='Unknown')
    def name(self):
        full_name = self.disc_data.get('name', 'Unknown')
        # Remove file extension from disc name for cleaner display
        if full_name != 'Unknown':
            from pathlib import Path
            return Path(full_name).stem
        return full_name

    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return self.disc_data.get('is_downloaded', False)

    @GObject.Property(type=str, default='Not downloaded')
    def size_text(self):
        if self.disc_data.get('is_downloaded'):
            disc_path = self.disc_data.get('path')
            if disc_path:
                disc_path_obj = Path(disc_path)

                # Try without extension if path doesn't exist (multi-file disc in folder)
                if not disc_path_obj.exists():
                    disc_path_obj = disc_path_obj.parent / disc_path_obj.stem

                if disc_path_obj.exists():
                    if disc_path_obj.is_dir():
                        size = sum(f.stat().st_size for f in disc_path_obj.rglob('*') if f.is_file())
                    else:
                        base_name = disc_path_obj.stem
                        parent = disc_path_obj.parent
                        matching = [f for f in parent.iterdir() if f.is_file() and f.stem == base_name]
                        size = sum(f.stat().st_size for f in matching) or disc_path_obj.stat().st_size
                else:
                    size = self.disc_data.get('size', 0)
            else:
                size = self.disc_data.get('size', 0)

            if size > 1000**3:
                return f"{size / (1000**3):.1f} GB"
            elif size > 1000**2:
                return f"{size / (1000**2):.1f} MB"
            elif size > 1000:
                return f"{size / 1000:.1f} KB"
            return f"{size} bytes"
        return "Not downloaded"

class PlatformItem(GObject.Object):
    def __init__(self, platform_name, games, loading=False, sync_status=None):
        super().__init__()
        self.platform_name = platform_name
        self.games = games
        self.loading = loading  # Flag to show loading state
        self.sync_status = sync_status  # Sync status for collections: 'synced', 'syncing', 'disabled'
        self.child_store = Gio.ListStore()
        self.rebuild_children()
    
    def update_games(self, new_games, loading=False, sync_status=None):
        self.games = new_games
        self.loading = loading  # Update loading state
        if sync_status is not None:
            self.sync_status = sync_status
        self.rebuild_children()
        # Notify all properties changed
        self.notify('name')
        self.notify('status-text')
        self.notify('size-text')
        self.notify('sync-status-text')
    
    def rebuild_children(self):
        """Optimized: Batch append game items instead of one-by-one"""
        self.child_store.remove_all()

        # Batch create GameItems for better performance
        if self.games:
            game_items = [GameItem(game) for game in self.games]
            # Use splice for batched insertion (much faster than individual appends)
            self.child_store.splice(0, 0, game_items)


    @GObject.Property(type=str, default='Unknown Platform')
    def name(self):
        # Just return the platform name without counts (counts are shown in status column)
        return self.platform_name
    
    @GObject.Property(type=str, default='0/0')
    def status_text(self):
        if self.loading:
            return "..."
        
        # Count games OR individual regional variants
        total_count = 0
        downloaded_count = 0
        
        for g in self.games:
            # Check if game has regional variants
            sibling_files = g.get('_sibling_files', [])
            if sibling_files:
                # Count variants for this game
                total_count += len(sibling_files)
                
                # Count downloaded variants
                if g.get('is_downloaded') and g.get('local_path'):
                    from pathlib import Path
                    folder_path = Path(g['local_path'])
                    if folder_path.exists() and folder_path.is_dir():
                        for sibling in sibling_files:
                            # Construct filename
                            fs_name = sibling.get('fs_name', '')
                            fs_extension = sibling.get('fs_extension', '')
                            if fs_name:
                                if fs_extension and not fs_name.lower().endswith(f'.{fs_extension.lower()}'):
                                    full_fs_name = f"{fs_name}.{fs_extension}"
                                else:
                                    full_fs_name = fs_name
                            else:
                                full_fs_name = sibling.get('name', 'Unknown')
                            
                            if full_fs_name:
                                variant_file = folder_path / full_fs_name
                                if variant_file.exists():
                                    downloaded_count += 1
            else:
                # Regular game (no variants) - count as 1
                total_count += 1
                if g.get('is_downloaded'):
                    downloaded_count += 1
        
        return f"{downloaded_count}/{total_count}"
    
    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return False  # Platforms don't have download status
    
    @GObject.Property(type=str, default='0 KB')
    def size_text(self):
        if self.loading:
            return "Loading..."
        # Calculate downloaded size (local files)
        downloaded_size = sum(g.get('local_size', 0) for g in self.games if g.get('is_downloaded'))
        
        def format_size(size_bytes):
            if size_bytes > 1000**3:
                return f"{size_bytes / (1000**3):.1f} GB"
            elif size_bytes > 1000**2:
                return f"{size_bytes / (1000**2):.1f} MB"
            else:
                return f"{size_bytes / 1000:.1f} KB"
        
        # Better detection: check if we're truly connected vs using cached data
        # If ALL games in the platform are downloaded, we're probably in offline mode
        all_games_downloaded = len(self.games) > 0 and all(g.get('is_downloaded', False) for g in self.games)
        
        # Calculate total library size from RomM data
        total_library_size = 0
        for g in self.games:
            romm_data = g.get('romm_data')
            if romm_data and isinstance(romm_data, dict):
                total_library_size += romm_data.get('fs_size_bytes', 0)
        
        # Check if any games have partial regional variant downloads
        has_partial_variants = any(
            g.get('_sibling_files') and g.get('is_downloaded') and
            g.get('local_size', 0) < sum(s.get('fs_size_bytes', 0) for s in g.get('_sibling_files', []))
            for g in self.games
        )

        # Only show downloaded/total format if:
        # 1. We have total library size data AND
        # 2. (NOT all games are downloaded OR has partial variant downloads) AND
        # 3. Total size is significantly larger than downloaded size
        should_show_total = (
            total_library_size > 0 and
            (not all_games_downloaded or has_partial_variants) and
            total_library_size > downloaded_size * 1.1  # At least 10% larger
        )
        
        if should_show_total:
            result = f"{format_size(downloaded_size)} / {format_size(total_library_size)}"
            return result
        else:
            # When offline, all downloaded, or sizes are equal, just show downloaded size
            result = format_size(downloaded_size)
            return result

    @GObject.Property(type=str, default='')
    def sync_status_text(self):
        """Return sync status indicator for collections"""
        if self.loading:
            return "loading"
        if self.sync_status is None:
            return ""  # Platforms don't have sync status

        # Return status string for visual rendering
        return self.sync_status  # 'synced', 'syncing', or 'disabled'

    def force_property_update(self):
        """Manually force property updates - for debugging"""
        print(f"🔄 Forcing property update for {self.platform_name}")

        # Use notify with property names (this should work)
        self.notify('name')
        self.notify('status-text')
        self.notify('size-text')
        self.notify('sync-status-text')
        
        # Alternative approach: get the current values and use freeze/thaw
        try:
            current_name = self.name
            current_status = self.status_text
            current_size = self.size_text

            # Force a freeze/thaw cycle to trigger updates
            self.freeze_notify()
            self.thaw_notify()
        except Exception as e:
            print(f"⚠️ Error in freeze/thaw: {e}")
            
        print(f"✅ Property update completed for {self.platform_name}")

class LibraryTreeModel:
    def __init__(self):
        self.root_store = Gio.ListStore()
        self.tree_model = Gtk.TreeListModel.new(
            self.root_store,
            False,
            False,
            self.create_child_model
        )
        self._platforms = {}
        self._pending_restore_id = None  # Track pending restoration timer
        
    def create_child_model(self, item):
        """Create child model for tree items

        Returns:
            - For PlatformItem: return child_store containing games
            - For GameItem (multi-disc or multi-regional): return child_store containing discs/variants
            - For DiscItem: return None (discs have no children)
        """
        if isinstance(item, PlatformItem):
            return item.child_store
        elif isinstance(item, GameItem):
            # Check if this game has children (multi-disc game OR regional variants)
            is_multi = item.game_data.get('is_multi_disc', False)
            has_siblings = bool(item.game_data.get('_sibling_files'))
            child_count = len(item.child_store)

            if child_count > 0 and (is_multi or has_siblings):
                return item.child_store
        return None

    def _get_current_expansion_state(self):
        """Get the current expansion state of all platform items"""
        expansion_state = {}
        for i in range(self.tree_model.get_n_items()):
            item = self.tree_model.get_item(i)
            if item and item.get_depth() == 0:
                platform = item.get_item()
                if isinstance(platform, PlatformItem):
                    expansion_state[platform.platform_name] = item.get_expanded()
        return expansion_state

    def _restore_expansion_from_state(self, expansion_state):
        """Restore expansion state for all platform items"""
        for i in range(self.tree_model.get_n_items()):
            item = self.tree_model.get_item(i)
            if item and item.get_depth() == 0:
                platform = item.get_item()
                if isinstance(platform, PlatformItem):
                    should_expand = expansion_state.get(platform.platform_name, False)
                    if should_expand:
                        item.set_expanded(True)
                    else:
                        item.set_expanded(False)

    def _restore_expansion_immediate(self, expansion_state):
        """Restore expansion state immediately (used by search)"""
        self._restore_expansion_from_state(expansion_state)

    def update_library(self, games, group_by='platform', flat=None, loading=False, sync_status_map=None):
        if flat is None:
            flat = getattr(self, 'is_flat_view', False)
        if flat:
            new_game_items = [GameItem(g) for g in games]
            if new_game_items:
                self.root_store.splice(0, self.root_store.get_n_items(), new_game_items)
            else:
                self.root_store.remove_all()
            return

        overall_start = time.time()

        # Save expansion state before update
        save_exp_start = time.time()
        expansion_state = {}
        for i in range(self.tree_model.get_n_items()):
            item = self.tree_model.get_item(i)
            if item and item.get_depth() == 0:
                platform = item.get_item()
                if isinstance(platform, PlatformItem):
                    expansion_state[platform.platform_name] = item.get_expanded()

        # Group games
        group_start = time.time()
        groups = {}
        for game in games:
            key = game.get(group_by, 'Unknown')
            groups.setdefault(key, []).append(game)

        # Build a map of existing platform items to reuse them
        map_start = time.time()
        existing_platforms = {}
        for i in range(self.root_store.get_n_items()):
            platform_item = self.root_store.get_item(i)
            if isinstance(platform_item, PlatformItem):
                existing_platforms[platform_item.platform_name] = platform_item

        # Build new list of platform items in sorted order
        new_platform_items = []
        for name, game_list in sorted(groups.items()):
            # Get sync status for this collection/platform
            sync_status = sync_status_map.get(name) if sync_status_map else None

            # Debug: check for multi-disc games in this group
            multi_count = sum(1 for g in game_list if g.get('is_multi_disc', False))
            if name in existing_platforms:
                # Reuse existing platform item (preserves state)
                platform = existing_platforms[name]
                platform.update_games(game_list, loading=loading, sync_status=sync_status)
            else:
                # Create new platform item
                platform = PlatformItem(name, game_list, loading=loading, sync_status=sync_status)
            new_platform_items.append(platform)

        # Use splice to update the store in-place (preserves tree item expansion state)
        # This is the key to preventing visual glitches
        if new_platform_items:
            self.root_store.splice(0, self.root_store.get_n_items(), new_platform_items)
        else:
            self.root_store.remove_all()
        
        # Restore expansion state IMMEDIATELY (no timer delay to prevent visual glitch)
        # The TreeListRow objects are recreated by splice(), so we must restore state now
        if expansion_state:
            for i in range(self.tree_model.get_n_items()):
                item = self.tree_model.get_item(i)
                if item and item.get_depth() == 0:
                    platform = item.get_item()
                    if isinstance(platform, PlatformItem):
                        if expansion_state.get(platform.platform_name, False):
                            item.set_expanded(True)
