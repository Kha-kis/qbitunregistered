#!/usr/bin/python3
import os
from typing import List

def list_directory_files(path: str) -> List[str]:
    """Function to return list of all files in a directory and its subdirectories"""
    file_list = []
    for root, dirs, files in os.walk(path):
        for file in files:
            file_list.append(os.path.join(root, file))
    return file_list

def check_files_on_disk(client, torrents: List) -> List[str]:
    # Get the default save path
    default_save_path = client.application.defaultSavePath
    save_paths = set([default_save_path])

    # Get the categories and their save paths
    categories = client.torrent_categories.categories

    # Add the save path for each category to the set of save paths
    for category in categories.values():
        save_paths.add(category['savePath'])

    # Identify all torrent-associated files
    torrent_files = [os.path.join(torrent.save_path, f.name) for torrent in torrents for f in torrent.files]

    # Find all files and folders in each save path
    all_files = []
    for path in save_paths:
        for root, dirs, files in os.walk(path):
            for name in files:
                all_files.append(os.path.join(root, name))

    # Identify orphaned files: those that exist in the file system but not in the list of torrent-associated files
    orphaned_files = [f for f in all_files if f not in torrent_files]