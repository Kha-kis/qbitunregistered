"""
Type definitions for qbitunregistered.

Provides Protocol classes for type hints to improve IDE support and type checking
without tight coupling to qbittorrent-api implementation details.
"""

from typing import Protocol, Any, runtime_checkable
from enum import Enum


class TorrentState(Enum):
    """Simplified torrent state enum for type hints."""
    pass


@runtime_checkable
class TorrentStateEnum(Protocol):
    """Protocol for torrent state enum with completion check."""
    is_complete: bool


@runtime_checkable
class TorrentInfo(Protocol):
    """
    Protocol defining the expected interface for torrent objects.

    This allows type checking without depending on specific qbittorrent-api classes.
    All attributes are based on qBittorrent Web API torrent properties.
    """
    hash: str
    name: str
    save_path: str
    category: str
    tags: str
    state_enum: TorrentStateEnum
    added_on: int  # Unix timestamp
    completion_on: int  # Unix timestamp

    # Optional attributes that may be None
    seeding_time: int
    ratio: float
    uploaded: int
    downloaded: int


@runtime_checkable
class TorrentFile(Protocol):
    """Protocol for torrent file information."""
    name: str
    size: int


@runtime_checkable
class QBittorrentClient(Protocol):
    """
    Protocol defining the expected interface for qBittorrent client.

    This allows type checking without depending on specific qbittorrent-api classes.
    Only includes methods actually used in the codebase.
    """
    def auth_log_out(self) -> None:
        """Log out from qBittorrent."""
        ...

    @property
    def torrents(self) -> Any:
        """Torrents API endpoint."""
        ...

    @property
    def application(self) -> Any:
        """Application API endpoint."""
        ...

    def torrents_info(self, **kwargs) -> list:
        """Get torrent information."""
        ...

    def torrents_trackers(self, torrent_hash: str) -> list:
        """Get torrent trackers."""
        ...

    def torrents_files(self, torrent_hash: str) -> list:
        """Get torrent files."""
        ...

    def torrents_tags(self, torrent_hashes: Any, tags: str) -> None:
        """Add tags to torrents."""
        ...

    def torrents_add_tags(self, torrent_hashes: Any, tags: str) -> None:
        """Add tags to torrents."""
        ...

    def torrents_remove_tags(self, torrent_hashes: Any, tags: str) -> None:
        """Remove tags from torrents."""
        ...

    def torrents_delete(self, delete_files: bool, torrent_hashes: Any) -> None:
        """Delete torrents."""
        ...

    def torrents_pause(self, torrent_hashes: Any) -> None:
        """Pause torrents."""
        ...

    def torrents_resume(self, torrent_hashes: Any) -> None:
        """Resume torrents."""
        ...

    def torrents_set_auto_management(self, enable: bool, torrent_hashes: Any) -> None:
        """Set automatic torrent management."""
        ...

    def torrents_set_share_limits(self, ratio_limit: float, seeding_time_limit: int, torrent_hashes: Any) -> None:
        """Set share limits for torrents."""
        ...

    def app_default_save_path(self) -> str:
        """Get default save path."""
        ...

    def torrents_categories(self) -> dict:
        """Get torrent categories."""
        ...
