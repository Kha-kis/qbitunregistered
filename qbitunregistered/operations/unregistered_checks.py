import logging
from collections import defaultdict
from pathlib import Path
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any
from tqdm import tqdm

from qbitunregistered.file_operations import (
    FileIdentity,
    RecycleBinMove,
    SafetyCheckError,
    capture_file_identity,
    fetch_torrent_files,
    move_files_to_recycle_bin,
    rollback_recycle_bin_moves,
    verify_file_identity,
)
from qbitunregistered.types import QBittorrentClient, TorrentInfo


class DeletionAction(str, Enum):
    """Filesystem behavior for one planned torrent deletion."""

    TORRENT_ONLY = "torrent_only"
    PRESERVE_SHARED = "preserve_shared"
    RECYCLE_FILES = "recycle_files"
    PERMANENT_DELETE = "permanent_delete"


@dataclass(frozen=True, slots=True)
class TorrentOwnership:
    """Ownership-relevant qBittorrent metadata for one torrent."""

    torrent_hash: str
    name: str
    save_path: Path
    category: str
    file_paths: tuple[Path, ...]
    source_paths: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class OwnershipSnapshot:
    """Canonical qBittorrent ownership state used by a deletion plan."""

    torrents: tuple[TorrentOwnership, ...]


@dataclass(frozen=True, slots=True)
class PlannedTorrentDeletion:
    """One confirmed torrent deletion and its exact file behavior."""

    torrent_hash: str
    torrent_name: str
    matching_tag: str
    category: str
    action: DeletionAction
    files: tuple[FileIdentity, ...] = ()
    shared_with: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnregisteredDeletionPlan:
    """Read-only deletion plan shared by impact preview and execution."""

    deletions: tuple[PlannedTorrentDeletion, ...]
    ownership_snapshot: OwnershipSnapshot | None


def _torrent_file_name(file_info: Any, torrent_hash: str) -> str:
    """Return one validated relative file name from qBittorrent metadata."""
    name = file_info.get("name") if isinstance(file_info, dict) else getattr(file_info, "name", None)
    if not isinstance(name, str) or not name:
        raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata")
    return name


def _build_ownership_snapshot(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    *,
    use_cache: bool,
) -> OwnershipSnapshot:
    """Read and canonicalize one complete torrent/file ownership snapshot."""
    ownership: list[TorrentOwnership] = []
    seen_hashes: set[str] = set()
    for torrent in torrents:
        torrent_hash = getattr(torrent, "hash", None)
        name = getattr(torrent, "name", None)
        save_path_value = getattr(torrent, "save_path", None)
        category = getattr(torrent, "category", "")
        if not isinstance(torrent_hash, str) or not torrent_hash or torrent_hash in seen_hashes:
            raise SafetyCheckError("qBittorrent returned a missing or duplicate torrent hash")
        if not isinstance(name, str) or not name:
            raise SafetyCheckError(f"Torrent {torrent_hash} has no valid name")
        if not isinstance(save_path_value, str) or not save_path_value:
            raise SafetyCheckError(f"Torrent {torrent_hash} has no valid save path")
        if not isinstance(category, str):
            raise SafetyCheckError(f"Torrent {torrent_hash} has no valid category")
        seen_hashes.add(torrent_hash)

        try:
            raw_files = (
                fetch_torrent_files(client, torrent_hash, cache_scope=id(client))
                if use_cache
                else client.torrents_files(torrent_hash)
            )
        except (KeyboardInterrupt, SystemExit, SafetyCheckError):
            raise
        except Exception as error:
            raise SafetyCheckError(f"Could not read file metadata for torrent {torrent_hash}") from error
        if raw_files is None:
            raise SafetyCheckError(f"Torrent {torrent_hash} returned no file metadata")
        save_path = Path(save_path_value).resolve()
        file_paths: dict[Path, Path] = {}
        for file_info in raw_files:
            source_path = save_path / _torrent_file_name(file_info, torrent_hash)
            ownership_path = source_path.resolve()
            if not ownership_path.is_relative_to(save_path):
                raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path")
            if ownership_path in file_paths and file_paths[ownership_path] != source_path:
                raise SafetyCheckError(f"Torrent {torrent_hash} returned ambiguous file paths")
            file_paths[ownership_path] = source_path

        sorted_file_paths = tuple(sorted(file_paths))
        ownership.append(
            TorrentOwnership(
                torrent_hash=torrent_hash,
                name=name,
                save_path=save_path,
                category=category,
                file_paths=sorted_file_paths,
                source_paths=tuple(file_paths[path] for path in sorted_file_paths),
            )
        )
    return OwnershipSnapshot(torrents=tuple(sorted(ownership, key=lambda item: item.torrent_hash)))


