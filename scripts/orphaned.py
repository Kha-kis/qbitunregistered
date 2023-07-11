import os
import logging

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.append(os.path.join(root, filename))
    return files

def check_files_on_disk(client, config, torrents):
    # Check files on disk against torrents in each save path.

    # Get default save path
    default_save_path = config['default_save_path']
    files_on_disk = get_files_in_directory(default_save_path)
    torrents = client.torrents.info()
    for torrent in torrents:
        if not torrent.category and torrent.save_path == default_save_path:
            check_files_for_torrent(torrent, files_on_disk)

    # Get save paths for each category
    categories = client.torrents.categories()
    for category in categories:
        save_path = category['savePath']
        files_on_disk = get_files_in_directory(save_path)
        torrents = client.torrents.info(category=category['name'])
        for torrent in torrents:
            if torrent.save_path == save_path:
                check_files_for_torrent(torrent, files_on_disk)

def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if file not in torrent.files:
            logging.info(f'File "{file}" is on disk but not in the client for save path "{torrent.save_path}"')