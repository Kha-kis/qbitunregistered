#!/usr/bin/python3
import os
from typing import List
from qbittorrentapi import TorrentInfo

def list_directory_files(path: str) -> List[str]:
    """Function to return list of all files in a directory and its subdirectories"""
    file_list = []
    for root, dirs, files in os.walk(path):
        for file in files:
            file_list.append(os.path.join(root, file))
    return file_list

def check_files_on_disk(client, torrents):
    """Check for orphaned files on disk"""
    # Fetch all categories
    categories = client.torrents.categories()

    # Identify all save paths
    save_paths = set([client.app.default_save_path()])
    for category in categories.values():
        save_paths.add(category['savePath'])

    # Identify all torrent associated files
    torrent_files = [file['content_path'] for torrent in torrents for file in torrent.files()]

    # Identify all files in the save paths
    all_files = []
    for path in save_paths:
        all_files += list_directory_files(path)

    # Identify orphaned files
    orphaned_files = set(all_files) - set(torrent_files)

    return orphaned_files