def _matching_delete_tag(torrent: TorrentInfo, delete_tags: Sequence[str]) -> str | None:
    """Return the first configured delete tag present on a torrent."""
    tags = getattr(torrent, "tags", "")
    if not isinstance(tags, str):
        raise SafetyCheckError(f"Torrent {torrent.hash} has malformed tags")
    torrent_tags = _parse_torrent_tags(tags)
    return next((tag for tag in delete_tags if tag in torrent_tags), None)


def build_unregistered_deletion_plan(
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: Mapping[str, Any],
    use_delete_tags: bool,
    delete_tags: Sequence[str],
    delete_files: Mapping[str, bool],
    recycle_bin: str | None,
) -> UnregisteredDeletionPlan:
    """Build the exact torrent and file actions used by preview and execution."""
    if not use_delete_tags:
        return UnregisteredDeletionPlan(deletions=(), ownership_snapshot=None)

    candidates: list[tuple[TorrentInfo, str, bool]] = []
    allow_file_deletion = config.get("use_delete_files", False) is True
    for torrent in torrents:
        matching_tag = _matching_delete_tag(torrent, delete_tags)
        if matching_tag is None:
            continue
        candidates.append((torrent, matching_tag, allow_file_deletion and delete_files.get(matching_tag, False)))

    if not candidates:
        return UnregisteredDeletionPlan(deletions=(), ownership_snapshot=None)

    needs_ownership = any(delete_requested for _torrent, _tag, delete_requested in candidates)
    snapshot = _build_ownership_snapshot(client, torrents, use_cache=True) if needs_ownership else None
    ownership_by_hash = {item.torrent_hash: item for item in snapshot.torrents} if snapshot is not None else {}
    owners_by_path: dict[Path, set[str]] = defaultdict(set)
    for ownership in ownership_by_hash.values():
        for file_path in ownership.file_paths:
            owners_by_path[file_path].add(ownership.torrent_hash)

    planned: list[PlannedTorrentDeletion] = []
    for torrent, matching_tag, delete_requested in candidates:
        torrent_hash = torrent.hash
        torrent_name = torrent.name
        category = torrent.category if isinstance(torrent.category, str) else ""
        if not delete_requested:
            planned.append(
                PlannedTorrentDeletion(
                    torrent_hash=torrent_hash,
                    torrent_name=torrent_name,
                    matching_tag=matching_tag,
                    category=category,
                    action=DeletionAction.TORRENT_ONLY,
                )
            )
            continue

        ownership = ownership_by_hash[torrent_hash]
        shared_hashes = {
            owner_hash
            for file_path in ownership.file_paths
            for owner_hash in owners_by_path[file_path]
            if owner_hash != torrent_hash
        }
        if shared_hashes:
            shared_names = tuple(sorted(ownership_by_hash[item].name for item in shared_hashes))
            planned.append(
                PlannedTorrentDeletion(
                    torrent_hash=torrent_hash,
                    torrent_name=torrent_name,
                    matching_tag=matching_tag,
                    category=category,
                    action=DeletionAction.PRESERVE_SHARED,
                    shared_with=shared_names,
                )
            )
            continue

        if not ownership.file_paths:
            planned.append(
                PlannedTorrentDeletion(
                    torrent_hash=torrent_hash,
                    torrent_name=torrent_name,
                    matching_tag=matching_tag,
                    category=category,
                    action=DeletionAction.TORRENT_ONLY,
                )
            )
            continue

        identities = tuple(capture_file_identity(path) for path in ownership.source_paths)
        planned.append(
            PlannedTorrentDeletion(
                torrent_hash=torrent_hash,
                torrent_name=torrent_name,
                matching_tag=matching_tag,
                category=category,
                action=DeletionAction.RECYCLE_FILES if recycle_bin else DeletionAction.PERMANENT_DELETE,
                files=identities,
            )
        )

    return UnregisteredDeletionPlan(deletions=tuple(planned), ownership_snapshot=snapshot)


