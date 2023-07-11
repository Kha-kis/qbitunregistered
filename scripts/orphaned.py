import os
import logging

def check_files_on_disk(client):
    logging.info("Checking for orphaned files on disk...")

    # Fetch torrent information from qBittorrent
    torrents = client.torrents.info()

    # Get the save paths of the torrents
    save_paths = set(torrent.save_path for torrent in torrents)

    # Determine the root directory based on the common parent directory of the save paths
    root_directory = os.path.commonpath(save_paths)

    # Traverse the root directory and identify orphaned files
    orphaned_files = set(root_files.result()) - set(torrent_files)
    for root, _, files in os.walk(root_directory):
        for file in files:
            file_path = os.path.join(root, file)
            if not any(os.path.join(save_path, file) == file_path for save_path in save_paths):
                orphaned_files.add(file_path)

    # Display the count and paths of orphaned files
    logging.info(f"Total orphaned files: {len(orphaned_files)}")
    for file_path in orphaned_files:
        logging.info(file_path)
