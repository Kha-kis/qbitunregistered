import os
import logging

def get_files_in_directory(directory):
    # Get all files within the specified save paths.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            file_path = os.path.join(root, filename)
            if file_path.startswith(directory):
                files.add(file_path)
    return files

def check_files_on_disk(client):
    # Check files on disk against torrents in each save path.

    # Get default save path
    default_save_path = client.application.default_save_path
    default_files_on_disk = get_files_in_directory(default_save_path)

    # Check default save path for torrents without category
    torrents = client.torrents.info(category=None)
    for torrent in torrents:
        if torrent.save_path == default_save_path:
            check_files_for_torrent(torrent, default_files_on_disk)

    # Get save paths for each category
    categories = client.application.categories()
    for category in categories:
        save_path = category.save_path
        category_files_on_disk = get_files_in_directory(save_path)

        # Check save path for torrents in the category
        torrents = client.torrents.info(category=category.name)
        for torrent in torrents:
            if torrent.save_path == save_path:
                check_files_for_torrent(torrent, category_files_on_disk)

def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    for file in files_on_disk:
        if file not in torrent.files:
            logging.info(f'File "{file}" is on disk but not in the client for save path "{torrent.save_path}"')