def _revalidate_ownership_snapshot(client: QBittorrentClient, expected: OwnershipSnapshot) -> None:
    """Fail closed unless the final uncached qBittorrent ownership state matches."""
    try:
        current_torrents = client.torrents.info()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise SafetyCheckError("Could not refresh qBittorrent state before deletion") from error
    if current_torrents is None:
        raise SafetyCheckError("qBittorrent returned no torrent list during final deletion validation")
    current = _build_ownership_snapshot(client, current_torrents, use_cache=False)
    if current != expected:
        raise SafetyCheckError("qBittorrent ownership state changed after deletion preview")


def compile_patterns(unregistered: list[str]) -> tuple[set[str], set[str]]:
    """
    Pre-compile patterns into two sets for efficient matching.

    Args:
        unregistered: List of unregistered patterns from config

    Returns:
        Tuple of (exact_match_set, starts_with_set)

    Security:
        Validates that starts_with patterns have non-empty prefixes to prevent
        universal matching (empty prefix would match everything).
    """
    exact_matches = set()
    starts_with_patterns = set()

    for pattern in unregistered:
        lower_pattern = pattern.lower()
        if lower_pattern.startswith("starts_with:"):
            # Extract the prefix after "starts_with:"
            prefix = lower_pattern.split("starts_with:", 1)[1]

            # Security: Normalize and validate prefix to prevent empty matches
            prefix = prefix.strip()

            if not prefix:
                # Empty prefix would match everything - log warning and skip
                logging.warning(
                    f"Skipping malformed pattern '{pattern}': starts_with prefix is empty. "
                    "Empty prefixes would match all messages."
                )
                continue

            starts_with_patterns.add(prefix)
        else:
            exact_matches.add(lower_pattern)

    return exact_matches, starts_with_patterns


def check_unregistered_message(tracker: Any, exact_matches: set[str], starts_with_patterns: set[str]) -> bool:
    """
    Check if tracker message matches any unregistered pattern.

    Args:
        tracker: Tracker object with msg attribute
        exact_matches: Set of exact match patterns (pre-compiled, lowercase)
        starts_with_patterns: Set of starts_with patterns (pre-compiled, lowercase)

    Returns:
        True if message matches any pattern
    """
    lower_msg = tracker.msg.lower()

    # Check exact matches first (O(1) lookup)
    if lower_msg in exact_matches:
        return True

    # Check starts_with patterns (O(n) where n is number of starts_with patterns)
    for prefix in starts_with_patterns:
        if lower_msg.startswith(prefix):
            return True

    return False


def process_torrent(torrent: Any, exact_matches: set[str], starts_with_patterns: set[str]) -> int:
    """
    Count unregistered trackers for a torrent.

    Args:
        torrent: Torrent object
        exact_matches: Pre-compiled exact match patterns
        starts_with_patterns: Pre-compiled starts_with patterns

    Returns:
        Count of unregistered trackers
    """
    unregistered_count = sum(
        1
        for tracker in torrent.trackers
        if check_unregistered_message(tracker, exact_matches, starts_with_patterns) and tracker.status in (4, 5)
    )
    return unregistered_count


