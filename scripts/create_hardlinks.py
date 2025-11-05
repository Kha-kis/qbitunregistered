import os
import logging
import shutil
from typing import List, Any
from pathlib import Path
from tqdm import tqdm


def _get_dir_size(path: Path) -> int:
    """
    Get total size of files in a directory.

    Args:
        path: Path to directory

    Returns:
        Total size in bytes
    """
    total = 0
    try:
        for entry in path.rglob('*'):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception as e:
        logging.warning(f"Error calculating size for {path}: {e}")
    return total


def _check_disk_space(target_path: Path, required_bytes: int, safety_margin: float = 0.1,
                      allow_proceed_on_check_failure: bool = False) -> bool:
    """
    Check if there's enough disk space.

    Args:
        target_path: Target directory path
        required_bytes: Required space in bytes
        safety_margin: Safety margin (0.1 = 10% extra space required)
        allow_proceed_on_check_failure: If True, return True when disk check fails;
                                       if False (default), fail-safe by returning False
                                       when the check cannot be performed

    Returns:
        True if enough space available (or if check fails and allow_proceed_on_check_failure=True)
        False if not enough space or if check fails (fail-safe default behavior)

    Note:
        The default behavior is fail-safe: if we cannot verify sufficient disk space
        (e.g., due to permission errors or invalid paths), we return False to prevent
        potentially dangerous operations. Set allow_proceed_on_check_failure=True to
        override this behavior and proceed despite check failures.
    """
    try:
        stat = shutil.disk_usage(target_path)
        required_with_margin = required_bytes * (1 + safety_margin)

        if stat.free < required_with_margin:
            logging.warning(f"Low disk space: {stat.free / (1024**3):.2f} GB available, "
                            f"{required_with_margin / (1024**3):.2f} GB required (with margin)")
            return False
        return True
    except Exception as e:
        logging.error(f"Error checking disk space: {e}")
        if allow_proceed_on_check_failure:
            logging.warning("Proceeding despite disk space check failure (allow_proceed_on_check_failure=True)")
            return True
        else:
            logging.warning("Failing safely due to disk space check failure (allow_proceed_on_check_failure=False)")
            return False


def _is_safe_path(base_path: Path, target_path: Path) -> bool:
    """
    Check if target_path is safely within base_path (no path traversal).

    Args:
        base_path: The base directory that should contain the target
        target_path: The path to validate

    Returns:
        True if safe, False if path traversal detected
    """
    try:
        # Resolve both paths to absolute, canonical paths
        base_resolved = base_path.resolve()
        target_resolved = target_path.resolve()

        # Check if target is within base
        return target_resolved.is_relative_to(base_resolved)
    except (ValueError, RuntimeError):
        return False


