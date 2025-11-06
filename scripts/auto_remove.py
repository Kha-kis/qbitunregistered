import logging
from typing import Sequence
from tqdm import tqdm
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.types import TorrentInfo, QBittorrentClient


def auto_remove(client: QBittorrentClient, torrents: Sequence[TorrentInfo], dry_run: bool = False) -> None:
    """
    Automatically remove completed torrents.

    Args:
        client: qBittorrent client instance
        torrents: List of torrent objects to check
        dry_run: If True, only log actions without removing torrents
    """
    logging.info("Starting auto_remove script...")

    total_removed = 0
    total_torrents = len(torrents)
    completed_count = 0

    logging.info(f"Total torrents found: {total_torrents}")

    for torrent in tqdm(torrents, desc="Checking torrents for removal", unit="torrent"):
        try:
            if torrent.state_enum.is_complete:
                completed_count += 1
                if dry_run:
                    logging.info(f"[Dry Run] Would remove completed torrent: {torrent.name}")
                    total_removed += 1
                else:
                    try:
                        # Use torrents_delete with hash directly (not in list)
                        client.torrents_delete(torrent_hashes=torrent.hash, delete_files=False)
                        logging.info(f"Removed completed torrent: {torrent.name}")
                        total_removed += 1
                    except Exception as e:
                        logging.error(f"Error removing torrent '{torrent.name}': {e}")
        except Exception as e:
            logging.error(f"Error checking torrent '{getattr(torrent, 'name', 'unknown')}': {e}")

    if total_removed == 0:
        logging.info("No completed torrents were removed.")
    else:
        if dry_run:
            logging.info(f"[Dry Run] Would remove {total_removed} out of {completed_count} completed torrents")
        else:
            logging.info(f"Removed {total_removed} out of {completed_count} completed torrents")

    logging.info("auto_remove script completed.")
