import os
import logging

def check_files_on_disk(client):
    # Get the list of root files
    root_files = get_root_files()

    # Get the list of files associated with torrents
    torrent_files = set(file['name'] for torrent in client.torrents.info() for file in torrent.files())

    # Identify orphaned files
    orphaned_files = set(root_files) - torrent_files

    return orphaned_files
