import logging
from typing import List, Any


def pause_torrents(client, torrents: List[Any], dry_run: bool = False) -> None:
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
            try:
                # Batch pause all torrents in a single API call
                client.torrents_pause(torrent_hashes=torrent_hashes)
                logging.info(f"Paused {len(torrent_hashes)} torrents")
            except Exception as e:
                logging.error(f"Failed to pause torrents: {e}")
                raise

    except Exception as e:
        logging.error(f"Error in pause_torrents: {e}")
        raise


def resume_torrents(client, torrents: List[Any], dry_run: bool = False) -> None:
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
            try:
                # Batch resume all torrents in a single API call
                client.torrents_resume(torrent_hashes=torrent_hashes)
                logging.info(f"Resumed {len(torrent_hashes)} torrents")
            except Exception as e:
                logging.error(f"Failed to resume torrents: {e}")
                raise

    except Exception as e:
        logging.error(f"Error in resume_torrents: {e}")
        raise