def update_torrent_file_paths(torrent_file_paths: dict[str, list[str]], torrent: TorrentInfo) -> None:
    torrent_file_paths.setdefault(torrent.save_path, []).append(torrent.hash)


def _parse_torrent_tags(tags: str) -> set[str]:
    """Parse qBittorrent's comma-separated tag value into exact tags."""
    return {tag.strip() for tag in tags.split(",") if tag.strip()}


def delete_torrents_and_files(
    client: QBittorrentClient,
    config: Mapping[str, Any],
    use_delete_tags: bool,
    delete_tags: Sequence[str],
    delete_files: Mapping[str, bool],
    dry_run: bool,
    torrents: Sequence[TorrentInfo] | None = None,
    recycle_bin: str | None = None,
    *,
    plan: UnregisteredDeletionPlan | None = None,
) -> None:
    """Execute a confirmed torrent deletion plan.

    Args:
        client: qBittorrent client instance
        config: Configuration dictionary
        use_delete_tags: Whether to use tag-based deletion
        delete_tags: List of tags that trigger deletion
        delete_files: Dictionary mapping tags to whether files should be deleted
        dry_run: If True, only simulate the operation
        torrents: Optional list of torrents (avoids redundant API call)
        recycle_bin: Optional path to recycle bin directory
        plan: Optional plan produced during impact analysis. When omitted, the
            same plan is built immediately before execution.
    """
    if not use_delete_tags:
        return

    if plan is None:
        if torrents is None:
            fetched_torrents = client.torrents.info()
            if fetched_torrents is None:
                raise SafetyCheckError("qBittorrent returned no torrent list; refusing torrent deletion")
            resolved_torrents = fetched_torrents
        else:
            resolved_torrents = torrents
        plan = build_unregistered_deletion_plan(
            client,
            resolved_torrents,
            config,
            use_delete_tags,
            delete_tags,
            delete_files,
            recycle_bin,
        )

    if not plan.deletions:
        return

    recycle_bin_path = Path(recycle_bin) if recycle_bin else None
    file_actions = {
        DeletionAction.RECYCLE_FILES,
        DeletionAction.PERMANENT_DELETE,
    }
    for deletion in plan.deletions:
        if deletion.action in file_actions:
            for identity in deletion.files:
                verify_file_identity(identity)

    if dry_run:
        for deletion in plan.deletions:
            if deletion.action is DeletionAction.TORRENT_ONLY:
                logging.info("[Dry Run] Would delete torrent '%s' but keep its files.", deletion.torrent_name)
            elif deletion.action is DeletionAction.PRESERVE_SHARED:
                logging.info(
                    "[Dry Run] Would delete torrent '%s' but preserve files shared with: %s.",
                    deletion.torrent_name,
                    ", ".join(deletion.shared_with),
                )
            elif deletion.action is DeletionAction.RECYCLE_FILES:
                assert recycle_bin_path is not None
                identities = {identity.path: identity for identity in deletion.files}
                move_files_to_recycle_bin(
                    list(identities),
                    recycle_bin_path,
                    "unregistered",
                    deletion.category or "uncategorized",
                    dry_run=True,
                    expected_identities=identities,
                )
                logging.info("[Dry Run] Would delete torrent '%s' after recycling its files.", deletion.torrent_name)
            else:
                logging.info("[Dry Run] Would permanently delete torrent '%s' and its files.", deletion.torrent_name)
        return

    if plan.ownership_snapshot is not None:
        _revalidate_ownership_snapshot(client, plan.ownership_snapshot)

    permanent_deletions = [deletion for deletion in plan.deletions if deletion.action is DeletionAction.PERMANENT_DELETE]
    if permanent_deletions:
        permanent_hashes = [deletion.torrent_hash for deletion in permanent_deletions]
        client.torrents_delete(torrent_hashes=permanent_hashes, delete_files=True)
        for deletion in permanent_deletions:
            logging.info("Permanently deleted torrent '%s' and its files.", deletion.torrent_name)

    keep_file_hashes: list[str] = []
    for deletion in plan.deletions:
        if deletion.action is DeletionAction.PERMANENT_DELETE:
            continue
        if deletion.action is DeletionAction.RECYCLE_FILES:
            assert recycle_bin_path is not None
            identities = {identity.path: identity for identity in deletion.files}
            move_records: list[RecycleBinMove] = []
            success_count, failed = move_files_to_recycle_bin(
                list(identities),
                recycle_bin_path,
                "unregistered",
                deletion.category or "uncategorized",
                all_or_nothing=True,
                expected_identities=identities,
                move_records=move_records,
            )
            if failed or success_count != len(identities):
                logging.error(
                    "Preserving torrent '%s' because not all files were moved safely (%d/%d moved).",
                    deletion.torrent_name,
                    success_count,
                    len(identities),
                )
                continue
            try:
                client.torrents_delete(torrent_hashes=[deletion.torrent_hash], delete_files=False)
            except BaseException:
                rollback_failures = rollback_recycle_bin_moves(move_records)
                if rollback_failures:
                    logging.critical(
                        "Torrent '%s' remains active and %d recycled files could not be restored.",
                        deletion.torrent_name,
                        len(rollback_failures),
                    )
                raise
            logging.info("Moved %d files and deleted torrent '%s'.", success_count, deletion.torrent_name)
            continue
        elif deletion.action is DeletionAction.PRESERVE_SHARED:
            logging.warning(
                "Preserving files for torrent '%s' (hash: %s); shared with: %s",
                deletion.torrent_name,
                deletion.torrent_hash,
                ", ".join(deletion.shared_with),
            )
        keep_file_hashes.append(deletion.torrent_hash)

    if keep_file_hashes:
        client.torrents_delete(torrent_hashes=keep_file_hashes, delete_files=False)


