"""Tests for file operations utilities."""

import errno
import os
import pytest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from qbitunregistered.file_operations import (
    SafetyCheckError,
    check_cross_seeding,
    move_files_to_recycle_bin,
    get_torrent_file_paths,
    fetch_torrent_files,
    rollback_recycle_bin_moves,
)
from qbitunregistered.cache import get_cache


class TestCheckCrossSeeding:
    """Test cross-seeding detection."""

    def test_no_cross_seeding_when_no_overlap(self):
        """Test that no cross-seeding is detected when files don't overlap."""
        mock_client = MagicMock()

        # Mock torrent with different files
        mock_torrent = MagicMock()
        mock_torrent.hash = "different123"
        mock_torrent.name = "Different Torrent"
        mock_torrent.save_path = "/data/different"

        mock_file = MagicMock()
        mock_file.name = "different.mkv"

        mock_client.torrents_info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file]

        # Test files to check (different from mock torrent)
        test_files = [Path("/data/test/movie.mkv")]

        is_cross_seeded, torrents = check_cross_seeding(mock_client, test_files, exclude_hash="exclude123")

        assert not is_cross_seeded
        assert len(torrents) == 0

    def test_cross_seeding_detected(self):
        """Test that cross-seeding is detected when files overlap."""
        mock_client = MagicMock()

        # Mock torrent with same file
        mock_torrent = MagicMock()
        mock_torrent.hash = "cross456"
        mock_torrent.name = "Cross-Seeded Torrent"
        mock_torrent.save_path = "/data/movies"

        mock_file = MagicMock()
        mock_file.name = "movie.mkv"

        mock_client.torrents_info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file]

        # Test files that match the mock torrent
        test_files = [Path("/data/movies/movie.mkv")]

        is_cross_seeded, torrents = check_cross_seeding(mock_client, test_files, exclude_hash="exclude123")

        assert is_cross_seeded
        assert len(torrents) == 1
        assert torrents[0] == "Cross-Seeded Torrent"

    def test_excludes_torrent_being_deleted(self):
        """Test that the torrent being deleted is excluded from cross-seed check."""
        mock_client = MagicMock()

        # Mock the same torrent that's being deleted
        mock_torrent = MagicMock()
        mock_torrent.hash = "exclude123"
        mock_torrent.name = "Torrent Being Deleted"
        mock_torrent.save_path = "/data/movies"

        mock_file = MagicMock()
        mock_file.name = "movie.mkv"

        mock_client.torrents_info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file]

        # Test files (same as the torrent being deleted)
        test_files = [Path("/data/movies/movie.mkv")]

        is_cross_seeded, torrents = check_cross_seeding(mock_client, test_files, exclude_hash="exclude123")

        # Should not detect cross-seeding because it's the same torrent
        assert not is_cross_seeded
        assert len(torrents) == 0

    def test_multiple_cross_seeded_torrents(self):
        """Test detection of multiple cross-seeded torrents."""
        mock_client = MagicMock()

        # Create multiple torrents with the same file
        mock_torrent1 = MagicMock()
        mock_torrent1.hash = "cross1"
        mock_torrent1.name = "Cross-Seed 1"
        mock_torrent1.save_path = "/data/movies"

        mock_torrent2 = MagicMock()
        mock_torrent2.hash = "cross2"
        mock_torrent2.name = "Cross-Seed 2"
        mock_torrent2.save_path = "/data/movies"

        mock_file = MagicMock()
        mock_file.name = "movie.mkv"

        mock_client.torrents_info.return_value = [mock_torrent1, mock_torrent2]
        mock_client.torrents_files.return_value = [mock_file]

        # Test files
        test_files = [Path("/data/movies/movie.mkv")]

        is_cross_seeded, torrents = check_cross_seeding(mock_client, test_files, exclude_hash="exclude123")

        assert is_cross_seeded
        assert len(torrents) == 2
        assert "Cross-Seed 1" in torrents
        assert "Cross-Seed 2" in torrents

    def test_empty_file_list(self):
        """Test that empty file list returns no cross-seeding."""
        mock_client = MagicMock()

        is_cross_seeded, torrents = check_cross_seeding(mock_client, [], exclude_hash="exclude123")

        assert not is_cross_seeded
        assert len(torrents) == 0

    def test_error_handling(self):
        """Test that transient ownership errors are distinguishable from no overlap."""
        mock_client = MagicMock()
        # Simulate a transient connection-like error
        mock_client.torrents_info.side_effect = ConnectionError("API Error")

        test_files = [Path("/data/movies/movie.mkv")]

        with pytest.raises(SafetyCheckError):
            check_cross_seeding(mock_client, test_files, exclude_hash="exclude123")

    def test_malformed_peer_fails_closed(self):
        mock_client = MagicMock()
        malformed_peer = MagicMock()
        malformed_peer.hash = "peer"
        malformed_peer.name = "Peer"
        malformed_peer.save_path = None
        mock_client.torrents_info.return_value = [malformed_peer]

        with pytest.raises(SafetyCheckError):
            check_cross_seeding(
                mock_client,
                [Path("/data/movies/movie.mkv")],
                exclude_hash="source",
            )


