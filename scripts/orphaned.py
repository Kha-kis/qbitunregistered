# File: scripts/orphaned.py
import os
import logging

def get_all_files_in_directory(directory):
    # Get all files in a given directory.
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files

def check_files_for_torrent(torrent, files_on_disk, counts_per_category):
    # Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if file not in torrent.files:
            save_path = torrent.save_path
            if save_path not in counts_per_category:
                counts_per_category[save_path] = 1
            else:
                counts_per_category[save_path] += 1
            logging.info(f'File "{file}" is on disk but not in the client for save path "{save_path}"')

def check_files_on_disk(client):
    # Check all files on disk against the files in each torrent.
    counts_per_category = {}

    for torrent in client.torrents.info():
        save_path = torrent.save_path
        files_on_disk = get_all_files_in_directory(save_path)
        check_files_for_torrent(torrent, files_on_disk, counts_per_category)

    # Log counts per category and save path
    for save_path, count in counts_per_category.items():
        logging.info(f"Found {count} orphaned file(s) in save path: {save_path}")

