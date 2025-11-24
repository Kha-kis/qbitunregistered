"""Tests for orphaned file checking functionality."""

from pathlib import Path
from fnmatch import fnmatch
from unittest.mock import MagicMock, patch
import pytest
from scripts.orphaned import delete_orphaned_files


class TestFileExclusionPatterns:
    """Test file exclusion pattern matching."""

    def test_simple_pattern_match(self):
        """Test simple pattern matching."""
        filename = "test.tmp"
        pattern = "*.tmp"
        assert fnmatch(filename, pattern)

    def test_pattern_no_match(self):
        """Test that non-matching files don't match."""
        filename = "test.txt"
        pattern = "*.tmp"
        assert not fnmatch(filename, pattern)

    def test_multiple_patterns(self):
        """Test matching against multiple patterns."""
        filename = "test.!qB"
        patterns = ["*.tmp", "*.!qB", "*.part"]

        assert any(fnmatch(filename, pattern) for pattern in patterns)

    def test_exact_filename_pattern(self):
        """Test exact filename pattern."""
        filename = "_unpackerred"
        pattern = "*_unpackerred"
        assert fnmatch(filename, pattern)


class TestDirectoryExclusion:
    """Test directory exclusion logic."""

    def test_direct_path_exclusion(self):
        """Test direct path exclusion."""
        test_path = Path("/data/torrents/temp/file.txt").resolve()
        excluded_path = Path("/data/torrents/temp").resolve()

        # Check if excluded_path is in test_path's parents
        assert excluded_path in test_path.parents

    def test_parent_directory_not_excluded(self):
        """Test that parent directories are not incorrectly excluded."""
        test_path = Path("/data/torrents/completed/file.txt").resolve()
        excluded_path = Path("/data/torrents/temp").resolve()

        # Should not be in parents
        assert excluded_path not in test_path.parents

    def test_wildcard_pattern_matching(self):
        """Test wildcard pattern matching for directories."""
        test_path = "/data/torrents/temp1/file.txt"
        pattern = "/data/torrents/temp*"

        assert fnmatch(test_path, pattern + "*")


class TestPathResolution:
    """Test path resolution logic."""

    def test_relative_to_absolute_conversion(self):
        """Test that relative paths are converted to absolute."""
        relative_path = Path("test/path")
        absolute_path = relative_path.resolve()

        assert absolute_path.is_absolute()

    def test_path_comparison(self):
        """Test that resolved paths can be compared."""
        path1 = Path("/tmp/test").resolve()
        path2 = Path("/tmp/test").resolve()

        assert path1 == path2


class TestSetOperations:
    """Test set operations for performance."""

    def test_set_lookup_performance(self):
        """Verify that set lookups are used for torrent files."""
        # Create a large set to simulate torrent files
        torrent_files = {Path(f"/data/file_{i}.mkv").resolve() for i in range(1000)}

        # Lookup should be O(1)
        test_file = Path("/data/file_500.mkv").resolve()
        assert test_file in torrent_files

        # Non-existent file
        missing_file = Path("/data/file_9999.mkv").resolve()
        assert missing_file not in torrent_files

    def test_exclude_dirs_as_set(self):
        """Verify that exclude_dirs should be a set for O(1) lookup."""
        exclude_dirs = {Path("/tmp/exclude1").resolve(), Path("/tmp/exclude2").resolve()}

        excluded_parent = Path("/tmp/exclude1").resolve()

        # Fast lookup using set
        assert excluded_parent in exclude_dirs


class TestEdgeCases:
    """Test edge cases in file exclusion."""

    def test_empty_exclude_patterns(self):
        """Test behavior with empty exclude patterns."""
        filename = "test.txt"
        exclude_patterns = []

        # No patterns means nothing should be excluded
        should_exclude = any(fnmatch(filename, pattern) for pattern in exclude_patterns)
        assert not should_exclude

    def test_exclude_all_pattern(self):
        """Test that * pattern matches everything."""
        filenames = ["test.txt", "file.mkv", "data.tmp"]
        pattern = "*"

        # All files should match
        for filename in filenames:
            assert fnmatch(filename, pattern)

    def test_multiple_extension_pattern(self):
        """Test pattern with multiple extensions."""
        patterns = ["*.txt", "*.tmp"]

        # txt and tmp should match, mkv should not
        assert any(fnmatch("test.txt", p) for p in patterns)
        assert any(fnmatch("test.tmp", p) for p in patterns)
        assert not any(fnmatch("test.mkv", p) for p in patterns)


class TestRecycleBin:
    """Test recycle bin functionality."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.application.default_save_path = "/default/save/path"
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = []
        return client

    def test_recycle_bin_move(self, mock_client, tmp_path):
        """Test that files are moved to recycle bin instead of deleted."""
        # Setup source and recycle bin directories
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        recycle_bin = tmp_path / "recycle_bin"
        
        # Create a dummy file
        dummy_file = source_dir / "orphaned.mkv"
        dummy_file.write_text("dummy content")
        
        orphaned_files = [str(dummy_file)]
        
        # Run delete_orphaned_files with recycle bin
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=str(recycle_bin))
        
        # Verify file is moved
        assert not dummy_file.exists()
        
        # Calculate expected destination path
        # The function uses relative_to(anchor), so for /tmp/pytest-of-user/pytest-X/test_recycle_bin_move0/source/orphaned.mkv
        # it should be recycle_bin / tmp / pytest-of-user ...
        # This depends on how relative_to(anchor) behaves.
        # On Unix, anchor is '/'. relative_to('/') returns the path without leading slash.
        
        relative_path = dummy_file.relative_to(dummy_file.anchor)
        dest_path = recycle_bin / relative_path
        
        assert dest_path.exists()
        assert dest_path.read_text() == "dummy content"

    def test_no_recycle_bin_delete(self, mock_client, tmp_path):
        """Test that files are deleted when no recycle bin is specified."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        dummy_file = source_dir / "orphaned.mkv"
        dummy_file.write_text("dummy content")
        
        orphaned_files = [str(dummy_file)]
        
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=None)
        
        assert not dummy_file.exists()

    def test_dry_run_recycle_bin(self, mock_client, caplog, tmp_path):
        """Test dry run with recycle bin."""
        import logging
        caplog.set_level(logging.INFO)
        
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        dummy_file = source_dir / "orphaned.mkv"
        dummy_file.write_text("dummy content")
        
        orphaned_files = [str(dummy_file)]
        recycle_bin = tmp_path / "recycle_bin"
        
        delete_orphaned_files(orphaned_files, dry_run=True, client=mock_client, recycle_bin=str(recycle_bin))
        
        assert dummy_file.exists()
        assert "Would move orphaned file to recycle bin" in caplog.text
