"""Impact analysis for dry-run preview.

This module provides functionality to analyze the potential impact of operations
before they are executed, giving users confidence and preventing accidental data loss.
"""

import logging
from typing import Dict, List, Any
from collections import defaultdict

from utils.types import TorrentInfo, QBittorrentClient

logger = logging.getLogger(__name__)


class ImpactSummary:
    """Stores and formats impact analysis results.

    This class accumulates impact data from various operations and provides
    human-readable summary formatting with warnings for large-scale changes.

    Attributes:
        torrents_to_delete: Mapping of tags to lists of torrent hashes to delete
        torrents_to_tag: Mapping of tags to lists of torrent hashes to tag
        torrents_to_pause: List of torrent hashes to pause
        torrents_to_resume: List of torrent hashes to resume
        orphaned_files: List of orphaned file paths to delete
        disk_to_free_bytes: Total bytes that will be freed
        operation_details: Additional details per operation
    """

    def __init__(self):
        """Initialize an empty impact summary."""
        self.torrents_to_delete: Dict[str, List[str]] = defaultdict(list)
        self.torrents_to_tag: Dict[str, List[str]] = defaultdict(list)
        self.torrents_to_pause: List[str] = []
        self.torrents_to_resume: List[str] = []
        self.orphaned_files: List[str] = []
        self.disk_to_free_bytes: int = 0
        self.operation_details: Dict[str, Any] = {}

    def add_deletion(self, tag: str, torrent_hash: str, size_bytes: int = 0) -> None:
        """Add a torrent deletion to the impact summary.

        Args:
            tag: Tag associated with the deletion reason
            torrent_hash: Hash of the torrent to delete
            size_bytes: Size of the torrent in bytes
        """
        self.torrents_to_delete[tag].append(torrent_hash)
        self.disk_to_free_bytes += size_bytes

    def add_tagging(self, tag: str, torrent_hash: str) -> None:
        """Add a torrent tagging operation to the impact summary.

        Args:
            tag: Tag to be applied
            torrent_hash: Hash of the torrent to tag
        """
        self.torrents_to_tag[tag].append(torrent_hash)

    def add_orphaned_file(self, file_path: str, size_bytes: int = 0) -> None:
        """Add an orphaned file to the impact summary.

        Args:
            file_path: Path to the orphaned file
            size_bytes: Size of the file in bytes
        """
        self.orphaned_files.append(file_path)
        self.disk_to_free_bytes += size_bytes

    def add_pause(self, torrent_hash: str) -> None:
        """Add a torrent pause operation.

        Args:
            torrent_hash: Hash of the torrent to pause
        """
        self.torrents_to_pause.append(torrent_hash)

    def add_resume(self, torrent_hash: str) -> None:
        """Add a torrent resume operation.

        Args:
            torrent_hash: Hash of the torrent to resume
        """
        self.torrents_to_resume.append(torrent_hash)

    def set_operation_detail(self, operation: str, detail_key: str, detail_value: Any) -> None:
        """Set additional details for an operation.

        Args:
            operation: Name of the operation
            detail_key: Key for the detail
            detail_value: Value of the detail
        """
        if operation not in self.operation_details:
            self.operation_details[operation] = {}
        self.operation_details[operation][detail_key] = detail_value

    def get_total_torrents_affected(self) -> int:
        """Calculate total number of unique torrents affected.

        Returns:
            Count of unique torrents that will be modified
        """
        affected = set()

        # Add torrents to be deleted
        for hashes in self.torrents_to_delete.values():
            affected.update(hashes)

        # Add torrents to be tagged
        for hashes in self.torrents_to_tag.values():
            affected.update(hashes)

        # Add torrents to be paused/resumed
        affected.update(self.torrents_to_pause)
        affected.update(self.torrents_to_resume)

        return len(affected)

    def get_warning_messages(self) -> List[str]:
        """Generate warning messages for potentially dangerous operations.

        Returns:
            List of warning messages
        """
        warnings = []

        # Warn about large disk space deletions
        gb_to_free = self.disk_to_free_bytes / (1024**3)
        if gb_to_free > 50:
            warnings.append(f"WARNING: {gb_to_free:.2f} GB will be freed. " "Verify this is expected!")

        # Warn about large number of deletions
        total_deletions = sum(len(hashes) for hashes in self.torrents_to_delete.values())
        if total_deletions > 20:
            warnings.append(f"WARNING: {total_deletions} torrents will be deleted. " "This is a large operation!")

        # Warn about large number of orphaned files
        if len(self.orphaned_files) > 50:
            warnings.append(
                f"WARNING: {len(self.orphaned_files)} orphaned files will be deleted. " "Verify these are not needed!"
            )

        return warnings

    def format_summary(self, show_details: bool = False) -> str:
        """Generate human-readable summary of the impact.

        Args:
            show_details: If True, include detailed lists of affected items

        Returns:
            Formatted summary string
        """
        lines = ["\n" + "=" * 70]
        lines.append("DRY-RUN IMPACT PREVIEW")
        lines.append("=" * 70)

        has_changes = False

        # Torrents to delete
        if self.torrents_to_delete:
            has_changes = True
            total = sum(len(v) for v in self.torrents_to_delete.values())
            lines.append(f"\n📛 Torrents to DELETE: {total}")
            for tag, hashes in sorted(self.torrents_to_delete.items()):
                lines.append(f"   - Tag '{tag}': {len(hashes)} torrents")
                if show_details and hashes:
                    # Show first few
                    preview = hashes[:3]
                    lines.append(f"     Hashes: {', '.join(preview)}")
                    if len(hashes) > 3:
                        lines.append(f"     ... and {len(hashes) - 3} more")

        # Torrents to tag
        if self.torrents_to_tag:
            has_changes = True
            total = sum(len(v) for v in self.torrents_to_tag.values())
            lines.append(f"\n🏷️  Torrents to TAG: {total}")
            for tag, hashes in sorted(self.torrents_to_tag.items()):
                lines.append(f"   - Tag '{tag}': {len(hashes)} torrents")

        # Torrents to pause
        if self.torrents_to_pause:
            has_changes = True
            lines.append(f"\n⏸️  Torrents to PAUSE: {len(self.torrents_to_pause)}")

        # Torrents to resume
        if self.torrents_to_resume:
            has_changes = True
            lines.append(f"\n▶️  Torrents to RESUME: {len(self.torrents_to_resume)}")

        # Orphaned files
        if self.orphaned_files:
            has_changes = True
            lines.append(f"\n🗑️  Orphaned files to DELETE: {len(self.orphaned_files)}")
            if show_details:
                preview = self.orphaned_files[:5]
                for file_path in preview:
                    lines.append(f"   - {file_path}")
                if len(self.orphaned_files) > 5:
                    lines.append(f"   ... and {len(self.orphaned_files) - 5} more")

        # Disk space
        if self.disk_to_free_bytes > 0:
            gb = self.disk_to_free_bytes / (1024**3)
            mb = self.disk_to_free_bytes / (1024**2)
            if gb >= 1:
                lines.append(f"\n💾 Disk space to free: {gb:.2f} GB")
            else:
                lines.append(f"\n💾 Disk space to free: {mb:.2f} MB")

        # Operation-specific details
        if self.operation_details:
            lines.append("\n📊 Operation Details:")
            for operation, details in sorted(self.operation_details.items()):
                lines.append(f"   {operation}:")
                for key, value in sorted(details.items()):
                    lines.append(f"      {key}: {value}")

        # If no changes
        if not has_changes:
            lines.append("\n✅ No changes will be made")

        # Warnings
        warnings = self.get_warning_messages()
        if warnings:
            lines.append("\n" + "!" * 70)
            for warning in warnings:
                lines.append(f"⚠️  {warning}")
            lines.append("!" * 70)

        lines.append("=" * 70 + "\n")
        return "\n".join(lines)

    def is_empty(self) -> bool:
        """Check if there are any impacts.

        Returns:
            True if no operations will be performed
        """
        return (
            not self.torrents_to_delete
            and not self.torrents_to_tag
            and not self.torrents_to_pause
            and not self.torrents_to_resume
            and not self.orphaned_files
        )


