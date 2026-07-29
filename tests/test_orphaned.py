"""Tests for orphaned file checking functionality."""

import logging
import os
import stat
from pathlib import Path, PureWindowsPath
from fnmatch import fnmatch
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
import pytest
from qbitunregistered.file_operations import (
    RECYCLE_STAGING_DIRECTORY_PREFIX,
    SafetyCheckError,
    capture_file_identity,
    fetch_torrent_files,
)
from qbitunregistered.operations.orphaned import (
    OrphanFilePlan,
    OrphanScanResult,
    _candidate_directory_boundaries,
    _capture_current_orphan_identity,
    _exact_torrent_owned_paths,
    _potential_empty_directories,
    _validated_content_boundary,
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


class TestOrphanFilePlanCapture:
    """Exercise live filesystem churn while capturing immutable plans."""

    def test_candidate_confirmed_missing_before_capture_is_omitted(self, tmp_path, caplog):
        missing = tmp_path / "removed-after-scan.mkv"
        caplog.set_level(logging.INFO)

        plan = build_orphan_file_plan([str(missing)])

        assert plan.files == ()
        assert "disappeared before plan capture" in caplog.text

    def test_existing_non_regular_candidate_still_fails_closed(self, tmp_path):
        directory = tmp_path / "not-a-file"
        directory.mkdir()

        with pytest.raises(SafetyCheckError, match="Could not inspect file safely"):
            build_orphan_file_plan([str(directory)])

    def test_inaccessible_candidate_still_fails_closed(self, tmp_path):
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("data", encoding="utf-8")

        with (
            patch.object(Path, "lstat", side_effect=PermissionError("access denied")),
            pytest.raises(SafetyCheckError, match="Could not inspect orphan candidate safely"),
        ):
            build_orphan_file_plan([str(candidate)])

    def test_replaced_candidate_during_capture_still_fails_closed(self, tmp_path):
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("original", encoding="utf-8")
        replacement = tmp_path / "replacement.mkv"
        replacement.write_text("replacement", encoding="utf-8")

        def replace_before_capture(path):
            os.replace(replacement, path)
            return capture_file_identity(path)

        with (
            patch(
                "qbitunregistered.operations.orphaned.capture_file_identity",
                side_effect=replace_before_capture,
            ),
            pytest.raises(SafetyCheckError, match="Orphan candidate changed during identity capture"),
        ):
            build_orphan_file_plan([str(candidate)])

    def test_replacement_after_discovery_cannot_enter_plan(self, tmp_path):
        """Discovery identity prevents a stable pre-plan replacement."""
        from qbitunregistered.cache import clear_cache

        clear_cache()
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("discovered", encoding="utf-8")
        client = MagicMock()
        client.application.default_save_path = str(tmp_path)
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = []
        orphaned_files = check_files_on_disk(client, [])
        replacement = tmp_path / "replacement.mkv"
        replacement.write_text("replacement", encoding="utf-8")
        os.replace(replacement, candidate)

        with pytest.raises(SafetyCheckError, match="changed after discovery"):
            build_orphan_file_plan(orphaned_files)

        assert candidate.read_text(encoding="utf-8") == "replacement"


class TestOrphanDiscoveryCanonicalization:
    """Keep canonical identity safety without redundant default resolution."""

    @staticmethod
    def _client(save_root: Path) -> MagicMock:
        client = MagicMock()
        client.application.default_save_path = str(save_root)
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = []
        return client

    def test_default_scan_strictly_resolves_candidate_once(self, tmp_path):
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("orphan", encoding="utf-8")
        client = self._client(tmp_path)
        real_resolve = Path.resolve
        candidate_resolutions: list[bool] = []

        def track_resolve(path, strict=False):
            if path == candidate:
                candidate_resolutions.append(strict)
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            orphaned_files = check_files_on_disk(client, [])

        assert isinstance(orphaned_files, OrphanScanResult)
        assert orphaned_files == [str(candidate)]
        identity = orphaned_files.discovered_identity(str(candidate))
        assert identity is not None
        assert identity.path == candidate
        assert candidate_resolutions == [True]

    def test_directory_exclusions_still_use_canonical_paths(self, tmp_path):
        excluded_dir = tmp_path / "excluded"
        excluded_dir.mkdir()
        excluded = excluded_dir / "excluded.mkv"
        included = tmp_path / "included.mkv"
        excluded.write_text("excluded", encoding="utf-8")
        included.write_text("included", encoding="utf-8")
        client = self._client(tmp_path)
        real_resolve = Path.resolve
        resolved_entries: list[tuple[Path, bool]] = []

        def track_resolve(path, strict=False):
            if path in {excluded, included}:
                resolved_entries.append((path, strict))
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            orphaned_files = check_files_on_disk(client, [], exclude_dirs=[str(excluded_dir)])

        assert orphaned_files == [str(included)]
        assert (excluded, False) in resolved_entries
        assert (included, False) in resolved_entries
        assert (included, True) in resolved_entries

    def test_symlink_candidate_keeps_canonical_discovery_and_direct_rejection(self, tmp_path):
        scan_root = tmp_path / "downloads"
        outside_root = tmp_path / "library"
        scan_root.mkdir()
        outside_root.mkdir()
        target = outside_root / "target.mkv"
        target.write_text("target", encoding="utf-8")
        link = scan_root / "link.mkv"
        link.symlink_to(target)
        client = self._client(scan_root)

        with pytest.raises(SafetyCheckError, match="Could not inspect file safely"):
            capture_file_identity(link)
        with pytest.raises(SafetyCheckError, match="Could not inspect file safely"):
            _capture_current_orphan_identity(link, missing_log_message="missing: %s")
        orphaned_files = check_files_on_disk(client, [])

        assert isinstance(orphaned_files, OrphanScanResult)
        assert orphaned_files == [str(link)]
        identity = orphaned_files.discovered_identity(str(link))
        assert identity is not None
        assert identity.path == target
        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "target"

    def test_plain_list_cleanup_cannot_delete_direct_symlink_target(self, tmp_path):
        target = tmp_path / "target.mkv"
        target.write_text("preserve", encoding="utf-8")
        link = tmp_path / "link.mkv"
        link.symlink_to(target)
        client = self._client(tmp_path)

        with pytest.raises(SafetyCheckError, match="Could not inspect file safely"):
            delete_orphaned_files(
                [str(link)],
                dry_run=False,
                client=client,
            )

        assert link.is_symlink()
        assert target.read_text(encoding="utf-8") == "preserve"
        client.torrents.info.assert_not_called()

    def test_replacement_during_discovery_still_fails_closed(self, tmp_path):
        candidate = tmp_path / "candidate.mkv"
        replacement = tmp_path / "replacement.mkv"
        candidate.write_text("original", encoding="utf-8")
        replacement.write_text("replacement", encoding="utf-8")
        client = self._client(tmp_path)

        def replace_before_capture(path):
            os.replace(replacement, path)
            return capture_file_identity(path)

        with (
            patch(
                "qbitunregistered.operations.orphaned.capture_file_identity",
                side_effect=replace_before_capture,
            ),
            pytest.raises(SafetyCheckError, match="changed during identity capture"),
        ):
            check_files_on_disk(client, [])

        assert candidate.read_text(encoding="utf-8") == "replacement"


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


class TestCandidateDirectoryBoundaries:
    """Verify parent expansion remains exact and within cleanup authority."""

    def test_multiple_candidates_with_shared_parents_have_exact_union(self, tmp_path):
        shared_root = tmp_path / "downloads"
        candidates = {
            shared_root / "show" / "season-01" / "episode-01.mkv",
            shared_root / "show" / "season-01" / "episode-02.mkv",
            shared_root / "show" / "season-02" / "episode-03.mkv",
            shared_root / "movie.mkv",
        }
        for candidate in candidates:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("candidate", encoding="utf-8")
        expected: set[Path] = set()
        for candidate in candidates:
            expected.add(candidate.parent)
            expected.update(candidate.parent.parents)

        boundaries = _candidate_directory_boundaries(candidates)

        assert boundaries == expected
        assert boundaries.isdisjoint(candidates)

    def test_nested_candidate_roots_deduplicate_shared_ancestors(self, tmp_path):
        outer_root = (tmp_path / "downloads").resolve()
        nested_root = outer_root / "series"
        outer_candidate = outer_root / "movie.mkv"
        nested_candidate = nested_root / "show" / "episode.mkv"
        nested_candidate.parent.mkdir(parents=True)
        outer_candidate.write_text("outer", encoding="utf-8")
        nested_candidate.write_text("nested", encoding="utf-8")

        boundaries = _candidate_directory_boundaries(
            {outer_candidate, nested_candidate}
        )

        expected = {
            outer_candidate.parent,
            nested_candidate.parent,
            *outer_candidate.parent.parents,
            *nested_candidate.parent.parents,
        }
        assert boundaries == expected
        assert outer_root in boundaries
        assert nested_root in boundaries

    def test_candidate_directly_under_save_root_indexes_root_not_file(self, tmp_path):
        save_root = (tmp_path / "downloads").resolve()
        save_root.mkdir()
        candidate = save_root / "movie.mkv"
        candidate.write_text("candidate", encoding="utf-8")

        boundaries = _candidate_directory_boundaries({candidate})

        assert save_root in boundaries
        assert candidate not in boundaries
        assert boundaries == {save_root, *save_root.parents}

    def test_empty_candidates_have_no_directory_boundaries(self):
        assert _candidate_directory_boundaries(set()) == set()

    def test_hardlink_candidates_index_both_directory_paths(self, tmp_path):
        first_dir = (tmp_path / "first").resolve()
        second_dir = (tmp_path / "second").resolve()
        first_dir.mkdir()
        second_dir.mkdir()
        original = first_dir / "movie.mkv"
        alias = second_dir / "movie.mkv"
        original.write_text("candidate", encoding="utf-8")
        alias.hardlink_to(original)

        boundaries = _candidate_directory_boundaries({original, alias})

        assert first_dir in boundaries
        assert second_dir in boundaries
        assert original not in boundaries
        assert alias not in boundaries
        assert original.stat().st_ino == alias.stat().st_ino

    def test_potential_empty_directories_stops_at_active_authorized_root(self, tmp_path):
        authorized_root = tmp_path / "downloads"
        active_torrent_root = authorized_root / "show"
        candidate = active_torrent_root / "season-01" / "episode.mkv"

        potential_dirs = _potential_empty_directories(
            {candidate},
            authorized_roots={authorized_root},
            active_save_paths={authorized_root, active_torrent_root},
        )

        assert potential_dirs == {candidate.parent}
        assert active_torrent_root not in potential_dirs
        assert authorized_root not in potential_dirs
        assert tmp_path not in potential_dirs
        assert tmp_path.parent not in potential_dirs

    def test_potential_empty_directories_ignores_candidate_outside_authority(self, tmp_path):
        authorized_root = tmp_path / "downloads"
        outside_candidate = tmp_path / "library" / "orphan.mkv"

        assert (
            _potential_empty_directories(
                {outside_candidate},
                authorized_roots={authorized_root},
                active_save_paths={authorized_root},
            )
            == set()
        )

    def test_cleanup_never_inspects_authorized_root_or_its_ancestors(self, tmp_path):
        authorized_root = tmp_path / "downloads"
        candidate_dir = authorized_root / "show" / "season-01"
        candidate_dir.mkdir(parents=True)
        candidate = candidate_dir / "episode.mkv"
        candidate.write_text("orphan", encoding="utf-8")
        client = MagicMock()
        client.application.default_save_path = str(authorized_root)
        client.torrent_categories.categories = {}
        inspected_directories: list[Path] = []
        real_iterdir = Path.iterdir

        def track_iterdir(path):
            inspected_directories.append(path)
            return real_iterdir(path)

        with patch.object(Path, "iterdir", autospec=True, side_effect=track_iterdir):
            delete_orphaned_files(
                [str(candidate)],
                dry_run=True,
                client=client,
                torrents=[],
            )

        assert inspected_directories
        assert all(path.is_relative_to(authorized_root) for path in inspected_directories)
        assert authorized_root not in inspected_directories
        assert tmp_path not in inspected_directories


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
        client.torrents_files.assert_called_with("existing", SIMPLE_RESPONSES=True)

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
        client.torrents_files.assert_called_once_with("new-owner", SIMPLE_RESPONSES=True)

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
        client.torrents_files.assert_called_with("same-hash", SIMPLE_RESPONSES=True)

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

        client.torrents_files.assert_called_once_with("same-hash", SIMPLE_RESPONSES=True)

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
        client.torrents_files.assert_called_once_with("same-hash", SIMPLE_RESPONSES=True)
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


class TestValidatedContentBoundary:
    """Exercise lexical and filesystem safety checks for bulk ownership."""

    @pytest.mark.parametrize("content_kind", ["file", "directory"])
    def test_returns_canonical_regular_boundary_after_final_resolution(
        self, tmp_path, content_kind
    ):
        save_root = tmp_path.resolve()
        content_path = save_root / "content"
        if content_kind == "file":
            content_path.write_text("owned", encoding="utf-8")
        else:
            content_path.mkdir()
        torrent = SimpleNamespace(content_path=str(content_path))
        real_resolve = Path.resolve
        resolved_paths: list[Path] = []

        def track_resolve(path: Path, strict: bool = False) -> Path:
            resolved_paths.append(path)
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            boundary = _validated_content_boundary(torrent, save_root)

        assert boundary is not None
        canonical_path, content_mode = boundary
        assert canonical_path == content_path
        assert (
            stat.S_ISREG(content_mode)
            if content_kind == "file"
            else stat.S_ISDIR(content_mode)
        )
        assert resolved_paths == [content_path]

    @pytest.mark.parametrize("symlink_kind", ["root", "component"])
    def test_symlink_boundary_falls_back_to_exact_metadata(
        self, tmp_path, symlink_kind
    ):
        real_root = tmp_path / "real"
        real_root.mkdir()
        owned = real_root / "owned.mkv"
        owned.write_text("owned", encoding="utf-8")
        if symlink_kind == "root":
            save_root = tmp_path / "save-link"
            save_root.symlink_to(real_root, target_is_directory=True)
            content_path = save_root / owned.name
        else:
            save_root = tmp_path
            parent_link = save_root / "parent-link"
            parent_link.symlink_to(real_root, target_is_directory=True)
            content_path = parent_link / owned.name

        assert (
            _validated_content_boundary(
                SimpleNamespace(content_path=str(content_path)), save_root
            )
            is None
        )

    @pytest.mark.parametrize("reparse_location", ["root", "component"])
    def test_reparse_boundary_falls_back_to_exact_metadata(
        self, tmp_path, reparse_location
    ):
        save_root = tmp_path.resolve()
        content_path = save_root / "owned.mkv"
        content_path.write_text("owned", encoding="utf-8")
        root_stat = save_root.lstat()
        component_stat = content_path.lstat()
        reparse_stat = SimpleNamespace(
            st_mode=(
                root_stat.st_mode
                if reparse_location == "root"
                else component_stat.st_mode
            ),
            st_reparse_tag=1,
        )
        lstat_results = (
            [reparse_stat] if reparse_location == "root" else [root_stat, reparse_stat]
        )

        with (
            patch.object(Path, "lstat", autospec=True, side_effect=lstat_results),
            patch.object(Path, "resolve", autospec=True) as resolve,
        ):
            boundary = _validated_content_boundary(
                SimpleNamespace(content_path=str(content_path)), save_root
            )

        assert boundary is None
        resolve.assert_not_called()

    def test_parent_swap_after_lstat_returns_canonical_boundary(self, tmp_path):
        save_root = tmp_path.resolve()
        inspected_parent = save_root / "inspected"
        inspected_content = inspected_parent / "content"
        inspected_content.mkdir(parents=True)
        canonical_parent = save_root / "canonical"
        canonical_content = canonical_parent / "content"
        canonical_content.mkdir(parents=True)
        candidate = canonical_content / "owned.mkv"
        candidate.write_text("owned", encoding="utf-8")
        displaced_parent = save_root / "displaced"
        real_lstat = Path.lstat
        swapped = False

        def swap_parent_after_lstat(path: Path) -> os.stat_result:
            nonlocal swapped
            inspected_stat = real_lstat(path)
            if path == inspected_content and not swapped:
                inspected_parent.rename(displaced_parent)
                inspected_parent.symlink_to(canonical_parent, target_is_directory=True)
                swapped = True
            return inspected_stat

        with patch.object(
            Path, "lstat", autospec=True, side_effect=swap_parent_after_lstat
        ):
            boundary = _validated_content_boundary(
                SimpleNamespace(content_path=str(inspected_content)), save_root
            )

        assert swapped
        assert boundary is not None
        canonical_boundary, content_mode = boundary
        assert canonical_boundary == canonical_content
        assert stat.S_ISDIR(content_mode)
        assert canonical_boundary in _candidate_directory_boundaries({candidate})

    def test_outside_boundary_falls_back_before_filesystem_inspection(self, tmp_path):
        save_root = (tmp_path / "save").resolve()
        save_root.mkdir()
        outside = tmp_path / "outside.mkv"
        outside.write_text("outside", encoding="utf-8")

        with patch.object(Path, "lstat", autospec=True) as lstat:
            boundary = _validated_content_boundary(
                SimpleNamespace(content_path=str(outside)), save_root
            )

        assert boundary is None
        lstat.assert_not_called()

    def test_parent_traversal_boundary_falls_back(self, tmp_path):
        save_root = tmp_path.resolve()
        content_path = save_root / "nested" / ".." / "owned.mkv"

        assert (
            _validated_content_boundary(
                SimpleNamespace(content_path=str(content_path)), save_root
            )
            is None
        )

    @pytest.mark.parametrize("failure", ["missing", "os-error"])
    def test_uninspectable_boundary_falls_back_to_exact_metadata(
        self, tmp_path, failure
    ):
        save_root = tmp_path.resolve()
        content_path = save_root / "owned.mkv"
        if failure == "os-error":
            content_path.write_text("owned", encoding="utf-8")
            root_stat = save_root.lstat()
            lstat_patch = patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=[root_stat, PermissionError("denied")],
            )
        else:
            lstat_patch = patch.object(Path, "resolve", autospec=True)

        with lstat_patch:
            boundary = _validated_content_boundary(
                SimpleNamespace(content_path=str(content_path)), save_root
            )

        assert boundary is None

    def test_hardlink_returns_the_inspected_alias(self, tmp_path):
        save_root = tmp_path.resolve()
        original = save_root / "original.mkv"
        alias = save_root / "alias.mkv"
        original.write_text("owned", encoding="utf-8")
        alias.hardlink_to(original)

        boundary = _validated_content_boundary(
            SimpleNamespace(content_path=str(alias)), save_root
        )

        assert boundary is not None
        inspected_path, content_mode = boundary
        assert inspected_path == alias
        assert stat.S_ISREG(content_mode)
        assert inspected_path.stat().st_ino == original.stat().st_ino

    def test_windows_casefolded_relative_path_reconstructs_canonical_spelling(self):
        canonical_root = PureWindowsPath("C:/Library")
        reported_path = PureWindowsPath("c:/library/Movie/owned.mkv")

        relative_path = reported_path.relative_to(canonical_root)
        reconstructed_path = canonical_root / relative_path

        assert str(relative_path) == r"Movie\owned.mkv"
        assert str(reconstructed_path) == r"C:\Library\Movie\owned.mkv"

    @pytest.mark.parametrize(
        ("save_root", "content_path"),
        [
            (PureWindowsPath("C:/save"), PureWindowsPath("D:/save/owned.mkv")),
            (
                PureWindowsPath(r"\\server\share\save"),
                PureWindowsPath(r"\\other\share\save\owned.mkv"),
            ),
        ],
    )
    def test_windows_different_anchor_cannot_be_relative_to_save_root(
        self, save_root, content_path
    ):
        with pytest.raises(ValueError):
            content_path.relative_to(save_root)


