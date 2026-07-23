import os
import logging
import re
import stat
from dataclasses import dataclass
from typing import Sequence
from pathlib import Path
from tqdm import tqdm

from qbitunregistered.types import TorrentInfo


class HardLinkPlanningError(RuntimeError):
    """Raised when hard-link targets cannot be derived safely."""


@dataclass(frozen=True)
class PlannedHardLink:
    """A read-only source-to-destination hard-link operation."""

    source: Path
    target: Path
    source_device: int
    source_inode: int
    source_size: int
    source_mtime_ns: int


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
        return ""

    # Remove leading/trailing whitespace first
    sanitized = category.strip()

    # Security: Explicitly reject '..' patterns (path traversal attempt)
    if ".." in sanitized:
        logging.warning(f"Path traversal pattern detected in category '{category}', replacing with 'uncategorized'")
        return "uncategorized"

    # Whitelist approach: Allow only safe characters
    # - Alphanumeric (any Unicode script)
    # - Spaces, hyphens, underscores
    # - Single periods (but not '..')
    # Replace unsafe characters with underscores
    sanitized = re.sub(r"[^\w\s\-.]", "_", sanitized, flags=re.UNICODE)

    # Replace multiple consecutive underscores/spaces with single underscore
    sanitized = re.sub(r"[_\s]+", "_", sanitized)

    # Remove leading/trailing underscores and periods
    sanitized = sanitized.strip("_.")

    # Ensure result is non-empty after sanitization (use 'uncategorized' as fallback)
    if not sanitized:
        logging.warning(f"Category name '{category}' sanitized to empty string, using 'uncategorized'")
        sanitized = "uncategorized"

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


def _strict_path_exists(path: Path) -> bool:
    """Check path existence without treating access failures as absence."""
    try:
        path.stat()
    except FileNotFoundError:
        return False
    return True


def plan_hard_links(target_dir: str, torrents: Sequence[TorrentInfo]) -> list[PlannedHardLink]:
    """Build the exact hard-link plan without mutating the filesystem.

    Existing destinations are omitted, matching execution behavior. Any
    inaccessible, missing, or unsafe source fails the entire plan closed.

    Args:
        target_dir: Existing destination directory for hard links.
        torrents: Torrent snapshot used by execution.

    Returns:
        Ordered source and destination pairs to create.

    Raises:
        HardLinkPlanningError: If paths cannot be inspected safely.
    """
    if not target_dir:
        raise HardLinkPlanningError("No target directory specified for hard link creation")

    try:
        target_path = Path(target_dir).resolve()
        if not target_path.is_dir():
            raise HardLinkPlanningError(f"Target directory does not exist or is not a directory: {target_path}")
        target_path.stat()

        planned_links: list[PlannedHardLink] = []
        planned_sources_by_target: dict[Path, Path] = {}
        for torrent in torrents:
            if not torrent.state_enum.is_complete:
                continue

            save_path = Path(torrent.save_path).resolve()
            content_path = (save_path / torrent.name).resolve()
            if not content_path.is_relative_to(save_path):
                raise HardLinkPlanningError(f"Unsafe source path for torrent '{torrent.name}'")

            category_dir = _sanitize_category_name(torrent.category or "")
            sources: list[tuple[Path, Path]] = []
            if content_path.is_dir():

                def raise_walk_error(error: OSError) -> None:
                    raise error

                for root, _directories, files in os.walk(content_path, onerror=raise_walk_error):
                    for filename in files:
                        source_path = (Path(root) / filename).resolve()
                        if not source_path.is_relative_to(content_path):
                            raise HardLinkPlanningError(
                                f"Source path escapes torrent content for torrent '{torrent.name}': {source_path}"
                            )
                        source_stat = source_path.lstat()
                        if not stat.S_ISREG(source_stat.st_mode):
                            raise HardLinkPlanningError(f"Source is not a regular file: {source_path}")
                        sources.append((source_path, source_path.relative_to(content_path)))
            elif content_path.is_file():
                source_stat = content_path.lstat()
                if not stat.S_ISREG(source_stat.st_mode):
                    raise HardLinkPlanningError(f"Source is not a regular file: {content_path}")
                sources.append((content_path, Path(content_path.name)))
            else:
                raise HardLinkPlanningError(f"Content path does not exist: {content_path}")

            for source_path, relative_path in sources:
                target_file_path = (target_path / category_dir / relative_path).resolve()
                if not _is_safe_path(target_path, target_file_path):
                    raise HardLinkPlanningError(f"Unsafe destination path for torrent '{torrent.name}': {target_file_path}")
                if not _strict_path_exists(target_file_path):
                    previous_source = planned_sources_by_target.get(target_file_path)
                    if previous_source is not None:
                        if previous_source != source_path:
                            raise HardLinkPlanningError(
                                "Multiple sources map to the same hard-link destination: " f"{target_file_path}"
                            )
                        continue
                    planned_sources_by_target[target_file_path] = source_path
                    source_stat = source_path.lstat()
                    planned_links.append(
                        PlannedHardLink(
                            source_path,
                            target_file_path,
                            source_stat.st_dev,
                            source_stat.st_ino,
                            source_stat.st_size,
                            source_stat.st_mtime_ns,
                        )
                    )

        return planned_links
    except (KeyboardInterrupt, SystemExit, HardLinkPlanningError):
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise HardLinkPlanningError("Could not build a complete hard-link plan") from error


