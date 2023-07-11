import os
import logging

def check_files_on_disk(client):
    # Check files on disk for orphaned files.

    # Get default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Create a dictionary to store the count of torrents per save path
    torrents_count_per_path = {default_save_path: 0}

    # Get save paths for each category
    categories = client.torrents_categories().values()
    for category in categories:
        save_path = category.savePath
        if save_path != '':
            torrents_count_per_path[save_path] = 0

    # Iterate over torrents and increment the count for each save path
    torrents = client.torrents.info()
    for torrent in torrents:
        save_path = torrent.save_path
        if save_path in torrents_count_per_path:
            torrents_count_per_path[save_path] += 1

    # Iterate over save paths and log the count of torrents and orphaned files
    for save_path, count in torrents_count_per_path.items():
        logging.info(f"Save Path: {save_path} | Torrents Count: {count}")

        # Get all files on disk in the save path
        files_on_disk = get_files_in_directory(save_path)

        # Get all torrent files in the save path
        torrent_files = set()
        for torrent in torrents:
            if torrent.save_path == save_path:
                torrent_files.update([os.path.join(save_path, file['name']) for file in torrent.files()])

        # Find orphaned files
        orphaned_files = files_on_disk - torrent_files

        if orphaned_files:
            logging.info(f"Orphaned Files Count: {len(orphaned_files)}")
            for file in orphaned_files:
                logging.info(file)

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files