class TestOrphanOwnershipFastPath:
    """Exercise exact bulk ownership shortcuts and conservative fallbacks."""

    @staticmethod
    def _client(save_root: Path, torrents: list[SimpleNamespace]) -> MagicMock:
        client = MagicMock()
        client.application.default_save_path = str(save_root)
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = torrents
        return client

    def test_bulk_single_file_snapshot_avoids_per_torrent_requests(self, tmp_path):
        torrents = []
        for index in range(250):
            owned_file = tmp_path / f"owned-{index}.mkv"
            owned_file.write_text("owned", encoding="utf-8")
            torrents.append(
                SimpleNamespace(
                    hash=f"hash-{index}",
                    save_path=str(tmp_path),
                    content_path=str(owned_file),
                )
            )
        orphan = tmp_path / "orphan.mkv"
        orphan.write_text("orphan", encoding="utf-8")
        client = self._client(tmp_path, torrents)

        assert check_files_on_disk(client, torrents) == [str(orphan)]
        client.torrents_files.assert_not_called()

    @pytest.mark.parametrize("fast_path", ["single", "disjoint-directory"])
    def test_fast_path_invalidates_pre_scan_file_metadata(self, tmp_path, fast_path):
        old_mapping = [{"name": "old-name.mkv"}]
        new_mapping = [{"name": "new-name.mkv"}]
        if fast_path == "single":
            content_path = tmp_path / "owned.mkv"
            content_path.write_text("owned", encoding="utf-8")
        else:
            content_path = tmp_path / "empty-bundle"
            content_path.mkdir()
        orphan = tmp_path / "orphan.nfo"
        orphan.write_text("orphan", encoding="utf-8")
        torrent = SimpleNamespace(
            hash=fast_path,
            save_path=str(tmp_path),
            content_path=str(content_path),
        )
        client = self._client(tmp_path, [torrent])
        client.torrents_files.return_value = old_mapping
        assert fetch_torrent_files(client, torrent.hash, cache_scope=id(client)) == old_mapping
        client.torrents_files.return_value = new_mapping

        check_files_on_disk(client, [torrent])

        assert fetch_torrent_files(client, torrent.hash, cache_scope=id(client)) == new_mapping
        assert client.torrents_files.call_count == 2

    def test_multi_file_boundary_fetches_exact_paths(self, tmp_path):
        content_dir = tmp_path / "bundle"
        content_dir.mkdir()
        owned = content_dir / "owned.mkv"
        orphan = content_dir / "orphan.nfo"
        owned.write_text("owned", encoding="utf-8")
        orphan.write_text("orphan", encoding="utf-8")
        torrent = SimpleNamespace(
            hash="multi",
            save_path=str(tmp_path),
            content_path=str(content_dir),
        )
        client = self._client(tmp_path, [torrent])
        client.torrents_files.return_value = [{"name": "bundle/owned.mkv"}]

        assert check_files_on_disk(client, [torrent]) == [str(orphan)]
        client.torrents_files.assert_called_once_with("multi", SIMPLE_RESPONSES=True)

    def test_disjoint_directory_boundary_skips_exact_metadata(self, tmp_path):
        content_dir = tmp_path / "disjoint-bundle"
        content_dir.mkdir()
        orphan = tmp_path / "orphan.nfo"
        orphan.write_text("orphan", encoding="utf-8")
        torrent = SimpleNamespace(
            hash="disjoint-directory",
            save_path=str(tmp_path),
            content_path=str(content_dir),
        )
        client = self._client(tmp_path, [torrent])

        assert check_files_on_disk(client, [torrent]) == [str(orphan)]
        client.torrents_files.assert_not_called()

    @pytest.mark.parametrize("content_path", ["relative/file.mkv", "missing.mkv"])
    def test_uncertain_bulk_content_path_falls_back_to_exact_metadata(self, tmp_path, content_path):
        owned = tmp_path / "owned.mkv"
        owned.write_text("owned", encoding="utf-8")
        if content_path == "missing.mkv":
            content_path = str(tmp_path / content_path)
        torrent = SimpleNamespace(
            hash="uncertain",
            save_path=str(tmp_path),
            content_path=content_path,
        )
        client = self._client(tmp_path, [torrent])
        client.torrents_files.return_value = [{"name": owned.name}]

        assert check_files_on_disk(client, [torrent]) == []
        client.torrents_files.assert_called_once_with("uncertain", SIMPLE_RESPONSES=True)

    @pytest.mark.parametrize("symlink_kind", ["file", "parent"])
    def test_symlinked_bulk_content_path_never_bypasses_exact_metadata(self, tmp_path, symlink_kind):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        owned = real_dir / "owned.mkv"
        owned.write_text("owned", encoding="utf-8")
        if symlink_kind == "file":
            content_path = tmp_path / "owned-link.mkv"
            content_path.symlink_to(owned)
        else:
            parent_link = tmp_path / "parent-link"
            parent_link.symlink_to(real_dir, target_is_directory=True)
            content_path = parent_link / owned.name
        torrent = SimpleNamespace(
            hash=f"symlink-{symlink_kind}",
            save_path=str(tmp_path),
            content_path=str(content_path),
        )
        client = self._client(tmp_path, [torrent])
        client.torrents_files.return_value = [{"name": "real/owned.mkv"}]

        check_files_on_disk(client, [torrent])

        client.torrents_files.assert_called_once_with(f"symlink-{symlink_kind}", SIMPLE_RESPONSES=True)

    def test_symlink_boundary_final_validation_preserves_exact_owner(self, tmp_path):
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("preserve", encoding="utf-8")
        content_alias = tmp_path / "content-link.mkv"
        content_alias.symlink_to(candidate)
        torrent = SimpleNamespace(
            hash="symlink-final",
            save_path=str(tmp_path),
            content_path=str(content_alias),
        )
        client = self._client(tmp_path, [torrent])
        client.torrents_files.return_value = [{"name": candidate.name}]

        with pytest.raises(SafetyCheckError, match="now owned by qBittorrent"):
            delete_orphaned_files(
                [str(candidate)],
                dry_run=False,
                client=client,
                plan=build_orphan_file_plan([str(candidate)]),
            )

        assert candidate.read_text(encoding="utf-8") == "preserve"
        client.torrents_files.assert_called_once_with(
            "symlink-final", SIMPLE_RESPONSES=True
        )
        client.torrents_delete.assert_not_called()

    def test_parent_swap_final_validation_preserves_exact_owner(self, tmp_path):
        save_root = tmp_path.resolve()
        inspected_parent = save_root / "inspected"
        inspected_content = inspected_parent / "content"
        inspected_content.mkdir(parents=True)
        canonical_parent = save_root / "canonical"
        canonical_content = canonical_parent / "content"
        canonical_content.mkdir(parents=True)
        candidate = canonical_content / "candidate.mkv"
        candidate.write_text("preserve", encoding="utf-8")
        displaced_parent = save_root / "displaced"
        torrent = SimpleNamespace(
            hash="parent-swap-final",
            save_path=str(save_root),
            content_path=str(inspected_content),
        )
        client = self._client(save_root, [torrent])
        client.torrents_files.return_value = [
            {"name": candidate.relative_to(save_root).as_posix()}
        ]
        plan = build_orphan_file_plan([str(candidate)])
        real_lstat = Path.lstat
        swapped = False

        def swap_parent_after_lstat(path: Path) -> os.stat_result:
            nonlocal swapped
            inspected_stat = real_lstat(path)
            if path == inspected_content and not swapped:
                inspected_parent.rename(displaced_parent)
                inspected_parent.symlink_to(canonical_parent, target_is_directory=True)
                swapped = True
            return inspected_stat

        with (
            patch.object(
                Path,
                "lstat",
                autospec=True,
                side_effect=swap_parent_after_lstat,
            ),
            pytest.raises(SafetyCheckError, match="now owned by qBittorrent"),
        ):
            delete_orphaned_files(
                [str(candidate)],
                dry_run=False,
                client=client,
                plan=plan,
            )

        assert swapped
        assert candidate.read_text(encoding="utf-8") == "preserve"
        client.torrents_files.assert_called_once_with(
            "parent-swap-final", SIMPLE_RESPONSES=True
        )
        client.torrents_delete.assert_not_called()

    def test_final_validation_only_fetches_overlapping_multi_file_boundary(self, tmp_path):
        overlap_dir = tmp_path / "overlap"
        disjoint_dir = tmp_path / "disjoint"
        overlap_dir.mkdir()
        disjoint_dir.mkdir()
        orphan = overlap_dir / "orphan.nfo"
        orphan.write_text("orphan", encoding="utf-8")
        overlap = SimpleNamespace(hash="overlap", save_path=str(tmp_path), content_path=str(overlap_dir))
        disjoint = SimpleNamespace(hash="disjoint", save_path=str(tmp_path), content_path=str(disjoint_dir))
        client = self._client(tmp_path, [overlap, disjoint])
        client.torrents_files.return_value = []

        delete_orphaned_files(
            [str(orphan)],
            dry_run=False,
            client=client,
            plan=build_orphan_file_plan([str(orphan)]),
        )

        assert not orphan.exists()
        assert client.torrents_files.call_args_list == [call("overlap", SIMPLE_RESPONSES=True)]


