"""Shared file operations for recycle bin functionality."""

import errno
import logging
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime
from typing import Any, cast

from qbitunregistered.cache import cached
from qbitunregistered.types import QBittorrentClient

RECYCLE_STAGING_DIRECTORY_PREFIX = ".qbitunregistered-recycle-"


class SafetyCheckError(RuntimeError):
    """Raised when file ownership cannot be established safely."""


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Immutable identity and content metadata for a regular file."""

    path: Path
    device: int
    inode: int
    mode: int
    size: int
    mtime_ns: int

    def matches(self, file_stat: os.stat_result) -> bool:
        """Return whether a stat result still describes this planned file."""
        return (
            file_stat.st_dev == self.device
            and file_stat.st_ino == self.inode
            and file_stat.st_mode == self.mode
            and file_stat.st_size == self.size
            and file_stat.st_mtime_ns == self.mtime_ns
            and stat.S_ISREG(file_stat.st_mode)
        )


@dataclass(frozen=True, slots=True)
class RecycleBinMove:
    """One completed recycle-bin move that can be rolled back safely."""

    original_path: Path
    recycled_path: Path


def is_internal_recycle_staging_path(path: Path) -> bool:
    """Return whether any path component is reserved for recycle recovery."""
    return any(part.startswith(RECYCLE_STAGING_DIRECTORY_PREFIX) for part in path.parts)


def capture_file_identity(file_path: Path) -> FileIdentity:
    """Capture a stable identity for an existing, non-symlink regular file.

    Raises:
        SafetyCheckError: If the path is missing, changes during inspection, or
            does not identify a regular file.
    """
    try:
        initial_stat = file_path.lstat()
        if stat.S_ISLNK(initial_stat.st_mode) or not stat.S_ISREG(initial_stat.st_mode):
            raise SafetyCheckError(f"Expected a regular non-symlink file: {file_path}")
        resolved_path = file_path.resolve(strict=True)
        resolved_stat = resolved_path.lstat()
    except (OSError, RuntimeError) as error:
        raise SafetyCheckError(f"Could not inspect file safely: {file_path}") from error

    if (
        (initial_stat.st_dev, initial_stat.st_ino) != (resolved_stat.st_dev, resolved_stat.st_ino)
        or stat.S_ISLNK(resolved_stat.st_mode)
        or not stat.S_ISREG(resolved_stat.st_mode)
    ):
        raise SafetyCheckError(f"File changed during safety inspection: {file_path}")

    return FileIdentity(
        path=resolved_path,
        device=resolved_stat.st_dev,
        inode=resolved_stat.st_ino,
        mode=resolved_stat.st_mode,
        size=resolved_stat.st_size,
        mtime_ns=resolved_stat.st_mtime_ns,
    )


def verify_file_identity(identity: FileIdentity) -> os.stat_result:
    """Verify that a planned path still identifies the same regular file.

    Raises:
        SafetyCheckError: If the file is missing, substituted, modified, or no
            longer a regular non-symlink file.
    """
    try:
        current_stat = identity.path.lstat()
    except OSError as error:
        raise SafetyCheckError(f"Planned file is missing or inaccessible: {identity.path}") from error
    if not identity.matches(current_stat):
        raise SafetyCheckError(f"Planned file changed after preview: {identity.path}")
    return current_stat


@cached(ttl=300, key_prefix="torrent_files")
def fetch_torrent_files(client: QBittorrentClient, torrent_hash: str, *, cache_scope: int) -> list[Any]:
    """
    Fetch file list for a torrent with TTL-based caching.

    Shared utility used by:
    - tag_cross_seeding.py (organizational tagging)
    - check_cross_seeding() (safety-critical file deletion checks)
    - get_torrent_file_paths() (file path retrieval before deletion)

    Cache is scoped to single execution (TTL=300s) and is safe because:
    1. All operations happen within same script run (typically < 60s)
    2. Cache is invalidated between runs
    3. Significantly reduces API load (4000+ calls → ~20 calls)

    Args:
        client: qBittorrent client instance
        torrent_hash: Hash of torrent to fetch files for
        cache_scope: REQUIRED - Unique identifier to scope cache per client.
                     Always pass id(client) to prevent cache contamination
                     across different client instances.

    Returns:
        List of file info dicts/objects from qBittorrent API

    Raises:
        AssertionError: If cache_scope is None (programming error)

    Security:
        Cache scope prevents different client instances from sharing cache.

    Performance:
        Reduces redundant API calls within a single execution. For a typical
        run with 1000 torrents, this reduces API calls by 95%+.
    """
    # Runtime assertion to prevent cache contamination
    assert cache_scope is not None, "cache_scope must be provided (use id(client))"
    return cast(list[Any], client.torrents_files(torrent_hash))


def move_files_to_recycle_bin(
    file_paths: list[Path],
    recycle_bin_path: Path,
    deletion_type: str,
    category: str = "uncategorized",
    dry_run: bool = False,
    *,
    all_or_nothing: bool = False,
    expected_identities: Mapping[Path, FileIdentity] | None = None,
    move_records: list[RecycleBinMove] | None = None,
) -> tuple[int, list[tuple[Path, str]]]:
    """
    Move files to recycle bin with hybrid organization (type + category).

    Args:
        file_paths: List of file paths to move
        recycle_bin_path: Root recycle bin directory
        deletion_type: Type of deletion ("orphaned" or "unregistered")
        category: Torrent category (default: "uncategorized")
        dry_run: If True, only simulate the operation
        all_or_nothing: Roll back prior moves if any file cannot be moved.
        expected_identities: Optional identities captured by a confirmed plan.
            Every input path must have a matching identity when provided.
        move_records: Optional caller-owned list populated with completed moves
            so a later external-operation failure can be rolled back.

    Returns:
        Tuple of (success_count, failed_files) where failed_files is list of (path, error_message)

    Directory Structure:
        /recycle_bin/
          ├── orphaned/
          │   ├── movies/
          │   │   └── [full path structure]
          │   └── tv/
          │       └── [full path structure]
          └── unregistered/
              ├── movies/
              │   └── [full path structure]
              └── tv/
                  └── [full path structure]
    """
    success_count = 0
    failed_files: list[tuple[Path, str]] = []
    moved_files: list[RecycleBinMove] = []

    # Validate deletion type
    if deletion_type not in ["orphaned", "unregistered"]:
        logging.warning(f"Invalid deletion_type '{deletion_type}', defaulting to 'orphaned'")
        deletion_type = "orphaned"

    # Sanitize category name (replace invalid characters)
    safe_category = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in category)
    if not safe_category:
        safe_category = "uncategorized"

    # Create base recycle bin structure: /recycle_bin/{type}/{category}/
    type_category_path = recycle_bin_path / deletion_type / safe_category

    if not dry_run:
        type_category_path.mkdir(parents=True, exist_ok=True)
        logging.debug(f"Ensured recycle bin path exists: {type_category_path}")

    for file_path in file_paths:
        try:
            expected_identity = None
            if expected_identities is not None:
                expected_identity = expected_identities.get(file_path)
                if expected_identity is None:
                    raise SafetyCheckError(f"No confirmed identity exists for planned file: {file_path}")
                verify_file_identity(expected_identity)

            source_stat = file_path.lstat()
            if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
                raise OSError("source is not a regular file")
            abs_file_path = file_path.resolve(strict=True)

            # Preserve original directory structure
            # For cross-platform compatibility, handle both Unix and Windows paths
            if abs_file_path.drive:
                # Windows path with drive letter (C: -> C_)
                relative_path = Path(abs_file_path.drive.replace(":", "_")) / abs_file_path.relative_to(abs_file_path.anchor)
            else:
                # Unix path (strip leading slash)
                relative_path = abs_file_path.relative_to(abs_file_path.anchor)

            # Final destination: /recycle_bin/{type}/{category}/{original_path}
            dest_path = type_category_path / relative_path

            if dry_run:
                logging.info(f"Would move to recycle bin ({deletion_type}/{safe_category}): {file_path} -> {dest_path}")
                success_count += 1
            else:
                if dest_path.exists() or dest_path.is_symlink():
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    stem = dest_path.stem
                    suffix = dest_path.suffix
                    collision_index = 0
                    while dest_path.exists() or dest_path.is_symlink():
                        collision_suffix = f"_{collision_index}" if collision_index else ""
                        dest_path = dest_path.parent / f"{stem}_{timestamp}{collision_suffix}{suffix}"
                        collision_index += 1
                    logging.info(f"Destination file exists, using timestamp suffix: {dest_path.name}")

                dest_path.parent.mkdir(parents=True, exist_ok=True)
                current_stat = file_path.lstat()
                if expected_identity is not None and not expected_identity.matches(current_stat):
                    raise SafetyCheckError(f"Planned file changed after preview: {file_path}")
                if expected_identity is None and (current_stat.st_dev, current_stat.st_ino) != (
                    source_stat.st_dev,
                    source_stat.st_ino,
                ):
                    raise OSError("source changed during recycle-bin safety checks")
                _move_without_overwrite(file_path, dest_path, expected_identity=expected_identity)
                logging.info(f"Moved to recycle bin ({deletion_type}/{safe_category}): {file_path} -> {dest_path}")
                success_count += 1
                move = RecycleBinMove(original_path=file_path, recycled_path=dest_path)
                moved_files.append(move)
                if move_records is not None:
                    move_records.append(move)

        except Exception as e:
            error_msg = str(e)
            logging.exception(f"Error moving file to recycle bin: {file_path}: {error_msg}")
            failed_files.append((file_path, error_msg))
            if all_or_nothing and not dry_run:
                rollback_failures = rollback_recycle_bin_moves(moved_files)
                success_count -= len(moved_files) - len(rollback_failures)
                failed_files.extend(rollback_failures)
                if move_records is not None:
                    move_records.clear()
                break

    return success_count, failed_files


def rollback_recycle_bin_moves(moves: list[RecycleBinMove]) -> list[tuple[Path, str]]:
    """Restore completed recycle moves without overwriting current paths."""
    failures: list[tuple[Path, str]] = []
    for move in reversed(moves):
        try:
            _move_without_overwrite(move.recycled_path, move.original_path)
            logging.info("Rolled back recycle-bin move: %s -> %s", move.recycled_path, move.original_path)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            logging.exception(
                "Failed to roll back recycle-bin move: %s -> %s",
                move.recycled_path,
                move.original_path,
            )
            failures.append((move.original_path, f"rollback failed: {error}"))
    return failures


def _move_without_overwrite(
    source: Path,
    destination: Path,
    *,
    expected_identity: FileIdentity | None = None,
) -> None:
    """Move one file while refusing to overwrite an existing destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_stat = source.lstat()
    if stat.S_ISLNK(source_stat.st_mode) or not stat.S_ISREG(source_stat.st_mode):
        raise OSError("source is not a regular file")
    if expected_identity is not None and not expected_identity.matches(source_stat):
        raise SafetyCheckError(f"Planned file changed after preview: {source}")

    try:
        os.link(source, destination, follow_symlinks=False)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
        _copy_then_unlink_without_overwrite(source, destination, source_stat)
        return

    destination_stat = destination.lstat()
    expected_inode = (source_stat.st_dev, source_stat.st_ino)
    if (destination_stat.st_dev, destination_stat.st_ino) != expected_inode:
        raise OSError("destination changed during recycle-bin move")
    if expected_identity is not None and not expected_identity.matches(destination_stat):
        raise SafetyCheckError(f"Planned file changed during recycle-bin move: {source}")
    try:
        _fsync_directory(destination.parent)
        _unlink_captured_source(source, source_stat)
    except BaseException:
        if _path_matches_file_state(source, source_stat):
            _unlink_if_same_file_state(destination, destination_stat)
        raise


