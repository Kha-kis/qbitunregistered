# File: scripts/orphaned.py
import os
import logging

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files

def check_files_on_disk(client):
    # Check files on disk against torrents in each save path.

    # Get default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Create a set of all unique save paths
    save_paths = set()
    save_paths.add(default_save_path)

    # Get save paths for each category
    categories = client.torrents_categories().values()
    for category in categories:
        if category.savePath != '':
            save_paths.add(category.savePath)

    # Get all torrents
    torrents = client.torrents.info()

    # Iterate over unique save paths
    for save_path in save_paths:
        logging.info(f"Checking save path: {save_path}")

        # Get files on disk for current save path
        files_on_disk = get_files_in_directory(save_path)

        # Collect unregistered files per save path
        unregistered_files = []

        # Check each torrent in current save path
        for torrent in torrents:
            if torrent.save_path == save_path:
                torrent_files = set(os.path.join(torrent.save_path, file['name']) for file in torrent.files)
                unregistered_files.extend(file for file in files_on_disk if file not in torrent_files)

        num_unregistered_files = len(unregistered_files)
        logging.info(f"Total unregistered files: {num_unregistered_files}")

        if num_unregistered_files > 0:
            logging.info("Unregistered Files:")
            # Set the maximum number of unregistered files to display per save path
            max_display = 10
            for file in unregistered_files[:max_display]:
                logging.info(file)
            if num_unregistered_files > max_display:
                logging.info(f"and {num_unregistered_files - max_display} more unregistered files.")
            logging.info("End of Unregistered Files")
        else:
            logging.info("No unregistered files found.")


def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    torrent_files = set(os.path.join(torrent.save_path, file['name']) for file in torrent.files)
    files_on_disk_set = set(files_on_disk)

    logging.info(f"Checking files for torrent '{torrent.name}' in save path '{torrent.save_path}'")
    logging.info(f"Total files on disk: {len(files_on_disk_set)}")
    logging.info(f"Total files associated with torrent: {len(torrent_files)}")

    unregistered_files = files_on_disk_set - torrent_files
    num_unregistered_files = len(unregistered_files)

    logging.info(f"Total unregistered files: {num_unregistered_files}")

    if num_unregistered_files > 0:
        logging.info("Unregistered Files:")
        for file in unregistered_files:
            logging.info(file)

        # Check for unregistered directories
        unregistered_dirs = set(os.path.dirname(file) for file in unregistered_files)
        for dir_path in unregistered_dirs:
            if not os.listdir(dir_path):
                logging.info(f"Orphaned directory found: {dir_path}")

        logging.info("End of Unregistered Files")
    else:
        logging.info("No unregistered files found.")
