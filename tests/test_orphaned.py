"""Tests for orphaned file checking functionality."""

from pathlib import Path
from fnmatch import fnmatch
from unittest.mock import MagicMock, patch
import pytest
from qbitunregistered.file_operations import (
    RECYCLE_STAGING_DIRECTORY_PREFIX,
    SafetyCheckError,
    capture_file_identity,
    fetch_torrent_files,
)
from qbitunregistered.operations.orphaned import (
    OrphanFilePlan,
    build_orphan_file_plan,
    check_files_on_disk,
    delete_orphaned_files,
)


def _expected_recycled_path(recycle_bin: Path, source: Path) -> Path:
    """Return the documented recycle destination for a source path."""
    resolved_source = source.resolve()
    relative_path = resolved_source.relative_to(resolved_source.anchor)
    if resolved_source.drive:
        relative_path = Path(resolved_source.drive.replace(":", "_")) / relative_path
    return recycle_bin / "orphaned" / "uncategorized" / relative_path


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


class TestManagedScanRoots:
    """Test the explicit traversal-authority boundary for orphan discovery."""

    @staticmethod
    def _client(default_root: Path, categories: dict | None = None) -> MagicMock:
        client = MagicMock()
        client.application.default_save_path = str(default_root)
        client.torrent_categories.categories = categories or {}
        client.torrents.info.return_value = []
        return client

    def test_default_root_is_scanned_and_outside_paths_are_untouched(self, tmp_path):
        default_root = tmp_path / "default"
        outside_root = tmp_path / "outside"
        default_root.mkdir()
        outside_root.mkdir()
        managed_orphan = default_root / "managed.mkv"
        outside_file = outside_root / "outside.mkv"
        managed_orphan.write_text("managed", encoding="utf-8")
        outside_file.write_text("outside", encoding="utf-8")
        client = self._client(default_root)

        assert check_files_on_disk(client, []) == [str(managed_orphan)]
        assert outside_file.read_text(encoding="utf-8") == "outside"

    def test_explicit_roots_are_additive_to_default_root(self, tmp_path):
        default_root = tmp_path / "default"
        explicit_root = tmp_path / "explicit"
        default_root.mkdir()
        explicit_root.mkdir()
        default_orphan = default_root / "default.mkv"
        explicit_orphan = explicit_root / "explicit.mkv"
        default_orphan.write_text("default", encoding="utf-8")
        explicit_orphan.write_text("explicit", encoding="utf-8")
        client = self._client(default_root)

        found = check_files_on_disk(client, [], orphan_scan_roots=[str(explicit_root)])

        assert set(found) == {str(default_orphan), str(explicit_orphan)}

    def test_category_root_is_scanned(self, tmp_path):
        default_root = tmp_path / "default"
        category_root = tmp_path / "category"
        default_root.mkdir()
        category_root.mkdir()
        category_orphan = category_root / "category.mkv"
        category_orphan.write_text("category", encoding="utf-8")
        client = self._client(default_root, {"movies": {"savePath": str(category_root)}})

        assert check_files_on_disk(client, []) == [str(category_orphan)]

    def test_torrent_save_path_outside_managed_roots_does_not_expand_traversal(self, tmp_path):
        default_root = tmp_path / "default"
        torrent_root = tmp_path / "torrent-save"
        default_root.mkdir()
        torrent_root.mkdir()
        managed_orphan = default_root / "managed.mkv"
        registered = torrent_root / "registered.mkv"
        outside_unregistered = torrent_root / "unregistered.mkv"
        managed_orphan.write_text("managed", encoding="utf-8")
        registered.write_text("registered", encoding="utf-8")
        outside_unregistered.write_text("outside", encoding="utf-8")
        client = self._client(default_root)
        torrent = MagicMock(hash="outside-owner", save_path=str(torrent_root))
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": registered.name}]

        assert check_files_on_disk(client, [torrent]) == [str(managed_orphan)]
        assert outside_unregistered.read_text(encoding="utf-8") == "outside"

    def test_active_qbittorrent_path_under_explicit_root_is_protected(self, tmp_path):
        default_root = tmp_path / "default"
        explicit_root = tmp_path / "explicit"
        default_root.mkdir()
        explicit_root.mkdir()
        registered = explicit_root / "registered.mkv"
        registered.write_text("registered", encoding="utf-8")
        client = self._client(default_root)
        torrent = MagicMock(hash="explicit-owner", save_path=str(explicit_root))
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": registered.name}]

        assert check_files_on_disk(client, [torrent], orphan_scan_roots=[str(explicit_root)]) == []

    def test_unregistered_hardlink_alias_inside_managed_root_remains_candidate(self, tmp_path):
        default_root = tmp_path / "default"
        explicit_root = tmp_path / "explicit"
        torrent_root = tmp_path / "torrent-save"
        default_root.mkdir()
        explicit_root.mkdir()
        torrent_root.mkdir()
        registered = torrent_root / "registered.mkv"
        alias = explicit_root / "alias.mkv"
        registered.write_text("shared inode", encoding="utf-8")
        alias.hardlink_to(registered)
        client = self._client(default_root)
        torrent = MagicMock(hash="outside-owner", save_path=str(torrent_root))
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": registered.name}]

        assert registered.stat().st_ino == alias.stat().st_ino
        assert check_files_on_disk(client, [torrent], orphan_scan_roots=[str(explicit_root)]) == [str(alias)]

    def test_existing_explicit_root_must_be_a_directory(self, tmp_path):
        default_root = tmp_path / "default"
        invalid_root = tmp_path / "not-a-directory"
        default_root.mkdir()
        invalid_root.write_text("file", encoding="utf-8")
        client = self._client(default_root)

        with pytest.raises(SafetyCheckError, match="not a directory"):
            check_files_on_disk(client, [], orphan_scan_roots=[str(invalid_root)])