def _copy_then_unlink_without_overwrite(source: Path, destination: Path, expected_source_stat: os.stat_result) -> None:
    """Copy across filesystems exclusively, then unlink only the verified source."""
    source_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor: int | None = os.open(source, source_flags)
    destination_descriptor: int | None = None
    destination_stat: os.stat_result | None = None
    opened_source_stat: os.stat_result | None = None
    try:
        assert source_descriptor is not None
        opened_source_stat = os.fstat(source_descriptor)
        if (opened_source_stat.st_dev, opened_source_stat.st_ino) != (
            expected_source_stat.st_dev,
            expected_source_stat.st_ino,
        ):
            raise OSError("source changed before cross-filesystem copy")

        destination_descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        while chunk := os.read(source_descriptor, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination_descriptor, view)
                if written == 0:
                    raise OSError("cross-filesystem copy made no progress")
                view = view[written:]
        os.fchmod(destination_descriptor, stat.S_IMODE(opened_source_stat.st_mode))
        if os.utime in os.supports_fd:
            os.utime(
                destination_descriptor,
                ns=(opened_source_stat.st_atime_ns, opened_source_stat.st_mtime_ns),
            )
        os.fsync(destination_descriptor)

        copied_source_stat = os.fstat(source_descriptor)
        destination_stat = os.fstat(destination_descriptor)
        if (
            copied_source_stat.st_size != opened_source_stat.st_size
            or copied_source_stat.st_mtime_ns != opened_source_stat.st_mtime_ns
            or destination_stat.st_size != copied_source_stat.st_size
        ):
            raise OSError("source changed during cross-filesystem copy")

        _fsync_directory(destination.parent)
        current_destination_stat = destination.lstat()
        if (current_destination_stat.st_dev, current_destination_stat.st_ino) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            raise OSError("destination changed during cross-filesystem move")

        os.close(destination_descriptor)
        destination_descriptor = None
        os.close(source_descriptor)
        source_descriptor = None
        _unlink_captured_source(source, opened_source_stat)
    except BaseException:
        if destination_stat is None and destination_descriptor is not None:
            destination_stat = os.fstat(destination_descriptor)
        if destination_descriptor is not None:
            os.close(destination_descriptor)
            destination_descriptor = None
        if (
            destination_stat is not None
            and opened_source_stat is not None
            and _path_matches_file_state(source, opened_source_stat)
        ):
            _unlink_if_same_file_state(destination, destination_stat)
        raise
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)


