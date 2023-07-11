# File: scripts/orphaned.py
import os
import logging
from collections import deque

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files

def get_directories_in_directory(directory):
    # Get all directories in a given directory.
    directories = []
    for root, dirs, _ in os.walk(directory):
        for directory in dirs:
            directories.append(os.path.join(root, directory))
    return directories


def check_files_on_disk(client):
    # Check files on disk for orphaned files.

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

    # Iterate over unique save paths
    for save_path in save_paths:
        logging.info(f"Checking save path: {save_path}")

        # Get all files on disk in the save path
        files_on_disk = get_files_in_directory(save_path)

        # Get all torrent files in the save path
        torrent_files = set()
        torrents = client.torrents.info()
        for torrent in torrents:
            if torrent.save_path == save_path:
                torrent_files.update([os.path.join(save_path, file['name']) for file in torrent.files()])

        # Find orphaned files
        orphaned_files = files_on_disk - torrent_files

        if orphaned_files:
            logging.info("Orphaned Files:")
            for file in orphaned_files:
                logging.info(file)
        else:
            logging.info("No orphaned files found.")

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files