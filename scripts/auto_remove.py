import logging

def auto_remove(client, torrents, dry_run):
    # Log script start
    logging.info("Starting auto_remove script...")

    # Iterate over the torrents and remove completed ones
    for torrent in torrents:
        if torrent.state_enum.is_completed:
            if dry_run:
                logging.info(f"Would remove completed torrent: {torrent.name}")
            else:
                client.torrents_delete([torrent.hash])
                logging.info(f"Removed completed torrent: {torrent.name}")

    # Log script end
    logging.info("auto_remove script completed.")