import os
import fnmatch
from typing import List
import logging

def check_files_on_disk(client, torrents: List, exclude_file_patterns: List[str] = [], exclude_dirs: List[str] = []) -> List[str]:
    logging.debug("Entering check_files_on_disk function...")

    def should_exclude_file(file: str) -> bool:
        return any(fnmatch.fnmatch(file, pattern) for pattern in exclude_file_patterns)

    # Get the save paths to check
    save_paths = {client.application.defaultSavePath}

    # Get the categories and their save paths
    categories = client.torrent_categories.categories
    save_paths.update(category['savePath'] for category in categories.values())

    # Print out all the paths to be checked
    for path in save_paths:
        logging.info(f"Checking file path: {path}")

    # Identify all torrent-associated files
    torrent_files = [os.path.join(torrent.save_path, file.name) for torrent in torrents for file in torrent.files]
    logging.debug(f"Torrent files: {torrent_files}")

    all_files = []
    for path in save_paths:
        for root, dirs, files in os.walk(path, topdown=True):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(os.path.join(root, d), pattern) for pattern in exclude_dirs)]
            
            for file in files:
                file_path = os.path.join(root, file)
                if not any(fnmatch.fnmatch(file, pattern) for pattern in exclude_files) and not should_exclude_file(file):
                    all_files.append(file_path)

    logging.debug(f"All files: {all_files}")

    # Identify orphaned files: those that exist in the file system but not in the list of torrent-associated files
    orphaned_files = [file for file in all_files if file not in torrent_files]
    logging.info("Orphaned files:")
    for file_path in orphaned_files:
        logging.info(file_path)

    return orphaned_files