def analyze_impact(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    operations: List[str],
) -> ImpactSummary:
    """Analyze the potential impact of specified operations.

    This function simulates the execution of operations in dry-run mode
    and collects impact data without making any actual changes.

    Args:
        client: qBittorrent API client instance
        torrents: List of all torrents
        config: Configuration dictionary
        operations: List of operation names to analyze
            (e.g., ["orphaned", "unregistered", "tag_by_tracker"])

    Returns:
        ImpactSummary object containing all potential impacts

    Example:
        >>> summary = analyze_impact(client, torrents, config, ["unregistered"])
        >>> print(summary.format_summary())
    """
    summary = ImpactSummary()

    # Import operation modules as needed
    operation_map = {
        "unregistered": _analyze_unregistered,
        "orphaned": _analyze_orphaned,
        "tag_by_tracker": _analyze_tag_by_tracker,
        "tag_by_age": _analyze_tag_by_age,
        "tag_cross_seeding": _analyze_tag_cross_seeding,
        "auto_remove": _analyze_auto_remove,
        "pause": _analyze_pause,
        "resume": _analyze_resume,
    }

    for operation in operations:
        if operation in operation_map:
            try:
                logger.debug(f"Analyzing impact for operation: {operation}")
                operation_map[operation](client, torrents, config, summary)
            except Exception as e:
                logger.error(f"Error analyzing {operation}: {e}", exc_info=True)
                summary.set_operation_detail(operation, "error", str(e))
        else:
            logger.warning(f"Unknown operation for impact analysis: {operation}")

    return summary


