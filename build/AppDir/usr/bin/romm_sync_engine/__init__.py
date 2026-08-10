"""GTK-free sync engine shared by Ludo and RomM RetroArch Sync.

Frontends should set their app identity before using anything else::

    from romm_sync_engine import paths
    paths.set_app_id("ludo")
"""

from . import paths

__all__ = ["paths"]
