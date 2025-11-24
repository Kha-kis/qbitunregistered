import logging
import re
from pathlib import Path
from typing import List, Dict, Any, Optional
from fnmatch import translate
import sys
import shutil

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.cache import cached  # noqa: E402


@cached(ttl=300, key_prefix="app_default_save_path")
def _get_default_save_path(client, *, cache_scope: int) -> str:
    """
    Cached wrapper for client.application.default_save_path.
    Reduces redundant API calls for read-only application settings.

    Args:
        client: qBittorrent client instance
        cache_scope: REQUIRED - Unique identifier to scope cache per client.
                     Always pass id(client) to prevent cache contamination
                     across different client instances.

    Raises:
        AssertionError: If cache_scope is None (programming error)
    """
    # Runtime assertion to prevent cache contamination
    assert cache_scope is not None, "cache_scope must be provided (use id(client))"
    return str(client.application.default_save_path)


@cached(ttl=300, key_prefix="torrent_categories")
def _get_categories(client, *, cache_scope: int) -> Dict[str, Any]:
    """
    Cached wrapper for client.torrent_categories.categories property.
    Reduces redundant API calls for category configuration.
    Returns the underlying dictionary with all defined categories.

    Args:
        client: qBittorrent client instance
        cache_scope: REQUIRED - Unique identifier to scope cache per client.
                     Always pass id(client) to prevent cache contamination
                     across different client instances.

    Raises:
        AssertionError: If cache_scope is None (programming error)
    """
    # Runtime assertion to prevent cache contamination
    assert cache_scope is not None, "cache_scope must be provided (use id(client))"
    return client.torrent_categories.categories


