import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from fnmatch import translate

from qbitunregistered.cache import cached
from qbitunregistered.file_operations import (
    FileIdentity,
    SafetyCheckError,
    capture_file_identity,
    is_internal_recycle_staging_path,
    move_files_to_recycle_bin,
    verify_file_identity,
)
from qbitunregistered.types import QBittorrentClient


@dataclass(frozen=True, slots=True)
class OrphanFilePlan:
    """Read-only set of filesystem identities confirmed as orphaned."""

    files: tuple[FileIdentity, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return the confirmed paths in deterministic order."""
        return tuple(identity.path for identity in self.files)


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
def _get_categories(client, *, cache_scope: int) -> dict[str, Any]:
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
    return cast(dict[str, Any], client.torrent_categories.categories)


def check_files_on_disk(  # noqa: C901
    client,
    torrents: list[Any],
    exclude_file_patterns: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
) -> list[str]:
    """
    Identifies orphaned files on disk that are not associated with any active torrents in qBittorrent.

    Returns:
        List of orphaned file paths as strings
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
            if is_internal_recycle_staging_path(entry):
                files_excluded_by_dir += 1
                continue

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


def build_orphan_file_plan(orphaned_files: list[str]) -> OrphanFilePlan:
    """Capture immutable identities for paths found by the orphan scan.

    Raises:
        SafetyCheckError: If any path cannot be confirmed as the same regular
            file discovered by the scan.
    """
    identities = tuple(
        sorted(
            (capture_file_identity(Path(path)) for path in orphaned_files if not is_internal_recycle_staging_path(Path(path))),
            key=lambda item: item.path,
        )
    )
    return OrphanFilePlan(files=identities)


def _resolve_active_save_root(value: object, *, description: str) -> Path:
    """Return one canonical absolute qBittorrent save root."""
    if not isinstance(value, str) or not value:
        raise SafetyCheckError(f"{description} is missing or malformed")
    try:
        configured_root = Path(value)
        if not configured_root.is_absolute():
            raise SafetyCheckError(f"{description} is not an absolute path")
        return configured_root.resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise SafetyCheckError(f"{description} could not be resolved safely") from error


def _get_active_configured_save_roots(client: QBittorrentClient, *, use_cache: bool) -> set[Path]:
    """Return canonical default and category roots from one validated read."""
    try:
        if use_cache:
            default_save_path_value = _get_default_save_path(client, cache_scope=id(client))
            categories = _get_categories(client, cache_scope=id(client))
        else:
            default_save_path_value = client.application.default_save_path
            categories = client.torrent_categories.categories
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise SafetyCheckError("Could not refresh qBittorrent save-path metadata") from error

    default_save_path = _resolve_active_save_root(
        default_save_path_value,
        description="Default save path",
    )
    active_save_paths = {default_save_path}
    if not isinstance(categories, Mapping):
        raise SafetyCheckError("qBittorrent returned malformed category save-path metadata")
    for category_name, category in categories.items():
        if not isinstance(category_name, str) or not isinstance(category, Mapping):
            raise SafetyCheckError("qBittorrent returned malformed category save-path metadata")
        configured_category_path = category.get("savePath")
        category_save_path = _resolve_active_save_root(
            str(default_save_path / category_name) if configured_category_path in (None, "") else configured_category_path,
            description=f"Category {category_name!r} save path",
        )
        active_save_paths.add(category_save_path)
    return active_save_paths


def _revalidate_orphan_ownership(client: QBittorrentClient, plan: OrphanFilePlan) -> set[Path]:
    """Fail closed on new owners and return current canonical save roots."""
    try:
        current_torrents = client.torrents.info()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise SafetyCheckError("Could not refresh qBittorrent state before orphan cleanup") from error
    if current_torrents is None:
        raise SafetyCheckError("qBittorrent returned no torrent list during final orphan validation")
    if not isinstance(current_torrents, Sequence) or isinstance(current_torrents, (str, bytes)):
        raise SafetyCheckError("qBittorrent returned a malformed torrent list during final orphan validation")

    owned_paths: set[Path] = set()
    active_save_paths: set[Path] = set()
    seen_hashes: set[str] = set()
    for torrent in current_torrents:
        torrent_hash = getattr(torrent, "hash", None)
        save_path_value = getattr(torrent, "save_path", None)
        if not isinstance(torrent_hash, str) or not torrent_hash or torrent_hash in seen_hashes:
            raise SafetyCheckError("qBittorrent returned a missing or duplicate torrent hash")
        if not isinstance(save_path_value, str) or not save_path_value:
            raise SafetyCheckError(f"Torrent {torrent_hash} has no valid save path")
        seen_hashes.add(torrent_hash)
        save_path = _resolve_active_save_root(
            save_path_value,
            description=f"Torrent {torrent_hash} save path",
        )
        active_save_paths.add(save_path)

        try:
            raw_files = client.torrents_files(torrent_hash)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            raise SafetyCheckError(f"Could not read file metadata for torrent {torrent_hash}") from error
        if raw_files is None:
            raise SafetyCheckError(f"Torrent {torrent_hash} returned no file metadata")
        if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
            raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata")

        for file_info in raw_files:
            name = file_info.get("name") if isinstance(file_info, dict) else getattr(file_info, "name", None)
            if not isinstance(name, str) or not name:
                raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata")
            try:
                owned_path = (save_path / name).resolve()
            except (OSError, RuntimeError, ValueError) as error:
                raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path") from error
            if not owned_path.is_relative_to(save_path):
                raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path")
            owned_paths.add(owned_path)

    claimed_paths = sorted(set(plan.paths) & owned_paths)
    if claimed_paths:
        formatted_paths = ", ".join(str(path) for path in claimed_paths)
        raise SafetyCheckError(f"Confirmed orphan path is now owned by qBittorrent: {formatted_paths}")
    return active_save_paths


def delete_orphaned_files(  # noqa: C901
    orphaned_files: list[str],
    dry_run: bool,
    client: QBittorrentClient,
    torrents: list[Any] | None = None,
    recycle_bin: str | None = None,
    *,
    plan: OrphanFilePlan | None = None,
) -> None:
    """
    Deletes orphaned files and removes empty directories, while preserving active save paths.
    If dry-run is enabled, it logs what would be deleted without actually deleting files.

    Args:
        orphaned_files: List of orphaned file paths to delete
        dry_run: If True, only log actions without deleting
        client: qBittorrent client instance
        torrents: Optional list of torrents (avoids redundant API call if provided)
        recycle_bin: Optional path to move files to instead of deleting
        plan: Optional identity plan produced during impact analysis. When
            omitted, identities are captured before any mutation for backward
            compatibility.
    """
    deleted_files_count = 0
    skipped_files: list[tuple[Path, str]] = []
    candidate_plan = plan if plan is not None else build_orphan_file_plan(orphaned_files)
    protected_paths = [identity.path for identity in candidate_plan.files if is_internal_recycle_staging_path(identity.path)]
    for protected_path in protected_paths:
        logging.warning("Preserving internal recycle recovery path during orphan cleanup: %s", protected_path)
    resolved_plan = OrphanFilePlan(
        files=tuple(identity for identity in candidate_plan.files if not is_internal_recycle_staging_path(identity.path))
    )
    orphaned_files_set = set(resolved_plan.paths)
    expected_identities = {identity.path: identity for identity in resolved_plan.files}
    processed_files: set[Path] = set()

    if not resolved_plan.files:
        logging.info("No orphaned files found. Nothing to delete.")
        return

    active_save_paths = _get_active_configured_save_roots(client, use_cache=dry_run)

    # Track directories that will become empty
    potential_empty_dirs = set()

    # Collect all parent directories for later cleanup
    for file_path in orphaned_files_set:
        parent_dir = file_path.parent
        while parent_dir != parent_dir.parent:  # Add parent and all ancestor directories
            potential_empty_dirs.add(parent_dir)
            parent_dir = parent_dir.parent

    if dry_run:
        if torrents is None:
            try:
                resolved_torrents = client.torrents.info()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                raise SafetyCheckError("Could not read qBittorrent save paths for orphan dry-run") from error
            if resolved_torrents is None:
                raise SafetyCheckError("qBittorrent returned no torrent list for orphan dry-run")
        else:
            resolved_torrents = torrents
        for torrent in resolved_torrents:
            active_save_paths.add(
                _resolve_active_save_root(
                    getattr(torrent, "save_path", None),
                    description="Torrent save path",
                )
            )
    else:
        active_save_paths.update(_revalidate_orphan_ownership(client, resolved_plan))

    # Handle recycle bin or deletion
    if recycle_bin:
        recycle_bin_path = Path(recycle_bin)

        # Use shared utility for moving files to recycle bin with hybrid structure
        # Orphaned files go to: /recycle_bin/orphaned/uncategorized/[original_path]
        success_count, failed = move_files_to_recycle_bin(
            file_paths=list(orphaned_files_set),
            recycle_bin_path=recycle_bin_path,
            deletion_type="orphaned",
            category="uncategorized",  # Orphaned files don't have a category
            dry_run=dry_run,
            expected_identities=expected_identities,
        )

        deleted_files_count = success_count
        skipped_files = failed
        failed_paths = {path for path, _reason in failed}
        processed_files.update(path for path in orphaned_files_set if path not in failed_paths)
    else:
        # Permanent deletion (no recycle bin)
        for identity in resolved_plan.files:
            file_path = identity.path
            try:
                verify_file_identity(identity)
                if dry_run:
                    logging.info(f"Would delete orphaned file: {file_path}")
                    deleted_files_count += 1
                    processed_files.add(file_path)
                else:
                    file_path.unlink()
                    logging.info(f"Deleted orphaned file: {file_path}")
                    deleted_files_count += 1
                    processed_files.add(file_path)
            except (KeyboardInterrupt, SystemExit):
                raise
            except (OSError, SafetyCheckError) as error:
                logging.error("Refusing to delete changed orphaned file %s: %s", file_path, error)
                skipped_files.append((file_path, str(error)))

    # Determine which directories would be empty
    empty_dirs_to_delete: set[Path] = set()

    for dir_path in sorted(potential_empty_dirs, key=lambda p: len(str(p)), reverse=True):
        while dir_path not in active_save_paths and dir_path not in empty_dirs_to_delete:
            if is_internal_recycle_staging_path(dir_path):
                break
            try:
                existing_files = set(dir_path.iterdir())  # Check existing files in the directory
            except (PermissionError, FileNotFoundError) as e:
                logging.warning(f"Cannot access directory {dir_path}: {e}")
                break  # Stop checking this path and its parents
            except Exception:
                logging.exception(f"Unexpected error accessing directory {dir_path}")
                break

            # Simulate child-directory removals already queued by the deepest-first
            # walk so empty parents are included in the same plan.
            remaining_files = existing_files - processed_files - empty_dirs_to_delete

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
            except Exception:
                logging.exception(f"Error deleting directory {dir_path}")

    # Final Summary
    action = "moved to recycle bin" if recycle_bin else "deleted"
    if dry_run:
        logging.info(
            f"Dry-run: Would have {action} {deleted_files_count} orphaned files and removed {deleted_dirs_count} empty directories."
        )
    else:
        logging.info(
            f"Successfully {action} {deleted_files_count} orphaned files and removed {deleted_dirs_count} empty directories."
        )

    if skipped_files:
        logging.warning(f"Skipped {len(skipped_files)} files due to errors:")
        for file_path, reason in skipped_files:
            logging.warning(f" - {file_path}: {reason}")
