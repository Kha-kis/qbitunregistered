# File: scripts/orphaned.py
import os
import logging

def get_files_in_directory(directory):
    # Get all files in a given directory.
    files = set()
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            files.add(os.path.join(root, filename))
    return files

def check_files_on_disk(client):
    # Check files on_disk against torrents in each save path.

    # Get default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Create a set of all unique save paths
    save_paths = set()
    save_paths.add(default_save_path)

    # Get all torrents
    torrents = client.torrents.info()

    # Add all unique save paths to set from torrents
    for torrent in torrents:
        save_paths.add(torrent.save_path)

    # Check each unique save path
    for save_path in save_paths:
        print(f"Checking save path: {save_path}")

        # Get files on_disk for current save path
        files_on_disk = get_files_in_directory(save_path)

        # Check each torrent in current save path
        for torrent in torrents:
            if torrent.save_path == save_path:
                check_files_for_torrent(torrent, files_on_disk)




    # Get save paths for each category
    categories = client.torrents_categories().values()
    for category in categories:
        if category.savePath != '':
            save_path = category.savePath
            print(f"Checking save path: {save_path}")
            files_on_disk = get_files_in_directory(save_path)
            category_torrents = client.torrents.info(category=category.name)
            for torrent in category_torrents:
                if torrent.save_path == save_path:
                    check_files_for_torrent(torrent, files_on_disk)


    # Get save paths for each category
    categories = client.torrents_categories()
    for category_name, category in categories.items():
        if category.savePath != '':
            save_path = category.savePath
            files_on_disk = get_files_in_directory(save_path)
            category_torrents = client.torrents.info(category=category_name)
            for torrent in category_torrents:
                if torrent.save_path == save_path:
                    check_files_for_torrent(torrent, files_on_disk)

def check_files_for_torrent(torrent, files_on_disk):
    # Check if each file in the given list is in the torrent's files.
    torrent_files = set(os.path.join(torrent.save_path, file['name']) for file in torrent.files)

    logging.info(f"Checking files for torrent '{torrent.name}' in save path '{torrent.save_path}'")
    logging.info(f"Total files on disk: {len(files_on_disk)}")
    logging.info(f"Total files associated with torrent: {len(torrent_files)}")

    unregistered_files = []
    for file in files_on_disk:
        if file not in torrent_files:
            unregistered_files.append(file)
#            logging.info(f'File "{file}" is on disk but not in the client for save path "{torrent.save_path}"')

    logging.info(f"Total unregistered files: {len(unregistered_files)}")
