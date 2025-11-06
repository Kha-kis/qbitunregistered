import logging
from typing import Sequence
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.types import TorrentInfo, QBittorrentClient  # noqa: E402


def apply_auto_tmm_per_torrent(client: QBittorrentClient, torrents: Sequence[TorrentInfo], dry_run: bool = False) -> None:
    """
    Enable Automatic Torrent Management (auto TMM) for all provided torrents.

    Uses a single batched API call for all torrents.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to enable auto TMM for
        dry_run: If True, only log actions without making changes

    Performance: Uses 1 batched API call instead of N individual calls.
    """
    try:
        if not torrents:
            logging.info("No torrents to enable auto TMM for")
            return

        torrent_hashes = [torrent.hash for torrent in torrents]

        if dry_run:
            logging.info(f"[Dry Run] Would enable auto TMM for {len(torrent_hashes)} torrents")
        else:
            # Batch enable auto TMM for all torrents in a single API call
            client.torrents_set_auto_management(enable=True, torrent_hashes=torrent_hashes)
            logging.info(f"Enabled auto TMM for {len(torrent_hashes)} torrents")

    except Exception:
        logging.exception("Error in apply_auto_tmm_per_torrent")
        raise