class TestOrphanScanReconciliation:
    """Regression tests for long-running scan ownership changes."""

    @pytest.fixture(autouse=True)
    def reset_metadata_cache(self):
        from qbitunregistered.cache import clear_cache

        clear_cache()
        yield
        clear_cache()

    @staticmethod
    def _client(save_root: Path) -> MagicMock:
        client = MagicMock()
        client.application.default_save_path = str(save_root)
        client.torrent_categories.categories = {}
        return client

    def test_scan_refreshes_canonical_file_metadata_instead_of_using_embedded_metadata(self, tmp_path):
        """Each scan refreshes the shared metadata and ignores embedded files."""
        tracked = tmp_path / "tracked.mkv"
        tracked.write_text("tracked", encoding="utf-8")
        client = self._client(tmp_path)
        torrent = MagicMock(hash="existing", save_path=str(tmp_path), files=[])
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": tracked.name}]

        assert check_files_on_disk(client, [torrent]) == []
        assert check_files_on_disk(client, [torrent]) == []

        assert client.torrents_files.call_count == 2
        client.torrents_files.assert_called_with("existing")

    def test_torrent_added_during_scan_removes_its_files_from_candidates(self, tmp_path):
        """A newly added owner is reconciled before an orphan plan is returned."""
        claimed = tmp_path / "claimed.mkv"
        unclaimed = tmp_path / "unclaimed.mkv"
        claimed.write_text("claimed", encoding="utf-8")
        unclaimed.write_text("unclaimed", encoding="utf-8")
        client = self._client(tmp_path)
        new_owner = MagicMock(hash="new-owner", save_path=str(tmp_path))
        client.torrents.info.return_value = [new_owner]
        client.torrents_files.return_value = [{"name": claimed.name}]

        assert check_files_on_disk(client, []) == [str(unclaimed)]
        client.torrents_files.assert_called_once_with("new-owner")

    def test_removed_torrent_becomes_a_candidate_without_stale_metadata_fetch(self, tmp_path):
        """A torrent absent after the walk no longer contributes ownership."""
        formerly_owned = tmp_path / "formerly-owned.mkv"
        formerly_owned.write_text("data", encoding="utf-8")
        client = self._client(tmp_path)
        removed_owner = MagicMock(hash="removed", save_path=str(tmp_path))
        client.torrents.info.return_value = []
        client.torrents_files.return_value = [{"name": formerly_owned.name}]

        assert check_files_on_disk(client, [removed_owner]) == [str(formerly_owned)]
        client.torrents_files.assert_not_called()

    def test_metadata_failure_is_ignored_only_after_confirmed_removal(self, tmp_path):
        """A per-torrent API failure is safe only when a refresh proves removal."""
        formerly_owned = tmp_path / "formerly-owned.mkv"
        formerly_owned.write_text("data", encoding="utf-8")
        client = self._client(tmp_path)
        initial_owner = MagicMock(hash="removed", save_path=str(tmp_path))
        current_owner = MagicMock(hash="removed", save_path=str(tmp_path))
        client.torrents.info.side_effect = [[current_owner], []]
        client.torrents_files.side_effect = RuntimeError("torrent disappeared")

        assert check_files_on_disk(client, [initial_owner]) == [str(formerly_owned)]
        assert client.torrents.info.call_count == 2

    def test_current_file_rename_is_reconciled_after_the_walk(self, tmp_path):
        """Post-walk metadata replaces stale cached rename information."""
        original = tmp_path / "original.mkv"
        renamed = tmp_path / "renamed.mkv"
        renamed.write_text("data", encoding="utf-8")
        client = self._client(tmp_path)
        torrent = MagicMock(hash="same-hash", save_path=str(tmp_path))
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": original.name}]
        assert fetch_torrent_files(client, torrent.hash, cache_scope=id(client)) == [{"name": original.name}]
        real_rglob = Path.rglob

        def rename_during_walk(path, pattern):
            client.torrents_files.return_value = [{"name": renamed.name}]
            return real_rglob(path, pattern)

        with patch.object(Path, "rglob", autospec=True, side_effect=rename_during_walk):
            assert check_files_on_disk(client, [torrent]) == []

        assert fetch_torrent_files(client, torrent.hash, cache_scope=id(client)) == [{"name": renamed.name}]
        assert client.torrents_files.call_count == 2
        client.torrents_files.assert_called_with("same-hash")

    def test_same_hash_save_path_change_recomputes_current_ownership(self, tmp_path):
        """Refreshed save paths relocate cached info-hash file metadata safely."""
        old_root = tmp_path / "old"
        new_root = tmp_path / "new"
        old_root.mkdir()
        new_root.mkdir()
        claimed = new_root / "claimed.mkv"
        claimed.write_text("claimed", encoding="utf-8")
        client = self._client(tmp_path)
        torrent = MagicMock(hash="same-hash", save_path=str(old_root))
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": claimed.name}]
        real_rglob = Path.rglob

        def change_save_path_during_walk(path, pattern):
            torrent.save_path = str(new_root)
            return real_rglob(path, pattern)

        with patch.object(Path, "rglob", autospec=True, side_effect=change_save_path_during_walk):
            assert check_files_on_disk(client, [torrent]) == []

        client.torrents_files.assert_called_once_with("same-hash")

    def test_torrent_readded_with_same_hash_uses_current_mapping_in_dry_run(self, tmp_path):
        """A re-added hash protects its current path with one post-walk fetch."""
        old_root = tmp_path / "old"
        new_root = tmp_path / "new"
        old_root.mkdir()
        new_root.mkdir()
        claimed = new_root / "claimed.mkv"
        true_orphan = tmp_path / "orphan.mkv"
        claimed.write_text("claimed", encoding="utf-8")
        true_orphan.write_text("orphan", encoding="utf-8")
        client = self._client(tmp_path)
        initial_owner = MagicMock(
            hash="same-hash",
            save_path=str(old_root),
            files=[MagicMock(name="previous-local-rename.mkv")],
        )
        readded_owner = MagicMock(hash="same-hash", save_path=str(new_root))
        client.torrents.info.return_value = [readded_owner]
        client.torrents_files.return_value = [{"name": claimed.name}]

        orphaned_files = check_files_on_disk(client, [initial_owner])
        plan = build_orphan_file_plan(orphaned_files)
        delete_orphaned_files(
            orphaned_files,
            dry_run=True,
            client=client,
            torrents=[readded_owner],
            plan=plan,
        )

        assert plan.paths == (true_orphan.resolve(),)
        assert claimed.read_text(encoding="utf-8") == "claimed"
        assert true_orphan.read_text(encoding="utf-8") == "orphan"
        client.torrents_files.assert_called_once_with("same-hash")
        client.torrents_delete.assert_not_called()

    def test_active_torrent_metadata_failure_aborts_scan(self, tmp_path):
        """Generic metadata failures cannot make an active file deletable."""
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("data", encoding="utf-8")
        client = self._client(tmp_path)
        active_owner = MagicMock(hash="active", save_path=str(tmp_path))
        client.torrents.info.return_value = [active_owner]
        client.torrents_files.side_effect = RuntimeError("temporary API failure")

        with pytest.raises(SafetyCheckError, match="active torrent active"):
            check_files_on_disk(client, [active_owner])

        assert candidate.read_text(encoding="utf-8") == "data"

    def test_new_owner_metadata_failure_aborts_reconciliation(self, tmp_path):
        """An uninspectable concurrent addition blocks every orphan target."""
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("data", encoding="utf-8")
        client = self._client(tmp_path)
        new_owner = MagicMock(hash="new-owner", save_path=str(tmp_path))
        client.torrents.info.side_effect = [[new_owner], [new_owner]]
        client.torrents_files.side_effect = RuntimeError("temporary API failure")

        with pytest.raises(SafetyCheckError, match="active torrent new-owner"):
            check_files_on_disk(client, [])

        assert candidate.read_text(encoding="utf-8") == "data"


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
    def mock_client(self, tmp_path):
        client = MagicMock()
        client.application.default_save_path = str(tmp_path)
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

        dest_path = _expected_recycled_path(recycle_bin, dummy_file)

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

        with pytest.raises(SafetyCheckError, match="0 of 1 planned files were deleted"):
            delete_orphaned_files(
                [str(source)],
                dry_run=False,
                client=mock_client,
                plan=plan,
            )

        assert source.read_text() == "replacement"
        assert "Planned file changed after preview" in caplog.text
        assert "Successfully deleted" not in caplog.text

    def test_recycle_refuses_file_substituted_after_preview(self, mock_client, tmp_path, caplog):
        """Recycle execution cannot move a regular file substituted after preview."""
        source = tmp_path / "orphaned.mkv"
        recycle_bin = tmp_path / "recycle"
        source.write_text("previewed")
        plan = build_orphan_file_plan([str(source)])

        source.unlink()
        source.write_text("replacement")

        with pytest.raises(SafetyCheckError, match="0 of 1 planned files were moved to the recycle bin"):
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
        assert "Successfully moved to recycle bin" not in caplog.text

    @pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
    def test_missing_confirmed_orphan_surfaces_incomplete_cleanup(self, mock_client, tmp_path, use_recycle_bin):
        """A missing preview target is an operation failure in either mode."""
        source = tmp_path / "orphaned.mkv"
        source.write_text("previewed", encoding="utf-8")
        plan = build_orphan_file_plan([str(source)])
        source.unlink()
        recycle_bin = tmp_path / "recycle" if use_recycle_bin else None

        with pytest.raises(SafetyCheckError, match="0 of 1 planned files"):
            delete_orphaned_files(
                [str(source)],
                dry_run=False,
                client=mock_client,
                recycle_bin=str(recycle_bin) if recycle_bin else None,
                plan=plan,
            )

        assert not source.exists()
        assert recycle_bin is None or not recycle_bin.exists()

    def test_permanent_partial_failure_reports_counts_without_success(self, mock_client, tmp_path, caplog):
        """An unlink failure reports a partial permanent cleanup accurately."""
        first = tmp_path / "first.mkv"
        second = tmp_path / "second.mkv"
        third = tmp_path / "third.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        third.write_text("third", encoding="utf-8")
        plan = build_orphan_file_plan([str(first), str(second), str(third)])
        real_unlink = Path.unlink

        def fail_second_unlink(path, *args, **kwargs):
            if path == second:
                raise OSError("simulated unlink failure")
            return real_unlink(path, *args, **kwargs)

        with (
            patch.object(Path, "unlink", autospec=True, side_effect=fail_second_unlink),
            pytest.raises(SafetyCheckError, match="1 of 3 planned files were deleted; 2 remain"),
        ):
            delete_orphaned_files(
                [str(first), str(second), str(third)],
                dry_run=False,
                client=mock_client,
                plan=plan,
            )

        assert not first.exists()
        assert second.read_text(encoding="utf-8") == "second"
        assert third.read_text(encoding="utf-8") == "third"
        assert "Successfully deleted" not in caplog.text

    def test_recycle_partial_failure_rolls_back_and_surfaces_failure(self, mock_client, tmp_path, caplog):
        """A later recycle failure restores earlier moves and fails the operation."""
        from qbitunregistered import file_operations

        first = tmp_path / "first.mkv"
        second = tmp_path / "second.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        recycle_bin = tmp_path / "recycle"
        plan = build_orphan_file_plan([str(first), str(second)])
        real_move_batch = file_operations.move_files_to_recycle_bin
        real_move_one = file_operations._move_without_overwrite
        second_path = second.resolve()
        failed_second = False

        def move_in_path_order(*args, **kwargs):
            kwargs["file_paths"] = sorted(kwargs["file_paths"])
            return real_move_batch(*args, **kwargs)

        def fail_second_move(source, destination, *, expected_identity=None):
            nonlocal failed_second
            if source == second_path and not failed_second:
                failed_second = True
                raise OSError("simulated recycle move failure")
            return real_move_one(source, destination, expected_identity=expected_identity)

        with (
            patch(
                "qbitunregistered.operations.orphaned.move_files_to_recycle_bin",
                side_effect=move_in_path_order,
            ) as move_batch,
            patch(
                "qbitunregistered.file_operations._move_without_overwrite",
                side_effect=fail_second_move,
            ),
            pytest.raises(SafetyCheckError, match="0 of 2 planned files were moved to the recycle bin"),
        ):
            delete_orphaned_files(
                [str(first), str(second)],
                dry_run=False,
                client=mock_client,
                recycle_bin=str(recycle_bin),
                plan=plan,
            )

        assert move_batch.call_args.kwargs["all_or_nothing"] is True
        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        assert list(recycle_bin.rglob("*.mkv")) == []
        assert "Successfully moved to recycle bin" not in caplog.text

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

    @pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
    def test_current_default_root_change_blocks_all_mutation(
        self,
        mock_client,
        tmp_path,
        use_recycle_bin,
    ):
        """A stale discovered root cannot authorize confirmed file mutation."""
        old_root = tmp_path / "old-default"
        new_root = tmp_path / "new-default"
        old_root.mkdir()
        new_root.mkdir()
        first = old_root / "first.mkv"
        second = old_root / "second.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        mock_client.application.default_save_path = str(old_root)
        mock_client.torrent_categories.categories = {}
        mock_client.torrents.info.return_value = []
        orphaned_files = check_files_on_disk(mock_client, [])
        plan = build_orphan_file_plan(orphaned_files)
        mock_client.application.default_save_path = str(new_root)
        recycle_bin = tmp_path / "recycle" if use_recycle_bin else None

        with pytest.raises(SafetyCheckError, match="0 of 2 planned files"):
            delete_orphaned_files(
                orphaned_files,
                dry_run=False,
                client=mock_client,
                recycle_bin=str(recycle_bin) if recycle_bin else None,
                plan=plan,
            )

        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        assert recycle_bin is None or not recycle_bin.exists()

    def test_removed_category_root_blocks_all_mutation(self, mock_client, tmp_path):
        """Removing a category revokes its independent traversal authority."""
        default_root = tmp_path / "default"
        category_root = tmp_path / "category"
        default_root.mkdir()
        category_root.mkdir()
        orphan = category_root / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        mock_client.application.default_save_path = str(default_root)
        mock_client.torrent_categories.categories = {"movies": {"savePath": str(category_root)}}
        mock_client.torrents.info.return_value = []
        orphaned_files = check_files_on_disk(mock_client, [])
        plan = build_orphan_file_plan(orphaned_files)
        mock_client.torrent_categories.categories = {}

        with pytest.raises(SafetyCheckError, match="0 of 1 planned files"):
            delete_orphaned_files(
                orphaned_files,
                dry_run=False,
                client=mock_client,
                plan=plan,
            )

        assert orphan.read_text(encoding="utf-8") == "orphan"

    def test_explicit_root_remains_authorized_when_default_root_changes(self, mock_client, tmp_path):
        """Stable explicit authority survives unrelated qB root changes."""
        old_default = tmp_path / "old-default"
        new_default = tmp_path / "new-default"
        explicit_root = tmp_path / "explicit"
        old_default.mkdir()
        new_default.mkdir()
        explicit_root.mkdir()
        orphan = explicit_root / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        mock_client.application.default_save_path = str(old_default)
        mock_client.torrent_categories.categories = {}
        mock_client.torrents.info.return_value = []
        orphaned_files = check_files_on_disk(
            mock_client,
            [],
            orphan_scan_roots=[str(explicit_root)],
        )
        plan = build_orphan_file_plan(orphaned_files)
        mock_client.application.default_save_path = str(new_default)

        delete_orphaned_files(
            orphaned_files,
            dry_run=False,
            client=mock_client,
            plan=plan,
            orphan_scan_roots=[str(explicit_root)],
        )

        assert not orphan.exists()
        assert explicit_root.is_dir()

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

    @pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
    def test_explicit_scan_root_is_never_pruned(self, mock_client, tmp_path, caplog, dry_run):
        """An operator-authorized scan root remains a pruning boundary."""
        default_root = tmp_path / "default"
        explicit_root = tmp_path / "explicit"
        content_dir = explicit_root / "nested"
        default_root.mkdir()
        content_dir.mkdir(parents=True)
        orphan = content_dir / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        mock_client.application.default_save_path = str(default_root)

        delete_orphaned_files(
            [str(orphan)],
            dry_run=dry_run,
            client=mock_client,
            torrents=[],
            orphan_scan_roots=[str(explicit_root)],
        )

        assert explicit_root.is_dir()
        action = "Would remove" if dry_run else "Deleted"
        assert f"{action} empty directory: {explicit_root}" not in caplog.messages
        assert orphan.exists() is dry_run
        assert content_dir.exists() is dry_run

    @pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
    def test_internal_recovery_path_is_excluded_from_scan_and_pruning(
        self,
        mock_client,
        tmp_path,
        caplog,
        dry_run,
    ):
        """Preserved staging data under an active root is never orphaned."""
        from qbitunregistered.cache import clear_cache

        clear_cache()
        save_root = tmp_path / "downloads"
        content_dir = save_root / "Show"
        content_dir.mkdir(parents=True)
        orphan = content_dir / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        recovery_directory = content_dir / f"{RECYCLE_STAGING_DIRECTORY_PREFIX}recovery"
        recovery_directory.mkdir()
        captured = recovery_directory / "captured"
        captured.write_text("preserved replacement", encoding="utf-8")
        mock_client.application.default_save_path = str(save_root)
        mock_client.torrent_categories.categories = {}
        mock_client.torrents.info.return_value = []

        orphaned_files = check_files_on_disk(mock_client, [])
        assert orphaned_files == [str(orphan)]

        delete_orphaned_files(
            orphaned_files,
            dry_run=dry_run,
            client=mock_client,
            torrents=[],
        )

        assert captured.read_text(encoding="utf-8") == "preserved replacement"
        assert recovery_directory.is_dir()
        assert content_dir.is_dir()
        assert orphan.exists() is dry_run
        assert all(str(recovery_directory) not in message for message in caplog.messages if "empty directory" in message)

    @pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
    def test_supplied_plan_cannot_delete_internal_recovery_path(self, mock_client, tmp_path, dry_run):
        """The execution boundary rejects internal paths from caller plans."""
        save_root = tmp_path / "downloads"
        recovery_directory = save_root / f"{RECYCLE_STAGING_DIRECTORY_PREFIX}recovery"
        recovery_directory.mkdir(parents=True)
        captured = recovery_directory / "captured"
        captured.write_text("preserved replacement", encoding="utf-8")
        mock_client.application.default_save_path = str(save_root)
        assert build_orphan_file_plan([str(captured)]).files == ()
        plan = OrphanFilePlan(files=(capture_file_identity(captured),))

        with pytest.raises(SafetyCheckError, match="0 of 1 planned files"):
            delete_orphaned_files(
                [str(captured)],
                dry_run=dry_run,
                client=mock_client,
                torrents=[],
                plan=plan,
            )

        assert captured.read_text(encoding="utf-8") == "preserved replacement"
        assert recovery_directory.is_dir()

    def test_final_current_torrent_save_root_is_never_pruned(self, mock_client, tmp_path):
        """The uncached current torrent snapshot protects canonical save roots."""
        default_save_root = tmp_path / "default"
        default_save_root.mkdir()
        real_torrent_root = default_save_root / "real-current"
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
        dest_dir = _expected_recycled_path(recycle_bin, dummy_file1).parent

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
        dest_path = _expected_recycled_path(recycle_bin, dummy_file)

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
