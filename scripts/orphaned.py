#!/usr/bin/python3
import os
from typing import List
import logging


def list_directory_files(path: str) -> List[str]:
    """Function to return a list of all files in a directory and its subdirectories"""
    file_list = []
    for root, dirs, files in os.walk(path):
        for file in files:
            file_list.append(os.path.join(root, file))
    return file_list


def check_files_on_disk(client, torrents: List) -> List[str]:
    logging.debug("Entering check_files_on_disk function...")

    # Get the save paths to check
    save_paths = {client.application.defaultSavePath}

    # Get the categories and their save paths
    categories = client.torrent_categories.categories
    save_paths.update(category['savePath'] for category in categories.values())

    # Print out all the paths to be checked
    for path in save_paths:
        logging.info(f"Checking file path: {path}")

    # Identify all torrent-associated files
    torrent_files = [os.path.join(torrent.save_path, file.name) for torrent in torrents for file in torrent.files]
    logging.debug(f"Torrent files: {torrent_files}")

    # Find all files and folders in each save path
    all_files = [os.path.join(root, file) for path in save_paths for root, _, files in os.walk(path) for file in files]
    logging.debug(f"All files: {all_files}")

    # Identify orphaned files: those that exist in the file system but not in the list of torrent-associated files
    orphaned_files = [file for file in all_files if file not in torrent_files]
    logging.info("Orphaned files:")
    for file_path in orphaned_files:
        logging.info(file_path)

    return orphaned_files
