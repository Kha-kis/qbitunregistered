# File: scripts/orphaned.py
import os
import logging
from typing import List
from qbittorrentapi import Client

def get_files_in_directory(directory: str) -> List[str]:
    # Get all files in a given directory.
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files

def check_files_on_disk(client: Client):
    # Check files on disk against torrents in each save path.

    # Get default save path
    default_save_path = client.application.default_save_path
    files_on_disk = get_files_in_directory(default_save_path)
    torrents = client.torrents.info(save_path=default_save_path)
    for torrent in torrents:
        if not torrent.category:
            check_files_for_torrent(torrent, files_on_disk)

    # Get save paths for each category
    categories = client.application.torrent_categories()
    for category in categories:
        save_path = category["savePath"]
        if save_path == default_save_path:
            continue  # Skip checking default save path for torrents with categories
        files_on_disk = get_files_in_directory(save_path)
        torrents = client.torrents.info(category=category["name"], save_path=save_path)
        for torrent in torrents:
            check_files_for_torrent(torrent, files_on_disk)

def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if (
            file not in torrent.files
            and not file.startswith(".!qB")
            and not file.endswith(".fastresume")
        ):
            logging.info(f'File "{file}" is on disk but not in the client for save path "{torrent.save_path}"')