class TestMoveFilesToRecycleBin:
    """Test moving files to recycle bin."""

    def test_invalid_deletion_type_fallback(self, tmp_path, caplog):
        """Test that invalid deletion type falls back to 'orphaned'."""
        recycle_bin = tmp_path / "recycle_bin"

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        success_count, failed = move_files_to_recycle_bin(
            file_paths=[test_file],
            recycle_bin_path=recycle_bin,
            deletion_type="invalid_type",
            category="test",
            dry_run=False,
        )

        assert success_count == 1
        assert len(failed) == 0
        assert "Invalid deletion_type 'invalid_type'" in caplog.text
        # Should fallback to orphaned
        assert (recycle_bin / "orphaned" / "test").exists()

    def test_category_sanitization(self, tmp_path):
        """Test that category names are sanitized."""
        recycle_bin = tmp_path / "recycle_bin"

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        success_count, failed = move_files_to_recycle_bin(
            file_paths=[test_file],
            recycle_bin_path=recycle_bin,
            deletion_type="orphaned",
            category="test/category with spaces!",
            dry_run=False,
        )

        assert success_count == 1
        # Category should be sanitized (only alphanumeric, dash, underscore)
        sanitized_dirs = list((recycle_bin / "orphaned").iterdir())
        assert len(sanitized_dirs) == 1
        # Should have replaced invalid chars
        assert sanitized_dirs[0].name.replace("_", "").replace("-", "").isalnum()

    def test_repeated_collision_never_overwrites_recycled_files(self, tmp_path):
        recycle_bin = tmp_path / "recycle_bin"
        source = tmp_path / "test.txt"

        with patch("qbitunregistered.file_operations.datetime") as mock_datetime:
            mock_datetime.now.return_value.strftime.return_value = "20260723_120000"
            for contents in ("first", "second", "third"):
                source.write_text(contents)
                success_count, failed = move_files_to_recycle_bin(
                    [source],
                    recycle_bin,
                    "orphaned",
                )
                assert success_count == 1
                assert failed == []

        recycled = sorted(recycle_bin.rglob("test*.txt"))
        assert [path.read_text() for path in recycled] == ["first", "second", "third"]

    def test_all_or_nothing_rolls_back_prior_moves(self, tmp_path):
        from qbitunregistered import file_operations

        recycle_bin = tmp_path / "recycle_bin"
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("first")
        second.write_text("second")
        real_move = file_operations._move_without_overwrite

        def fail_second_move(source, destination):
            if source == second:
                raise OSError("simulated move failure")
            return real_move(source, destination)

        with patch("qbitunregistered.file_operations._move_without_overwrite", side_effect=fail_second_move):
            success_count, failed = move_files_to_recycle_bin(
                [first, second],
                recycle_bin,
                "unregistered",
                all_or_nothing=True,
            )

        assert success_count == 0
        assert len(failed) == 1
        assert first.read_text() == "first"
        assert second.read_text() == "second"
        assert not list(recycle_bin.rglob("*.txt"))

    def test_destination_race_never_overwrites_existing_file(self, tmp_path):
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("source")
        destination.write_text("existing")

        with pytest.raises(FileExistsError):
            _move_without_overwrite(source, destination)

        assert source.read_text() == "source"
        assert destination.read_text() == "existing"

    def test_source_replacement_race_preserves_replacement(self, tmp_path):
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        replacement = tmp_path / "replacement.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original")
        replacement.write_text("replacement")
        real_link = os.link

        def replace_source_after_link(link_source, link_destination, **kwargs):
            real_link(link_source, link_destination, **kwargs)
            os.replace(replacement, source)

        with (
            patch("qbitunregistered.file_operations.os.link", side_effect=replace_source_after_link),
            pytest.raises(SafetyCheckError, match="replacement restored"),
        ):
            _move_without_overwrite(source, destination)

        assert source.read_text() == "replacement"
        assert destination.read_text() == "original"

    def test_source_disappearance_race_preserves_destination(self, tmp_path):
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original")
        real_link = os.link

        def remove_source_after_link(link_source, link_destination, **kwargs):
            real_link(link_source, link_destination, **kwargs)
            source.unlink()

        with (
            patch("qbitunregistered.file_operations.os.link", side_effect=remove_source_after_link),
            pytest.raises(OSError, match="disappeared"),
        ):
            _move_without_overwrite(source, destination)

        assert not source.exists()
        assert destination.read_text() == "original"

    def test_cross_filesystem_source_replacement_preserves_verified_copy(self, tmp_path):
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        replacement = tmp_path / "replacement.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original")
        replacement.write_text("replacement")
        real_link = os.link

        def replace_source_after_copy(_directory):
            if replacement.exists():
                os.replace(replacement, source)

        def force_cross_filesystem_move(link_source, link_destination, **kwargs):
            if Path(link_source) == source and Path(link_destination) == destination:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_link(link_source, link_destination, **kwargs)

        with (
            patch(
                "qbitunregistered.file_operations.os.link",
                side_effect=force_cross_filesystem_move,
            ),
            patch(
                "qbitunregistered.file_operations._fsync_directory",
                side_effect=replace_source_after_copy,
            ),
            pytest.raises(SafetyCheckError, match="replacement restored"),
        ):
            _move_without_overwrite(source, destination)

        assert source.read_text() == "replacement"
        assert destination.read_text() == "original"

    @pytest.mark.parametrize("cross_filesystem", [False, True], ids=["same-fs", "cross-fs"])
    def test_atomic_source_capture_restores_replacement_without_deletion(self, tmp_path, cross_filesystem):
        """A replacement immediately before capture is restored, never unlinked."""
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        replacement = tmp_path / "replacement.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original", encoding="utf-8")
        replacement.write_text("replacement", encoding="utf-8")
        real_link = os.link
        real_rename = os.rename

        def link_or_force_cross_filesystem(link_source, link_destination, **kwargs):
            if cross_filesystem and Path(link_source) == source and Path(link_destination) == destination:
                raise OSError(errno.EXDEV, "cross-device link")
            return real_link(link_source, link_destination, **kwargs)

        def replace_before_capture(rename_source, rename_destination, **kwargs):
            if Path(rename_source) == source and replacement.exists():
                os.replace(replacement, source)
            return real_rename(rename_source, rename_destination, **kwargs)

        with (
            patch("qbitunregistered.file_operations.os.link", side_effect=link_or_force_cross_filesystem),
            patch("qbitunregistered.file_operations.os.rename", side_effect=replace_before_capture),
            pytest.raises(SafetyCheckError, match="replacement restored without deletion"),
        ):
            _move_without_overwrite(source, destination)

        assert source.read_text(encoding="utf-8") == "replacement"
        assert destination.read_text(encoding="utf-8") == "original"
        assert not list(tmp_path.glob(".qbitunregistered-recycle-*"))

    @pytest.mark.parametrize("cross_filesystem", [False, True], ids=["same-fs", "cross-fs"])
    def test_inode_reuse_during_restore_conflict_preserves_destination(self, tmp_path, cross_filesystem):
        """Cleanup rejects a changed source even when its inode appears reused."""
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        replacement = tmp_path / "replacement.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original", encoding="utf-8")
        replacement.write_text("replacement", encoding="utf-8")
        original_stat = source.lstat()
        real_link = os.link
        real_rename = os.rename
        real_lstat = Path.lstat
        simulate_inode_reuse = False
        reused_inode_observed = False

        def link_with_restore_conflict(link_source, link_destination, **kwargs):
            nonlocal simulate_inode_reuse
            if cross_filesystem and Path(link_source) == source and Path(link_destination) == destination:
                raise OSError(errno.EXDEV, "cross-device link")
            if Path(link_destination) == source and Path(link_source).name == "captured":
                source.write_text("newer replacement", encoding="utf-8")
                simulate_inode_reuse = True
            return real_link(link_source, link_destination, **kwargs)

        def replace_before_capture(rename_source, rename_destination, **kwargs):
            if Path(rename_source) == source and replacement.exists():
                os.replace(replacement, source)
            return real_rename(rename_source, rename_destination, **kwargs)

        def lstat_with_reused_inode(path):
            nonlocal reused_inode_observed
            current_stat = real_lstat(path)
            if Path(path) != source or not simulate_inode_reuse:
                return current_stat
            reused_inode_observed = True
            return SimpleNamespace(
                st_dev=original_stat.st_dev,
                st_ino=original_stat.st_ino,
                st_mode=current_stat.st_mode,
                st_size=current_stat.st_size,
                st_mtime_ns=current_stat.st_mtime_ns,
            )

        with (
            patch("qbitunregistered.file_operations.os.link", side_effect=link_with_restore_conflict),
            patch("qbitunregistered.file_operations.os.rename", side_effect=replace_before_capture),
            patch.object(Path, "lstat", autospec=True, side_effect=lstat_with_reused_inode),
            pytest.raises(SafetyCheckError, match="replacement preserved for recovery at"),
        ):
            _move_without_overwrite(source, destination)

        assert reused_inode_observed
        recovery_paths = list(tmp_path.glob(".qbitunregistered-recycle-*/captured"))
        assert len(recovery_paths) == 1
        assert recovery_paths[0].read_text(encoding="utf-8") == "replacement"
        assert source.read_text(encoding="utf-8") == "newer replacement"
        assert destination.read_text(encoding="utf-8") == "original"

    def test_all_or_nothing_rolls_back_prior_move_after_capture_race(self, tmp_path):
        """A later capture race fails the batch and rolls back earlier moves."""
        recycle_bin = tmp_path / "recycle"
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        replacement = tmp_path / "replacement.txt"
        first.write_text("first", encoding="utf-8")
        second.write_text("second original", encoding="utf-8")
        replacement.write_text("second replacement", encoding="utf-8")
        real_rename = os.rename

        def replace_second_before_capture(rename_source, rename_destination, **kwargs):
            if Path(rename_source) == second and replacement.exists():
                os.replace(replacement, second)
            return real_rename(rename_source, rename_destination, **kwargs)

        with patch("qbitunregistered.file_operations.os.rename", side_effect=replace_second_before_capture):
            success_count, failed = move_files_to_recycle_bin(
                [first, second],
                recycle_bin,
                "unregistered",
                all_or_nothing=True,
            )

        assert success_count == 0
        assert len(failed) == 1
        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second replacement"
        recycled_files = list(recycle_bin.rglob("*.txt"))
        assert len(recycled_files) == 1
        assert recycled_files[0].read_text(encoding="utf-8") == "second original"

    def test_staging_creation_failure_preserves_source_and_destination(self, tmp_path):
        """Failure before capture retains both verified filesystem objects."""
        from qbitunregistered import file_operations

        source = tmp_path / "source.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original", encoding="utf-8")

        with (
            patch.object(file_operations.tempfile, "mkdtemp", side_effect=OSError("staging unavailable")),
            pytest.raises(OSError, match="staging unavailable"),
        ):
            file_operations._move_without_overwrite(source, destination)

        assert source.read_text(encoding="utf-8") == "original"
        assert destination.read_text(encoding="utf-8") == "original"
        assert not list(tmp_path.glob(".qbitunregistered-recycle-*"))

    def test_staged_unlink_failure_restores_source_and_cleans_destination(self, tmp_path):
        """Failure removing the captured object restores it before propagating."""
        from qbitunregistered import file_operations

        source = tmp_path / "source.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original", encoding="utf-8")
        real_unlink = Path.unlink
        failed_once = False

        def fail_first_staged_unlink(path, *args, **kwargs):
            nonlocal failed_once
            if path.name == "captured" and not failed_once:
                failed_once = True
                raise OSError("simulated staged unlink failure")
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(Path, "unlink", autospec=True, side_effect=fail_first_staged_unlink),
            pytest.raises(OSError, match="simulated staged unlink failure"),
        ):
            file_operations._move_without_overwrite(source, destination)

        assert source.read_text(encoding="utf-8") == "original"
        assert not destination.exists()
        assert not list(tmp_path.glob(".qbitunregistered-recycle-*"))

    def test_cross_filesystem_metadata_update_uses_open_descriptor(self, tmp_path):
        from qbitunregistered.file_operations import _move_without_overwrite

        source = tmp_path / "source.txt"
        destination = tmp_path / "destination.txt"
        source.write_text("original")

        with patch(
            "qbitunregistered.file_operations.os.link",
            side_effect=OSError(errno.EXDEV, "cross-device link"),
        ):
            _move_without_overwrite(source, destination)

        assert not source.exists()
        assert destination.read_text() == "original"

    @pytest.mark.parametrize("cross_filesystem", [False, True], ids=["same-fs", "cross-fs"])
    def test_post_unlink_fsync_failure_records_move_for_rollback(self, tmp_path, caplog, cross_filesystem):
        """A completed move remains rollback-capable after durability uncertainty."""
        recycle_bin = tmp_path / "recycle"
        source = tmp_path / "source.txt"
        source.write_text("original", encoding="utf-8")
        move_records = []

        def fail_source_parent_fsync(directory):
            if directory == source.parent and not source.exists():
                raise OSError("simulated post-unlink fsync failure")

        with ExitStack() as stack:
            if cross_filesystem:
                stack.enter_context(
                    patch(
                        "qbitunregistered.file_operations.os.link",
                        side_effect=OSError(errno.EXDEV, "cross-device link"),
                    )
                )
            stack.enter_context(
                patch(
                    "qbitunregistered.file_operations._fsync_directory",
                    side_effect=fail_source_parent_fsync,
                )
            )
            success_count, failed = move_files_to_recycle_bin(
                [source],
                recycle_bin,
                "unregistered",
                all_or_nothing=True,
                move_records=move_records,
            )

        assert success_count == 1
        assert failed == []
        assert len(move_records) == 1
        assert not source.exists()
        assert move_records[0].recycled_path.read_text(encoding="utf-8") == "original"
        assert "could not confirm source-directory durability" in caplog.text

        assert rollback_recycle_bin_moves(move_records) == []
        assert source.read_text(encoding="utf-8") == "original"
        assert not move_records[0].recycled_path.exists()

    def test_pre_unlink_fsync_failure_is_not_hidden(self, tmp_path):
        """Durability failure before source unlink remains a failed move."""
        recycle_bin = tmp_path / "recycle"
        source = tmp_path / "source.txt"
        source.write_text("original", encoding="utf-8")
        move_records = []

        with patch(
            "qbitunregistered.file_operations._fsync_directory",
            side_effect=OSError("simulated pre-unlink fsync failure"),
        ):
            success_count, failed = move_files_to_recycle_bin(
                [source],
                recycle_bin,
                "unregistered",
                all_or_nothing=True,
                move_records=move_records,
            )

        assert success_count == 0
        assert len(failed) == 1
        assert source.read_text(encoding="utf-8") == "original"
        assert move_records == []
        assert not list(recycle_bin.rglob("*.txt"))


