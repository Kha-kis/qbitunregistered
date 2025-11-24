"""Tests for file operations utilities."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock
from utils.file_operations import check_cross_seeding, move_files_to_recycle_bin, get_torrent_file_paths


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
        """Test that errors during check don't crash and return safe default."""
        mock_client = MagicMock()
        mock_client.torrents_info.side_effect = Exception("API Error")

        test_files = [Path("/data/movies/movie.mkv")]

        # Should not raise exception and return safe default (not cross-seeded)
        is_cross_seeded, torrents = check_cross_seeding(mock_client, test_files, exclude_hash="exclude123")

        assert not is_cross_seeded
        assert len(torrents) == 0


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

        with patch.object(Path, "exists", return_value=True):
            file_paths = get_torrent_file_paths(mock_client, "test_hash")

        assert len(file_paths) == 2
        assert any("movie.mkv" in str(p) for p in file_paths)
        assert any("subtitle.srt" in str(p) for p in file_paths)

    def test_torrent_not_found(self):
        """Test handling when torrent is not found."""
        mock_client = MagicMock()
        mock_client.torrents_info.return_value = []

        file_paths = get_torrent_file_paths(mock_client, "nonexistent_hash")

        assert len(file_paths) == 0

    def test_error_handling(self):
        """Test error handling during file path retrieval."""
        mock_client = MagicMock()
        mock_client.torrents_info.side_effect = Exception("API Error")

        file_paths = get_torrent_file_paths(mock_client, "test_hash")

        assert len(file_paths) == 0
