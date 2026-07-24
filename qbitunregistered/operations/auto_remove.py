import logging
from typing import Sequence
from tqdm import tqdm

from qbitunregistered.types import TorrentInfo, QBittorrentClient


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
    completed_hashes: list[str] = []

    logging.info(f"Total torrents found: {total_torrents}")

    for torrent in tqdm(torrents, desc="Checking torrents for removal", unit="torrent"):
        try:
            if torrent.state_enum.is_complete:
                completed_hashes.append(torrent.hash)
                if dry_run:
                    logging.info(f"[Dry Run] Would remove completed torrent: {torrent.name}")
        except Exception as e:
            logging.error(f"Error checking torrent '{getattr(torrent, 'name', 'unknown')}': {e}")

    completed_count = len(completed_hashes)
    if completed_hashes:
        if dry_run:
            total_removed = completed_count
        else:
            try:
                client.torrents_delete(torrent_hashes=completed_hashes, delete_files=False)
            except Exception:
                logging.exception("Failed to remove completed torrent batch")
                raise
            total_removed = completed_count
            logging.info("Removed %d completed torrents in one batch", total_removed)

    if total_removed == 0:
        logging.info("No completed torrents were removed.")
    else:
        if dry_run:
            logging.info(f"[Dry Run] Would remove {total_removed} out of {completed_count} completed torrents")
        else:
            logging.info(f"Removed {total_removed} out of {completed_count} completed torrents")

    logging.info("auto_remove script completed.")
