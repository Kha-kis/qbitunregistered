import logging
from typing import Sequence
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.types import TorrentInfo, QBittorrentClient  # noqa: E402


def pause_torrents(client: QBittorrentClient, torrents: Sequence[TorrentInfo], dry_run: bool = False) -> None:
    """
    Pause all provided torrents using a single batched API call.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to pause
        dry_run: If True, only log actions without making changes

    Performance: Uses 1 batched API call instead of N individual calls.
    """
    try:
        if not torrents:
            logging.info("No torrents to pause")
            return

        torrent_hashes = [torrent.hash for torrent in torrents]

        if dry_run:
            logging.info(f"[Dry Run] Would pause {len(torrent_hashes)} torrents")
        else:
            # Batch pause all torrents in a single API call
            client.torrents_pause(torrent_hashes=torrent_hashes)
            logging.info(f"Paused {len(torrent_hashes)} torrents")

    except Exception:
        logging.exception("Error in pause_torrents")
        raise


def resume_torrents(client: QBittorrentClient, torrents: Sequence[TorrentInfo], dry_run: bool = False) -> None:
    """
    Resume all provided torrents using a single batched API call.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to resume
        dry_run: If True, only log actions without making changes

    Performance: Uses 1 batched API call instead of N individual calls.
    """
    try:
        if not torrents:
            logging.info("No torrents to resume")
            return

        torrent_hashes = [torrent.hash for torrent in torrents]

        if dry_run:
            logging.info(f"[Dry Run] Would resume {len(torrent_hashes)} torrents")
        else:
            # Batch resume all torrents in a single API call
            client.torrents_resume(torrent_hashes=torrent_hashes)
            logging.info(f"Resumed {len(torrent_hashes)} torrents")

    except Exception:
        logging.exception("Error in resume_torrents")
        raise
