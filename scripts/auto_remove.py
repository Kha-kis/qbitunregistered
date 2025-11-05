import logging

def auto_remove(client, torrents, dry_run):
    logging.info("Starting auto_remove script...")

    total_removed = 0
    total_torrents = len(torrents)
    logging.info(f"Total torrents found: {total_torrents}")

    for torrent in torrents:
        if torrent.state_enum.is_complete:
            if dry_run:
                logging.info(f"Would remove completed torrent: {torrent.name}")
            else:
                try:
                    client.torrents_delete([torrent.hash])
                    logging.info(f"Removed completed torrent: {torrent.name}")
                    total_removed += 1
                except Exception as e:
                    logging.error(f"Error removing {torrent.name}: {e}")

    if total_removed == 0:
        logging.info("No completed torrents were removed.")

    logging.info(f"auto_remove script completed. Removed {total_removed} torrents.")
