"""Tests for unregistered checks functionality."""

import pytest
from unittest.mock import MagicMock
from scripts.unregistered_checks import (
    compile_patterns,
    check_unregistered_message,
    process_torrent,
)


class MockTracker:
    """Mock tracker object for testing."""

    def __init__(self, msg, status=4):
        self.msg = msg
        self.status = status


class MockTorrent:
    """Mock torrent object for testing."""

    def __init__(self, trackers):
        self.trackers = trackers


class TestCompilePatterns:
    """Test pattern compilation."""

    def test_compile_exact_patterns(self):
        """Test that exact match patterns are compiled correctly."""
        patterns = ["Unregistered torrent", "Torrent not found"]
        exact, starts_with = compile_patterns(patterns)

        assert "unregistered torrent" in exact
        assert "torrent not found" in exact
        assert len(starts_with) == 0

    def test_compile_starts_with_patterns(self):
        """Test that starts_with patterns are compiled correctly."""
        patterns = ["starts_with:Trump", "starts_with:Error"]
        exact, starts_with = compile_patterns(patterns)

        assert "trump" in starts_with
        assert "error" in starts_with
        assert len(exact) == 0

    def test_compile_mixed_patterns(self):
        """Test mixed pattern types."""
        patterns = ["Unregistered", "starts_with:Error", "Not found"]
        exact, starts_with = compile_patterns(patterns)

        assert "unregistered" in exact
        assert "not found" in exact
        assert "error" in starts_with


class TestCheckUnregisteredMessage:
    """Test unregistered message checking."""

    def test_exact_match(self):
        """Test exact message matching."""
        tracker = MockTracker("Unregistered torrent")
        exact = {"unregistered torrent"}
        starts_with = set()

        assert check_unregistered_message(tracker, exact, starts_with) is True

    def test_exact_match_case_insensitive(self):
        """Test case-insensitive exact matching."""
        tracker = MockTracker("UNREGISTERED TORRENT")
        exact = {"unregistered torrent"}
        starts_with = set()

        assert check_unregistered_message(tracker, exact, starts_with) is True

    def test_starts_with_match(self):
        """Test starts_with pattern matching."""
        tracker = MockTracker("Trump: Access denied")
        exact = set()
        starts_with = {"trump"}

        assert check_unregistered_message(tracker, exact, starts_with) is True

    def test_no_match(self):
        """Test that non-matching messages return False."""
        tracker = MockTracker("Working fine")
        exact = {"unregistered torrent"}
        starts_with = {"error"}

        assert check_unregistered_message(tracker, exact, starts_with) is False

    def test_partial_match_does_not_trigger(self):
        """Test that partial matches don't trigger exact matches."""
        tracker = MockTracker("This torrent is unregistered here")
        exact = {"unregistered"}
        starts_with = set()

        # Should not match because it's not an exact match
        assert check_unregistered_message(tracker, exact, starts_with) is False


class TestProcessTorrent:
    """Test torrent processing."""

    def test_single_unregistered_tracker(self):
        """Test torrent with one unregistered tracker."""
        trackers = [
            MockTracker("Unregistered torrent", status=4),
            MockTracker("Working fine", status=2),
        ]
        torrent = MockTorrent(trackers)

        exact = {"unregistered torrent"}
        starts_with = set()

        count = process_torrent(torrent, exact, starts_with)
        assert count == 1

    def test_multiple_unregistered_trackers(self):
        """Test torrent with multiple unregistered trackers."""
        trackers = [
            MockTracker("Unregistered torrent", status=4),
            MockTracker("Torrent not found", status=4),
            MockTracker("Working fine", status=2),
        ]
        torrent = MockTorrent(trackers)

        exact = {"unregistered torrent", "torrent not found"}
        starts_with = set()

        count = process_torrent(torrent, exact, starts_with)
        assert count == 2

    def test_unregistered_but_wrong_status(self):
        """Test that unregistered messages with wrong status are not counted."""
        trackers = [
            MockTracker("Unregistered torrent", status=2),  # Wrong status
        ]
        torrent = MockTorrent(trackers)

        exact = {"unregistered torrent"}
        starts_with = set()

        count = process_torrent(torrent, exact, starts_with)
        assert count == 0

    def test_no_unregistered_trackers(self):
        """Test torrent with no unregistered trackers."""
        trackers = [
            MockTracker("Working fine", status=2),
            MockTracker("All good", status=2),
        ]
        torrent = MockTorrent(trackers)

        exact = {"unregistered torrent"}
        starts_with = set()

        count = process_torrent(torrent, exact, starts_with)
        assert count == 0


class TestPatternPerformance:
    """Test that pattern compilation improves performance."""

    def test_pattern_compilation_caching(self):
        """Verify that patterns are pre-compiled once."""
        patterns = ["Pattern 1", "Pattern 2", "starts_with:Error"]
        exact1, starts_with1 = compile_patterns(patterns)
        exact2, starts_with2 = compile_patterns(patterns)

        # Results should be identical
        assert exact1 == exact2
        assert starts_with1 == starts_with2


