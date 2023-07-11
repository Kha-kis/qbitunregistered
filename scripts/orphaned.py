# File: scripts/orphaned.py
import os
import logging

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files

def check_files_for_save_path(client, save_path):
    # Check files on disk against torrents in the specified save path.
    files_on_disk = get_files_in_directory(save_path)
    torrents = client.torrents.info(save_path=save_path)
    
    for torrent in torrents:
        for file in files_on_disk:
            if file not in torrent.files:
                logging.info(f'File "{file}" is on disk but not in the client for save path "{save_path}"')

def check_files_on_disk(client):
    # Check files on disk against torrents in each save path.

    # Check default save path
    default_save_path = client.app.default_save_path
    check_files_for_save_path(client, default_save_path)

    # Check save path for each category
    categories = client.torrents.categories()
    for category in categories:
        save_path = category.savePath
        check_files_for_save_path(client, save_path)