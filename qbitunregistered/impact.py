"""Impact analysis for dry-run preview.

This module provides functionality to analyze the potential impact of operations
before they are executed, giving users confidence and preventing accidental data loss.
"""

import logging
import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections import defaultdict
from collections.abc import Sequence

from qbitunregistered.types import TorrentInfo, QBittorrentClient

if TYPE_CHECKING:
    from qbitunregistered.operations.create_hardlinks import PlannedHardLink
    from qbitunregistered.operations.orphaned import OrphanFilePlan
    from qbitunregistered.operations.unregistered_checks import UnregisteredDeletionPlan

logger = logging.getLogger(__name__)


class ImpactAnalysisError(RuntimeError):
    """Raised when a complete, reliable impact preview cannot be produced."""


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

    def __init__(self) -> None:
        """Initialize an empty impact summary."""
        self.torrents_to_delete: dict[str, list[str]] = defaultdict(list)
        self.torrents_to_tag: dict[str, list[str]] = defaultdict(list)
        self.torrents_to_pause: list[str] = []
        self.torrents_to_resume: list[str] = []
        self.orphaned_files: list[str] = []
        self.disk_to_free_bytes: int = 0
        self.operation_details: dict[str, Any] = {}
        self.operation_targets: dict[str, list[str]] = defaultdict(list)
        self.hard_link_plan: list["PlannedHardLink"] | None = None
        self.orphan_file_plan: "OrphanFilePlan | None" = None
        self.unregistered_deletion_plan: "UnregisteredDeletionPlan | None" = None

    def add_operation_target(self, operation: str, target: str) -> None:
        """Add a concrete target for an operation."""
        self.operation_targets[operation].append(target)

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

    def get_warning_messages(self) -> list[str]:
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
        if self.operation_targets:
            has_changes = True
            lines.append("\n🎯 Other operation targets:")
            for operation, targets in sorted(self.operation_targets.items()):
                lines.append(f"   - {operation}: {len(targets)}")
                if show_details:
                    for target in targets[:5]:
                        lines.append(f"     {target}")
                    if len(targets) > 5:
                        lines.append(f"     ... and {len(targets) - 5} more")

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
            and not self.operation_targets
        )


def analyze_impact(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    operations: Sequence[str],
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
        "seeding_management": _analyze_seeding_management,
        "auto_tmm": _analyze_auto_tmm,
        "create_hard_links": _analyze_create_hard_links,
        "pause": _analyze_pause,
        "resume": _analyze_resume,
    }

    for operation in operations:
        analyzer = operation_map.get(operation)
        if analyzer is None:
            raise ImpactAnalysisError(f"Unknown operation for impact analysis: {operation}")
        try:
            logger.debug("Analyzing impact for operation: %s", operation)
            analyzer(client, torrents, config, summary)
        except (KeyboardInterrupt, SystemExit, ImpactAnalysisError):
            raise
        except Exception as error:
            raise ImpactAnalysisError(f"Could not analyze operation '{operation}'") from error

    return summary


