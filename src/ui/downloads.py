import os
import sys
import time
import threading
from pathlib import Path
from gi.repository import GLib

class DownloadManager:
    """Manages background download concurrency, cancellation tracking, and progress dispatch."""

    def __init__(self, window):
        self.window = window
        self.download_progress = {}
        self._download_threads = {}
        self._cancelled_downloads = set()
        self._cancellation_lock = threading.Lock()
        self._bulk_download_in_progress = False
        self._bulk_download_cancelled = False
        self._current_download_rom_id = None

    def cancel_download(self, rom_id):
        """Mark a download as cancelled and signal its thread"""
        with self._cancellation_lock:
            self._cancelled_downloads.add(rom_id)
            if rom_id in self.download_progress:
                self.download_progress[rom_id]['downloading'] = False
                self.download_progress[rom_id]['cancelled'] = True

    def is_cancelled(self, rom_id):
        """Check if a specific download has been cancelled"""
        with self._cancellation_lock:
            return rom_id in self._cancelled_downloads

    def cancel_all_downloads(self):
        """Cancel all in-flight downloads"""
        self._bulk_download_cancelled = True
        with self._cancellation_lock:
            for rom_id in list(self.download_progress.keys()):
                self._cancelled_downloads.add(rom_id)
                self.download_progress[rom_id]['downloading'] = False

    def clear_cancelled(self, rom_id):
        """Clear cancellation state for a ROM"""
        with self._cancellation_lock:
            self._cancelled_downloads.discard(rom_id)
            if rom_id in self._download_threads:
                del self._download_threads[rom_id]