def unregistered_checks(  # noqa: C901
    client: QBittorrentClient,
    torrents: Sequence[TorrentInfo],
    config: dict[str, Any],
    use_delete_tags: bool,
    delete_tags: list[str],
    delete_files: dict[str, bool],
    dry_run: bool,
    recycle_bin: str | None = None,
    *,
    deletion_plan: UnregisteredDeletionPlan | None = None,
) -> tuple[dict[str, list[str]], dict[str, int]]:
    """
    Check torrents for unregistered status and apply appropriate tags.

    Uses batched API calls for maximum performance.

    Args:
        client: qBittorrent client
        torrents: List of torrents to check
        config: Configuration dictionary
        use_delete_tags: Whether to use delete tags
        delete_tags: List of tags that trigger deletion
        delete_files: Dictionary mapping tags to delete_files boolean
        dry_run: If True, don't make actual changes
        recycle_bin: Optional path to recycle bin directory
        deletion_plan: Optional deletion plan confirmed during impact preview.

    Returns:
        Tuple of (torrent_file_paths, unregistered_counts_per_path)
    """
    torrent_file_paths: dict[str, list[str]] = {}
    unregistered_counts_per_path: dict[str, int] = {}
    unregistered_torrents_per_path: dict[str, int] = {}
    tag_counts: dict[str, int] = {}
    default_tag = config["default_unregistered_tag"]
    cross_seeding_tag = config["cross_seeding_tag"]
    resolved_deletion_plan = deletion_plan or build_unregistered_deletion_plan(
        client,
        torrents,
        config,
        use_delete_tags,
        delete_tags,
        delete_files,
        recycle_bin,
    )

    # Pre-compile patterns for efficient matching
    unregistered_patterns = config.get("unregistered", [])
    exact_matches, starts_with_patterns = compile_patterns(unregistered_patterns)

    # First pass: Collect all torrent data and unregistered status
    # Store per-path lists of unregistered torrent hashes for second pass
    unregistered_hashes_per_path: dict[str, list[str]] = {}

    for torrent in tqdm(torrents, desc="Checking for unregistered torrents", unit="torrent"):
        update_torrent_file_paths(torrent_file_paths, torrent)

        # Use pre-compiled patterns for faster matching
        unregistered_count = process_torrent(torrent, exact_matches, starts_with_patterns)

        unregistered_counts_per_path[torrent.save_path] = (
            unregistered_counts_per_path.get(torrent.save_path, 0) + unregistered_count
        )

        # Track unregistered torrents per path (don't assign tags yet)
        if unregistered_count > 0:
            # Track number of torrents (not tracker hits) with any unregistered tracker
            unregistered_torrents_per_path[torrent.save_path] = unregistered_torrents_per_path.get(torrent.save_path, 0) + 1
            # Store this torrent hash for the path
            if torrent.save_path not in unregistered_hashes_per_path:
                unregistered_hashes_per_path[torrent.save_path] = []
            unregistered_hashes_per_path[torrent.save_path].append(torrent.hash)

    # Second pass: Now that we have complete per-path counts, assign tags correctly
    default_tag_hashes: list[str] = []
    cross_seeding_tag_hashes: list[str] = []

    for save_path, unregistered_hashes in unregistered_hashes_per_path.items():
        # Now we can accurately check if ALL torrents in this path have unregistered trackers
        total_torrents_in_path = len(torrent_file_paths[save_path])
        unregistered_torrents_in_path = unregistered_torrents_per_path[save_path]

        is_all_unregistered = unregistered_torrents_in_path == total_torrents_in_path

        if is_all_unregistered:
            # All torrents in this path have unregistered trackers
            default_tag_hashes.extend(unregistered_hashes)
            tag_counts[default_tag] = tag_counts.get(default_tag, 0) + len(unregistered_hashes)
        else:
            # Only some torrents have unregistered trackers (cross-seeding)
            cross_seeding_tag_hashes.extend(unregistered_hashes)
            tag_counts[cross_seeding_tag] = tag_counts.get(cross_seeding_tag, 0) + len(unregistered_hashes)

    # Apply tags in batches (2 API calls instead of N)
    if not dry_run:
        if default_tag_hashes:
            try:
                client.torrents_add_tags(torrent_hashes=default_tag_hashes, tags=[default_tag])
                logging.info(f"Added tag '{default_tag}' to {len(default_tag_hashes)} torrents")
            except Exception:
                logging.exception(f"Failed to add tag '{default_tag}' in batch")

        if cross_seeding_tag_hashes:
            try:
                client.torrents_add_tags(torrent_hashes=cross_seeding_tag_hashes, tags=[cross_seeding_tag])
                logging.info(f"Added tag '{cross_seeding_tag}' to {len(cross_seeding_tag_hashes)} torrents")
            except Exception:
                logging.exception(f"Failed to add tag '{cross_seeding_tag}' in batch")
    else:
        if default_tag_hashes:
            logging.info(f"[Dry Run] Would add tag '{default_tag}' to {len(default_tag_hashes)} torrents")
        if cross_seeding_tag_hashes:
            logging.info(f"[Dry Run] Would add tag '{cross_seeding_tag}' to {len(cross_seeding_tag_hashes)} torrents")

    delete_torrents_and_files(
        client,
        config,
        use_delete_tags,
        delete_tags,
        delete_files,
        dry_run,
        torrents,
        recycle_bin,
        plan=resolved_deletion_plan,
    )

    for tag, count in tag_counts.items():
        logging.info("Tag: %s, Count: %d", tag, count)

    return torrent_file_paths, unregistered_counts_per_path
