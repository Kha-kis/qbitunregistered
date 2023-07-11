import os
import logging

def check_files_on_disk(client):
    # Get the default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Get the category save paths
    categories = client.torrents_categories().values()
    category_save_paths = [category.savePath for category in categories if category.savePath != '']

    # Combine the default save path and category save paths
    save_paths = [default_save_path] + category_save_paths

    # Collect all files and directories within the save paths
    files_on_disk = set()
    for save_path in save_paths:
        files_on_disk.update(get_files_in_directory(save_path))

    # Get all torrents from qBittorrent
    torrents = client.torrents.info()

    # Check if torrents are orphaned and log the file paths
    for torrent in torrents:
        content_path = torrent.content_path
        if content_path not in files_on_disk:
            logging.info(f"Orphaned file: {content_path}")

def get_files_in_directory(directory):
    # Get all files and directories in a given directory
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files

