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

    # Get files on disk for default save path
    files_on_disk = get_files_in_directory(default_save_path)
    torrents = client.torrents.info()
    for torrent in torrents:
        if not torrent.category and torrent.save_path == default_save_path:
            check_files_for_torrent(torrent, files_on_disk)

    # Get save paths for each category
    categories = client.torrents.categories()
    for category in categories:
        save_path = category.save_path
        files_on_disk = get_files_in_directory(save_path)
        category_torrents = client.torrents.info(category=category.name)
        for torrent in category_torrents:
            if torrent.save_path == save_path:
                check_files_for_torrent(torrent, files_on_disk)

def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if file not in torrent.files:
            logging.info(f'File "{file}" is on disk but not in the client for save path "{torrent.save_path}"')
