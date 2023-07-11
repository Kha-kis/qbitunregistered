import os
import logging
from tqdm import tqdm

def find_orphaned_files(client):
    logging.info("Checking for orphaned files...")
    save_paths = set()
    orphaned_files = []

    # Get save paths from qBittorrent client
    categories = client.torrents_categories()
    for category in categories.values():
        save_path = category.savePath
        if save_path:
            save_paths.add(save_path)

    # Traverse the save paths and find orphaned files
    total_save_paths = len(save_paths)
    with tqdm(total=total_save_paths, desc="Progress") as pbar:
        for save_path in save_paths:
            for root, dirs, files in os.walk(save_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    if not any(torrent.content_path == file_path for torrent in client.torrents.info()):
                        orphaned_files.append(file_path)

            pbar.update(1)

    # Log the orphaned files count and return the list of orphaned files
    logging.info("Total orphaned files: %d", len(orphaned_files))
    return orphaned_files