def _unlink_captured_source(source: Path, expected_stat: os.stat_result) -> None:
    """Atomically capture and remove only the verified source object.

    POSIX has no portable conditional-unlink primitive. Renaming the public
    source entry into a private directory first closes the ordinary pathname
    replacement race: the captured entry is verified before its private name is
    unlinked. A mismatched entry is restored without overwrite or left at an
    actionable recovery path.
    """
    staging_directory = Path(tempfile.mkdtemp(prefix=RECYCLE_STAGING_DIRECTORY_PREFIX, dir=source.parent))
    staged_source = staging_directory / "captured"
    preserve_staging = False
    captured_stat: os.stat_result | None = None
    try:
        try:
            os.rename(source, staged_source)
        except FileNotFoundError as error:
            raise OSError("source disappeared during recycle-bin move; preserved destination") from error
        captured_stat = staged_source.lstat()
        if not _same_file_state(captured_stat, expected_stat):
            restored = _restore_staged_source_without_overwrite(staged_source, source, captured_stat)
            if restored:
                raise SafetyCheckError(
                    f"Source changed during recycle-bin move; replacement restored without deletion: {source}"
                )
            preserve_staging = True
            raise SafetyCheckError(
                "Source changed during recycle-bin move and its replacement could not be restored without "
                f"overwriting {source}; replacement preserved for recovery at {staged_source}"
            )

        staged_source.unlink()
    except BaseException:
        if captured_stat is not None:
            try:
                if _path_matches_file_state(staged_source, captured_stat):
                    _restore_staged_source_without_overwrite(staged_source, source, captured_stat)
            except OSError:
                pass
        if _path_is_present(staged_source):
            preserve_staging = True
            logging.critical(
                "Recycle source for %s remains preserved for recovery at %s",
                source,
                staged_source,
            )
        raise
    finally:
        if not preserve_staging:
            try:
                staging_directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError as error:
                logging.warning("Could not remove empty recycle staging directory %s: %s", staging_directory, error)

    _fsync_source_parent_after_unlink(source)


