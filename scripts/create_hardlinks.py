import os
import logging
from typing import List, Any
from pathlib import Path


def create_hard_links(target_dir: str, torrents: List[Any], dry_run: bool = False) -> None:
    """
    Create hard links for completed torrents in the target directory.

    Args:
        target_dir: Target directory where hard links will be created
        torrents: List of torrent objects to process
        dry_run: If True, only log actions without creating links

    Note: Hard links only work within the same filesystem. Cross-filesystem
          linking will fail.
    """
    try:
        if not target_dir:
            logging.error("No target directory specified for hard link creation")
            return

        target_path = Path(target_dir)

        # Validate target directory
        if not dry_run and not target_path.exists():
            logging.error(f"Target directory does not exist: {target_dir}")
            return

        completed_torrents = [t for t in torrents if t.state_enum.is_complete]
        logging.info(f"Processing {len(completed_torrents)} completed torrents out of {len(torrents)} total")

        total_links = 0
        total_skipped = 0
        total_errors = 0

        for torrent in completed_torrents:
            try:
                content_path = Path(torrent.save_path) / torrent.name

                # Handle both directories and single files
                if content_path.is_dir():
                    # Process directory torrents
                    for root, dirs, files in os.walk(content_path):
                        for file in files:
                            try:
                                source_path = Path(root) / file

                                # Preserve relative directory structure
                                rel_path = source_path.relative_to(content_path)
                                category_dir = torrent.category or ''
                                target_file_path = target_path / category_dir / rel_path

                                if target_file_path.exists():
                                    logging.debug(f"Hard link already exists: {target_file_path}")
                                    total_skipped += 1
                                    continue

                                if dry_run:
                                    logging.info(f"[Dry Run] Would create hard link: {source_path} -> {target_file_path}")
                                    total_links += 1
                                else:
                                    # Create parent directories
                                    target_file_path.parent.mkdir(parents=True, exist_ok=True)

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

                        if target_file_path.exists():
                            logging.debug(f"Hard link already exists: {target_file_path}")
                            total_skipped += 1
                        elif dry_run:
                            logging.info(f"[Dry Run] Would create hard link: {content_path} -> {target_file_path}")
                            total_links += 1
                        else:
                            # Create parent directories
                            target_file_path.parent.mkdir(parents=True, exist_ok=True)

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
