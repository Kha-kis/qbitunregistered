import os
import logging
from qbittorrentapi import Client

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files

def check_files_on_disk(client):
    # Check files on disk against torrents in each save path.

    # Get download folders from qBittorrent client
    download_folders = set(client.app.default_save_path)
    categories = client.torrents_categories()
    for category in categories.values():
        if category.savePath:
            download_folders.add(category.savePath)

    # Collect files from download folders
    files_on_disk = set()
    for folder in download_folders:
        files_on_disk.update(get_files_in_directory(folder))

    # Fetch file information from qBittorrent
    qbit_files = set()
    torrents = client.torrents.info()
    for torrent in torrents:
        for file in torrent.files:
            qbit_files.add(os.path.join(torrent.save_path, file.name))

    # Calculate orphaned files
    orphaned_files = files_on_disk - qbit_files

    # Display orphaned file information
    logging.info(f"Total orphaned files: {len(orphaned_files)}")
    total_size = sum(os.path.getsize(file) for file in orphaned_files)
    logging.info(f"Total size of orphaned files: {total_size} bytes")

    for file in orphaned_files:
        size = os.path.getsize(file)
        logging.info(f"Orphaned File: {file} - Size: {size} bytes")

def __init__(args, logger):
    client = Client(host=args.host, port=args.port, username=args.username, password=args.password)

    # Check files on disk against torrents in each save path
    check_files_on_disk(client)

def add_arguments(subparser):
    parser = subparser.add_parser('orphaned')
    parser.add_argument('--host', type=str, default='localhost', help='qBittorrent host')
    parser.add_argument('--port', type=int, default=8080, help='qBittorrent port')
    parser.add_argument('--username', type=str, default='', help='qBittorrent username')
    parser.add_argument('--password', type=str, default='', help='qBittorrent password')
