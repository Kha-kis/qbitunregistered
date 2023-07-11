# File: scripts/orphaned.py
import os
import logging

def check_files_on_disk(client):
    # Check files on disk against torrents in each save path.

    # Get default save path from qBittorrent API
    default_save_path = client.app.default_save_path

    # Create a set of all unique save paths
    save_paths = set()
    save_paths.add(default_save_path)

    # Get save paths for each category
    categories = client.torrents_categories().values()
    for category in categories:
        if category.savePath != '':
            save_paths.add(category.savePath)

    # Get all torrents
    torrents = client.torrents.info()

    # Iterate over unique save paths
    for save_path in save_paths:
        logging.info(f"Checking save path: {save_path}")

        # Get files and directories on disk for current save path
        files_on_disk = set()
        dirs_on_disk = set()
        for root, dirs, filenames in os.walk(save_path):
            files_on_disk.update([os.path.join(root, filename) for filename in filenames])
            dirs_on_disk.update([os.path.join(root, directory) for directory in dirs])

        # Collect unregistered files and directories per save path
        unregistered_files = set()
        unregistered_dirs = set()

        # Check each torrent in current save path
        for torrent in torrents:
            if torrent.save_path == save_path:
                torrent_files = set(os.path.join(torrent.save_path, file['name']) for file in torrent.files)
                unregistered_files.update(file for file in files_on_disk if file not in torrent_files)

        # Check for unregistered directories
        unregistered_dirs = dirs_on_disk - set(os.path.dirname(file) for file in unregistered_files)
        unregistered_dirs = set(dir_path for dir_path in unregistered_dirs if not os.listdir(dir_path))

        num_unregistered_files = len(unregistered_files)
        num_unregistered_dirs = len(unregistered_dirs)

        logging.info(f"Total unregistered files: {num_unregistered_files}")
        logging.info(f"Total unregistered directories: {num_unregistered_dirs}")

        if num_unregistered_files > 0 or num_unregistered_dirs > 0:
            logging.info("Unregistered Files:")
            for file in unregistered_files:
                logging.info(file)

            logging.info("Unregistered Directories:")
            for directory in unregistered_dirs:
                logging.info(directory)

            logging.info("End of Unregistered Files and Directories")
        else:
            logging.info("No unregistered files or directories found.")