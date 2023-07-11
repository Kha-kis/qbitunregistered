import os
import logging

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files

def find_orphaned_files(client):
    # Get default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Get files on disk for default save path
    files_on_disk = get_files_in_directory(default_save_path)

    # Check files on disk against torrents in each save path
    torrents = client.torrents.info()
    orphaned_files = []
    for torrent in torrents:
        if torrent.save_path == default_save_path:
            check_files_for_torrent(torrent, files_on_disk)

    return orphaned_files

def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if file not in torrent.files:
            logging.info(f'File "{file}" is orphaned')

# Find orphaned files
orphaned_files = find_orphaned_files(client)

# Display the count of orphaned files
logging.info(f"Total orphaned files: {len(orphaned_files)}")
