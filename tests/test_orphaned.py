"""Tests for orphaned file checking functionality."""

from pathlib import Path
from fnmatch import fnmatch
from unittest.mock import MagicMock
import pytest
from qbitunregistered.file_operations import SafetyCheckError
from qbitunregistered.operations.orphaned import build_orphan_file_plan, check_files_on_disk, delete_orphaned_files


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

        # Calculate expected destination path with new hybrid structure
        # Files go to: recycle_bin/orphaned/uncategorized/[original_path]
        relative_path = dummy_file.resolve().relative_to(dummy_file.resolve().anchor)
        dest_path = recycle_bin / "orphaned" / "uncategorized" / relative_path

        assert dest_path.exists(), f"Expected file at {dest_path}"
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

    def test_permanent_delete_refuses_file_substituted_after_preview(self, mock_client, tmp_path, caplog):
        """A confirmed orphan identity cannot authorize a replacement file."""
        source = tmp_path / "orphaned.mkv"
        source.write_text("previewed")
        plan = build_orphan_file_plan([str(source)])

        source.unlink()
        source.write_text("replacement")

        delete_orphaned_files(
            [str(source)],
            dry_run=False,
            client=mock_client,
            plan=plan,
        )

        assert source.read_text() == "replacement"
        assert "Planned file changed after preview" in caplog.text

    def test_recycle_refuses_file_substituted_after_preview(self, mock_client, tmp_path, caplog):
        """Recycle execution cannot move a regular file substituted after preview."""
        source = tmp_path / "orphaned.mkv"
        recycle_bin = tmp_path / "recycle"
        source.write_text("previewed")
        plan = build_orphan_file_plan([str(source)])

        source.unlink()
        source.write_text("replacement")

        delete_orphaned_files(
            [str(source)],
            dry_run=False,
            client=mock_client,
            recycle_bin=str(recycle_bin),
            plan=plan,
        )

        assert source.read_text() == "replacement"
        assert list(recycle_bin.rglob("orphaned.mkv")) == []
        assert "Planned file changed after preview" in caplog.text

    def test_ownership_refresh_failure_blocks_every_orphan_mutation(self, mock_client, tmp_path):
        """An unavailable final qBittorrent snapshot aborts the whole plan."""
        first = tmp_path / "first.mkv"
        second = tmp_path / "second.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        plan = build_orphan_file_plan([str(first), str(second)])
        mock_client.torrents.info.side_effect = RuntimeError("temporary API failure")

        with pytest.raises(SafetyCheckError, match="Could not refresh qBittorrent state"):
            delete_orphaned_files(
                [str(first), str(second)],
                dry_run=False,
                client=mock_client,
                torrents=[],
                plan=plan,
            )

        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"

    def test_malformed_final_file_metadata_blocks_every_orphan_mutation(self, mock_client, tmp_path):
        """Incomplete ownership metadata cannot authorize any orphan mutation."""
        first = tmp_path / "first.mkv"
        second = tmp_path / "second.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        plan = build_orphan_file_plan([str(first), str(second)])
        owner = MagicMock(hash="owner", save_path=str(tmp_path))
        mock_client.torrents.info.return_value = [owner]
        mock_client.torrents_files.return_value = [{}]

        with pytest.raises(SafetyCheckError, match="malformed file metadata"):
            delete_orphaned_files(
                [str(first), str(second)],
                dry_run=False,
                client=mock_client,
                torrents=[],
                recycle_bin=str(tmp_path / "recycle"),
                plan=plan,
            )

        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        assert not (tmp_path / "recycle").exists()

    @pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
    def test_symlinked_default_save_root_is_never_pruned(self, mock_client, tmp_path, caplog, dry_run):
        """Canonical default roots protect their real directories."""
        real_save_root = tmp_path / "real-default"
        real_save_root.mkdir()
        configured_save_root = tmp_path / "configured-default"
        configured_save_root.symlink_to(real_save_root, target_is_directory=True)
        orphan = real_save_root / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        mock_client.application.default_save_path = str(configured_save_root)

        delete_orphaned_files(
            [str(orphan)],
            dry_run=dry_run,
            client=mock_client,
        )

        assert real_save_root.is_dir()
        assert configured_save_root.resolve(strict=True) == real_save_root
        assert f"remove empty directory: {real_save_root}" not in caplog.text
        assert orphan.exists() is dry_run

    @pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
    def test_nested_empty_directories_are_pruned_below_active_root(self, mock_client, tmp_path, caplog, dry_run):
        """Queued child removals make their empty parents eligible for pruning."""
        import logging

        caplog.set_level(logging.INFO)
        save_root = tmp_path / "downloads"
        season_dir = save_root / "Show" / "Season 01"
        season_dir.mkdir(parents=True)
        orphan = season_dir / "episode.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        mock_client.application.default_save_path = str(save_root)

        delete_orphaned_files(
            [str(orphan)],
            dry_run=dry_run,
            client=mock_client,
            torrents=[],
        )

        assert save_root.is_dir()
        action = "Would remove" if dry_run else "Deleted"
        assert f"{action} empty directory: {season_dir}" in caplog.messages
        assert f"{action} empty directory: {season_dir.parent}" in caplog.messages
        assert f"{action} empty directory: {save_root}" not in caplog.messages
        assert orphan.exists() is dry_run
        assert season_dir.exists() is dry_run
        assert season_dir.parent.exists() is dry_run

    def test_final_current_torrent_save_root_is_never_pruned(self, mock_client, tmp_path):
        """The uncached current torrent snapshot protects canonical save roots."""
        default_save_root = tmp_path / "default"
        default_save_root.mkdir()
        real_torrent_root = tmp_path / "real-current"
        real_torrent_root.mkdir()
        configured_torrent_root = tmp_path / "configured-current"
        configured_torrent_root.symlink_to(real_torrent_root, target_is_directory=True)
        orphan = real_torrent_root / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        mock_client.application.default_save_path = str(default_save_root)
        current_torrent = MagicMock(hash="current", save_path=str(configured_torrent_root))
        mock_client.torrents.info.return_value = [current_torrent]
        mock_client.torrents_files.return_value = []

        delete_orphaned_files(
            [str(orphan)],
            dry_run=False,
            client=mock_client,
            torrents=[],
        )

        assert not orphan.exists()
        assert real_torrent_root.is_dir()
        assert configured_torrent_root.resolve(strict=True) == real_torrent_root
        mock_client.torrents.info.assert_called_once_with()

    @pytest.mark.parametrize("root_source", ["default", "category"])
    def test_execute_refreshes_configured_save_roots_without_cache(self, mock_client, tmp_path, root_source):
        """Final pruning uses current default and category roots, not preview cache."""
        from qbitunregistered.cache import clear_cache

        clear_cache()
        old_root = tmp_path / "old-root"
        old_root.mkdir()
        default_root = tmp_path / "default"
        default_root.mkdir()
        real_current_root = tmp_path / "real-current"
        real_current_root.mkdir()
        configured_current_root = tmp_path / "configured-current"
        configured_current_root.symlink_to(real_current_root, target_is_directory=True)

        if root_source == "default":
            mock_client.application.default_save_path = str(old_root)
            mock_client.torrent_categories.categories = {}
        else:
            mock_client.application.default_save_path = str(default_root)
            mock_client.torrent_categories.categories = {"movies": {"savePath": str(old_root)}}
        assert check_files_on_disk(mock_client, []) == []

        if root_source == "default":
            mock_client.application.default_save_path = str(configured_current_root)
        else:
            mock_client.torrent_categories.categories = {"movies": {"savePath": str(configured_current_root)}}
        orphan = real_current_root / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")

        delete_orphaned_files(
            [str(orphan)],
            dry_run=False,
            client=mock_client,
            torrents=[],
        )

        assert not orphan.exists()
        assert real_current_root.is_dir()
        assert configured_current_root.resolve(strict=True) == real_current_root

    def test_dry_run_consumes_confirmed_plan_without_mutation(self, mock_client, tmp_path):
        """Dry-run validates and reports the same immutable plan."""
        source = tmp_path / "orphaned.mkv"
        source.write_text("previewed")
        plan = build_orphan_file_plan([str(source)])

        delete_orphaned_files(
            [str(source)],
            dry_run=True,
            client=mock_client,
            plan=plan,
        )

        assert source.read_text() == "previewed"

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
        assert "Would move to recycle bin (orphaned/uncategorized)" in caplog.text

    @pytest.mark.skipif(
        not __import__("sys").platform.startswith("win"),
        reason="Windows-specific path handling cannot be fully tested on non-Windows platforms",
    )
    def test_windows_path_handling(self, mock_client, tmp_path):
        """Test Windows path handling with drive letters.

        This test validates that Windows drive letters (e.g., C:) are correctly
        converted to directory names (e.g., C_) when moving files to the recycle bin.
        Skipped on non-Windows platforms as the behavior cannot be accurately tested.
        """
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        recycle_bin = tmp_path / "recycle_bin"

        # Create a dummy file
        dummy_file = source_dir / "orphaned.mkv"
        dummy_file.write_text("dummy content")

        orphaned_files = [str(dummy_file)]

        # On Windows, run the actual operation and verify drive letter conversion
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=str(recycle_bin))

        # Verify file was moved
        assert not dummy_file.exists(), "Source file should be moved"

        # Check that file exists in recycle bin with drive letter converted
        moved_files = list(recycle_bin.rglob("orphaned.mkv"))
        assert len(moved_files) >= 1, "File should exist in recycle bin"

        # On Windows, the path should contain the drive letter converted to directory
        # e.g., C: -> C_
        dest_path = moved_files[0]
        relative_to_bin = dest_path.relative_to(recycle_bin)
        # Verify the hybrid structure: orphaned/uncategorized/...
        assert "orphaned" in str(relative_to_bin), "Should be in 'orphaned' subdirectory"

    def test_file_collision_with_timestamp(self, mock_client, tmp_path):
        """Test file collision handling with timestamp suffix."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        recycle_bin = tmp_path / "recycle_bin"

        # Create first file
        dummy_file1 = source_dir / "orphaned1.mkv"
        dummy_file1.write_text("content 1")

        # Move first file to recycle bin
        orphaned_files = [str(dummy_file1)]
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=str(recycle_bin))

        # Ensure source directory still exists for second file
        source_dir.mkdir(exist_ok=True)

        # Recreate same file (simulating collision)
        dummy_file2 = source_dir / "orphaned1.mkv"
        dummy_file2.write_text("content 2")

        # Move second file with same name
        orphaned_files = [str(dummy_file2)]
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=str(recycle_bin))

        # Verify both files exist in recycle bin with different names
        relative_path = dummy_file1.resolve().relative_to(dummy_file1.resolve().anchor)
        dest_dir = recycle_bin / "orphaned" / "uncategorized" / relative_path.parent

        # Should have original file and one with timestamp
        files = list(dest_dir.glob("orphaned1*.mkv"))
        assert len(files) >= 2, f"Expected at least 2 files, found {len(files)} in {dest_dir}"

        # Verify both have different content
        contents = {f.read_text() for f in files}
        assert "content 1" in contents
        assert "content 2" in contents

    def test_recycle_bin_preserves_directory_structure(self, mock_client, tmp_path):
        """Test that directory structure is preserved in recycle bin."""
        # Create nested directory structure
        source_dir = tmp_path / "source" / "movies" / "action"
        source_dir.mkdir(parents=True)
        recycle_bin = tmp_path / "recycle_bin"

        # Create file in nested directory
        dummy_file = source_dir / "movie.mkv"
        dummy_file.write_text("movie content")

        orphaned_files = [str(dummy_file)]
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=str(recycle_bin))

        # Verify directory structure is preserved with hybrid structure
        relative_path = dummy_file.resolve().relative_to(dummy_file.resolve().anchor)
        dest_path = recycle_bin / "orphaned" / "uncategorized" / relative_path

        assert dest_path.exists(), f"Expected file at {dest_path}"
        assert dest_path.read_text() == "movie content"

        # Verify parent directories exist
        assert dest_path.parent.exists()
        assert dest_path.parent.parent.exists()

    def test_recycle_bin_cross_platform_compatibility(self, mock_client, tmp_path):
        """Test cross-platform path handling."""
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        recycle_bin = tmp_path / "recycle_bin"

        dummy_file = source_dir / "test.mkv"
        dummy_file.write_text("test")

        orphaned_files = [str(dummy_file)]
        delete_orphaned_files(orphaned_files, dry_run=False, client=mock_client, recycle_bin=str(recycle_bin))

        # Verify file was moved successfully regardless of platform
        assert not dummy_file.exists()

        # File should exist somewhere in recycle bin
        moved_files = list(recycle_bin.rglob("test.mkv"))
        assert len(moved_files) >= 1
        assert moved_files[0].read_text() == "test"