class TestGetTorrentFilePaths:
    """Test getting torrent file paths."""

    def test_get_file_paths_success(self):
        """Test successful retrieval of torrent file paths."""
        mock_client = MagicMock()

        mock_torrent = MagicMock()
        mock_torrent.save_path = "/data/movies"

        mock_file1 = MagicMock()
        mock_file1.name = "movie.mkv"

        mock_file2 = MagicMock()
        mock_file2.name = "subtitle.srt"

        mock_client.torrents_info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file1, mock_file2]

        # Mock Path.exists() to return True
        from unittest.mock import patch

        with patch.object(Path, "is_file", return_value=True):
            file_paths = get_torrent_file_paths(mock_client, "test_hash")

        assert len(file_paths) == 2
        assert any("movie.mkv" in str(p) for p in file_paths)
        assert any("subtitle.srt" in str(p) for p in file_paths)

    def test_torrent_not_found(self):
        """Test handling when torrent is not found."""
        mock_client = MagicMock()
        mock_client.torrents_info.return_value = []

        with pytest.raises(SafetyCheckError):
            get_torrent_file_paths(mock_client, "nonexistent_hash")

    def test_error_handling(self):
        """Test error handling during file path retrieval."""
        mock_client = MagicMock()
        mock_client.torrents_info.side_effect = Exception("API Error")

        with pytest.raises(SafetyCheckError):
            get_torrent_file_paths(mock_client, "test_hash")


