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

def get_directories_in_directory(directory):
    # Get all directories in a given directory.
    directories = []
    for root, dirs, _ in os.walk(directory):
        for directory in dirs:
            directories.append(os.path.join(root, directory))
    return directories


def check_files_on_disk(client):
    # Check files on disk for orphaned files and directories.

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

    # Iterate over unique save paths
    for save_path in save_paths:
        logging.info(f"Checking save path: {save_path}")

        # Get files and directories on disk for current save path
        files_on_disk = set()
        dirs_on_disk = set()
        for root, dirs, filenames in os.walk(save_path):
            files_on_disk.update([os.path.join(root, filename) for filename in filenames])
            dirs_on_disk.update([os.path.join(root, directory) for directory in dirs])

        # Check for orphaned files
        orphaned_files = set()
        for file in files_on_disk:
            if not any(os.path.join(save_path, torrent_file) == file for torrent_file in client.torrents.files):
                orphaned_files.add(file)

        # Check for orphaned directories
        orphaned_dirs = set()
        for directory in dirs_on_disk:
            dir_path = os.path.join(save_path, directory)
            if not os.listdir(dir_path):
                orphaned_dirs.add(dir_path)

        num_orphaned_files = len(orphaned_files)
        num_orphaned_dirs = len(orphaned_dirs)

        logging.info(f"Total orphaned files: {num_orphaned_files}")
        logging.info(f"Total orphaned directories: {num_orphaned_dirs}")

        if num_orphaned_files > 0 or num_orphaned_dirs > 0:
            logging.info("Orphaned Files:")
            for file in orphaned_files:
                logging.info(file)

            logging.info("Orphaned Directories:")
            for directory in orphaned_dirs:
                logging.info(directory)

            logging.info("End of Orphaned Files and Directories")
        else:
            logging.info("No orphaned files or directories found.")