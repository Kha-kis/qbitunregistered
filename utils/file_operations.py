"""Shared file operations for recycle bin functionality."""

import logging
import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional


def move_files_to_recycle_bin(
    file_paths: List[Path], recycle_bin_path: Path, deletion_type: str, category: str = "uncategorized", dry_run: bool = False
) -> Tuple[int, List[Tuple[Path, str]]]:
    """
    Move files to recycle bin with hybrid organization (type + category).

    Args:
        file_paths: List of file paths to move
        recycle_bin_path: Root recycle bin directory
        deletion_type: Type of deletion ("orphaned" or "unregistered")
        category: Torrent category (default: "uncategorized")
        dry_run: If True, only simulate the operation

    Returns:
        Tuple of (success_count, failed_files) where failed_files is list of (path, error_message)

    Directory Structure:
        /recycle_bin/
          ├── orphaned/
          │   ├── movies/
          │   │   └── [full path structure]
          │   └── tv/
          │       └── [full path structure]
          └── unregistered/
              ├── movies/
              │   └── [full path structure]
              └── tv/
                  └── [full path structure]
    """
    success_count = 0
    failed_files = []

    # Validate deletion type
    if deletion_type not in ["orphaned", "unregistered"]:
        logging.warning(f"Invalid deletion_type '{deletion_type}', defaulting to 'orphaned'")
        deletion_type = "orphaned"

    # Sanitize category name (replace invalid characters)
    safe_category = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in category)
    if not safe_category:
        safe_category = "uncategorized"

    # Create base recycle bin structure: /recycle_bin/{type}/{category}/
    type_category_path = recycle_bin_path / deletion_type / safe_category

    if not dry_run:
        type_category_path.mkdir(parents=True, exist_ok=True)
        logging.debug(f"Ensured recycle bin path exists: {type_category_path}")

    for file_path in file_paths:
        try:
            # Get absolute path
            abs_file_path = file_path.resolve()

            # Preserve original directory structure
            # For cross-platform compatibility, handle both Unix and Windows paths
            if abs_file_path.drive:
                # Windows path with drive letter (C: -> C_)
                relative_path = Path(abs_file_path.drive.replace(":", "_")) / abs_file_path.relative_to(abs_file_path.anchor)
            else:
                # Unix path (strip leading slash)
                relative_path = abs_file_path.relative_to(abs_file_path.anchor)

            # Final destination: /recycle_bin/{type}/{category}/{original_path}
            dest_path = type_category_path / relative_path

            if dry_run:
                logging.info(f"Would move to recycle bin ({deletion_type}/{safe_category}): {file_path} -> {dest_path}")
                success_count += 1
            else:
                # Handle file collision with timestamp suffix
                if dest_path.exists():
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    stem = dest_path.stem
                    suffix = dest_path.suffix
                    dest_path = dest_path.parent / f"{stem}_{timestamp}{suffix}"
                    logging.info(f"Destination file exists, using timestamp suffix: {dest_path.name}")

                # Create parent directories
                dest_path.parent.mkdir(parents=True, exist_ok=True)

                # Move the file
                shutil.move(str(file_path), str(dest_path))
                logging.info(f"Moved to recycle bin ({deletion_type}/{safe_category}): {file_path} -> {dest_path}")
                success_count += 1

        except Exception as e:
            error_msg = str(e)
            logging.exception(f"Error moving file to recycle bin: {file_path}: {error_msg}")
            failed_files.append((file_path, error_msg))

    return success_count, failed_files


def get_torrent_file_paths(client, torrent_hash: str) -> List[Path]:
    """
    Get all file paths for a torrent before deletion.

    Args:
        client: qBittorrent client instance
        torrent_hash: Torrent hash

    Returns:
        List of absolute file paths for the torrent
    """
    try:
        # Get torrent info
        torrent_info = client.torrents_info(torrent_hashes=torrent_hash)
        if not torrent_info:
            logging.warning(f"Torrent with hash {torrent_hash} not found")
            return []

        torrent = torrent_info[0]
        save_path = Path(torrent.save_path)

        # Get all files for this torrent
        files = client.torrents_files(torrent_hash)
        file_paths = []

        for file_info in files:
            file_path = save_path / file_info.name
            if file_path.exists():
                file_paths.append(file_path)
            else:
                logging.debug(f"File does not exist (may have been moved): {file_path}")

        return file_paths

    except Exception as e:
        logging.exception(f"Error getting file paths for torrent {torrent_hash}: {e}")
        return []