def _analyze_unregistered(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of unregistered checks operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    from scripts.unregistered_checks import (
        compile_patterns,
        check_unregistered_message,
    )

    def _fetch_trackers(cli, torrent_hash):
        """Fetch trackers for a torrent."""
        try:
            return cli.torrents_trackers(torrent_hash)
        except Exception as e:
            logger.debug(f"Error fetching trackers for {torrent_hash}: {e}")
            return []

    exact_patterns, starts_with_patterns = compile_patterns(config.get("unregistered", []))
    default_tag = config.get("default_unregistered_tag", "unregistered")
    cross_seed_tag = config.get("cross_seeding_tag", "unregistered:crossseeding")
    use_delete = config.get("use_delete_tags", False)
    delete_tags = set(config.get("delete_tags", []))

    unregistered_count = 0
    cross_seed_count = 0

    for torrent in torrents:
        try:
            trackers = _fetch_trackers(client, torrent.hash)
            unregistered_trackers = sum(
                1 for t in trackers if check_unregistered_message(t, exact_patterns, starts_with_patterns)
            )

            if unregistered_trackers > 0:
                # Determine tag
                total_trackers = len([t for t in trackers if t.get("url", "").startswith("http")])
                if total_trackers > 1 and unregistered_trackers < total_trackers:
                    tag = cross_seed_tag
                    cross_seed_count += 1
                else:
                    tag = default_tag
                    unregistered_count += 1

                # Add tagging impact
                summary.add_tagging(tag, torrent.hash)

                # Check if deletion configured
                if use_delete and tag in delete_tags:
                    # Calculate size
                    size = 0
                    try:
                        torrent_info = client.torrents_info(torrent_hashes=torrent.hash)[0]
                        size = torrent_info.get("size", 0)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception as e:
                        logger.debug(f"Error fetching size for torrent {torrent.hash}: {e}")

                    summary.add_deletion(tag, torrent.hash, size)

        except Exception as e:
            logger.debug(f"Error analyzing torrent {torrent.hash}: {e}")

    summary.set_operation_detail("unregistered_checks", "unregistered_found", unregistered_count)
    summary.set_operation_detail("unregistered_checks", "cross_seeding_found", cross_seed_count)


def _analyze_orphaned(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of orphaned files operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    from pathlib import Path

    # This is a simplified version - full implementation would need to scan disk
    # For now, we'll note that orphaned check requires actual disk scanning
    summary.set_operation_detail(
        "orphaned_files",
        "note",
        "Orphaned file detection requires disk scan - run with --orphaned to see results",
    )


def _analyze_tag_by_tracker(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of tag by tracker operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    from utils.tracker_matcher import find_tracker_domain

    def _fetch_trackers(cli, torrent_hash):
        """Fetch trackers for a torrent."""
        try:
            return cli.torrents_trackers(torrent_hash)
        except Exception as e:
            logger.debug(f"Error fetching trackers for {torrent_hash}: {e}")
            return []

    tracker_tags = config.get("tracker_tags", {})
    if not tracker_tags:
        return

    for torrent in torrents:
        try:
            trackers = _fetch_trackers(client, torrent.hash)
            for tracker in trackers:
                url = tracker.get("url", "")
                if not url or not url.startswith("http"):
                    continue

                domain = find_tracker_domain(url)
                if domain in tracker_tags:
                    tag = tracker_tags[domain].get("tag")
                    if tag:
                        summary.add_tagging(tag, torrent.hash)
                        break

        except Exception as e:
            logger.debug(f"Error analyzing torrent {torrent.hash}: {e}")


def _analyze_tag_by_age(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of tag by age operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    # Simplified - would need full age calculation logic
    summary.set_operation_detail("tag_by_age", "note", "Age-based tagging will be applied based on completion time")


def _analyze_tag_cross_seeding(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of cross-seeding detection.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    summary.set_operation_detail("tag_cross_seeding", "note", "Cross-seeding detection will identify duplicates")


def _analyze_auto_remove(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of auto-remove operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    # Would need to implement actual removal logic
    summary.set_operation_detail("auto_remove", "note", "Auto-removal will delete completed torrents matching criteria")


def _analyze_pause(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of pause operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    for torrent in torrents:
        if hasattr(torrent.state_enum, "is_paused") and not torrent.state_enum.is_paused:
            summary.add_pause(torrent.hash)


def _analyze_resume(
    client: QBittorrentClient,
    torrents: List[TorrentInfo],
    config: Dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze impact of resume operation.

    Args:
        client: qBittorrent API client
        torrents: List of torrents
        config: Configuration dict
        summary: ImpactSummary to update
    """
    for torrent in torrents:
        if hasattr(torrent.state_enum, "is_paused") and torrent.state_enum.is_paused:
            summary.add_resume(torrent.hash)
