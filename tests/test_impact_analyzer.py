"""Tests for the impact analyzer module."""

import pytest
from unittest.mock import Mock, patch
from qbitunregistered.impact import (
    ImpactAnalysisError,
    ImpactSummary,
    analyze_impact,
    _analyze_create_hard_links,
    _analyze_seeding_management,
    _analyze_tag_cross_seeding,
    _analyze_tag_by_tracker,
    _analyze_unregistered,
    _analyze_pause,
    _analyze_resume,
)


class TestImpactSummary:
    """Tests for ImpactSummary class."""

    def test_empty_summary(self):
        """Test that a new summary is empty."""
        summary = ImpactSummary()
        assert summary.is_empty()
        assert summary.get_total_torrents_affected() == 0

    def test_add_deletion(self):
        """Test adding deletion impacts."""
        summary = ImpactSummary()
        summary.add_deletion("unregistered", "hash1", 1024 * 1024 * 100)  # 100 MB
        summary.add_deletion("unregistered", "hash2", 1024 * 1024 * 200)  # 200 MB

        assert not summary.is_empty()
        assert len(summary.torrents_to_delete["unregistered"]) == 2
        assert summary.disk_to_free_bytes == 1024 * 1024 * 300

    def test_add_tagging(self):
        """Test adding tagging impacts."""
        summary = ImpactSummary()
        summary.add_tagging("tracker1", "hash1")
        summary.add_tagging("tracker1", "hash2")
        summary.add_tagging("tracker2", "hash3")

        assert not summary.is_empty()
        assert len(summary.torrents_to_tag["tracker1"]) == 2
        assert len(summary.torrents_to_tag["tracker2"]) == 1

    def test_add_orphaned_file(self):
        """Test adding orphaned file impacts."""
        summary = ImpactSummary()
        summary.add_orphaned_file("/path/to/file1.txt", 1024)
        summary.add_orphaned_file("/path/to/file2.txt", 2048)

        assert not summary.is_empty()
        assert len(summary.orphaned_files) == 2
        assert summary.disk_to_free_bytes == 3072

    def test_add_pause_resume(self):
        """Test adding pause and resume impacts."""
        summary = ImpactSummary()
        summary.add_pause("hash1")
        summary.add_pause("hash2")
        summary.add_resume("hash3")

        assert not summary.is_empty()
        assert len(summary.torrents_to_pause) == 2
        assert len(summary.torrents_to_resume) == 1

    def test_get_total_torrents_affected(self):
        """Test counting unique affected torrents."""
        summary = ImpactSummary()
        summary.add_deletion("tag1", "hash1", 0)
        summary.add_deletion("tag1", "hash2", 0)
        summary.add_tagging("tag2", "hash2")  # Duplicate
        summary.add_tagging("tag2", "hash3")
        summary.add_pause("hash4")

        # hash1, hash2, hash3, hash4 = 4 unique
        assert summary.get_total_torrents_affected() == 4

    def test_warning_large_deletion(self):
        """Test warning for large disk space deletion."""
        summary = ImpactSummary()
        # Add 60 GB worth of deletions
        summary.disk_to_free_bytes = 60 * 1024**3

        warnings = summary.get_warning_messages()
        assert len(warnings) > 0
        assert any("GB will be freed" in w for w in warnings)

    def test_warning_many_deletions(self):
        """Test warning for many torrent deletions."""
        summary = ImpactSummary()
        for i in range(25):
            summary.add_deletion("unregistered", f"hash{i}", 0)

        warnings = summary.get_warning_messages()
        assert len(warnings) > 0
        assert any("torrents will be deleted" in w for w in warnings)

    def test_warning_many_orphaned_files(self):
        """Test warning for many orphaned files."""
        summary = ImpactSummary()
        for i in range(60):
            summary.add_orphaned_file(f"/path/file{i}", 0)

        warnings = summary.get_warning_messages()
        assert len(warnings) > 0
        assert any("orphaned files will be deleted" in w for w in warnings)

    def test_format_summary_empty(self):
        """Test formatting empty summary."""
        summary = ImpactSummary()
        formatted = summary.format_summary()

        assert "DRY-RUN IMPACT PREVIEW" in formatted
        assert "No changes will be made" in formatted

    def test_format_summary_with_deletions(self):
        """Test formatting summary with deletions."""
        summary = ImpactSummary()
        summary.add_deletion("unregistered", "hash1", 1024**3)  # 1 GB
        summary.add_deletion("unregistered", "hash2", 2 * 1024**3)  # 2 GB

        formatted = summary.format_summary()

        assert "Torrents to DELETE: 2" in formatted
        assert "unregistered" in formatted
        assert "3.00 GB" in formatted or "3.0 GB" in formatted  # Allow both formats

    def test_format_summary_with_tagging(self):
        """Test formatting summary with tagging."""
        summary = ImpactSummary()
        summary.add_tagging("tracker1", "hash1")
        summary.add_tagging("tracker1", "hash2")

        formatted = summary.format_summary()

        assert "Torrents to TAG: 2" in formatted
        assert "tracker1" in formatted

    def test_format_summary_with_orphaned_files(self):
        """Test formatting summary with orphaned files."""
        summary = ImpactSummary()
        for i in range(3):
            summary.add_orphaned_file(f"/path/file{i}.txt", 1024)

        formatted = summary.format_summary(show_details=True)

        assert "Orphaned files to DELETE: 3" in formatted
        assert "/path/file0.txt" in formatted  # First file should be shown

    def test_format_summary_with_details(self):
        """Test formatting summary with details enabled."""
        summary = ImpactSummary()
        summary.add_deletion("unregistered", "hash1", 0)
        summary.add_deletion("unregistered", "hash2", 0)

        formatted = summary.format_summary(show_details=True)

        assert "hash1" in formatted or "hash2" in formatted

    def test_operation_details(self):
        """Test setting and retrieving operation details."""
        summary = ImpactSummary()
        summary.set_operation_detail("unregistered", "found", 10)
        summary.set_operation_detail("unregistered", "cross_seed", 2)

        assert summary.operation_details["unregistered"]["found"] == 10
        assert summary.operation_details["unregistered"]["cross_seed"] == 2

        formatted = summary.format_summary()
        assert "unregistered" in formatted
        assert "found: 10" in formatted


