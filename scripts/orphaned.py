import os
import logging

def check_files_on_disk(client):
    # Get the default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Get all torrents from qBittorrent
    torrents = client.torrents.info()

    # Collect all files and directories within the default save path
    files_on_disk = get_files_in_directory(default_save_path)

    # Check if files on disk have corresponding torrents
    orphaned_files = []
    for file_path in files_on_disk:
        found = False
        for torrent in torrents:
            content_path = torrent.content_path
            if content_path and file_path.startswith(content_path):
                found = True
                break
        if not found:
            orphaned_files.append(file_path)

    # Log the orphaned file paths
    if orphaned_files:
        logging.info("Orphaned files:")
        for file_path in orphaned_files:
            logging.info(file_path)
    else:
        logging.info("No orphaned files found.")

def get_files_in_directory(directory):
    # Get all files and directories in a given directory
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files
