"""Tests for the impact analyzer module."""

import pytest
from unittest.mock import Mock, MagicMock, patch
from utils.impact_analyzer import (
    ImpactSummary,
    analyze_impact,
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

        # Should not raise, just log warning
        summary = analyze_impact(mock_client, torrents, config, ["unknown_operation"])

        assert summary.is_empty()

    @patch('utils.impact_analyzer._analyze_pause')
    def test_analyze_pause_operation(self, mock_analyze):
        """Test analyzing pause operation."""
        mock_client = Mock()
        torrents = [Mock()]
        config = {}

        analyze_impact(mock_client, torrents, config, ["pause"])

        mock_analyze.assert_called_once()

    @patch('utils.impact_analyzer._analyze_resume')
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

        # Mock tracker response - create tracker objects with .msg attribute
        mock_tracker = Mock()
        mock_tracker.msg = "not registered"
        mock_tracker.url = "http://tracker.example.com"
        # Also support dict-style access for backward compatibility
        mock_tracker.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker]
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

        # Mock tracker with .msg attribute
        mock_tracker = Mock()
        mock_tracker.msg = "not registered"
        mock_tracker.url = "http://tracker.example.com"
        mock_tracker.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker]
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
        assert summary.disk_to_free_bytes == 2 * 1024**3

    def test_analyze_unregistered_cross_seeding(self):
        """Test analyzing unregistered with cross-seeding detection."""
        mock_client = Mock()

        mock_torrent = Mock()
        mock_torrent.hash = "hash1"

        # Two trackers: one unregistered, one working
        mock_tracker1 = Mock()
        mock_tracker1.msg = "not registered"
        mock_tracker1.url = "http://tracker1.example.com"
        mock_tracker1.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker1.example.com"}.get(k, d)

        mock_tracker2 = Mock()
        mock_tracker2.msg = "Working"
        mock_tracker2.url = "http://tracker2.example.com"
        mock_tracker2.get = lambda k, d=None: {"msg": "Working", "url": "http://tracker2.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker1, mock_tracker2]

        torrents = [mock_torrent]
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

        # Only active torrent should be in pause list
        assert len(summary.torrents_to_pause) == 1
        assert "hash1" in summary.torrents_to_pause
        assert "hash2" not in summary.torrents_to_pause

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

        # Only paused torrent should be in resume list
        assert len(summary.torrents_to_resume) == 1
        assert "hash1" in summary.torrents_to_resume
        assert "hash2" not in summary.torrents_to_resume


@pytest.mark.integration
class TestImpactAnalyzerIntegration:
    """Integration tests for impact analyzer."""

    def test_full_analysis_workflow(self):
        """Test complete analysis workflow with multiple operations."""
        mock_client = Mock()

        # Create mock torrents
        mock_torrent1 = Mock()
        mock_torrent1.hash = "hash1"
        mock_torrent1.state_enum = Mock()
        mock_torrent1.state_enum.is_paused = False

        # Mock unregistered tracker
        mock_tracker = Mock()
        mock_tracker.msg = "not registered"
        mock_tracker.url = "http://tracker.example.com"
        mock_tracker.get = lambda k, d=None: {"msg": "not registered", "url": "http://tracker.example.com"}.get(k, d)

        mock_client.torrents_trackers.return_value = [mock_tracker]
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
        assert summary.disk_to_free_bytes > 0

        # Verify formatted output
        formatted = summary.format_summary()
        assert "DELETE" in formatted
        assert "TAG" in formatted
        assert "PAUSE" in formatted