class TestExactOwnershipCandidateFastPath:
    """Resolve metadata unless an exact canonical candidate proves the path."""

    @pytest.fixture(autouse=True)
    def reset_metadata_cache(self):
        from qbitunregistered.cache import clear_cache

        clear_cache()
        yield
        clear_cache()

    @staticmethod
    def _owned_paths(
        client: MagicMock,
        torrent: SimpleNamespace,
        save_root: Path,
        candidate_paths: set[Path],
    ) -> set[Path]:
        return _exact_torrent_owned_paths(
            client,
            torrent,
            {str(save_root): save_root},
            candidate_paths,
            context="in candidate fast-path test",
        )

    def test_exact_relative_candidate_skips_resolution(self, tmp_path):
        save_root = tmp_path.resolve()
        owned = save_root / "bundle" / "owned.mkv"
        owned.parent.mkdir()
        owned.write_text("owned", encoding="utf-8")
        torrent = SimpleNamespace(hash="exact-relative", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": "bundle/owned.mkv"}]

        with (
            patch.object(Path, "resolve", autospec=True) as resolve,
            patch.object(Path, "is_relative_to", autospec=True) as is_relative_to,
        ):
            owned_paths = self._owned_paths(client, torrent, save_root, {owned})

        assert owned_paths == {owned}
        resolve.assert_not_called()
        is_relative_to.assert_not_called()

    def test_windows_rooted_relative_metadata_is_anchored(self):
        metadata_path = PureWindowsPath(r"\outside\owned.mkv")

        assert not metadata_path.is_absolute()
        assert metadata_path.anchor == "\\"
        assert PureWindowsPath("C:/save") / metadata_path == PureWindowsPath("C:/outside/owned.mkv")

    @pytest.mark.skipif(os.name != "nt", reason="requires native Windows path semantics")
    def test_windows_rooted_relative_metadata_cannot_claim_candidate_from_other_root(self, tmp_path):
        save_root = tmp_path / "save"
        candidate_root = tmp_path / "other"
        save_root.mkdir()
        candidate_root.mkdir()
        candidate = candidate_root / "owned.mkv"
        candidate.write_text("preserve", encoding="utf-8")
        rooted_metadata_name = str(candidate)[len(candidate.drive) :]
        metadata_path = Path(rooted_metadata_name)
        lexical_path = save_root / metadata_path
        torrent = SimpleNamespace(hash="windows-rooted-relative", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": rooted_metadata_name}]
        real_resolve = Path.resolve
        real_is_relative_to = Path.is_relative_to
        resolved_paths: list[Path] = []
        containment_checks: list[tuple[Path, Path]] = []

        def track_resolve(path, strict=False):
            resolved_paths.append(path)
            return real_resolve(path, strict=strict)

        def track_is_relative_to(path, other):
            containment_checks.append((path, other))
            return real_is_relative_to(path, other)

        assert not metadata_path.is_absolute()
        assert metadata_path.anchor
        assert lexical_path == candidate

        with (
            patch.object(Path, "resolve", autospec=True, side_effect=track_resolve),
            patch.object(Path, "is_relative_to", autospec=True, side_effect=track_is_relative_to),
            pytest.raises(SafetyCheckError, match="unsafe file path"),
        ):
            self._owned_paths(client, torrent, save_root, {candidate})

        assert lexical_path in resolved_paths
        assert (candidate, save_root) in containment_checks
        assert candidate.read_text(encoding="utf-8") == "preserve"

    @pytest.mark.parametrize("symlink_kind", ["direct", "parent"])
    def test_symlink_alias_uses_resolution_fallback(self, tmp_path, symlink_kind):
        save_root = tmp_path.resolve()
        real_dir = save_root / "real"
        real_dir.mkdir()
        owned = real_dir / "owned.mkv"
        owned.write_text("owned", encoding="utf-8")
        if symlink_kind == "direct":
            alias = save_root / "owned-link.mkv"
            alias.symlink_to(owned)
            metadata_name = alias.name
            lexical_path = alias
        else:
            alias = save_root / "parent-link"
            alias.symlink_to(real_dir, target_is_directory=True)
            metadata_name = f"{alias.name}/{owned.name}"
            lexical_path = alias / owned.name
        torrent = SimpleNamespace(hash=f"symlink-{symlink_kind}", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": metadata_name}]
        real_resolve = Path.resolve
        resolved_paths: list[Path] = []

        def track_resolve(path, strict=False):
            resolved_paths.append(path)
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            owned_paths = self._owned_paths(client, torrent, save_root, {owned})

        assert owned_paths == {owned}
        assert lexical_path in resolved_paths

    def test_absolute_metadata_uses_resolution_fallback(self, tmp_path):
        save_root = tmp_path.resolve()
        owned = save_root / "owned.mkv"
        owned.write_text("owned", encoding="utf-8")
        torrent = SimpleNamespace(hash="absolute", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": str(owned)}]
        real_resolve = Path.resolve
        resolved_paths: list[Path] = []

        def track_resolve(path, strict=False):
            resolved_paths.append(path)
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            owned_paths = self._owned_paths(client, torrent, save_root, {owned})

        assert owned_paths == {owned}
        assert owned in resolved_paths

    def test_parent_traversal_metadata_uses_resolution_fallback(self, tmp_path):
        save_root = tmp_path.resolve()
        (save_root / "nested").mkdir()
        owned = save_root / "owned.mkv"
        owned.write_text("owned", encoding="utf-8")
        metadata_name = "nested/../owned.mkv"
        lexical_path = save_root / metadata_name
        torrent = SimpleNamespace(hash="parent-traversal", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": metadata_name}]
        real_resolve = Path.resolve
        resolved_paths: list[Path] = []

        def track_resolve(path, strict=False):
            resolved_paths.append(path)
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            owned_paths = self._owned_paths(client, torrent, save_root, {owned})

        assert owned_paths == {owned}
        assert lexical_path in resolved_paths

    def test_non_candidate_metadata_uses_resolution_fallback(self, tmp_path):
        save_root = tmp_path.resolve()
        owned = save_root / "owned.mkv"
        other_candidate = save_root / "other.mkv"
        owned.write_text("owned", encoding="utf-8")
        other_candidate.write_text("other", encoding="utf-8")
        torrent = SimpleNamespace(hash="non-candidate", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": owned.name}]
        real_resolve = Path.resolve
        resolved_paths: list[Path] = []

        def track_resolve(path, strict=False):
            resolved_paths.append(path)
            return real_resolve(path, strict=strict)

        with patch.object(Path, "resolve", autospec=True, side_effect=track_resolve):
            owned_paths = self._owned_paths(client, torrent, save_root, {other_candidate})

        assert owned_paths == {owned}
        assert owned in resolved_paths

    @pytest.mark.parametrize("metadata_name", [".", "foo/.."])
    def test_metadata_collapsing_to_save_root_fails_closed(self, tmp_path, metadata_name):
        save_root = tmp_path.resolve()
        candidate = save_root / "candidate.mkv"
        candidate.write_text("preserve", encoding="utf-8")
        torrent = SimpleNamespace(hash=f"root-collapse-{metadata_name}", save_path=str(save_root))
        client = MagicMock()
        client.torrents_files.return_value = [{"name": metadata_name}]

        with pytest.raises(SafetyCheckError, match="unsafe file path"):
            self._owned_paths(client, torrent, save_root, {candidate})

        assert candidate.read_text(encoding="utf-8") == "preserve"

    def test_final_validation_preserves_candidate_claimed_by_exact_metadata(self, tmp_path):
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("preserve", encoding="utf-8")
        torrent = SimpleNamespace(hash="new-owner", save_path=str(tmp_path), content_path=str(tmp_path))
        client = MagicMock()
        client.application.default_save_path = str(tmp_path)
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": candidate.name}]
        plan = build_orphan_file_plan([str(candidate)])

        with pytest.raises(SafetyCheckError, match="now owned by qBittorrent"):
            delete_orphaned_files(
                [str(candidate)],
                dry_run=False,
                client=client,
                plan=plan,
            )

        assert candidate.read_text(encoding="utf-8") == "preserve"

    @pytest.mark.parametrize("metadata_name", [".", "foo/.."])
    def test_final_validation_rejects_root_collapsing_metadata_before_delete(self, tmp_path, metadata_name):
        candidate = tmp_path / "candidate.mkv"
        candidate.write_text("preserve", encoding="utf-8")
        torrent = SimpleNamespace(hash=f"root-collapse-{metadata_name}", save_path=str(tmp_path), content_path=str(tmp_path))
        client = MagicMock()
        client.application.default_save_path = str(tmp_path)
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = [torrent]
        client.torrents_files.return_value = [{"name": metadata_name}]
        plan = build_orphan_file_plan([str(candidate)])

        with pytest.raises(SafetyCheckError, match="unsafe file path"):
            delete_orphaned_files(
                [str(candidate)],
                dry_run=False,
                client=client,
                plan=plan,
            )

        assert candidate.read_text(encoding="utf-8") == "preserve"
        client.torrents_delete.assert_not_called()


class TestOrphanCircuitBreakers:
    """Verify age and candidate-count controls without changing defaults."""

    @staticmethod
    def _client(save_root: Path) -> MagicMock:
        client = MagicMock()
        client.application.default_save_path = str(save_root)
        client.torrent_categories.categories = {}
        client.torrents.info.return_value = []
        return client

    def test_minimum_age_filters_recent_candidates(self, tmp_path):
        old_file = tmp_path / "old.mkv"
        recent_file = tmp_path / "recent.mkv"
        old_file.write_text("old", encoding="utf-8")
        recent_file.write_text("recent", encoding="utf-8")
        old_timestamp = old_file.stat().st_mtime - 120
        os.utime(old_file, (old_timestamp, old_timestamp))
        client = self._client(tmp_path)

        assert check_files_on_disk(client, [], orphan_min_age_seconds=60) == [str(old_file)]
        assert set(check_files_on_disk(client, [])) == {str(old_file), str(recent_file)}

    @pytest.mark.parametrize("recycle", [False, True])
    def test_maximum_candidate_limit_blocks_real_run_before_mutation(self, tmp_path, recycle):
        first = tmp_path / "first.mkv"
        second = tmp_path / "second.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        client = self._client(tmp_path)
        recycle_bin = str(tmp_path / "recycle") if recycle else None
        plan = build_orphan_file_plan([str(first), str(second)])

        with pytest.raises(SafetyCheckError, match="2 candidates exceed.*maximum of 1"):
            delete_orphaned_files(
                [str(first), str(second)],
                dry_run=False,
                client=client,
                recycle_bin=recycle_bin,
                plan=plan,
                orphan_max_candidates=1,
            )

        assert first.read_text(encoding="utf-8") == "first"
        assert second.read_text(encoding="utf-8") == "second"
        assert not (tmp_path / "recycle").exists()
        client.torrents.info.assert_not_called()

    def test_maximum_candidate_limit_does_not_hide_dry_run_targets(self, tmp_path, caplog):
        first = tmp_path / "first.mkv"
        second = tmp_path / "second.mkv"
        first.write_text("first", encoding="utf-8")
        second.write_text("second", encoding="utf-8")
        client = self._client(tmp_path)
        plan = build_orphan_file_plan([str(first), str(second)])

        with caplog.at_level("INFO"):
            delete_orphaned_files(
                [str(first), str(second)],
                dry_run=True,
                client=client,
                torrents=[],
                plan=plan,
                orphan_max_candidates=1,
            )

        assert "Would delete orphaned file" in caplog.text
        assert first.exists()
        assert second.exists()


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