def _restore_staged_source_without_overwrite(
    staged_source: Path,
    source: Path,
    staged_stat: os.stat_result,
) -> bool:
    """Restore a captured regular file atomically without overwriting its path."""
    if not stat.S_ISREG(staged_stat.st_mode):
        return False
    try:
        os.link(staged_source, source, follow_symlinks=False)
    except OSError:
        if not _path_matches_file_state(source, staged_stat):
            return False
    try:
        staged_source.unlink()
    except OSError as error:
        logging.warning(
            "Restored captured source %s but could not remove its staging hard link %s: %s",
            source,
            staged_source,
            error,
        )
    return True


def _same_file_state(current_stat: os.stat_result, expected_stat: os.stat_result) -> bool:
    """Return whether two stat snapshots describe the same unchanged file."""
    return (
        (current_stat.st_dev, current_stat.st_ino) == (expected_stat.st_dev, expected_stat.st_ino)
        and current_stat.st_mode == expected_stat.st_mode
        and current_stat.st_size == expected_stat.st_size
        and current_stat.st_mtime_ns == expected_stat.st_mtime_ns
        and stat.S_ISREG(current_stat.st_mode)
    )


def _unlink_if_same_file_state(path: Path, expected_stat: os.stat_result) -> None:
    """Remove a path only when it still names the expected unchanged file."""
    try:
        _unlink_captured_source(path, expected_stat)
    except FileNotFoundError:
        return
    except (OSError, SafetyCheckError) as error:
        logging.warning("Preserved changed cleanup target %s: %s", path, error)


def _path_matches_file_state(path: Path, expected_stat: os.stat_result) -> bool:
    """Return whether a path still names the expected unchanged regular file."""
    try:
        current_stat = path.lstat()
    except OSError:
        return False
    return _same_file_state(current_stat, expected_stat)


def _path_is_present(path: Path) -> bool:
    """Return whether a path may still contain a filesystem object."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _fsync_directory(directory: Path) -> None:
    """Persist directory-entry changes where the platform supports directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_source_parent_after_unlink(source: Path) -> None:
    """Log durability uncertainty after a move has already completed."""
    try:
        _fsync_directory(source.parent)
    except OSError as error:
        logging.warning(
            "Moved file %s, but could not confirm source-directory durability: %s",
            source,
            error,
        )


