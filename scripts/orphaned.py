import logging
from pathlib import Path
from typing import List

def check_files_on_disk(client, torrents: List, exclude_file_patterns: List[str] = [], exclude_dirs: List[str] = []) -> List[str]:
    """
    Identifies orphaned files on disk that are not associated with any active torrents in qBittorrent.
    """
    logging.debug("Entering check_files_on_disk function...")

    # Get the default save path
    default_save_path = Path(client.application.defaultSavePath)

    # Get explicitly defined category save paths
    category_paths = {
        Path(category.get('savePath', '')).resolve() if category.get('savePath') else default_save_path / category_name
        for category_name, category in client.torrent_categories.categories.items()
    }

    # Only scan the default save path and category save paths
    valid_save_paths = {default_save_path} | category_paths

    # Ensure paths exist before scanning
    valid_save_paths = {path for path in valid_save_paths if path.exists()}

    # Remove redundant subdirectories (keep only the highest-level paths)
    filtered_save_paths = set()
    for path in sorted(valid_save_paths, key=lambda p: len(str(p))):
        if not any(parent in filtered_save_paths for parent in path.parents):
            filtered_save_paths.add(path)

    valid_save_paths = filtered_save_paths

    logging.info(f"Scanning {len(valid_save_paths)} save paths for orphaned files...")

    # Track files used by torrents
    torrent_files = {Path(torrent.save_path) / file.name for torrent in torrents for file in torrent.files}

    orphaned_files = []

    # Scan category paths recursively
    for save_path in sorted(valid_save_paths, key=lambda p: len(str(p))):  # Sort by shortest path first
        logging.info(f"Checking files in: {save_path}")

        for entry in save_path.rglob("*"):  # Recursive check inside category paths
            if entry.is_file():
                if entry in torrent_files:
                    continue  # Skip files that are tracked by torrents
                orphaned_files.append(str(entry))

    return orphaned_files

def delete_orphaned_files(orphaned_files: List[str], dry_run: bool, client):
    """
    Deletes orphaned files and removes empty directories, while preserving active save paths.
    If dry-run is enabled, it logs what would be deleted without actually deleting files.
    """
    deleted_files_count = 0
    skipped_files = []
    orphaned_files_set = {Path(file) for file in orphaned_files}  # Convert to Path objects for easier comparison

    # Get active save paths to prevent accidental deletion
    active_save_paths = {Path(client.application.defaultSavePath)}

    # Get save paths from all torrents
    torrents = client.torrents.info()
    active_save_paths.update(Path(torrent.save_path) for torrent in torrents)

    # Get save paths from categories
    categories = client.torrent_categories.categories
    default_save_path = Path(client.application.defaultSavePath)
    for category_name, category in categories.items():
        category_save_path = Path(category.get('savePath', '')).resolve() if category.get('savePath') else default_save_path / category_name
        active_save_paths.add(category_save_path)

    if not orphaned_files:
        logging.info("No orphaned files found. Nothing to delete.")
        return

    # Track directories that will become empty
    potential_empty_dirs = set()

    for file_path in orphaned_files_set:
        parent_dir = file_path.parent
        while parent_dir != parent_dir.parent:  # Add parent and all ancestor directories
            potential_empty_dirs.add(parent_dir)
            parent_dir = parent_dir.parent

        if dry_run:
            logging.info(f"Would delete orphaned file: {file_path}")
            deleted_files_count += 1
        else:
            try:
                file_path.unlink()
                logging.info(f"Deleted orphaned file: {file_path}")
                deleted_files_count += 1
            except Exception as e:
                logging.error(f"Error deleting {file_path}: {e}")
                skipped_files.append((file_path, str(e)))

    # Determine which directories would be empty
    empty_dirs_to_delete = set()

    for dir_path in sorted(potential_empty_dirs, key=lambda p: len(str(p)), reverse=True):
        while dir_path not in active_save_paths and dir_path not in empty_dirs_to_delete:
            existing_files = set(dir_path.iterdir())  # Check existing files in the directory
            remaining_files = existing_files - orphaned_files_set  # What's left after simulated deletion

            if not remaining_files:  # If directory would be empty
                empty_dirs_to_delete.add(dir_path)
                orphaned_files_set.add(dir_path)  # Mark as processed
                dir_path = dir_path.parent  # Move to parent directory and check again
            else:
                break  # Stop checking if this directory is not empty

    # Log directories that would be deleted
    deleted_dirs_count = 0
    for dir_path in sorted(empty_dirs_to_delete, key=lambda p: len(str(p)), reverse=True):
        if dry_run:
            logging.info(f"Would remove empty directory: {dir_path}")
            deleted_dirs_count += 1  # Increment count in dry-run mode
        else:
            try:
                dir_path.rmdir()
                logging.info(f"Deleted empty directory: {dir_path}")
                deleted_dirs_count += 1
            except Exception as e:
                logging.error(f"Error deleting directory {dir_path}: {e}")

    # Final Summary
    if dry_run:
        logging.info(f"Dry-run: Would have deleted {deleted_files_count} orphaned files and {deleted_dirs_count} empty directories.")
    else:
        logging.info(f"Deleted {deleted_files_count} orphaned files and {deleted_dirs_count} empty directories.")

    if skipped_files:
        logging.warning(f"Skipped {len(skipped_files)} files due to errors:")
        for file_path, reason in skipped_files:
            logging.warning(f" - {file_path}: {reason}")