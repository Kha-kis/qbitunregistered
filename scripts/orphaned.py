# File: scripts/orphaned.py
import os
import logging

def get_all_files_in_directory(directory):
    #Get all files in a given directory.
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files

def check_files_for_torrent(torrent, files_on_disk):
    #Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if file not in torrent.files:
            logging.info(f'File "{file}" is on disk but not in the client for save path "{torrent.save_path}"')

def check_files_on_disk(client):
    #Check all files on disk against the files in each torrent.
    for torrent in client.torrents.info():
        files_on_disk = get_all_files_in_directory(torrent.save_path)
        check_files_for_torrent(torrent, files_on_disk)