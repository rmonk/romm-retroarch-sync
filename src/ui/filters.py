import time

class LibraryFilterSort:
    """Provides filtering and sorting utilities for the ROM library."""

    @staticmethod
    def apply_filters(games, selected_platform="All Platforms", search_text="", show_downloaded_only=False):
        """Apply platform, search, and download status filters to the games list"""
        if not games:
            return []

        filtered = games

        # 1. Platform filter
        if selected_platform and selected_platform != "All Platforms":
            filtered = [g for g in filtered if g.get('platform', 'Unknown') == selected_platform]

        # 2. Search query filter
        if search_text:
            query = search_text.lower().strip()
            filtered = [
                g for g in filtered
                if query in g.get('name', '').lower() or query in g.get('platform', '').lower()
            ]

        # 3. Downloaded-only filter
        if show_downloaded_only:
            filtered = [g for g in filtered if g.get('is_downloaded', False)]

        return filtered

    @staticmethod
    def sort_games_consistently(games, sort_downloaded_first=False):
        """Lightning-fast sorting with key pre-computation"""
        if not games:
            return games

        game_count = len(games)

        # For small lists, standard sorted is fastest
        if game_count < 200:
            if sort_downloaded_first:
                return sorted(games, key=lambda g: (
                    g.get('platform', 'ZZZ_Unknown'),
                    not g.get('is_downloaded', False),
                    g.get('name', '').lower()
                ))
            return sorted(games, key=lambda g: (
                g.get('platform', 'ZZZ_Unknown'),
                g.get('name', '').lower()
            ))

        # For large lists, pre-compute keys in a single pass
        keyed_games = []
        for g in games:
            platform = g.get('platform', 'ZZZ_Unknown')
            name_lower = g.get('name', '').lower()
            if sort_downloaded_first:
                sort_key = (platform, not g.get('is_downloaded', False), name_lower)
            else:
                sort_key = (platform, name_lower)
            keyed_games.append((sort_key, g))

        keyed_games.sort(key=lambda x: x[0])
        return [g for _, g in keyed_games]

    @staticmethod
    def get_platforms_with_results(filtered_games):
        """Extract set of unique platform names from filtered games"""
        platforms = set()
        for g in filtered_games:
            platforms.add(g.get('platform', 'Unknown'))
        return platforms