def create_hard_links(target_dir: str, torrents: List[Any], dry_run: bool = False) -> None:
    """
    Create hard links for completed torrents in the target directory.

    Includes security checks to prevent path traversal attacks.

    Args:
        target_dir: Target directory where hard links will be created
        torrents: List of torrent objects to process
        dry_run: If True, only log actions without creating links

    Note: Hard links only work within the same filesystem. Cross-filesystem
          linking will fail.

    Security: Validates all paths to prevent directory traversal attacks.
    """
    try:
        if not target_dir:
            logging.error("No target directory specified for hard link creation")
            return

        target_path = Path(target_dir).resolve()  # Resolve to absolute path

        # Validate target directory
        if not dry_run and not target_path.exists():
            logging.error(f"Target directory does not exist: {target_dir}")
            return

        # Security: Defensive check - resolve() should always return absolute path,
        # but verify as safeguard against future code changes
        if not target_path.is_absolute():
            logging.error(f"Target directory must be an absolute path: {target_dir}")
            return

        completed_torrents = [t for t in torrents if t.state_enum.is_complete]
        logging.info(f"Processing {len(completed_torrents)} completed torrents out of {len(torrents)} total")

        # Calculate total size required for hard links
        if not dry_run:
            logging.info("Calculating required disk space...")
            total_size_required = 0
            for torrent in completed_torrents:
                content_path = Path(torrent.save_path) / torrent.name
                if content_path.exists():
                    if content_path.is_dir():
                        total_size_required += _get_dir_size(content_path)
                    else:
                        try:
                            total_size_required += content_path.stat().st_size
                        except Exception as e:
                            logging.warning(f"Could not get size for {content_path}: {e}")

            # Check if there's enough disk space
            if total_size_required > 0:
                logging.info(f"Total space required: {total_size_required / (1024**3):.2f} GB")
                if not _check_disk_space(target_path, total_size_required):
                    logging.error("Insufficient disk space for hard link creation. Aborting.")
                    return
                logging.info("Disk space check passed")

        total_links = 0
        total_skipped = 0
        total_errors = 0
        created_dirs = set()  # Cache for created directories to avoid redundant mkdir calls

        for torrent in tqdm(completed_torrents, desc="Creating hard links", unit="torrent"):
            try:
                content_path = Path(torrent.save_path) / torrent.name

                # Handle both directories and single files
                if content_path.is_dir():
                    # Process directory torrents
                    for root, _dirs, files in os.walk(content_path):
                        for file in files:
                            try:
                                source_path = Path(root) / file

                                # Preserve relative directory structure
                                rel_path = source_path.relative_to(content_path)
                                category_dir = torrent.category or ''
                                target_file_path = target_path / category_dir / rel_path

                                # Security: Check for path traversal
                                if not _is_safe_path(target_path, target_file_path):
                                    logging.error(f"Security: Path traversal detected, skipping: {target_file_path}")
                                    total_errors += 1
                                    continue

                                if target_file_path.exists():
                                    logging.debug(f"Hard link already exists: {target_file_path}")
                                    total_skipped += 1
                                    continue

                                if dry_run:
                                    logging.info(f"[Dry Run] Would create hard link: {source_path} -> {target_file_path}")
                                    total_links += 1
                                else:
                                    # Create parent directories (cached to avoid redundant mkdir calls)
                                    parent_dir = target_file_path.parent
                                    if parent_dir not in created_dirs:
                                        parent_dir.mkdir(parents=True, exist_ok=True)
                                        created_dirs.add(parent_dir)

                                    # Create hard link
                                    os.link(source_path, target_file_path)
                                    logging.info(f"Hard link created: {source_path} -> {target_file_path}")
                                    total_links += 1

                            except OSError as e:
                                logging.error(f"Failed to create hard link for '{source_path}': {e}")
                                total_errors += 1
                            except Exception as e:
                                logging.error(f"Unexpected error processing file '{source_path}': {e}")
                                total_errors += 1

                elif content_path.is_file():
                    # Handle single-file torrents
                    try:
                        category_dir = torrent.category or ''
                        target_file_path = target_path / category_dir / content_path.name

                        # Security: Check for path traversal
                        if not _is_safe_path(target_path, target_file_path):
                            logging.error(f"Security: Path traversal detected, skipping: {target_file_path}")
                            total_errors += 1
                            continue

                        if target_file_path.exists():
                            logging.debug(f"Hard link already exists: {target_file_path}")
                            total_skipped += 1
                        elif dry_run:
                            logging.info(f"[Dry Run] Would create hard link: {content_path} -> {target_file_path}")
                            total_links += 1
                        else:
                            # Create parent directories (cached to avoid redundant mkdir calls)
                            parent_dir = target_file_path.parent
                            if parent_dir not in created_dirs:
                                parent_dir.mkdir(parents=True, exist_ok=True)
                                created_dirs.add(parent_dir)

                            # Create hard link
                            os.link(content_path, target_file_path)
                            logging.info(f"Hard link created: {content_path} -> {target_file_path}")
                            total_links += 1

                    except OSError as e:
                        logging.error(f"Failed to create hard link for single file '{content_path}': {e}")
                        total_errors += 1
                    except Exception as e:
                        logging.error(f"Unexpected error processing single file '{content_path}': {e}")
                        total_errors += 1

                else:
                    logging.warning(f"Content path does not exist: {content_path}")
                    total_errors += 1

            except Exception as e:
                logging.error(f"Error processing torrent '{getattr(torrent, 'name', 'unknown')}': {e}")
                total_errors += 1

        # Summary
        if dry_run:
            logging.info(f"[Dry Run] Hard link summary: {total_links} would be created, {total_skipped} already exist, {total_errors} errors")
        else:
            logging.info(f"Hard link summary: {total_links} created, {total_skipped} already exist, {total_errors} errors")

    except Exception as e:
        logging.error(f"Error in create_hard_links: {e}")
        raise
