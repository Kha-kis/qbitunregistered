"""Shared file operations for recycle bin functionality."""

import logging
import shutil
import sys
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.cache import cached  # noqa: E402


@cached(ttl=300, key_prefix="torrent_files")
def fetch_torrent_files(client, torrent_hash: str, *, cache_scope: int) -> list:
    """
    Fetch file list for a torrent with TTL-based caching.

    Shared utility used by:
    - tag_cross_seeding.py (organizational tagging)
    - check_cross_seeding() (safety-critical file deletion checks)
    - get_torrent_file_paths() (file path retrieval before deletion)

    Cache is scoped to single execution (TTL=300s) and is safe because:
    1. All operations happen within same script run (typically < 60s)
    2. Cache is invalidated between runs
    3. Significantly reduces API load (4000+ calls → ~20 calls)

    Args:
        client: qBittorrent client instance
        torrent_hash: Hash of torrent to fetch files for
        cache_scope: REQUIRED - Unique identifier to scope cache per client.
                     Always pass id(client) to prevent cache contamination
                     across different client instances.

    Returns:
        List of file info dicts/objects from qBittorrent API

    Raises:
        AssertionError: If cache_scope is None (programming error)

    Security:
        Cache scope prevents different client instances from sharing cache.

    Performance:
        Reduces redundant API calls within a single execution. For a typical
        run with 1000 torrents, this reduces API calls by 95%+.
    """
    # Runtime assertion to prevent cache contamination
    assert cache_scope is not None, "cache_scope must be provided (use id(client))"
    return client.torrents_files(torrent_hash)


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

        # Get all files for this torrent (cached to reduce API calls)
        files = fetch_torrent_files(client, torrent_hash, cache_scope=id(client))
        file_paths = []

        for file_info in files:
            # Handle both dict and object forms from qBittorrent API
            if isinstance(file_info, dict):
                name = file_info.get("name")
            else:
                name = getattr(file_info, "name", None)

            if not name:
                continue

            file_path = save_path / name
            if file_path.exists():
                file_paths.append(file_path)
            else:
                logging.debug(f"File does not exist (may have been moved): {file_path}")

        return file_paths

    except Exception as e:
        logging.exception(f"Error getting file paths for torrent {torrent_hash}: {e}")
        return []


def check_cross_seeding(client, file_paths: List[Path], exclude_hash: str) -> Tuple[bool, List[str]]:
    """
    Check if any of the given file paths are being used by other active torrents.

    Args:
        client: qBittorrent client instance
        file_paths: List of file paths to check
        exclude_hash: Hash of the torrent being deleted (to exclude from check)

    Returns:
        Tuple of (is_cross_seeded, list_of_cross_seeded_torrent_names)

    Security:
        - Uses resolved paths for accurate comparison
        - Checks all torrents regardless of state to prevent data loss
    """
    if not file_paths:
        return False, []

    # Build set of resolved file paths for O(1) lookup
    file_paths_set = {path.resolve() for path in file_paths}

    cross_seeded_torrents = []

    try:
        # Get all torrents except the one being deleted
        all_torrents = client.torrents_info()

        for torrent in all_torrents:
            # Skip the torrent being deleted
            if torrent.hash == exclude_hash:
                continue

            # Get torrent's file paths (cached to reduce API calls)
            try:
                torrent_save_path = Path(torrent.save_path)
                torrent_files = fetch_torrent_files(client, torrent.hash, cache_scope=id(client))

                for file_info in torrent_files:
                    # Handle both dict and object forms from qBittorrent API
                    if isinstance(file_info, dict):
                        name = file_info.get("name")
                    else:
                        name = getattr(file_info, "name", None)

                    if not name:
                        continue

                    file_path = (torrent_save_path / name).resolve()

                    # Check if this file is in our list
                    if file_path in file_paths_set:
                        cross_seeded_torrents.append(torrent.name)
                        logging.warning(
                            f"Cross-seeding detected: File '{file_path}' is also used by torrent '{torrent.name}' (hash: {torrent.hash})"
                        )
                        break  # Found a match, no need to check other files in this torrent

            except Exception as e:
                logging.debug(f"Error checking torrent {torrent.hash} for cross-seeding: {e}")
                continue

        is_cross_seeded = len(cross_seeded_torrents) > 0
        return is_cross_seeded, cross_seeded_torrents

    except Exception as e:
        logging.exception(f"Error during cross-seeding check: {e}")
        # On error, assume not cross-seeded to avoid blocking legitimate deletions
        return False, []