def _analyze_unregistered(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Analyze the exact tags and deletions selected by unregistered checks."""
    from qbitunregistered.operations.unregistered_checks import (
        DeletionAction,
        build_unregistered_deletion_plan,
        compile_patterns,
        process_torrent,
    )

    exact_patterns, starts_with_patterns = compile_patterns(config.get("unregistered", []))
    default_tag = config.get("default_unregistered_tag", "unregistered")
    cross_seed_tag = config.get("cross_seeding_tag", "unregistered:crossseeding")
    use_delete = config.get("use_delete_tags", False) is True
    delete_tags = config.get("delete_tags", [])
    hashes_by_path: dict[str, list[str]] = defaultdict(list)
    all_hashes_by_path: dict[str, list[str]] = defaultdict(list)
    for torrent in torrents:
        all_hashes_by_path[torrent.save_path].append(torrent.hash)
        if process_torrent(torrent, exact_patterns, starts_with_patterns):
            hashes_by_path[torrent.save_path].append(torrent.hash)

    for save_path, hashes in hashes_by_path.items():
        tag = default_tag if len(hashes) == len(all_hashes_by_path[save_path]) else cross_seed_tag
        for torrent_hash in hashes:
            summary.add_tagging(tag, torrent_hash)

    summary.unregistered_deletion_plan = build_unregistered_deletion_plan(
        client,
        torrents,
        config,
        use_delete,
        delete_tags,
        config.get("delete_files", {}),
        config.get("recycle_bin"),
    )
    action_labels = {
        DeletionAction.TORRENT_ONLY: "delete torrent only (keep files)",
        DeletionAction.PRESERVE_SHARED: "delete torrent only (preserve cross-seeded files)",
        DeletionAction.RECYCLE_FILES: "recycle files, then delete torrent",
        DeletionAction.PERMANENT_DELETE: "permanently delete torrent and files",
    }
    for deletion in summary.unregistered_deletion_plan.deletions:
        size = sum(identity.size for identity in deletion.files) if deletion.action is DeletionAction.PERMANENT_DELETE else 0
        summary.add_deletion(deletion.matching_tag, deletion.torrent_hash, size)
        summary.add_operation_target(action_labels[deletion.action], deletion.torrent_hash)


def _analyze_orphaned(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Scan the filesystem and record each concrete orphan target."""
    from qbitunregistered.operations.orphaned import build_orphan_file_plan, check_files_on_disk

    exclude_dirs = list(config.get("exclude_dirs", []))
    recycle_bin = config.get("recycle_bin")
    if recycle_bin:
        exclude_dirs.append(str(Path(recycle_bin).resolve()))
    orphaned_files = check_files_on_disk(
        client,
        list(torrents),
        exclude_file_patterns=config.get("exclude_files", []),
        exclude_dirs=exclude_dirs,
    )
    summary.orphan_file_plan = build_orphan_file_plan(orphaned_files)
    for identity in summary.orphan_file_plan.files:
        summary.add_orphaned_file(str(identity.path), identity.size)


def _analyze_tag_by_tracker(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record tracker tags and share-limit targets."""
    from qbitunregistered.operations.seeding_management import find_tracker_config

    for torrent in torrents:
        tracker_config = find_tracker_config(client, torrent, config, raise_on_error=True)
        if tracker_config is None:
            continue
        tag = tracker_config.get("tag")
        if tag is None:
            continue
        if isinstance(tag, str) and tag:
            summary.add_tagging(tag, torrent.hash)
        if "seed_time_limit" in tracker_config or "seed_ratio_limit" in tracker_config:
            summary.add_operation_target("share limits", torrent.hash)


def _analyze_tag_by_age(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record the same month-bucket tags used by the age operation."""
    now = datetime.datetime.now()
    for torrent in torrents:
        if not torrent.added_on:
            continue
        created_at = datetime.datetime.fromtimestamp(torrent.added_on)
        months = (now.year - created_at.year) * 12 + now.month - created_at.month
        if now.day < created_at.day:
            months -= 1
        if months < 1:
            continue
        if months >= 6:
            tag = "6_months_plus"
        elif months >= 5:
            tag = ">5_months"
        elif months >= 4:
            tag = ">4_months"
        elif months >= 3:
            tag = ">3_months"
        elif months >= 2:
            tag = ">2_months"
        else:
            tag = ">1_month"
        summary.add_tagging(tag, torrent.hash)


def _analyze_tag_cross_seeding(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record cross-seed tags from complete file-structure data."""
    from qbitunregistered.file_operations import fetch_torrent_files
    from qbitunregistered.operations.tag_cross_seeding import _build_file_structure

    structure_map: dict[frozenset[Any], list[TorrentInfo]] = defaultdict(list)
    for torrent in torrents:
        files = fetch_torrent_files(client, torrent.hash, cache_scope=id(client))
        file_structure = _build_file_structure(files)
        if not file_structure:
            continue
        structure_map[file_structure].append(torrent)
    for grouped_torrents in structure_map.values():
        tag = "cross-seed" if len(grouped_torrents) > 1 else "not-cross-seeding"
        opposite_tag = "not-cross-seeding" if tag == "cross-seed" else "cross-seed"
        for torrent in grouped_torrents:
            current_tags = {current_tag.strip() for current_tag in torrent.tags.split(",") if current_tag.strip()}
            if opposite_tag in current_tags:
                summary.add_operation_target(f"remove tag '{opposite_tag}'", torrent.hash)
            summary.add_tagging(tag, torrent.hash)


def _analyze_auto_remove(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record every completed torrent auto-remove will delete."""
    for torrent in torrents:
        if torrent.state_enum.is_complete:
            summary.add_deletion("completed", torrent.hash)


def _analyze_seeding_management(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record torrents with configured tracker share limits."""
    from qbitunregistered.operations.seeding_management import find_tracker_config

    for torrent in torrents:
        tracker_config = find_tracker_config(client, torrent, config, raise_on_error=True)
        if tracker_config and ("seed_time_limit" in tracker_config or "seed_ratio_limit" in tracker_config):
            summary.add_operation_target("seeding management", torrent.hash)


def _analyze_auto_tmm(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record every torrent whose auto-management flag will be enabled."""
    for torrent in torrents:
        summary.add_operation_target("auto TMM", torrent.hash)


def _analyze_create_hard_links(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record concrete destination paths from the hard-link planner."""
    from qbitunregistered.operations.create_hardlinks import plan_hard_links

    planned_links = plan_hard_links(config.get("target_dir", ""), torrents)
    summary.hard_link_plan = planned_links
    for planned_link in planned_links:
        summary.add_operation_target("create hard links", str(planned_link.target))


def _analyze_pause(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record every torrent passed to the batched pause call."""
    for torrent in torrents:
        summary.add_pause(torrent.hash)


def _analyze_resume(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    summary: ImpactSummary,
) -> None:
    """Record every torrent passed to the batched resume call."""
    for torrent in torrents:
        summary.add_resume(torrent.hash)
