import os
import logging
import re
from typing import Sequence
from pathlib import Path
from tqdm import tqdm
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.types import TorrentInfo


def _sanitize_category_name(category: str) -> str:
    """
    Sanitize category name to prevent path traversal attacks.

    Uses a whitelist approach: only allows alphanumeric characters, hyphens,
    underscores, spaces, and periods (but not '..'). This is more secure than
    trying to filter out dangerous patterns.

    Security approach:
    - Whitelist safe characters: alphanumeric, spaces, hyphens, underscores, periods
    - Explicitly reject '..' patterns
    - Replace unsafe characters with underscores
    - Ensures non-empty result

    Args:
        category: Raw category name from torrent

    Returns:
        Sanitized category name safe for use in paths
    """
    if not category:
        return ''

    # Remove leading/trailing whitespace first
    sanitized = category.strip()

    # Security: Explicitly reject '..' patterns (path traversal attempt)
    if '..' in sanitized:
        logging.warning(f"Path traversal pattern detected in category '{category}', replacing with 'uncategorized'")
        return 'uncategorized'

    # Whitelist approach: Allow only safe characters
    # - Alphanumeric (any Unicode script)
    # - Spaces, hyphens, underscores
    # - Single periods (but not '..')
    # Replace unsafe characters with underscores
    sanitized = re.sub(r'[^\w\s\-.]', '_', sanitized, flags=re.UNICODE)

    # Replace multiple consecutive underscores/spaces with single underscore
    sanitized = re.sub(r'[_\s]+', '_', sanitized)

    # Remove leading/trailing underscores and periods
    sanitized = sanitized.strip('_.')

    # Ensure result is non-empty after sanitization (use 'uncategorized' as fallback)
    if not sanitized:
        logging.warning(f"Category name '{category}' sanitized to empty string, using 'uncategorized'")
        sanitized = 'uncategorized'

    return sanitized


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
    except (ValueError, RuntimeError) as e:
        logging.warning(f"Path validation failed for base='{base_path}', target='{target_path}': {e}")
        return False


def create_hard_links(target_dir: str, torrents: Sequence[TorrentInfo], dry_run: bool = False) -> None:
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

        # Note: Hard links don't consume additional disk space - they're directory entries
        # pointing to existing inodes. No disk space check needed.

        # Pre-flight check: verify filesystem compatibility for hard links across all unique source devices
        # Track each unique device ID to warn about cross-filesystem issues upfront
        if not dry_run and completed_torrents:
            try:
                target_stat = os.stat(target_path)
                target_device = target_stat.st_dev
                checked_devices = {}  # Maps device ID to example save path for reporting
                incompatible_devices = []

                # Collect unique source device IDs from all torrents
                for torrent in completed_torrents:
                    try:
                        source_stat = os.stat(torrent.save_path)
                        source_device = source_stat.st_dev

                        # Track this device if not seen before
                        if source_device not in checked_devices:
                            checked_devices[source_device] = torrent.save_path

                            # Check compatibility with target
                            if source_device != target_device:
                                incompatible_devices.append((torrent.save_path, source_device))
                    except Exception as e:
                        logging.debug(f"Could not check filesystem for {torrent.save_path}: {e}")

                # Report findings
                if incompatible_devices:
                    logging.warning(f"WARNING: Found {len(incompatible_devices)} source filesystem(s) incompatible with target:")
                    for save_path, device_id in incompatible_devices:
                        logging.warning(f"  - {save_path} (device {device_id})")
                    logging.warning(f"  Target: {target_path} (device {target_device})")
                    logging.warning("  Hard links only work within the same filesystem.")
                    logging.warning("  Operations on incompatible paths will fail. Consider using symlinks or copying instead.")
                elif checked_devices:
                    logging.debug(f"Filesystem check passed: {len(checked_devices)} unique source device(s) compatible with target")
            except Exception as e:
                logging.debug(f"Could not verify filesystem compatibility: {e}")

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

                                # Security: Sanitize category name to prevent path traversal
                                category_dir = _sanitize_category_name(torrent.category or '')

                                # Construct target path and resolve to absolute path
                                target_file_path = (target_path / category_dir / rel_path).resolve()

                                # Security: Check for path traversal after resolution
                                if not _is_safe_path(target_path, target_file_path):
                                    logging.error(f"Security: Path traversal detected for torrent '{torrent.name}', category '{torrent.category}', skipping: {target_file_path}")
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
                                    try:
                                        os.link(source_path, target_file_path)
                                        logging.debug(f"Hard link created: {source_path} -> {target_file_path}")
                                        total_links += 1
                                    except FileExistsError:
                                        # TOCTOU: File created between existence check and link creation
                                        logging.debug(f"Hard link already exists (race condition): {target_file_path}")
                                        total_skipped += 1

                            except OSError as e:
                                if e.errno == 18:  # EXDEV - Cross-device link
                                    logging.error(f"Cannot create hard link: source and target are on different filesystems.\n"
                                                  f"  Source: {source_path}\n"
                                                  f"  Target: {target_file_path}\n"
                                                  f"  Hard links only work within the same filesystem.")
                                else:
                                    logging.exception(f"Failed to create hard link for '{source_path}'")
                                total_errors += 1
                            except Exception:
                                logging.exception(f"Unexpected error processing file '{source_path}'")
                                total_errors += 1

                elif content_path.is_file():
                    # Handle single-file torrents
                    try:
                        # Security: Sanitize category name to prevent path traversal
                        category_dir = _sanitize_category_name(torrent.category or '')

                        # Construct target path and resolve to absolute path
                        target_file_path = (target_path / category_dir / content_path.name).resolve()

                        # Security: Check for path traversal after resolution
                        if not _is_safe_path(target_path, target_file_path):
                            logging.error(f"Security: Path traversal detected for torrent '{torrent.name}', category '{torrent.category}', skipping: {target_file_path}")
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
                            try:
                                os.link(content_path, target_file_path)
                                logging.debug(f"Hard link created: {content_path} -> {target_file_path}")
                                total_links += 1
                            except FileExistsError:
                                # TOCTOU: File created between existence check and link creation
                                logging.debug(f"Hard link already exists (race condition): {target_file_path}")
                                total_skipped += 1

                    except OSError as e:
                        if e.errno == 18:  # EXDEV - Cross-device link
                            logging.error(f"Cannot create hard link: source and target are on different filesystems.\n"
                                          f"  Source: {content_path}\n"
                                          f"  Target: {target_file_path}\n"
                                          f"  Hard links only work within the same filesystem.")
                        else:
                            logging.exception(f"Failed to create hard link for single file '{content_path}'")
                        total_errors += 1
                    except Exception:
                        logging.exception(f"Unexpected error processing single file '{content_path}'")
                        total_errors += 1

                else:
                    logging.warning(f"Content path does not exist: {content_path}")
                    total_errors += 1

            except Exception:
                logging.exception(f"Error processing torrent '{getattr(torrent, 'name', 'unknown')}'")
                total_errors += 1

        # Summary
        if dry_run:
            logging.info(f"[Dry Run] Hard link summary: {total_links} would be created, {total_skipped} already exist, {total_errors} errors")
        else:
            logging.info(f"Hard link summary: {total_links} created, {total_skipped} already exist, {total_errors} errors")

    except Exception:
        logging.exception("Error in create_hard_links")
        raise