class TestFetchTorrentFiles:
    """Test cached torrent file fetching."""

    def test_fetch_torrent_files_caching(self):
        """Test that fetch_torrent_files caches API calls."""
        mock_client = MagicMock()
        mock_file = MagicMock()
        mock_file.name = "test.mkv"
        mock_client.torrents_files.return_value = [mock_file]

        # Clear cache before test
        get_cache().clear()

        # First call should hit API
        result1 = fetch_torrent_files(mock_client, "test_hash", cache_scope=id(mock_client))
        assert len(result1) == 1
        assert mock_client.torrents_files.call_count == 1

        # Second call with same hash should use cache (no additional API call)
        result2 = fetch_torrent_files(mock_client, "test_hash", cache_scope=id(mock_client))
        assert len(result2) == 1
        assert mock_client.torrents_files.call_count == 1  # Still 1, not 2

        # Third call with different hash should hit API
        result3 = fetch_torrent_files(mock_client, "different_hash", cache_scope=id(mock_client))
        assert len(result3) == 1
        assert mock_client.torrents_files.call_count == 2  # Now 2

    def test_fetch_torrent_files_requires_cache_scope(self):
        """Test that fetch_torrent_files requires cache_scope parameter."""
        mock_client = MagicMock()

        with pytest.raises(AssertionError) as exc_info:
            fetch_torrent_files(mock_client, "test_hash", cache_scope=None)

        assert "cache_scope must be provided" in str(exc_info.value)

    def test_fetch_torrent_files_cache_isolation(self):
        """Test that different clients don't share cache."""
        mock_client1 = MagicMock()
        mock_client2 = MagicMock()

        mock_file1 = MagicMock()
        mock_file1.name = "file1.mkv"
        mock_client1.torrents_files.return_value = [mock_file1]

        mock_file2 = MagicMock()
        mock_file2.name = "file2.mkv"
        mock_client2.torrents_files.return_value = [mock_file2]

        # Clear cache before test
        get_cache().clear()

        # Fetch with client1
        result1 = fetch_torrent_files(mock_client1, "test_hash", cache_scope=id(mock_client1))
        assert result1[0].name == "file1.mkv"

        # Fetch with client2 (different cache scope)
        result2 = fetch_torrent_files(mock_client2, "test_hash", cache_scope=id(mock_client2))
        assert result2[0].name == "file2.mkv"

        # Both clients should have made API calls (no cache sharing)
        assert mock_client1.torrents_files.call_count == 1
        assert mock_client2.torrents_files.call_count == 1

    def test_get_torrent_file_paths_uses_cache(self):
        """Test that get_torrent_file_paths uses cached fetch_torrent_files."""
        mock_client = MagicMock()

        mock_torrent = MagicMock()
        mock_torrent.save_path = "/data/movies"
        mock_client.torrents_info.return_value = [mock_torrent]

        mock_file = MagicMock()
        mock_file.name = "movie.mkv"
        mock_client.torrents_files.return_value = [mock_file]

        # Clear cache before test
        get_cache().clear()

        # First call
        from unittest.mock import patch

        with patch.object(Path, "is_file", return_value=True):
            result1 = get_torrent_file_paths(mock_client, "test_hash")

        # torrents_files should be called once
        assert mock_client.torrents_files.call_count == 1

        # Second call with same hash should use cache
        with patch.object(Path, "is_file", return_value=True):
            result2 = get_torrent_file_paths(mock_client, "test_hash")

        # torrents_files should still be 1 (cached)
        assert mock_client.torrents_files.call_count == 1

    def test_check_cross_seeding_uses_cache(self):
        """Test that check_cross_seeding uses cached fetch_torrent_files."""
        mock_client = MagicMock()

        mock_torrent = MagicMock()
        mock_torrent.hash = "torrent1"
        mock_torrent.name = "Test Torrent"
        mock_torrent.save_path = "/data/movies"

        mock_client.torrents_info.return_value = [mock_torrent]

        mock_file = MagicMock()
        mock_file.name = "movie.mkv"
        mock_client.torrents_files.return_value = [mock_file]

        test_files = [Path("/data/movies/movie.mkv")]

        # Clear cache before test
        get_cache().clear()

        # First call
        is_cross_seeded1, torrents1 = check_cross_seeding(mock_client, test_files, exclude_hash="exclude")

        # torrents_files should be called once
        assert mock_client.torrents_files.call_count == 1

        # Second call with same torrent should use cache
        is_cross_seeded2, torrents2 = check_cross_seeding(mock_client, test_files, exclude_hash="exclude")

        # torrents_files should still be 1 (cached)
        assert mock_client.torrents_files.call_count == 1
