import logging
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn, cast
from fnmatch import translate

from qbitunregistered.cache import cached
from qbitunregistered.file_operations import (
    FileIdentity,
    SafetyCheckError,
    cache_torrent_files,
    capture_file_identity,
    fetch_torrent_files,
    invalidate_torrent_files,
    is_internal_recycle_staging_path,
    move_files_to_recycle_bin,
    verify_file_identity,
)
from qbitunregistered.types import QBittorrentClient

_INCLUDED_FILES_MISSING = object()


@dataclass(frozen=True, slots=True)
class OrphanFilePlan:
    """Read-only set of filesystem identities confirmed as orphaned."""

    files: tuple[FileIdentity, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return the confirmed paths in deterministic order."""
        return tuple(identity.path for identity in self.files)


class OrphanScanResult(list[str]):
    """List-compatible orphan paths with immutable discovery identities."""

    __slots__ = ("_discovered_identities",)

    def __init__(self, paths: Sequence[str], discovered_identities: Mapping[str, FileIdentity]) -> None:
        super().__init__(paths)
        self._discovered_identities: Mapping[str, FileIdentity] = MappingProxyType(dict(discovered_identities))

    def discovered_identity(self, path: str) -> FileIdentity | None:
        """Return the filesystem identity captured when this path was discovered."""
        return self._discovered_identities.get(path)


@dataclass(frozen=True, slots=True)
class _TorrentOwnership:
    """Exact owned files and active save paths from one torrent snapshot."""

    owned_paths: frozenset[Path]
    active_save_paths: frozenset[Path]
    file_metadata_fetches: int


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


def _index_torrent_snapshot(torrents: object, *, context: str) -> dict[str, Any]:
    """Validate and index one qBittorrent torrent snapshot by hash."""
    if torrents is None:
        raise SafetyCheckError(f"qBittorrent returned no torrent list {context}")
    if not isinstance(torrents, Sequence) or isinstance(torrents, (str, bytes)):
        raise SafetyCheckError(f"qBittorrent returned a malformed torrent list {context}")

    indexed: dict[str, Any] = {}
    for torrent in torrents:
        torrent_hash = getattr(torrent, "hash", None)
        if not isinstance(torrent_hash, str) or not torrent_hash or torrent_hash in indexed:
            raise SafetyCheckError(f"qBittorrent returned a missing or duplicate torrent hash {context}")
        indexed[torrent_hash] = torrent
    return indexed


def _refresh_torrent_snapshot(
    client: QBittorrentClient,
    *,
    context: str,
    include_files: bool = False,
) -> dict[str, Any]:
    """Return one validated current torrent snapshot, failing closed."""
    try:
        torrents = client.torrents.info(include_files=True) if include_files else client.torrents.info()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        if not include_files:
            raise SafetyCheckError(f"Could not refresh qBittorrent state {context}") from error
        logging.warning(
            "qBittorrent rejected the bulk file-metadata snapshot %s; retrying with the compatible torrent list.",
            context,
        )
        try:
            torrents = client.torrents.info()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as fallback_error:
            raise SafetyCheckError(f"Could not refresh qBittorrent state {context}") from fallback_error
    return _index_torrent_snapshot(torrents, context=context)


def _included_torrent_files(torrent: Any) -> object:
    """Return embedded file metadata only when it is a response mapping key."""
    if isinstance(torrent, Mapping) and "files" in torrent:
        return torrent["files"]
    return _INCLUDED_FILES_MISSING


def _index_candidate_paths(candidate_paths: set[Path]) -> dict[str, Path | None]:
    """Index canonical candidates, marking native path-key collisions ambiguous."""
    candidate_lookup: dict[str, Path | None] = {}
    for candidate_path in candidate_paths:
        candidate_key = os.path.normcase(str(candidate_path))
        if candidate_key not in candidate_lookup:
            candidate_lookup[candidate_key] = candidate_path
        elif candidate_lookup[candidate_key] != candidate_path:
            candidate_lookup[candidate_key] = None
    return candidate_lookup


def _exact_torrent_owned_paths(  # noqa: C901
    client: QBittorrentClient,
    torrent: Any,
    resolved_save_paths: dict[str, Path],
    candidate_lookup: Mapping[str, Path | None],
    save_path_strings: dict[str, str],
    *,
    context: str,
    refresh_file_metadata: bool = False,
    tolerate_confirmed_removal: bool = False,
    included_files: object = _INCLUDED_FILES_MISSING,
) -> set[Path]:
    """Return canonical owned paths or confirm that a failed torrent is gone."""
    torrent_hash = getattr(torrent, "hash", None)
    save_path_value = getattr(torrent, "save_path", None)
    if not isinstance(torrent_hash, str) or not torrent_hash:
        raise SafetyCheckError(f"qBittorrent returned a torrent without a valid hash {context}")
    if not isinstance(save_path_value, str) or not save_path_value:
        raise SafetyCheckError(f"Torrent {torrent_hash} has no valid save path {context}")

    fetched_separately = included_files is _INCLUDED_FILES_MISSING
    raw_files: object
    if fetched_separately:
        try:
            raw_files = fetch_torrent_files(
                client,
                torrent_hash,
                cache_scope=id(client),
                refresh=refresh_file_metadata,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            if tolerate_confirmed_removal:
                refreshed = _refresh_torrent_snapshot(
                    client,
                    context=f"after file metadata failed for torrent {torrent_hash}",
                )
                if torrent_hash not in refreshed:
                    logging.info(
                        "Torrent %s disappeared during orphan scanning; ignoring its unavailable metadata.",
                        torrent_hash,
                    )
                    return set()
            raise SafetyCheckError(f"Could not read file metadata for active torrent {torrent_hash} {context}") from error
    else:
        raw_files = included_files

    if raw_files is None:
        raise SafetyCheckError(f"Torrent {torrent_hash} returned no file metadata {context}")
    if not isinstance(raw_files, Sequence) or isinstance(raw_files, (str, bytes)):
        raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata {context}")

    if save_path_value not in resolved_save_paths:
        resolved_save_paths[save_path_value] = _resolve_active_save_root(
            save_path_value,
            description=f"Torrent {torrent_hash} save path",
        )
    save_path = resolved_save_paths[save_path_value]
    if save_path_value not in save_path_strings:
        save_path_strings[save_path_value] = str(save_path)
    save_path_string = save_path_strings[save_path_value]
    owned_paths: set[Path] = set()
    for file_info in raw_files:
        name = file_info.get("name") if isinstance(file_info, Mapping) else getattr(file_info, "name", None)
        if not isinstance(name, str) or not name:
            raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata {context}")
        try:
            metadata_path = Path(name)
            metadata_parts = metadata_path.parts
            if metadata_parts and not metadata_path.anchor and ".." not in metadata_parts:
                candidate_key = os.path.normcase(os.path.join(save_path_string, str(metadata_path)))
                candidate_path = candidate_lookup.get(candidate_key)
                if candidate_path is not None:
                    owned_paths.add(candidate_path)
                    continue
            lexical_path = save_path / metadata_path
            owned_path = lexical_path.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path {context}") from error
        if owned_path == save_path or not owned_path.is_relative_to(save_path):
            raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path {context}")
        owned_paths.add(owned_path)
    if not fetched_separately:
        cache_torrent_files(torrent_hash, list(raw_files), cache_scope=id(client))
    return owned_paths


def _candidate_directory_boundaries(candidate_paths: set[Path]) -> set[Path]:
    """Return candidate parent directories and ancestors for boundary checks."""
    boundaries: set[Path] = set()
    for candidate_path in candidate_paths:
        current_path = candidate_path.parent
        while current_path not in boundaries:
            boundaries.add(current_path)
            parent_path = current_path.parent
            if parent_path == current_path:
                break
            current_path = parent_path
    return boundaries


def _validated_content_boundary(torrent: Any, save_path: Path) -> tuple[Path, int] | None:
    """Return a trustworthy bulk content path and mode, or request exact fallback."""
    content_path_value = getattr(torrent, "content_path", None)
    if not isinstance(content_path_value, str) or not content_path_value:
        return None
    try:
        content_path = Path(content_path_value)
        if not content_path.is_absolute():
            return None
        relative_content_path = content_path.relative_to(save_path)
        if ".." in relative_content_path.parts:
            return None
        inspected_path = save_path
        content_stat = save_path.lstat()
        if stat.S_ISLNK(content_stat.st_mode) or bool(getattr(content_stat, "st_reparse_tag", 0)):
            return None
        for part in relative_content_path.parts:
            inspected_path /= part
            content_stat = inspected_path.lstat()
            if stat.S_ISLNK(content_stat.st_mode) or bool(getattr(content_stat, "st_reparse_tag", 0)):
                return None
        resolved_content_path = content_path.resolve()
        if not resolved_content_path.is_relative_to(save_path):
            return None
        content_stat = resolved_content_path.lstat()
        if stat.S_ISLNK(content_stat.st_mode) or bool(getattr(content_stat, "st_reparse_tag", 0)):
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved_content_path, content_stat.st_mode


def _build_torrent_ownership(
    client: QBittorrentClient,
    torrents: dict[str, Any],
    candidate_paths: set[Path],
    *,
    context: str,
    refresh_file_metadata: bool,
    tolerate_confirmed_removal: bool,
) -> _TorrentOwnership:
    """Build exact ownership, using bulk boundaries only when they are conclusive."""
    candidate_boundaries = _candidate_directory_boundaries(candidate_paths)
    candidate_lookup = _index_candidate_paths(candidate_paths)
    resolved_save_paths: dict[str, Path] = {}
    save_path_strings: dict[str, str] = {}
    owned_paths: set[Path] = set()
    active_save_paths: set[Path] = set()
    file_metadata_fetches = 0

    for torrent_hash in sorted(torrents):
        torrent = torrents[torrent_hash]
        if refresh_file_metadata:
            # Fast paths must still retire pre-walk mappings so later
            # operations cannot consume metadata older than reconciliation.
            invalidate_torrent_files(torrent_hash, cache_scope=id(client))
        save_path_value = getattr(torrent, "save_path", None)
        if not isinstance(save_path_value, str) or not save_path_value:
            raise SafetyCheckError(f"Torrent {torrent_hash} has no valid save path {context}")
        if save_path_value not in resolved_save_paths:
            resolved_save_paths[save_path_value] = _resolve_active_save_root(
                save_path_value,
                description=f"Torrent {torrent_hash} save path",
            )
        save_path = resolved_save_paths[save_path_value]
        active_save_paths.add(save_path)

        content_boundary = _validated_content_boundary(torrent, save_path)
        if content_boundary is not None:
            content_path, content_mode = content_boundary
            if stat.S_ISREG(content_mode):
                owned_paths.add(content_path)
                continue
            if stat.S_ISDIR(content_mode) and content_path not in candidate_boundaries:
                continue

        included_files = _included_torrent_files(torrent)
        if included_files is _INCLUDED_FILES_MISSING:
            file_metadata_fetches += 1
        owned_paths.update(
            _exact_torrent_owned_paths(
                client,
                torrent,
                resolved_save_paths,
                candidate_lookup,
                save_path_strings,
                context=context,
                refresh_file_metadata=refresh_file_metadata,
                tolerate_confirmed_removal=tolerate_confirmed_removal,
                included_files=included_files,
            )
        )

    return _TorrentOwnership(
        owned_paths=frozenset(owned_paths),
        active_save_paths=frozenset(active_save_paths),
        file_metadata_fetches=file_metadata_fetches,
    )


def _resolve_explicit_scan_roots(orphan_scan_roots: Sequence[str] | None) -> set[Path]:
    """Return canonical operator-authorized orphan traversal roots."""
    if orphan_scan_roots is None:
        return set()
    if not isinstance(orphan_scan_roots, Sequence) or isinstance(orphan_scan_roots, (str, bytes)):
        raise SafetyCheckError("Explicit orphan scan roots are malformed")

    resolved_roots: set[Path] = set()
    for index, root in enumerate(orphan_scan_roots):
        resolved_roots.add(
            _resolve_active_save_root(
                root,
                description=f"Explicit orphan scan root at index {index}",
            )
        )
    return resolved_roots


def check_files_on_disk(  # noqa: C901
    client: QBittorrentClient,
    torrents: list[Any],
    exclude_file_patterns: list[str] | None = None,
    exclude_dirs: list[str] | None = None,
    orphan_scan_roots: Sequence[str] | None = None,
    orphan_min_age_seconds: int = 0,
) -> list[str]:
    """
    Identifies orphaned files on disk that are not associated with any active torrents in qBittorrent.

    Returns:
        List of orphaned file paths as strings

    Raises:
        SafetyCheckError: If complete torrent ownership cannot be established.
    """
    if isinstance(orphan_min_age_seconds, bool) or not isinstance(orphan_min_age_seconds, int) or orphan_min_age_seconds < 0:
        raise SafetyCheckError("Minimum orphan age must be a non-negative integer number of seconds")

    # Avoid mutable default arguments - create fresh lists if None
    exclude_file_patterns = exclude_file_patterns or []
    exclude_dirs = exclude_dirs or []

    logging.debug("Entering check_files_on_disk function...")

    # qBittorrent's configured roots preserve the existing default traversal
    # scope. Operator roots are additive; individual torrent save paths never
    # grant traversal authority.
    managed_scan_roots = _get_active_configured_save_roots(client, use_cache=True)
    managed_scan_roots.update(_resolve_explicit_scan_roots(orphan_scan_roots))

    existing_scan_roots: set[Path] = set()
    for root in managed_scan_roots:
        try:
            root_stat = root.stat()
        except FileNotFoundError:
            logging.warning("Skipping orphan scan root that does not exist: %s", root)
            continue
        except OSError as error:
            raise SafetyCheckError(f"Could not inspect orphan scan root safely: {root}") from error
        if not stat.S_ISDIR(root_stat.st_mode):
            raise SafetyCheckError(f"Orphan scan root is not a directory: {root}")
        existing_scan_roots.add(root)

    # Remove redundant subdirectories (keep only the highest-level paths)
    filtered_scan_roots: set[Path] = set()
    for path in sorted(existing_scan_roots, key=lambda item: len(item.parts)):
        if not any(parent in filtered_scan_roots for parent in path.parents):
            filtered_scan_roots.add(path)

    logging.info("Scanning %d managed roots for orphaned files...", len(filtered_scan_roots))

    # Validate the caller's start-of-scan snapshot, but defer file metadata
    # reads until after the filesystem walk so rename and re-add mappings are
    # current without fetching every torrent twice.
    initial_torrents = _index_torrent_snapshot(torrents, context="at orphan scan start")

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

    candidate_files: list[tuple[FileIdentity, str]] = []
    scan_started_ns = time.time_ns()
    minimum_age_ns = orphan_min_age_seconds * 1_000_000_000
    files_checked = 0
    files_excluded_by_pattern = 0
    files_excluded_by_dir = 0
    files_excluded_by_age = 0
    canonical_directory_exclusions = bool(exclude_dir_paths or compiled_dir_patterns)

    # Scan managed roots recursively.
    for save_path in sorted(filtered_scan_roots, key=lambda path: len(path.parts)):
        logging.info("Checking files in: %s", save_path)

        for entry in save_path.rglob("*"):  # Recursive check inside category paths
            if is_internal_recycle_staging_path(entry):
                files_excluded_by_dir += 1
                continue

            # Check if entry is in an excluded directory (early exit for better performance)
            entry_resolved: Path | None = None
            if canonical_directory_exclusions:
                entry_resolved = entry.resolve()
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

                if minimum_age_ns:
                    try:
                        modified_ns = (entry_resolved if entry_resolved is not None else entry).stat().st_mtime_ns
                    except OSError as error:
                        raise SafetyCheckError(f"Could not verify orphan candidate age safely: {entry}") from error
                    if scan_started_ns - modified_ns < minimum_age_ns:
                        files_excluded_by_age += 1
                        continue

                candidate_identity = _capture_current_orphan_identity(
                    entry_resolved if entry_resolved is not None else entry,
                    missing_log_message="Orphan candidate disappeared during discovery; omitting path: %s",
                    canonicalize_symlink=True,
                )
                if candidate_identity is not None:
                    candidate_files.append((candidate_identity, str(entry)))

    logging.info(
        "Scanned %d files, excluded %d by pattern, %d by directory, and %d by minimum age",
        files_checked,
        files_excluded_by_pattern,
        files_excluded_by_dir,
        files_excluded_by_age,
    )

    # A long filesystem walk can overlap additions, re-additions, file renames,
    # and save-path changes. Refresh ownership only after the walk, replacing
    # any metadata cached by an earlier operation in this execution.
    current_torrents = _refresh_torrent_snapshot(
        client,
        context="after the orphan filesystem scan",
        include_files=True,
    )
    ownership = _build_torrent_ownership(
        client,
        current_torrents,
        {identity.path for identity, _display_path in candidate_files},
        context="while reconciling current ownership after the orphan scan",
        refresh_file_metadata=True,
        tolerate_confirmed_removal=True,
    )
    logging.debug(
        "Tracking %d files from %d current torrents after scanning from an initial snapshot of %d torrents "
        "(using %d unique save paths and %d exact file metadata requests)",
        len(ownership.owned_paths),
        len(current_torrents),
        len(initial_torrents),
        len(ownership.active_save_paths),
        ownership.file_metadata_fetches,
    )

    orphaned_candidates = [
        (identity, display_path) for identity, display_path in candidate_files if identity.path not in ownership.owned_paths
    ]
    removed_count = len(candidate_files) - len(orphaned_candidates)
    if removed_count:
        logging.info(
            "Removed %d orphan candidates owned by the refreshed snapshot of %d torrents.",
            removed_count,
            len(current_torrents),
        )

    return OrphanScanResult(
        [display_path for _identity, display_path in orphaned_candidates],
        {display_path: identity for identity, display_path in orphaned_candidates},
    )


def _capture_current_orphan_identity(
    path: Path,
    *,
    missing_log_message: str,
    canonicalize_symlink: bool = False,
) -> FileIdentity | None:
    """Capture one unchanged regular file, or return ``None`` if it is absent."""
    try:
        discovered_stat = path.lstat()
        if canonicalize_symlink and stat.S_ISLNK(discovered_stat.st_mode):
            # Filesystem discovery historically reconciles canonical
            # pathnames. Direct plan callers retain strict symlink rejection.
            path = path.resolve()
            discovered_stat = path.lstat()
    except FileNotFoundError:
        logging.info(missing_log_message, path)
        return None
    except (OSError, RuntimeError) as error:
        raise SafetyCheckError(f"Could not inspect orphan candidate safely: {path}") from error
    identity = capture_file_identity(path)
    if not identity.matches(discovered_stat):
        raise SafetyCheckError(f"Orphan candidate changed during identity capture: {path}")
    return identity


def build_orphan_file_plan(orphaned_files: Sequence[str]) -> OrphanFilePlan:
    """Capture immutable identities for paths found by the orphan scan.

    Raises:
        SafetyCheckError: If any existing path cannot be confirmed as the same
            regular file discovered by the scan.
    """
    identities: list[FileIdentity] = []
    for value in orphaned_files:
        path = Path(value)
        if is_internal_recycle_staging_path(path):
            continue
        if isinstance(orphaned_files, OrphanScanResult):
            identity = orphaned_files.discovered_identity(value)
            if identity is None:
                raise SafetyCheckError(f"Orphan scan evidence is missing for candidate: {path}")
            try:
                current_stat = path.lstat()
            except FileNotFoundError:
                logging.info("Orphan candidate disappeared before plan capture; omitting path: %s", path)
                continue
            except OSError as error:
                raise SafetyCheckError(f"Could not inspect orphan candidate before plan capture: {path}") from error
            if not identity.matches(current_stat):
                raise SafetyCheckError(f"Orphan candidate changed after discovery: {path}")
        else:
            identity = _capture_current_orphan_identity(
                path,
                missing_log_message="Orphan candidate disappeared before plan capture; omitting path: %s",
            )
            if identity is None:
                continue
        identities.append(identity)
    return OrphanFilePlan(files=tuple(sorted(identities, key=lambda item: item.path)))


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
    current_torrents = _refresh_torrent_snapshot(
        client,
        context="before orphan cleanup",
        include_files=True,
    )
    ownership = _build_torrent_ownership(
        client,
        current_torrents,
        set(plan.paths),
        context="during final orphan validation",
        refresh_file_metadata=True,
        tolerate_confirmed_removal=False,
    )
    claimed_paths = sorted(set(plan.paths) & ownership.owned_paths)
    if claimed_paths:
        formatted_paths = ", ".join(str(path) for path in claimed_paths)
        raise SafetyCheckError(f"Confirmed orphan path is now owned by qBittorrent: {formatted_paths}")
    return set(ownership.active_save_paths)


def _raise_incomplete_orphan_cleanup(
    skipped_files: Sequence[tuple[Path, str]],
    *,
    action: str,
    completed_count: int,
    planned_count: int,
) -> NoReturn:
    """Raise after logging an incomplete orphan cleanup without claiming success."""
    incomplete_count = planned_count - completed_count
    logging.error(
        "Orphan cleanup incomplete: %d of %d planned files were %s; %d remain incomplete.",
        completed_count,
        planned_count,
        action,
        incomplete_count,
    )
    for file_path, reason in skipped_files:
        logging.error("Skipped orphan path %s: %s", file_path, reason)
    raise SafetyCheckError(
        f"Orphan cleanup incomplete: {completed_count} of {planned_count} planned files were {action}; "
        f"{incomplete_count} remain. See logs for details."
    )


def _potential_empty_directories(
    candidate_paths: set[Path],
    *,
    authorized_roots: set[Path],
    active_save_paths: set[Path],
) -> set[Path]:
    """Return candidate parents strictly below active authorized roots."""
    potential_empty_dirs: set[Path] = set()
    for candidate_path in candidate_paths:
        containing_roots = [root for root in authorized_roots if candidate_path.is_relative_to(root)]
        if not containing_roots:
            continue
        authorized_root = max(containing_roots, key=lambda root: len(root.parts))
        parent_dir = candidate_path.parent
        while parent_dir != authorized_root and parent_dir not in active_save_paths:
            if parent_dir in potential_empty_dirs:
                break
            potential_empty_dirs.add(parent_dir)
            parent_dir = parent_dir.parent
    return potential_empty_dirs


def delete_orphaned_files(  # noqa: C901
    orphaned_files: list[str],
    dry_run: bool,
    client: QBittorrentClient,
    torrents: list[Any] | None = None,
    recycle_bin: str | None = None,
    *,
    plan: OrphanFilePlan | None = None,
    orphan_scan_roots: Sequence[str] | None = None,
    orphan_max_candidates: int | None = None,
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
        orphan_scan_roots: Optional operator-authorized traversal roots to
            preserve during empty-directory pruning.
        orphan_max_candidates: Optional real-run candidate limit. Dry-run
            continues reporting the complete intended target set.

    Raises:
        SafetyCheckError: If traversal authority, ownership, or identity
            validation fails, or any planned file action cannot be completed.
    """
    deleted_files_count = 0
    skipped_files: list[tuple[Path, str]] = []
    candidate_plan = plan if plan is not None else build_orphan_file_plan(orphaned_files)
    protected_paths = [identity.path for identity in candidate_plan.files if is_internal_recycle_staging_path(identity.path)]
    for protected_path in protected_paths:
        logging.warning("Preserving internal recycle recovery path during orphan cleanup: %s", protected_path)
    if protected_paths:
        action = "moved to the recycle bin" if recycle_bin else "deleted"
        _raise_incomplete_orphan_cleanup(
            [(path, "internal recycle recovery paths cannot be orphan cleanup targets") for path in protected_paths],
            action=action,
            completed_count=0,
            planned_count=len(candidate_plan.files),
        )
    resolved_plan = OrphanFilePlan(
        files=tuple(identity for identity in candidate_plan.files if not is_internal_recycle_staging_path(identity.path))
    )
    if orphan_max_candidates is not None and (
        isinstance(orphan_max_candidates, bool) or not isinstance(orphan_max_candidates, int) or orphan_max_candidates < 1
    ):
        raise SafetyCheckError("Maximum orphan candidate count must be a positive integer")
    if not dry_run and orphan_max_candidates is not None and len(resolved_plan.files) > orphan_max_candidates:
        raise SafetyCheckError(
            f"Orphan cleanup blocked: {len(resolved_plan.files)} candidates exceed the configured maximum "
            f"of {orphan_max_candidates}"
        )
    orphaned_files_set = set(resolved_plan.paths)
    expected_identities = {identity.path: identity for identity in resolved_plan.files}
    processed_files: set[Path] = set()

    if not resolved_plan.files:
        logging.info("No orphaned files found. Nothing to delete.")
        return

    managed_scan_roots = _get_active_configured_save_roots(client, use_cache=dry_run)
    managed_scan_roots.update(_resolve_explicit_scan_roots(orphan_scan_roots))
    if not dry_run:
        unauthorized_paths = sorted(
            path for path in resolved_plan.paths if not any(path.is_relative_to(root) for root in managed_scan_roots)
        )
        if unauthorized_paths:
            action = "moved to the recycle bin" if recycle_bin else "deleted"
            _raise_incomplete_orphan_cleanup(
                [(path, "path is no longer beneath a current managed orphan scan root") for path in unauthorized_paths],
                action=action,
                completed_count=0,
                planned_count=len(resolved_plan.files),
            )
    active_save_paths = set(managed_scan_roots)

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

    potential_empty_dirs = _potential_empty_directories(
        orphaned_files_set,
        authorized_roots=managed_scan_roots,
        active_save_paths=active_save_paths,
    )

    if not dry_run:
        identity_failures: list[tuple[Path, str]] = []
        for identity in resolved_plan.files:
            try:
                verify_file_identity(identity)
            except SafetyCheckError as error:
                identity_failures.append((identity.path, str(error)))
        if identity_failures:
            action = "moved to the recycle bin" if recycle_bin else "deleted"
            _raise_incomplete_orphan_cleanup(
                identity_failures,
                action=action,
                completed_count=0,
                planned_count=len(resolved_plan.files),
            )

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
            all_or_nothing=True,
            expected_identities=expected_identities,
        )

        deleted_files_count = success_count
        if failed or success_count != len(resolved_plan.files):
            _raise_incomplete_orphan_cleanup(
                failed,
                action="moved to the recycle bin",
                completed_count=success_count,
                planned_count=len(resolved_plan.files),
            )
        processed_files.update(orphaned_files_set)
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
                break

        if skipped_files or deleted_files_count != len(resolved_plan.files):
            _raise_incomplete_orphan_cleanup(
                skipped_files,
                action="deleted",
                completed_count=deleted_files_count,
                planned_count=len(resolved_plan.files),
            )

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