class TestUnregisteredRecycleBin:
    """Test recycle bin functionality for unregistered torrents."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.torrents.delete = MagicMock()
        client.torrents_info = MagicMock(return_value=[])
        client.torrents_files = MagicMock(return_value=[])
        return client

    @pytest.fixture
    def config(self):
        return {
            "default_unregistered_tag": "unregistered",
            "cross_seeding_tag": "unregistered:crossseeding",
            "unregistered": ["unregistered"],
        }

    def test_unregistered_deletion_with_recycle_bin(self, mock_client, config, tmp_path):
        """Test that unregistered torrent files are moved to recycle bin."""
        from scripts.unregistered_checks import delete_torrents_and_files
        from pathlib import Path

        # Create test file structure
        source_dir = tmp_path / "torrents"
        source_dir.mkdir()
        test_file = source_dir / "movie.mkv"
        test_file.write_text("test content")

        recycle_bin = tmp_path / "recycle_bin"

        # Mock torrent
        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.category = "movies"
        mock_torrent.tags = "unregistered"
        mock_torrent.save_path = str(source_dir)

        # Mock torrents_info to return file list
        mock_file = MagicMock()
        mock_file.name = "movie.mkv"
        mock_client.torrents_files.return_value = [mock_file]

        mock_torrent_info = MagicMock()
        mock_torrent_info.save_path = str(source_dir)
        mock_client.torrents_info.return_value = [mock_torrent_info]

        # Run deletion with recycle bin
        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=False,
            torrents=[mock_torrent],
            recycle_bin=str(recycle_bin)
        )

        # Verify torrent was deleted WITHOUT files
        mock_client.torrents.delete.assert_called_once_with("abc123", delete_files=False)

        # Verify file was moved to recycle bin with hybrid structure
        # Should be: recycle_bin/unregistered/movies/[original_path]
        expected_dest = recycle_bin / "unregistered" / "movies"
        assert expected_dest.exists()

        # File should be somewhere in the recycle bin
        moved_files = list(recycle_bin.rglob("movie.mkv"))
        assert len(moved_files) == 1
        assert moved_files[0].read_text() == "test content"
        assert not test_file.exists()

    def test_unregistered_deletion_without_recycle_bin(self, mock_client, config):
        """Test permanent deletion when no recycle bin is configured."""
        from scripts.unregistered_checks import delete_torrents_and_files

        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.tags = "unregistered"

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=False,
            torrents=[mock_torrent],
            recycle_bin=None
        )

        # Verify torrent was deleted WITH files (permanent deletion)
        mock_client.torrents.delete.assert_called_once_with("abc123", delete_files=True)

    def test_unregistered_deletion_dry_run_with_recycle_bin(self, mock_client, config, tmp_path, caplog):
        """Test dry run mode with recycle bin."""
        import logging
        from scripts.unregistered_checks import delete_torrents_and_files

        caplog.set_level(logging.INFO)

        recycle_bin = tmp_path / "recycle_bin"

        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.category = "movies"
        mock_torrent.tags = "unregistered"

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=True,
            torrents=[mock_torrent],
            recycle_bin=str(recycle_bin)
        )

        # Verify nothing was actually deleted
        mock_client.torrents.delete.assert_not_called()

        # Verify dry run log message
        assert "Would move files to recycle bin" in caplog.text

    def test_category_based_organization(self, mock_client, config, tmp_path):
        """Test that files are organized by category in recycle bin."""
        from scripts.unregistered_checks import delete_torrents_and_files
        from pathlib import Path

        # Create test files for different categories
        source_dir = tmp_path / "torrents"
        source_dir.mkdir()

        recycle_bin = tmp_path / "recycle_bin"

        # Test multiple categories
        test_cases = [
            ("movies", "movie.mkv"),
            ("tv", "show.mkv"),
            ("", "other.mkv"),  # Empty category should become "uncategorized"
        ]

        for category, filename in test_cases:
            test_file = source_dir / filename
            test_file.write_text(f"content of {filename}")

            mock_torrent = MagicMock()
            mock_torrent.name = f"Test {filename}"
            mock_torrent.hash = f"hash_{filename}"
            mock_torrent.category = category
            mock_torrent.tags = "unregistered"
            mock_torrent.save_path = str(source_dir)

            mock_file = MagicMock()
            mock_file.name = filename
            mock_client.torrents_files.return_value = [mock_file]

            mock_torrent_info = MagicMock()
            mock_torrent_info.save_path = str(source_dir)
            mock_client.torrents_info.return_value = [mock_torrent_info]

            delete_torrents_and_files(
                client=mock_client,
                config=config,
                use_delete_tags=True,
                delete_tags=["unregistered"],
                delete_files={"unregistered": True},
                dry_run=False,
                torrents=[mock_torrent],
                recycle_bin=str(recycle_bin)
            )

        # Verify directory structure
        expected_dirs = [
            recycle_bin / "unregistered" / "movies",
            recycle_bin / "unregistered" / "tv",
            recycle_bin / "unregistered" / "uncategorized",
        ]

        for expected_dir in expected_dirs:
            assert expected_dir.exists(), f"Expected directory {expected_dir} to exist"

    def test_torrent_without_files_deletion(self, mock_client, config, tmp_path):
        """Test deletion when delete_files is False."""
        from scripts.unregistered_checks import delete_torrents_and_files

        recycle_bin = tmp_path / "recycle_bin"

        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.tags = "unregistered"

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": False},  # Don't delete files
            dry_run=False,
            torrents=[mock_torrent],
            recycle_bin=str(recycle_bin)
        )

        # Verify torrent was deleted without files
        mock_client.torrents.delete.assert_called_once_with("abc123", delete_files=False)

        # Recycle bin should not be used
        assert not recycle_bin.exists()