def get_torrent_file_paths(client: QBittorrentClient, torrent_hash: str) -> list[Path]:
    """
    Get all file paths for a torrent before deletion.

    Args:
        client: qBittorrent client instance
        torrent_hash: Torrent hash

    Returns:
        List of absolute, existing file paths for the torrent. An empty list is
        returned only when the API confirms that the torrent has no files.

    Raises:
        SafetyCheckError: If torrent metadata, file metadata, or disk state
            cannot be established.
    """
    try:
        # Get torrent info
        torrent_info = client.torrents_info(torrent_hashes=torrent_hash)
        if not torrent_info:
            raise SafetyCheckError(f"Torrent with hash {torrent_hash} was not found")

        torrent = torrent_info[0]
        save_path_value = getattr(torrent, "save_path", None)
        if not isinstance(save_path_value, str) or not save_path_value:
            raise SafetyCheckError(f"Torrent {torrent_hash} has no valid save path")
        save_path = Path(save_path_value).resolve()

        # Get all files for this torrent (cached to reduce API calls)
        files = fetch_torrent_files(client, torrent_hash, cache_scope=id(client))
        file_paths: list[Path] = []

        for file_info in files:
            # Handle both dict and object forms from qBittorrent API
            if isinstance(file_info, dict):
                name = file_info.get("name")
            else:
                name = getattr(file_info, "name", None)

            if not isinstance(name, str) or not name:
                raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata")

            file_path = (save_path / name).resolve()
            if not file_path.is_relative_to(save_path):
                raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path")
            if not file_path.is_file():
                raise SafetyCheckError(f"Expected torrent file is missing or inaccessible: {file_path}")
            file_paths.append(file_path)

        return file_paths

    except (KeyboardInterrupt, SystemExit, SafetyCheckError):
        raise
    except Exception as error:
        raise SafetyCheckError(f"Could not discover files for torrent {torrent_hash}") from error


def check_cross_seeding(client: QBittorrentClient, file_paths: list[Path], exclude_hash: str) -> tuple[bool, list[str]]:
    """
    Check if any of the given file paths are being used by other active torrents.

    Raises:
        SafetyCheckError: If any torrent cannot be inspected completely.
    """
    if not file_paths:
        return False, []

    # Build set of resolved file paths for O(1) lookup
    file_paths_set = {path.resolve() for path in file_paths}

    cross_seeded_torrents: list[str] = []

    try:
        # Get all torrents except the one being deleted
        all_torrents = client.torrents_info()

        for torrent in all_torrents:
            torrent_hash = getattr(torrent, "hash", None)
            if not isinstance(torrent_hash, str) or not torrent_hash:
                raise SafetyCheckError("qBittorrent returned a torrent without a valid hash")
            if torrent_hash == exclude_hash:
                continue

            save_path_value = getattr(torrent, "save_path", None)
            torrent_name = getattr(torrent, "name", None)
            if not isinstance(save_path_value, str) or not save_path_value:
                raise SafetyCheckError(f"Torrent {torrent_hash} has no valid save path")
            if not isinstance(torrent_name, str) or not torrent_name:
                raise SafetyCheckError(f"Torrent {torrent_hash} has no valid name")

            torrent_save_path = Path(save_path_value).resolve()
            torrent_files = fetch_torrent_files(client, torrent_hash, cache_scope=id(client))
            for file_info in torrent_files:
                if isinstance(file_info, dict):
                    name = file_info.get("name")
                else:
                    name = getattr(file_info, "name", None)

                if not isinstance(name, str) or not name:
                    raise SafetyCheckError(f"Torrent {torrent_hash} returned malformed file metadata")

                file_path = (torrent_save_path / name).resolve()
                if not file_path.is_relative_to(torrent_save_path):
                    raise SafetyCheckError(f"Torrent {torrent_hash} returned an unsafe file path")
                if file_path in file_paths_set:
                    cross_seeded_torrents.append(torrent_name)
                    logging.warning(
                        "Cross-seeding detected: file '%s' is also used by torrent '%s' (hash: %s)",
                        file_path,
                        torrent_name,
                        torrent_hash,
                    )
                    break

        is_cross_seeded = len(cross_seeded_torrents) > 0
        return is_cross_seeded, cross_seeded_torrents

    except (KeyboardInterrupt, SystemExit, SafetyCheckError):
        raise
    except Exception as error:
        raise SafetyCheckError("Could not complete the cross-seeding ownership scan") from error