def create_hard_links(
    target_dir: str,
    torrents: Sequence[TorrentInfo],
    dry_run: bool = False,
    *,
    planned_links: Sequence[PlannedHardLink] | None = None,
) -> None:
    """
    Create hard links for completed torrents in the target directory.

    Includes security checks to prevent path traversal attacks.

    Args:
        target_dir: Target directory where hard links will be created
        torrents: List of torrent objects to process
        dry_run: If True, only log actions without creating links
        planned_links: Confirmed read-only plan to execute without rescanning.

    Note: Hard links only work within the same filesystem. Cross-filesystem
          linking will fail.

    Security: Validates all paths to prevent directory traversal attacks.
    """
    if planned_links is None:
        planned_links = plan_hard_links(target_dir, torrents)
    logging.info("Planned %d hard links from %d torrents", len(planned_links), len(torrents))
    total_links = 0
    total_errors = 0
    created_dirs: set[Path] = set()
    target_base = Path(target_dir).resolve()

    for planned_link in tqdm(planned_links, desc="Creating hard links", unit="link"):
        if dry_run:
            logging.info("[Dry Run] Would create hard link: %s -> %s", planned_link.source, planned_link.target)
            total_links += 1
            continue

        try:
            source_stat = planned_link.source.lstat()
            if (
                not stat.S_ISREG(source_stat.st_mode)
                or source_stat.st_dev != planned_link.source_device
                or source_stat.st_ino != planned_link.source_inode
                or source_stat.st_size != planned_link.source_size
                or source_stat.st_mtime_ns != planned_link.source_mtime_ns
            ):
                raise HardLinkPlanningError(f"Source changed after planning: {planned_link.source}")

            parent_dir = planned_link.target.parent
            if parent_dir not in created_dirs:
                parent_dir.mkdir(parents=True, exist_ok=True)
                created_dirs.add(parent_dir)
            resolved_target = planned_link.target.resolve()
            if resolved_target != planned_link.target or not resolved_target.is_relative_to(target_base):
                raise HardLinkPlanningError(f"Target changed after planning: {planned_link.target}")

            os.link(planned_link.source, planned_link.target, follow_symlinks=False)
            target_stat = planned_link.target.lstat()
            if (
                target_stat.st_dev != planned_link.source_device
                or target_stat.st_ino != planned_link.source_inode
                or not stat.S_ISREG(target_stat.st_mode)
            ):
                _remove_target_if_source_still_matches(planned_link.source, planned_link.target, target_stat)
                raise HardLinkPlanningError(f"Source changed while creating hard link: {planned_link.source}")
            total_links += 1
        except (OSError, HardLinkPlanningError):
            logging.exception("Failed to create hard link: %s -> %s", planned_link.source, planned_link.target)
            total_errors += 1

    if dry_run:
        logging.info("[Dry Run] Hard link summary: %d would be created, %d errors", total_links, total_errors)
    else:
        logging.info("Hard link summary: %d created, %d errors", total_links, total_errors)
    if total_errors:
        raise HardLinkPlanningError(f"Failed to create {total_errors} of {len(planned_links)} planned hard links")


def _remove_target_if_source_still_matches(source: Path, target: Path, target_stat: os.stat_result) -> None:
    """Remove a mismatched target only while the linked source is still present."""
    try:
        source_stat = source.lstat()
        current_target_stat = target.lstat()
    except FileNotFoundError:
        return
    target_identity = (target_stat.st_dev, target_stat.st_ino)
    if (source_stat.st_dev, source_stat.st_ino) == target_identity and (
        current_target_stat.st_dev,
        current_target_stat.st_ino,
    ) == target_identity:
        target.unlink()
