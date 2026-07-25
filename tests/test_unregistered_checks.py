"""Tests for unregistered checks functionality."""

import pytest
from unittest.mock import MagicMock, patch
from qbitunregistered.operations.unregistered_checks import (
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

    def test_tracker_error_status_is_counted(self):
        """Test qBittorrent 5.2 tracker-error status."""
        torrent = MockTorrent([MockTracker("Unregistered torrent", status=5)])

        count = process_torrent(torrent, {"unregistered torrent"}, set())

        assert count == 1

    def test_unreachable_status_is_not_counted(self):
        """Test that an unreachable tracker is not treated as unregistered."""
        torrent = MockTorrent([MockTracker("Unregistered torrent", status=6)])

        count = process_torrent(torrent, {"unregistered torrent"}, set())

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

    def test_mapping_tracker_metadata(self):
        """Canonical API dictionaries use the same matching semantics."""
        torrent = MockTorrent([])
        trackers = [{"msg": "Unregistered torrent", "status": 4}]

        assert process_torrent(torrent, {"unregistered torrent"}, set(), trackers) == 1


def test_tracker_metadata_is_reused_across_preview_execution_and_seeding() -> None:
    """All tracker consumers share one execution-scoped API fetch."""
    from qbitunregistered.cache import clear_cache
    from qbitunregistered.impact import ImpactSummary, _analyze_unregistered
    from qbitunregistered.operations.seeding_management import find_tracker_config
    from qbitunregistered.operations.unregistered_checks import unregistered_checks

    clear_cache()
    client = MagicMock()
    client.torrents_trackers.return_value = [
        {
            "url": "https://tracker.example/announce",
            "msg": "Unregistered torrent",
            "status": 4,
        }
    ]
    torrent = MagicMock(
        hash="hash",
        name="torrent",
        save_path="/downloads",
        category="",
        tags="",
        trackers=[],
    )
    config = {
        "default_unregistered_tag": "unregistered",
        "cross_seeding_tag": "unregistered:crossseeding",
        "unregistered": ["unregistered torrent"],
        "use_delete_files": False,
        "tracker_tags": {"tracker.example": {"seed_time_limit": 60}},
    }

    _analyze_unregistered(client, [torrent], config, ImpactSummary())
    unregistered_checks(
        client,
        [torrent],
        config,
        use_delete_tags=False,
        delete_tags=[],
        delete_files={},
        dry_run=True,
    )
    assert find_tracker_config(client, torrent, config) == {"seed_time_limit": 60}

    client.torrents_trackers.assert_called_once_with(torrent_hash="hash")
    client.torrents_add_tags.assert_not_called()


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
        client.torrents.info.return_value = []
        client.torrents_info = MagicMock(return_value=[])
        client.torrents_files = MagicMock(return_value=[])
        return client

    @pytest.fixture
    def config(self):
        return {
            "default_unregistered_tag": "unregistered",
            "cross_seeding_tag": "unregistered:crossseeding",
            "unregistered": ["unregistered"],
            "use_delete_files": True,
        }

    @pytest.fixture(autouse=True)
    def clear_file_cache(self):
        from qbitunregistered.cache import clear_cache

        clear_cache()
        yield
        clear_cache()

    def test_unregistered_deletion_with_recycle_bin(self, mock_client, config, tmp_path):
        """Test that unregistered torrent files are moved to recycle bin."""
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

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

        mock_file = MagicMock()
        mock_file.name = "movie.mkv"
        mock_client.torrents.info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file]

        # Run deletion with recycle bin
        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=False,
            torrents=[mock_torrent],
            recycle_bin=str(recycle_bin),
        )

        # Verify torrent was deleted WITHOUT files
        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["abc123"], delete_files=False)

        # Verify file was moved to recycle bin with hybrid structure
        # Should be: recycle_bin/unregistered/movies/[original_path]
        expected_dest = recycle_bin / "unregistered" / "movies"
        assert expected_dest.exists()

        # File should be somewhere in the recycle bin
        moved_files = list(recycle_bin.rglob("movie.mkv"))
        assert len(moved_files) == 1
        assert moved_files[0].read_text() == "test content"
        assert not test_file.exists()

    def test_unregistered_deletion_without_recycle_bin(self, mock_client, config, tmp_path):
        """Test permanent deletion when no recycle bin is configured."""
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        source_dir = tmp_path / "torrents"
        source_dir.mkdir()
        (source_dir / "movie.mkv").write_text("content")

        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.tags = "unregistered"
        mock_torrent.save_path = str(source_dir)
        mock_torrent.category = "movies"
        mock_file = MagicMock(name="file")
        mock_file.name = "movie.mkv"
        mock_client.torrents.info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file]

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=False,
            torrents=[mock_torrent],
            recycle_bin=None,
        )

        # Verify torrent was deleted WITH files (permanent deletion)
        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["abc123"], delete_files=True)

    def test_permanent_deletion_preserves_cross_seeded_files(self, mock_client, config, tmp_path):
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        source_dir = tmp_path / "torrents"
        source_dir.mkdir()
        (source_dir / "movie.mkv").write_text("content")
        source = MagicMock(hash="source", tags="unregistered", save_path=str(source_dir), category="movies")
        source.name = "source"
        peer = MagicMock(hash="peer", tags="", save_path=str(source_dir), category="movies")
        peer.name = "peer"
        file_info = MagicMock()
        file_info.name = "movie.mkv"
        mock_client.torrents.info.return_value = [source, peer]
        mock_client.torrents_files.return_value = [file_info]

        delete_torrents_and_files(
            mock_client,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            False,
            [source, peer],
        )

        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["source"], delete_files=False)

    def test_permanent_deletion_removes_all_shared_owners_once(self, mock_client, config, tmp_path):
        """A fully selected cross-seed group is one permanent deletion batch."""
        from qbitunregistered.operations.unregistered_checks import (
            DeletionAction,
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content", encoding="utf-8")
        first = MagicMock(hash="first", tags="unregistered", save_path=str(tmp_path), category="movies")
        first.name = "first"
        second = MagicMock(hash="second", tags="unregistered", save_path=str(tmp_path), category="movies")
        second.name = "second"
        file_info = MagicMock()
        file_info.name = shared_file.name
        torrents = [first, second]
        mock_client.torrents.info.return_value = torrents
        mock_client.torrents_files.return_value = [file_info]

        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            None,
        )

        assert [deletion.action for deletion in plan.deletions] == [
            DeletionAction.PERMANENT_DELETE,
            DeletionAction.PERMANENT_DELETE,
        ]
        assert sum(len(deletion.files) for deletion in plan.deletions) == 1

        delete_torrents_and_files(
            mock_client,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            False,
            torrents,
            plan=plan,
        )

        mock_client.torrents_delete.assert_called_once_with(
            torrent_hashes=["first", "second"],
            delete_files=True,
        )

    def test_recycle_moves_shared_group_file_once(self, mock_client, config, tmp_path):
        """A fully selected cross-seed group recycles one canonical path once."""
        from qbitunregistered.file_operations import move_files_to_recycle_bin as real_move_files
        from qbitunregistered.operations.unregistered_checks import (
            DeletionAction,
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "downloads" / "movie.mkv"
        shared_file.parent.mkdir()
        shared_file.write_text("content", encoding="utf-8")
        recycle_bin = tmp_path / "recycle"
        first = MagicMock(
            hash="first",
            tags="unregistered",
            save_path=str(shared_file.parent),
            category="movies",
        )
        first.name = "first"
        second = MagicMock(
            hash="second",
            tags="unregistered",
            save_path=str(shared_file.parent),
            category="movies",
        )
        second.name = "second"
        file_info = MagicMock()
        file_info.name = shared_file.name
        torrents = [first, second]
        mock_client.torrents.info.return_value = torrents
        mock_client.torrents_files.return_value = [file_info]

        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )

        assert [deletion.action for deletion in plan.deletions] == [
            DeletionAction.RECYCLE_FILES,
            DeletionAction.RECYCLE_FILES,
        ]
        assert sum(len(deletion.files) for deletion in plan.deletions) == 1

        with patch(
            "qbitunregistered.operations.unregistered_checks.move_files_to_recycle_bin",
            wraps=real_move_files,
        ) as move_files:
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                torrents,
                str(recycle_bin),
                plan=plan,
            )

        move_files.assert_called_once()
        assert not shared_file.exists()
        recycled_files = list(recycle_bin.rglob(shared_file.name))
        assert len(recycled_files) == 1
        assert recycled_files[0].read_text(encoding="utf-8") == "content"
        mock_client.torrents_delete.assert_called_once_with(
            torrent_hashes=["first", "second"],
            delete_files=False,
        )

    def test_file_deletion_ineligible_owner_preserves_shared_content(self, mock_client, config, tmp_path):
        """A torrent-only deletion candidate remains a protected file owner."""
        from qbitunregistered.operations.unregistered_checks import (
            DeletionAction,
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content", encoding="utf-8")
        deleting_files = MagicMock(hash="delete-files", tags="delete-files", save_path=str(tmp_path), category="")
        deleting_files.name = "delete-files"
        keeping_files = MagicMock(hash="keep-files", tags="keep-files", save_path=str(tmp_path), category="")
        keeping_files.name = "keep-files"
        file_info = MagicMock()
        file_info.name = shared_file.name
        torrents = [deleting_files, keeping_files]
        mock_client.torrents.info.return_value = torrents
        mock_client.torrents_files.return_value = [file_info]
        delete_tags = ["delete-files", "keep-files"]
        delete_files = {"delete-files": True, "keep-files": False}

        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            delete_tags,
            delete_files,
            None,
        )

        assert plan.deletions[0].action is DeletionAction.PRESERVE_SHARED
        assert plan.deletions[0].shared_with == ("keep-files",)
        assert plan.deletions[1].action is DeletionAction.TORRENT_ONLY

        delete_torrents_and_files(
            mock_client,
            config,
            True,
            delete_tags,
            delete_files,
            False,
            torrents,
            plan=plan,
        )

        assert shared_file.read_text(encoding="utf-8") == "content"
        mock_client.torrents_delete.assert_called_once_with(
            torrent_hashes=["delete-files", "keep-files"],
            delete_files=False,
        )

    def test_shared_group_dry_run_matches_unique_recycle_plan(self, mock_client, config, tmp_path):
        """Dry-run previews the same single shared path without mutation."""
        from qbitunregistered.file_operations import move_files_to_recycle_bin as real_move_files
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content", encoding="utf-8")
        recycle_bin = tmp_path / "recycle"
        first = MagicMock(hash="first", tags="unregistered", save_path=str(tmp_path), category="movies")
        first.name = "first"
        second = MagicMock(hash="second", tags="unregistered", save_path=str(tmp_path), category="movies")
        second.name = "second"
        file_info = MagicMock()
        file_info.name = shared_file.name
        torrents = [first, second]
        mock_client.torrents_files.return_value = [file_info]
        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )

        with patch(
            "qbitunregistered.operations.unregistered_checks.move_files_to_recycle_bin",
            wraps=real_move_files,
        ) as move_files:
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                True,
                torrents,
                str(recycle_bin),
                plan=plan,
            )

        move_files.assert_called_once()
        assert move_files.call_args.kwargs["dry_run"] is True
        assert shared_file.read_text(encoding="utf-8") == "content"
        assert not recycle_bin.exists()
        mock_client.torrents_delete.assert_not_called()

    @pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
    def test_new_shared_owner_invalidates_group_plan(self, mock_client, config, tmp_path, use_recycle_bin):
        """A new owner before execution blocks every planned group mutation."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content", encoding="utf-8")
        recycle_bin = tmp_path / "recycle" if use_recycle_bin else None
        first = MagicMock(hash="first", tags="unregistered", save_path=str(tmp_path), category="")
        first.name = "first"
        second = MagicMock(hash="second", tags="unregistered", save_path=str(tmp_path), category="")
        second.name = "second"
        new_owner = MagicMock(hash="new-owner", tags="", save_path=str(tmp_path), category="")
        new_owner.name = "new-owner"
        file_info = MagicMock()
        file_info.name = shared_file.name
        planned_torrents = [first, second]
        mock_client.torrents_files.return_value = [file_info]
        plan = build_unregistered_deletion_plan(
            mock_client,
            planned_torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin) if recycle_bin else None,
        )
        mock_client.torrents.info.return_value = [first, second, new_owner]

        with pytest.raises(SafetyCheckError, match="ownership state changed"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                planned_torrents,
                str(recycle_bin) if recycle_bin else None,
                plan=plan,
            )

        assert shared_file.read_text(encoding="utf-8") == "content"
        if recycle_bin is not None:
            assert not recycle_bin.exists()
        mock_client.torrents_delete.assert_not_called()

    def test_shared_group_delete_failure_rolls_recycled_file_back(self, mock_client, config, tmp_path):
        """A failed group deletion restores its single recycled shared path."""
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content", encoding="utf-8")
        recycle_bin = tmp_path / "recycle"
        first = MagicMock(hash="first", tags="unregistered", save_path=str(tmp_path), category="")
        first.name = "first"
        second = MagicMock(hash="second", tags="unregistered", save_path=str(tmp_path), category="")
        second.name = "second"
        file_info = MagicMock()
        file_info.name = shared_file.name
        torrents = [first, second]
        mock_client.torrents.info.return_value = torrents
        mock_client.torrents_files.return_value = [file_info]
        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )
        mock_client.torrents_delete.side_effect = RuntimeError("simulated group deletion failure")

        with pytest.raises(RuntimeError, match="simulated group deletion failure"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                torrents,
                str(recycle_bin),
                plan=plan,
            )

        assert shared_file.read_text(encoding="utf-8") == "content"
        assert not list(recycle_bin.rglob(shared_file.name))
        mock_client.torrents_delete.assert_called_once_with(
            torrent_hashes=["first", "second"],
            delete_files=False,
        )

    def test_later_recycle_move_failure_rolls_back_prior_deletion(self, mock_client, config, tmp_path):
        """A later move failure restores files moved for earlier deletions."""
        from qbitunregistered.file_operations import (
            SafetyCheckError,
            move_files_to_recycle_bin as real_move_files,
        )
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        first_file = tmp_path / "first.mkv"
        first_file.write_text("first content", encoding="utf-8")
        second_file = tmp_path / "second.mkv"
        second_file.write_text("second content", encoding="utf-8")
        recycle_bin = tmp_path / "recycle"
        first = MagicMock(hash="first", tags="unregistered", save_path=str(tmp_path), category="movies")
        first.name = "first"
        second = MagicMock(hash="second", tags="unregistered", save_path=str(tmp_path), category="shows")
        second.name = "second"
        first_file_info = MagicMock()
        first_file_info.name = first_file.name
        second_file_info = MagicMock()
        second_file_info.name = second_file.name
        torrents = [first, second]
        files_by_hash = {
            "first": [first_file_info],
            "second": [second_file_info],
        }
        mock_client.torrents.info.return_value = torrents
        mock_client.torrents_files.side_effect = lambda torrent_hash, **_kwargs: files_by_hash[torrent_hash]
        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )
        move_count = 0

        def move_first_then_fail(file_paths, recycle_path, deletion_type, category, **kwargs):
            nonlocal move_count
            move_count += 1
            if move_count == 1:
                return real_move_files(
                    file_paths,
                    recycle_path,
                    deletion_type,
                    category,
                    **kwargs,
                )
            return 0, [(file_paths[0], "simulated later move failure")]

        with patch(
            "qbitunregistered.operations.unregistered_checks.move_files_to_recycle_bin",
            side_effect=move_first_then_fail,
        ) as move_files:
            with pytest.raises(SafetyCheckError, match="simulated later move failure"):
                delete_torrents_and_files(
                    mock_client,
                    config,
                    True,
                    ["unregistered"],
                    {"unregistered": True},
                    False,
                    torrents,
                    str(recycle_bin),
                    plan=plan,
                )

        assert move_files.call_count == 2
        assert first_file.read_text(encoding="utf-8") == "first content"
        assert second_file.read_text(encoding="utf-8") == "second content"
        assert not list(recycle_bin.rglob("*.mkv"))
        assert [torrent.hash for torrent in mock_client.torrents.info()] == ["first", "second"]
        mock_client.torrents_delete.assert_not_called()

    def test_shared_group_tag_change_after_recycle_rolls_file_back(self, mock_client, config, tmp_path):
        """A final group tag change restores recycled data before aborting."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content", encoding="utf-8")
        recycle_bin = tmp_path / "recycle"
        first = MagicMock(hash="first", tags="unregistered", save_path=str(tmp_path), category="")
        first.name = "first"
        second = MagicMock(hash="second", tags="unregistered", save_path=str(tmp_path), category="")
        second.name = "second"
        first_without_tag = MagicMock(hash="first", tags="", save_path=str(tmp_path), category="")
        first_without_tag.name = "first"
        file_info = MagicMock()
        file_info.name = shared_file.name
        torrents = [first, second]
        mock_client.torrents_files.return_value = [file_info]
        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )
        mock_client.torrents.info.side_effect = [
            torrents,
            torrents,
            [first_without_tag, second],
        ]

        with pytest.raises(SafetyCheckError, match="Delete tag changed"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                torrents,
                str(recycle_bin),
                plan=plan,
            )

        assert shared_file.read_text(encoding="utf-8") == "content"
        assert not list(recycle_bin.rglob(shared_file.name))
        mock_client.torrents_delete.assert_not_called()

    def test_unregistered_deletion_dry_run_with_recycle_bin(self, mock_client, config, tmp_path, caplog):
        """Test dry run mode with recycle bin."""
        import logging
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        caplog.set_level(logging.INFO)

        recycle_bin = tmp_path / "recycle_bin"

        source_dir = tmp_path / "torrents"
        source_dir.mkdir()
        (source_dir / "movie.mkv").write_text("content")
        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.category = "movies"
        mock_torrent.tags = "unregistered"
        mock_torrent.save_path = str(source_dir)
        mock_file = MagicMock()
        mock_file.name = "movie.mkv"
        mock_client.torrents.info.return_value = [mock_torrent]
        mock_client.torrents_files.return_value = [mock_file]

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=True,
            torrents=[mock_torrent],
            recycle_bin=str(recycle_bin),
        )

        # Verify nothing was actually deleted
        mock_client.torrents_delete.assert_not_called()

        # Verify dry run log message
        assert "Would move to recycle bin" in caplog.text

    def test_delete_tag_matching_is_exact(self, mock_client, config):
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        torrent = MagicMock(hash="hash", tags="unregistered, other")
        torrent.name = "torrent"

        delete_torrents_and_files(
            mock_client,
            config,
            True,
            ["reg"],
            {"reg": False},
            False,
            [torrent],
        )

        mock_client.torrents_delete.assert_not_called()

    def test_global_file_deletion_gate_preserves_files(self, mock_client, config):
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        torrent = MagicMock(hash="hash", tags=" first, unregistered ,third ")
        torrent.name = "torrent"
        gated_config = {**config, "use_delete_files": False}
        mock_client.torrents.info.return_value = [torrent]

        delete_torrents_and_files(
            mock_client,
            gated_config,
            True,
            ["unregistered"],
            {"unregistered": True},
            False,
            [torrent],
        )

        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["hash"], delete_files=False)

    @pytest.mark.parametrize(
        "failure",
        [ConnectionError("offline"), TimeoutError("timeout"), ValueError("malformed")],
    )
    def test_file_discovery_failure_aborts_all_deletion(self, mock_client, config, failure, tmp_path):
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files
        from qbitunregistered.file_operations import SafetyCheckError

        torrent = MagicMock(hash="hash", tags="unregistered", save_path=str(tmp_path), category="")
        torrent.name = "torrent"
        mock_client.torrents_files.side_effect = failure

        with pytest.raises(SafetyCheckError):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                [torrent],
            )

        mock_client.torrents_delete.assert_not_called()

    def test_missing_torrent_file_aborts_all_deletion(self, mock_client, config, tmp_path):
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files
        from qbitunregistered.file_operations import SafetyCheckError

        torrent = MagicMock(hash="hash", tags="unregistered", save_path=str(tmp_path), category="")
        torrent.name = "torrent"
        file_info = MagicMock()
        file_info.name = "missing.mkv"
        mock_client.torrents_info.return_value = [torrent]
        mock_client.torrents_files.return_value = [file_info]

        with pytest.raises(SafetyCheckError):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                [torrent],
            )

        mock_client.torrents_delete.assert_not_called()

    def test_category_based_organization(self, mock_client, config, tmp_path):
        """Test that files are organized by category in recycle bin."""
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

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

            mock_client.torrents.info.return_value = [mock_torrent]
            mock_client.torrents_files.return_value = [mock_file]

            delete_torrents_and_files(
                client=mock_client,
                config=config,
                use_delete_tags=True,
                delete_tags=["unregistered"],
                delete_files={"unregistered": True},
                dry_run=False,
                torrents=[mock_torrent],
                recycle_bin=str(recycle_bin),
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
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        recycle_bin = tmp_path / "recycle_bin"

        mock_torrent = MagicMock()
        mock_torrent.name = "Test Movie"
        mock_torrent.hash = "abc123"
        mock_torrent.tags = "unregistered"
        mock_client.torrents.info.return_value = [mock_torrent]

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": False},  # Don't delete files
            dry_run=False,
            torrents=[mock_torrent],
            recycle_bin=str(recycle_bin),
        )

        # Verify torrent was deleted without files
        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["abc123"], delete_files=False)

        # Recycle bin should not be used
        assert not recycle_bin.exists()

    def test_cross_seeding_detection_prevents_file_move(self, mock_client, config, tmp_path):
        """Test that cross-seeding detection prevents file deletion."""
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        recycle_bin = tmp_path / "recycle_bin"

        # Create test files
        test_file_dir = tmp_path / "torrents" / "movies"
        test_file_dir.mkdir(parents=True)
        test_file = test_file_dir / "movie.mkv"
        test_file.write_text("content")

        # Mock torrent to be deleted
        mock_unregistered_torrent = MagicMock()
        mock_unregistered_torrent.name = "Unregistered Movie"
        mock_unregistered_torrent.hash = "unreg123"
        mock_unregistered_torrent.tags = "unregistered"
        mock_unregistered_torrent.category = "movies"
        mock_unregistered_torrent.save_path = str(test_file_dir)

        # Mock cross-seeded torrent using the same files
        mock_cross_seeded_torrent = MagicMock()
        mock_cross_seeded_torrent.name = "Cross-Seeded Movie"
        mock_cross_seeded_torrent.hash = "cross456"
        mock_cross_seeded_torrent.tags = ""
        mock_cross_seeded_torrent.category = "movies"
        mock_cross_seeded_torrent.save_path = str(test_file_dir)

        # Mock file info
        mock_file_info = MagicMock()
        mock_file_info.name = "movie.mkv"

        # Setup client mocks
        mock_client.torrents.info.return_value = [mock_unregistered_torrent, mock_cross_seeded_torrent]
        mock_client.torrents_files.return_value = [mock_file_info]

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=False,
            torrents=[mock_unregistered_torrent, mock_cross_seeded_torrent],
            recycle_bin=str(recycle_bin),
        )

        # Verify torrent was deleted without files (due to cross-seeding)
        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["unreg123"], delete_files=False)

        # Verify file was NOT moved (still exists in original location)
        assert test_file.exists(), "File should not be moved due to cross-seeding"

        # Recycle bin should not contain the file
        assert not (recycle_bin / "unregistered" / "movies").exists()

    def test_no_cross_seeding_allows_file_move(self, mock_client, config, tmp_path):
        """Test that files are moved when no cross-seeding is detected."""
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        recycle_bin = tmp_path / "recycle_bin"

        # Create test files
        test_file_dir = tmp_path / "torrents" / "movies"
        test_file_dir.mkdir(parents=True)
        test_file = test_file_dir / "movie.mkv"
        test_file.write_text("content")

        # Mock torrent to be deleted
        mock_torrent = MagicMock()
        mock_torrent.name = "Unregistered Movie"
        mock_torrent.hash = "unreg123"
        mock_torrent.tags = "unregistered"
        mock_torrent.category = "movies"
        mock_torrent.save_path = str(test_file_dir)

        # Mock another torrent with different files
        mock_other_torrent = MagicMock()
        mock_other_torrent.name = "Other Movie"
        mock_other_torrent.hash = "other456"
        mock_other_torrent.tags = ""
        mock_other_torrent.category = "movies"
        mock_other_torrent.save_path = str(tmp_path / "other")

        # Mock file info
        mock_file_info = MagicMock()
        mock_file_info.name = "movie.mkv"

        mock_other_file_info = MagicMock()
        mock_other_file_info.name = "different.mkv"

        # Setup client mocks
        mock_client.torrents.info.return_value = [mock_torrent, mock_other_torrent]
        mock_client.torrents_files.side_effect = lambda torrent_hash, **_kwargs: {
            "unreg123": [mock_file_info],
            "other456": [mock_other_file_info],
        }[torrent_hash]

        delete_torrents_and_files(
            client=mock_client,
            config=config,
            use_delete_tags=True,
            delete_tags=["unregistered"],
            delete_files={"unregistered": True},
            dry_run=False,
            torrents=[mock_torrent, mock_other_torrent],
            recycle_bin=str(recycle_bin),
        )

        # Verify torrent was deleted without files (we moved them to recycle bin)
        mock_client.torrents_delete.assert_called_once_with(torrent_hashes=["unreg123"], delete_files=False)

        # Verify file was moved to recycle bin
        assert not test_file.exists(), "File should be moved from original location"

    def test_incomplete_recycle_move_fails_operation_and_preserves_torrent(self, mock_client, config, tmp_path):
        """A preserved torrent is reported as a failed destructive operation."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        source_file = tmp_path / "movie.mkv"
        source_file.write_text("content", encoding="utf-8")
        torrent = MagicMock(
            hash="unreg123",
            tags="unregistered",
            save_path=str(tmp_path),
            category="movies",
        )
        torrent.name = "Unregistered Movie"
        file_info = MagicMock()
        file_info.name = source_file.name
        mock_client.torrents.info.return_value = [torrent]
        mock_client.torrents_files.return_value = [file_info]

        with (
            patch(
                "qbitunregistered.operations.unregistered_checks.move_files_to_recycle_bin",
                return_value=(0, [(source_file, "permission denied")]),
            ),
            pytest.raises(SafetyCheckError, match="Could not safely recycle all files"),
        ):
            delete_torrents_and_files(
                client=mock_client,
                config=config,
                use_delete_tags=True,
                delete_tags=["unregistered"],
                delete_files={"unregistered": True},
                dry_run=False,
                torrents=[torrent],
                recycle_bin=str(tmp_path / "recycle"),
            )

        assert source_file.read_text(encoding="utf-8") == "content"
        mock_client.torrents_delete.assert_not_called()

    def test_final_qbittorrent_state_change_aborts_before_mutation(self, mock_client, config, tmp_path):
        """A new owner after preview invalidates permanent deletion."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        source_file = tmp_path / "movie.mkv"
        source_file.write_text("content")
        source = MagicMock(
            hash="source",
            tags="unregistered",
            save_path=str(tmp_path),
            category="movies",
        )
        source.name = "source"
        peer = MagicMock(hash="peer", tags="", save_path=str(tmp_path), category="movies")
        peer.name = "peer"
        file_info = MagicMock()
        file_info.name = "movie.mkv"
        mock_client.torrents_files.return_value = [file_info]

        plan = build_unregistered_deletion_plan(
            mock_client,
            [source],
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            None,
        )
        mock_client.torrents.info.return_value = [source, peer]

        with pytest.raises(SafetyCheckError, match="ownership state changed"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                [source],
                plan=plan,
            )

        assert source_file.read_text() == "content"
        mock_client.torrents_delete.assert_not_called()

    @pytest.mark.parametrize("delete_files", [False, True], ids=["torrent-only", "permanent-files"])
    def test_removed_delete_tag_blocks_execution_without_preview(self, mock_client, config, tmp_path, delete_files):
        """The current tag gate applies when execution builds its own plan."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        planned = MagicMock(
            hash="source",
            tags="unregistered",
            save_path=str(tmp_path),
            category="movies",
        )
        planned.name = "source"
        current = MagicMock(
            hash="source",
            tags="",
            save_path=str(tmp_path),
            category="movies",
        )
        current.name = "source"
        if delete_files:
            (tmp_path / "movie.mkv").write_text("content")
            file_info = MagicMock()
            file_info.name = "movie.mkv"
            mock_client.torrents_files.return_value = [file_info]
        mock_client.torrents.info.return_value = [current]

        with pytest.raises(SafetyCheckError, match="Delete tag changed"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": delete_files},
                False,
                [planned],
            )

        mock_client.torrents_delete.assert_not_called()
        if delete_files:
            assert (tmp_path / "movie.mkv").read_text() == "content"

    def test_delete_tag_refresh_failure_blocks_deletion(self, mock_client, config):
        """API uncertainty while refreshing tags preserves the planned torrent."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import delete_torrents_and_files

        torrent = MagicMock(hash="source", tags="unregistered")
        torrent.name = "source"
        mock_client.torrents.info.side_effect = ConnectionError("offline")

        with pytest.raises(SafetyCheckError, match="Could not refresh"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": False},
                False,
                [torrent],
            )

        mock_client.torrents_delete.assert_not_called()

    def test_recycle_tag_removal_before_delete_restores_files(self, mock_client, config, tmp_path):
        """A tag removed during a recycle move aborts deletion and rolls back."""
        from qbitunregistered.file_operations import SafetyCheckError
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        source_file = tmp_path / "movie.mkv"
        source_file.write_text("content")
        recycle_bin = tmp_path / "recycle"
        planned = MagicMock(
            hash="source",
            tags="unregistered",
            save_path=str(tmp_path),
            category="movies",
        )
        planned.name = "source"
        current = MagicMock(
            hash="source",
            tags="",
            save_path=str(tmp_path),
            category="movies",
        )
        current.name = "source"
        file_info = MagicMock()
        file_info.name = "movie.mkv"
        mock_client.torrents_files.return_value = [file_info]
        plan = build_unregistered_deletion_plan(
            mock_client,
            [planned],
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )
        mock_client.torrents.info.side_effect = [[planned], [planned], [current]]

        with pytest.raises(SafetyCheckError, match="Delete tag changed"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                [planned],
                str(recycle_bin),
                plan=plan,
            )

        assert source_file.read_text() == "content"
        assert list(recycle_bin.rglob("movie.mkv")) == []
        mock_client.torrents_delete.assert_not_called()

    def test_ownership_index_reads_each_torrent_once_per_snapshot(self, mock_client, config, tmp_path):
        """Deletion candidates share one O(torrents) ownership scan."""
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        torrents = []
        files_by_hash = {}
        for index in range(3):
            torrent_dir = tmp_path / f"torrent-{index}"
            torrent_dir.mkdir()
            (torrent_dir / "data.bin").write_text(str(index))
            torrent_hash = f"hash-{index}"
            torrent = MagicMock(
                hash=torrent_hash,
                tags="unregistered" if index < 2 else "",
                save_path=str(torrent_dir),
                category="data",
            )
            torrent.name = torrent_hash
            torrents.append(torrent)
            file_info = MagicMock()
            file_info.name = "data.bin"
            files_by_hash[torrent_hash] = [file_info]

        mock_client.torrents_files.side_effect = lambda torrent_hash, **_kwargs: files_by_hash[torrent_hash]
        plan = build_unregistered_deletion_plan(
            mock_client,
            torrents,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            None,
        )

        assert mock_client.torrents_files.call_count == len(torrents)
        mock_client.torrents.info.return_value = torrents

        delete_torrents_and_files(
            mock_client,
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            False,
            torrents,
            plan=plan,
        )

        assert mock_client.torrents.info.call_count == 2
        assert mock_client.torrents_files.call_count == len(torrents) * 2
        mock_client.torrents_delete.assert_called_once_with(
            torrent_hashes=["hash-0", "hash-1"],
            delete_files=True,
        )

    def test_recycle_rolls_back_when_torrent_delete_fails(self, mock_client, config, tmp_path):
        """A live torrent retains its files when qBittorrent deletion fails."""
        from qbitunregistered.operations.unregistered_checks import (
            build_unregistered_deletion_plan,
            delete_torrents_and_files,
        )

        source_file = tmp_path / "movie.mkv"
        source_file.write_text("content")
        recycle_bin = tmp_path / "recycle"
        torrent = MagicMock(
            hash="source",
            tags="unregistered",
            save_path=str(tmp_path),
            category="movies",
        )
        torrent.name = "source"
        file_info = MagicMock()
        file_info.name = "movie.mkv"
        mock_client.torrents_files.return_value = [file_info]
        mock_client.torrents.info.return_value = [torrent]
        plan = build_unregistered_deletion_plan(
            mock_client,
            [torrent],
            config,
            True,
            ["unregistered"],
            {"unregistered": True},
            str(recycle_bin),
        )
        mock_client.torrents_delete.side_effect = ConnectionError("qBittorrent unavailable")

        with pytest.raises(ConnectionError, match="unavailable"):
            delete_torrents_and_files(
                mock_client,
                config,
                True,
                ["unregistered"],
                {"unregistered": True},
                False,
                [torrent],
                str(recycle_bin),
                plan=plan,
            )

        assert source_file.read_text() == "content"
        assert list(recycle_bin.rglob("movie.mkv")) == []