class TestAnalyzeImpact:
    """Tests for analyze_impact function."""

    def test_analyze_empty_operations(self):
        """Test analyzing with no operations."""
        mock_client = Mock()
        torrents = []
        config = {}

        summary = analyze_impact(mock_client, torrents, config, [])

        assert summary.is_empty()

    def test_analyze_unknown_operation(self):
        """Test analyzing with unknown operation."""
        mock_client = Mock()
        torrents = []
        config = {}

        with pytest.raises(ImpactAnalysisError):
            analyze_impact(mock_client, torrents, config, ["unknown_operation"])

    @patch("qbitunregistered.impact._analyze_pause")
    def test_analyze_pause_operation(self, mock_analyze):
        """Test analyzing pause operation."""
        mock_client = Mock()
        torrents = [Mock()]
        config = {}

        analyze_impact(mock_client, torrents, config, ["pause"])

        mock_analyze.assert_called_once()

    @patch("qbitunregistered.impact._analyze_resume")
    def test_analyze_resume_operation(self, mock_analyze):
        """Test analyzing resume operation."""
        mock_client = Mock()
        torrents = [Mock()]
        config = {}

        analyze_impact(mock_client, torrents, config, ["resume"])

        mock_analyze.assert_called_once()


class TestAnalyzeUnregistered:
    """Tests for _analyze_unregistered function."""

    def test_analyze_unregistered_no_torrents(self):
        """Test analyzing unregistered with no torrents."""
        mock_client = Mock()
        torrents = []
        config = {"unregistered": ["not found"]}
        summary = ImpactSummary()

        _analyze_unregistered(mock_client, torrents, config, summary)

        assert summary.is_empty()

    def test_analyze_unregistered_with_matches(self):
        """Test analyzing unregistered with matching torrents."""
        mock_client = Mock()

        # Mock torrent
        mock_torrent = Mock()
        mock_torrent.hash = "hash1"
        mock_torrent.name = "Test Torrent"
        mock_torrent.save_path = "/data"
        mock_torrent.tags = ""

        # Mock tracker response - create tracker objects with .msg attribute
        mock_tracker = Mock()
        mock_tracker.msg = "not registered"
        mock_tracker.url = "http://tracker.example.com"
        mock_tracker.status = 4
        # Also support dict-style access for backward compatibility
        mock_tracker.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker]
        mock_torrent.trackers = [mock_tracker]
        mock_client.torrents_info.return_value = [{"size": 1024**3}]  # 1 GB

        torrents = [mock_torrent]
        config = {
            "unregistered": ["not registered"],
            "default_unregistered_tag": "unregistered",
            "use_delete_tags": False,
        }
        summary = ImpactSummary()

        _analyze_unregistered(mock_client, torrents, config, summary)

        # Should be tagged
        assert len(summary.torrents_to_tag["unregistered"]) == 1
        # Should NOT be deleted (use_delete_tags=False)
        assert len(summary.torrents_to_delete) == 0

    def test_analyze_unregistered_with_deletion(self):
        """Test analyzing unregistered with deletion enabled."""
        mock_client = Mock()

        mock_torrent = Mock()
        mock_torrent.hash = "hash1"
        mock_torrent.save_path = "/data"
        mock_torrent.tags = "unregistered"

        # Mock tracker with .msg attribute
        mock_tracker = Mock()
        mock_tracker.msg = "not registered"
        mock_tracker.url = "http://tracker.example.com"
        mock_tracker.status = 4
        mock_tracker.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker]
        mock_torrent.trackers = [mock_tracker]
        mock_client.torrents_info.return_value = [{"size": 2 * 1024**3}]  # 2 GB

        torrents = [mock_torrent]
        config = {
            "unregistered": ["not registered"],
            "default_unregistered_tag": "unregistered",
            "use_delete_tags": True,
            "delete_tags": ["unregistered"],
        }
        summary = ImpactSummary()

        _analyze_unregistered(mock_client, torrents, config, summary)

        # Should be tagged AND deleted
        assert len(summary.torrents_to_tag["unregistered"]) == 1
        assert len(summary.torrents_to_delete["unregistered"]) == 1
        assert summary.disk_to_free_bytes == 0
        assert "files to permanently delete" not in summary.operation_targets

    def test_analyze_unregistered_uses_ordered_delete_policy(self, tmp_path):
        tracker = Mock(msg="not registered", status=4)
        torrent_file = tmp_path / "movie.mkv"
        torrent_file.write_bytes(b"movie")
        torrent = Mock(
            hash="hash1",
            save_path=str(tmp_path),
            category="movies",
            tags="keep-files, delete-files",
            trackers=[tracker],
        )
        torrent.name = "movie"
        client = Mock()
        file_info = Mock()
        file_info.name = "movie.mkv"
        client.torrents_files.return_value = [file_info]
        config = {
            "unregistered": ["not registered"],
            "default_unregistered_tag": "unregistered",
            "use_delete_tags": True,
            "use_delete_files": True,
            "delete_tags": ["keep-files", "delete-files"],
            "delete_files": {"keep-files": False, "delete-files": True},
        }
        summary = ImpactSummary()

        _analyze_unregistered(client, [torrent], config, summary)

        assert summary.torrents_to_delete["keep-files"] == ["hash1"]
        assert "delete-files" not in summary.torrents_to_delete
        assert summary.disk_to_free_bytes == 0
        assert summary.operation_targets["delete torrent only (keep files)"] == ["hash1"]

        config["delete_tags"] = ["delete-files", "keep-files"]
        summary = ImpactSummary()
        _analyze_unregistered(client, [torrent], config, summary)

        assert summary.torrents_to_delete["delete-files"] == ["hash1"]
        assert summary.disk_to_free_bytes == torrent_file.stat().st_size
        assert summary.operation_targets["permanently delete torrent and files"] == ["hash1"]

    def test_analyze_unregistered_global_file_gate_prevents_file_claim(self):
        tracker = Mock(msg="not registered", status=4)
        torrent = Mock(
            hash="hash1",
            save_path="/data",
            tags="delete-files",
            trackers=[tracker],
            size=1024,
        )
        summary = ImpactSummary()

        _analyze_unregistered(
            Mock(),
            [torrent],
            {
                "unregistered": ["not registered"],
                "use_delete_tags": True,
                "use_delete_files": False,
                "delete_tags": ["delete-files"],
                "delete_files": {"delete-files": True},
            },
            summary,
        )

        assert summary.disk_to_free_bytes == 0
        assert "files to permanently delete" not in summary.operation_targets

    def test_analyze_unregistered_cross_seeding(self):
        """Test analyzing unregistered with cross-seeding detection."""
        mock_client = Mock()

        mock_torrent = Mock()
        mock_torrent.hash = "hash1"
        mock_torrent.save_path = "/data"
        mock_torrent.tags = ""

        # Two trackers: one unregistered, one working
        mock_tracker1 = Mock()
        mock_tracker1.msg = "not registered"
        mock_tracker1.url = "http://tracker1.example.com"
        mock_tracker1.status = 4
        mock_tracker1.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker1.example.com"}.get(k, d)

        mock_tracker2 = Mock()
        mock_tracker2.msg = "Working"
        mock_tracker2.url = "http://tracker2.example.com"
        mock_tracker2.status = 2
        mock_tracker2.get = lambda k, d=None: {"msg": "Working", "url": "http://tracker2.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker1, mock_tracker2]
        mock_torrent.trackers = [mock_tracker1, mock_tracker2]
        working_torrent = Mock(hash="hash2", save_path="/data", tags="")
        working_torrent.trackers = [mock_tracker2]

        torrents = [mock_torrent, working_torrent]
        config = {
            "unregistered": ["not registered"],
            "default_unregistered_tag": "unregistered",
            "cross_seeding_tag": "unregistered:crossseeding",
        }
        summary = ImpactSummary()

        _analyze_unregistered(mock_client, torrents, config, summary)

        # Should use cross-seeding tag
        assert len(summary.torrents_to_tag["unregistered:crossseeding"]) == 1
        assert len(summary.torrents_to_tag.get("unregistered", [])) == 0

    def test_preview_distinguishes_cross_seed_preservation_from_file_deletion(self, tmp_path):
        """Impact output reports shared files as preserved, not authorized for deletion."""
        shared_file = tmp_path / "movie.mkv"
        shared_file.write_text("content")
        tracker = Mock(msg="not registered", status=4)
        source = Mock(
            hash="source",
            save_path=str(tmp_path),
            category="movies",
            tags="unregistered",
            trackers=[tracker],
        )
        source.name = "source"
        peer = Mock(
            hash="peer",
            save_path=str(tmp_path),
            category="movies",
            tags="",
            trackers=[],
        )
        peer.name = "peer"
        file_info = Mock()
        file_info.name = "movie.mkv"
        client = Mock()
        client.torrents_files.return_value = [file_info]
        summary = ImpactSummary()

        _analyze_unregistered(
            client,
            [source, peer],
            {
                "unregistered": ["not registered"],
                "default_unregistered_tag": "unregistered",
                "cross_seeding_tag": "unregistered:crossseeding",
                "use_delete_tags": True,
                "use_delete_files": True,
                "delete_tags": ["unregistered"],
                "delete_files": {"unregistered": True},
            },
            summary,
        )

        assert summary.operation_targets["delete torrent only (preserve cross-seeded files)"] == ["source"]
        assert "permanently delete torrent and files" not in summary.operation_targets
        assert "recycle files, then delete torrent" not in summary.operation_targets
        assert summary.disk_to_free_bytes == 0

    def test_analyze_unregistered_ignores_non_error_tracker_status(self):
        """Preview only matches statuses handled by the real operation."""
        mock_client = Mock()
        mock_torrent = Mock(hash="hash1")
        mock_torrent.save_path = "/data"
        mock_torrent.tags = ""
        mock_tracker = Mock(msg="not registered", url="http://tracker.example.com", status=2)
        mock_tracker.get = lambda key, default=None: {
            "msg": "not registered",
            "url": "http://tracker.example.com",
            "status": 2,
        }.get(key, default)
        mock_client.torrents_trackers.return_value = [mock_tracker]
        mock_torrent.trackers = [mock_tracker]
        summary = ImpactSummary()

        _analyze_unregistered(
            mock_client,
            [mock_torrent],
            {"unregistered": ["not registered"]},
            summary,
        )

        assert summary.is_empty()


class TestAnalyzeTagByTracker:
    """Tests for tracker-tag impact analysis."""

    def test_matches_configured_tracker(self):
        mock_client = Mock()
        mock_client.torrents_trackers.return_value = [{"url": "https://tracker.example.com/announce"}]
        torrent = Mock(hash="hash1")
        summary = ImpactSummary()

        _analyze_tag_by_tracker(
            mock_client,
            [torrent],
            {"tracker_tags": {"tracker.example.com": {"tag": "example"}}},
            summary,
        )

        assert summary.torrents_to_tag["example"] == ["hash1"]

    def test_object_tracker_metadata_is_skipped_like_execution(self):
        from qbitunregistered.cache import clear_cache

        clear_cache()
        mock_client = Mock()
        mock_client.torrents_trackers.return_value = [Mock(url="https://tracker.example.com/announce")]
        torrent = Mock(hash="hash1")
        config = {
            "tracker_tags": {
                "tracker.example.com": {
                    "tag": "example",
                    "seed_time_limit": 60,
                }
            }
        }
        tag_summary = ImpactSummary()
        seeding_summary = ImpactSummary()

        _analyze_tag_by_tracker(mock_client, [torrent], config, tag_summary)
        _analyze_seeding_management(mock_client, [torrent], config, seeding_summary)

        assert tag_summary.is_empty()
        assert seeding_summary.is_empty()


class TestAnalyzeCrossSeeding:
    def test_object_file_metadata_is_skipped_like_execution(self):
        from qbitunregistered.cache import clear_cache

        clear_cache()
        client = Mock()
        client.torrents_files.return_value = [Mock(name="movie.mkv")]
        torrent = Mock(hash="hash1")
        summary = ImpactSummary()

        _analyze_tag_cross_seeding(client, [torrent], {}, summary)

        assert summary.is_empty()


class TestAnalyzeCreateHardLinks:
    def test_previews_concrete_sanitized_destination_and_skips_existing(self, tmp_path):
        from qbitunregistered.operations.create_hardlinks import create_hard_links

        source_root = tmp_path / "source"
        content_root = source_root / "Show"
        content_root.mkdir(parents=True)
        (content_root / "episode.mkv").write_text("episode")
        (content_root / "existing.mkv").write_text("existing")
        target_root = tmp_path / "target"
        existing_target = target_root / "TV_Shows" / "existing.mkv"
        existing_target.parent.mkdir(parents=True)
        existing_target.write_text("already linked")
        torrent = Mock(
            hash="hash1",
            save_path=str(source_root),
            category="TV / Shows",
        )
        torrent.name = "Show"
        torrent.state_enum.is_complete = True
        summary = ImpactSummary()

        _analyze_create_hard_links(
            Mock(),
            [torrent],
            {"target_dir": str(target_root)},
            summary,
        )

        expected_target = target_root / "TV_Shows" / "episode.mkv"
        assert summary.operation_targets["create hard links"] == [str(expected_target)]
        assert summary.hard_link_plan is not None
        assert not expected_target.exists()

        create_hard_links(
            str(target_root),
            [torrent],
            planned_links=summary.hard_link_plan,
        )

        assert expected_target.exists()
        assert expected_target.stat().st_ino == (content_root / "episode.mkv").stat().st_ino

    def test_missing_source_fails_closed_without_mutation(self, tmp_path):
        target_root = tmp_path / "target"
        target_root.mkdir()
        torrent = Mock(
            hash="hash1",
            save_path=str(tmp_path / "source"),
            category="movies",
        )
        torrent.name = "missing.mkv"
        torrent.state_enum.is_complete = True

        with pytest.raises(ImpactAnalysisError):
            analyze_impact(
                Mock(),
                [torrent],
                {"target_dir": str(target_root)},
                ["create_hard_links"],
            )

        assert list(target_root.iterdir()) == []

    def test_duplicate_destination_fails_closed_without_mutation(self, tmp_path):
        target_root = tmp_path / "target"
        target_root.mkdir()
        torrents = []
        for source_name, contents in (("source-a", "first"), ("source-b", "second")):
            source_root = tmp_path / source_name
            source_root.mkdir()
            (source_root / "movie.mkv").write_text(contents)
            torrent = Mock(
                hash=source_name,
                save_path=str(source_root),
                category="movies",
            )
            torrent.name = "movie.mkv"
            torrent.state_enum.is_complete = True
            torrents.append(torrent)

        with pytest.raises(ImpactAnalysisError) as exc_info:
            analyze_impact(
                Mock(),
                torrents,
                {"target_dir": str(target_root)},
                ["create_hard_links"],
            )

        assert "Multiple sources map" in str(exc_info.value.__cause__)
        assert list(target_root.iterdir()) == []

    def test_symlink_source_outside_torrent_content_fails_closed(self, tmp_path):
        source_root = tmp_path / "source"
        content_root = source_root / "Show"
        content_root.mkdir(parents=True)
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("not torrent content")
        (content_root / "episode.mkv").symlink_to(outside_file)
        target_root = tmp_path / "target"
        target_root.mkdir()
        torrent = Mock(
            hash="hash1",
            save_path=str(source_root),
            category="tv",
        )
        torrent.name = "Show"
        torrent.state_enum.is_complete = True

        with pytest.raises(ImpactAnalysisError):
            analyze_impact(
                Mock(),
                [torrent],
                {"target_dir": str(target_root)},
                ["create_hard_links"],
            )

        assert list(target_root.iterdir()) == []

    def test_source_swapped_to_external_symlink_after_planning_is_rejected(self, tmp_path):
        from qbitunregistered.operations.create_hardlinks import HardLinkPlanningError, create_hard_links

        source_root = tmp_path / "source"
        source_root.mkdir()
        source = source_root / "movie.mkv"
        source.write_text("torrent content")
        outside_file = tmp_path / "secret.txt"
        outside_file.write_text("not torrent content")
        target_root = tmp_path / "target"
        target_root.mkdir()
        torrent = Mock(
            hash="hash1",
            save_path=str(source_root),
            category="movies",
        )
        torrent.name = "movie.mkv"
        torrent.state_enum.is_complete = True
        summary = ImpactSummary()
        _analyze_create_hard_links(
            Mock(),
            [torrent],
            {"target_dir": str(target_root)},
            summary,
        )
        assert summary.hard_link_plan is not None

        source.unlink()
        source.symlink_to(outside_file)
        with pytest.raises(HardLinkPlanningError, match="Failed to create 1"):
            create_hard_links(
                str(target_root),
                [torrent],
                planned_links=summary.hard_link_plan,
            )

        assert not (target_root / "movies" / "movie.mkv").exists()


class TestAnalyzePauseResume:
    """Tests for _analyze_pause and _analyze_resume functions."""

    def test_analyze_pause_active_torrents(self):
        """Test analyzing pause for active torrents."""
        mock_client = Mock()

        # Create mock torrents - one paused, one active
        mock_torrent1 = Mock()
        mock_torrent1.hash = "hash1"
        mock_torrent1.state_enum = Mock()
        mock_torrent1.state_enum.is_paused = False

        mock_torrent2 = Mock()
        mock_torrent2.hash = "hash2"
        mock_torrent2.state_enum = Mock()
        mock_torrent2.state_enum.is_paused = True

        torrents = [mock_torrent1, mock_torrent2]
        config = {}
        summary = ImpactSummary()

        _analyze_pause(mock_client, torrents, config, summary)

        # Execution passes every torrent to the batched pause call.
        assert len(summary.torrents_to_pause) == 2
        assert "hash1" in summary.torrents_to_pause
        assert "hash2" in summary.torrents_to_pause

    def test_analyze_resume_paused_torrents(self):
        """Test analyzing resume for paused torrents."""
        mock_client = Mock()

        # Create mock torrents - one paused, one active
        mock_torrent1 = Mock()
        mock_torrent1.hash = "hash1"
        mock_torrent1.state_enum = Mock()
        mock_torrent1.state_enum.is_paused = True

        mock_torrent2 = Mock()
        mock_torrent2.hash = "hash2"
        mock_torrent2.state_enum = Mock()
        mock_torrent2.state_enum.is_paused = False

        torrents = [mock_torrent1, mock_torrent2]
        config = {}
        summary = ImpactSummary()

        _analyze_resume(mock_client, torrents, config, summary)

        # Execution passes every torrent to the batched resume call.
        assert len(summary.torrents_to_resume) == 2
        assert "hash1" in summary.torrents_to_resume
        assert "hash2" in summary.torrents_to_resume


@pytest.mark.integration
class TestImpactAnalyzerIntegration:
    """Integration tests for impact analyzer."""

    def test_full_analysis_workflow(self):
        """Test complete analysis workflow with multiple operations."""
        mock_client = Mock()

        # Create mock torrents
        mock_torrent1 = Mock()
        mock_torrent1.hash = "hash1"
        mock_torrent1.save_path = "/data"
        mock_torrent1.tags = "unregistered"
        mock_torrent1.state_enum = Mock()
        mock_torrent1.state_enum.is_paused = False

        # Mock unregistered tracker
        mock_tracker = Mock()
        mock_tracker.msg = "not registered"
        mock_tracker.url = "http://tracker.example.com"
        mock_tracker.status = 4
        mock_tracker.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker]
        mock_torrent1.trackers = [mock_tracker]
        mock_client.torrents_info.return_value = [{"size": 5 * 1024**3}]

        torrents = [mock_torrent1]
        config = {
            "unregistered": ["not registered"],
            "default_unregistered_tag": "unregistered",
            "use_delete_tags": True,
            "delete_tags": ["unregistered"],
        }

        operations = ["unregistered", "pause"]
        summary = analyze_impact(mock_client, torrents, config, operations)

        # Should have both unregistered and pause impacts
        assert not summary.is_empty()
        assert len(summary.torrents_to_tag) > 0
        assert len(summary.torrents_to_pause) > 0
        assert summary.disk_to_free_bytes == 0

        # Verify formatted output
        formatted = summary.format_summary()
        assert "DELETE" in formatted
        assert "TAG" in formatted
        assert "PAUSE" in formatted