def check_files_on_disk(
    client, torrents: List, exclude_file_patterns: Optional[List[str]] = None, exclude_dirs: Optional[List[str]] = None
) -> List[str]:
    """
    Identifies orphaned files on disk that are not associated with any active torrents in qBittorrent.
    """
    # Avoid mutable default arguments - create fresh lists if None
    exclude_file_patterns = exclude_file_patterns or []
    exclude_dirs = exclude_dirs or []

    logging.debug("Entering check_files_on_disk function...")

    # Get the default save path (cached to reduce API calls)
    # Use id(client) to scope cache per client instance, preventing contamination
    default_save_path = Path(_get_default_save_path(client, cache_scope=id(client)))

    # Get explicitly defined category save paths (cached to reduce API calls)
    categories = _get_categories(client, cache_scope=id(client))
    category_paths = {
        Path(category.get("savePath", "")).resolve() if category.get("savePath") else default_save_path / category_name
        for category_name, category in categories.items()
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

    # Track files used by torrents - use resolved paths for accurate comparison
    # Cache resolved save paths to avoid redundant syscalls (1M+ syscalls → ~1K for 1K torrents)
    resolved_save_paths = {}
    torrent_files: set[Path] = set()

    for torrent in torrents:
        # Cache resolve() result per unique save_path
        if torrent.save_path not in resolved_save_paths:
            resolved_save_paths[torrent.save_path] = Path(torrent.save_path).resolve()

        base_path = resolved_save_paths[torrent.save_path]
        # Add all files for this torrent
        torrent_files.update((base_path / file.name).resolve() for file in torrent.files)

    logging.debug(
        f"Tracking {len(torrent_files)} files from {len(torrents)} torrents (using {len(resolved_save_paths)} unique save paths)"
    )

    # Separate literal paths from patterns for efficient handling
    # Patterns (with wildcards) are compiled to regex, literals are resolved for exact matching
    exclude_dir_paths = set()
    exclude_dir_patterns_raw = []

    for d in exclude_dirs or []:
        if "*" in d or "?" in d:
            # This is a pattern - keep for regex compilation
            exclude_dir_patterns_raw.append(d)
        else:
            # This is a literal path - resolve for exact matching
            exclude_dir_paths.add(Path(d).resolve())

    # Pre-compile file patterns to regex for performance (O(1) matching vs O(n) fnmatch)
    compiled_file_patterns = []
    if exclude_file_patterns:
        for pattern in exclude_file_patterns:
            try:
                regex_pattern = translate(pattern)
                compiled_file_patterns.append(re.compile(regex_pattern))
            except re.error as e:
                logging.warning(f"Invalid file pattern '{pattern}': {e}")

    # Pre-compile directory patterns to regex for performance
    compiled_dir_patterns = []
    for pattern in exclude_dir_patterns_raw:
        try:
            regex_pattern = translate(pattern)
            compiled_dir_patterns.append(re.compile(regex_pattern))
        except re.error as e:
            logging.warning(f"Invalid directory pattern '{pattern}': {e}")

    orphaned_files = []
    files_checked = 0
    files_excluded_by_pattern = 0
    files_excluded_by_dir = 0

    # Scan category paths recursively
    for save_path in sorted(valid_save_paths, key=lambda p: len(str(p))):  # Sort by shortest path first
        logging.info(f"Checking files in: {save_path}")

        for entry in save_path.rglob("*"):  # Recursive check inside category paths
            # Resolve path once at the start of the loop for performance
            entry_resolved = entry.resolve()

            # Check if entry is in an excluded directory (early exit for better performance)
            if exclude_dir_paths or compiled_dir_patterns:
                entry_str = str(entry_resolved)

                # Check direct match first (O(1) lookup)
                if entry_resolved in exclude_dir_paths:
                    files_excluded_by_dir += 1
                    continue

                # Check if any parent is in excluded paths (optimized: O(p) where p = parent count)
                # Instead of O(e*p) where e = excluded path count
                is_parent_excluded = any(parent in exclude_dir_paths for parent in entry_resolved.parents)
                if is_parent_excluded:
                    files_excluded_by_dir += 1
                    continue

                # Check pattern matching last (most expensive)
                if compiled_dir_patterns and any(pattern.match(entry_str) for pattern in compiled_dir_patterns):
                    files_excluded_by_dir += 1
                    continue

            if entry.is_file():
                files_checked += 1

                # Check if file matches any exclude patterns (using pre-compiled regex)
                if compiled_file_patterns:
                    if any(pattern.match(entry.name) for pattern in compiled_file_patterns):
                        logging.debug(f"Excluding file matching pattern: {entry}")
                        files_excluded_by_pattern += 1
                        continue

                # Use resolved path for comparison (already computed above)
                if entry_resolved in torrent_files:
                    continue  # Skip files that are tracked by torrents

                orphaned_files.append(str(entry))

    logging.info(
        f"Scanned {files_checked} files, excluded {files_excluded_by_pattern} by pattern, "
        f"excluded {files_excluded_by_dir} by directory"
    )

    return orphaned_files


def delete_orphaned_files(
    orphaned_files: List[str], dry_run: bool, client, torrents: Optional[List] = None, recycle_bin: Optional[str] = None
):
    """
    Deletes orphaned files and removes empty directories, while preserving active save paths.
    If dry-run is enabled, it logs what would be deleted without actually deleting files.

    Args:
        orphaned_files: List of orphaned file paths to delete
        dry_run: If True, only log actions without deleting
        client: qBittorrent client instance
        torrents: Optional list of torrents (avoids redundant API call if provided)
        recycle_bin: Optional path to move files to instead of deleting
    """
    deleted_files_count = 0
    skipped_files = []
    orphaned_files_set = {Path(file) for file in orphaned_files}  # Convert to Path objects for easier comparison

    # Get active save paths to prevent accidental deletion (cached to reduce API calls)
    # Use id(client) to scope cache per client instance, preventing contamination
    default_save_path = Path(_get_default_save_path(client, cache_scope=id(client)))
    active_save_paths = {default_save_path}

    # Get save paths from all torrents (reuse provided list to avoid redundant API call)
    if torrents is None:
        torrents = client.torrents.info()
    active_save_paths.update(Path(torrent.save_path) for torrent in torrents)

    # Get save paths from categories (cached to reduce API calls)
    categories = _get_categories(client, cache_scope=id(client))
    for category_name, category in categories.items():
        category_save_path = (
            Path(category.get("savePath", "")).resolve() if category.get("savePath") else default_save_path / category_name
        )
        active_save_paths.add(category_save_path)

    if not orphaned_files:
        logging.info("No orphaned files found. Nothing to delete.")
        return

    # Track directories that will become empty
    potential_empty_dirs = set()

    recycle_bin_path = Path(recycle_bin) if recycle_bin else None
    if recycle_bin_path and not dry_run:
        if not recycle_bin_path.exists():
            logging.info(f"Creating recycle bin directory: {recycle_bin_path}")
        recycle_bin_path.mkdir(parents=True, exist_ok=True)

    for file_path in orphaned_files_set:
        parent_dir = file_path.parent
        while parent_dir != parent_dir.parent:  # Add parent and all ancestor directories
            potential_empty_dirs.add(parent_dir)
            parent_dir = parent_dir.parent

        if dry_run:
            if recycle_bin:
                logging.info(f"Would move orphaned file to recycle bin: {file_path}")
            else:
                logging.info(f"Would delete orphaned file: {file_path}")
            deleted_files_count += 1
        else:
            try:
                if recycle_bin_path:
                    # Maintain directory structure in recycle bin
                    # Calculate relative path from the root of the file system or a common base?
                    # Since files can be anywhere, we'll use the full path structure inside the recycle bin
                    # e.g. /mnt/data/movie.mkv -> /recycle_bin/mnt/data/movie.mkv
                    # This avoids collisions and makes restoration easier.
                    
                    # Remove drive letter (Windows) or leading slash (Unix) for safe joining
                    relative_path = file_path.relative_to(file_path.anchor)
                    dest_path = recycle_bin_path / relative_path
                    
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(file_path), str(dest_path))
                    logging.info(f"Moved orphaned file to recycle bin: {file_path} -> {dest_path}")
                else:
                    file_path.unlink()
                    logging.info(f"Deleted orphaned file: {file_path}")
                deleted_files_count += 1
            except Exception as e:
                logging.error(f"Error processing {file_path}: {e}")
                skipped_files.append((file_path, str(e)))

    # Determine which directories would be empty
    empty_dirs_to_delete = set()

    for dir_path in sorted(potential_empty_dirs, key=lambda p: len(str(p)), reverse=True):
        while dir_path not in active_save_paths and dir_path not in empty_dirs_to_delete:
            try:
                existing_files = set(dir_path.iterdir())  # Check existing files in the directory
            except (PermissionError, FileNotFoundError) as e:
                logging.warning(f"Cannot access directory {dir_path}: {e}")
                break  # Stop checking this path and its parents
            except Exception as e:
                logging.error(f"Unexpected error accessing directory {dir_path}: {e}")
                break

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
    action = "moved to recycle bin" if recycle_bin else "deleted"
    if dry_run:
        logging.info(
            f"Dry-run: Would have {action} {deleted_files_count} orphaned files and removed {deleted_dirs_count} empty directories."
        )
    else:
        logging.info(f"Successfully {action} {deleted_files_count} orphaned files and removed {deleted_dirs_count} empty directories.")

    if skipped_files:
        logging.warning(f"Skipped {len(skipped_files)} files due to errors:")
        for file_path, reason in skipped_files:
            logging.warning(f" - {file_path}: {reason}")
