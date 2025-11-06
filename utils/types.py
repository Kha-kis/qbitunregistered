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
