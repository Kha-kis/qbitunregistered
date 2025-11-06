"""Tests for tag operations."""

import pytest
from datetime import datetime
from collections import defaultdict


class MockTorrent:
    """Mock torrent object for testing."""

    def __init__(self, name, hash_val, creation_date=None, state="complete"):
        self.name = name
        self.hash = hash_val
        self.creation_date = creation_date or datetime.now()

        # Mock state enum
        class MockStateEnum:
            is_complete = state == "complete"

        self.state_enum = MockStateEnum()


class MockClient:
    """Mock qBittorrent client for testing."""

    def __init__(self):
        self.tagged_torrents = defaultdict(list)
        self.api_calls = []

    def torrents_add_tags(self, torrent_hashes, tags):
        """Mock add tags method."""
        self.api_calls.append(("add_tags", torrent_hashes, tags))
        if isinstance(torrent_hashes, list):
            for hash_val in torrent_hashes:
                self.tagged_torrents[hash_val].extend(tags)
        else:
            self.tagged_torrents[torrent_hashes].extend(tags)

    def torrents_files(self, torrent_hash):
        """Mock files method."""
        self.api_calls.append(("get_files", torrent_hash))
        # Return mock file list
        return [{"name": "file1.mkv"}, {"name": "file2.mkv"}]


class TestTagByAge:
    """Test tag_by_age functionality."""

    def test_age_bucket_assignments(self):
        """Test that torrents are assigned to correct age buckets."""
        from scripts.tag_by_age import tag_by_age
        from datetime import datetime, timedelta

        client = MockClient()

        # Create torrents with different ages
        torrents = [
            MockTorrent("new", "hash1", datetime.now()),  # 0 months
            MockTorrent("old2", "hash2", datetime.now() - timedelta(days=60)),  # ~2 months
            MockTorrent("old5", "hash3", datetime.now() - timedelta(days=150)),  # ~5 months
            MockTorrent("very_old", "hash4", datetime.now() - timedelta(days=365)),  # ~12 months
        ]

        # Run tagging
        tag_by_age(client, torrents, {}, dry_run=False)

        # Verify API calls were batched
        assert len(client.api_calls) <= 7  # Max 7 age buckets

        # Verify torrents were tagged (check that calls were made)
        assert len(client.api_calls) > 0

    def test_dry_run_mode(self):
        """Test that dry_run mode doesn't make API calls."""
        from scripts.tag_by_age import tag_by_age

        client = MockClient()
        torrents = [MockTorrent("test", "hash1", datetime.now())]

        # Run in dry-run mode
        tag_by_age(client, torrents, {}, dry_run=True)

        # Should not have made any API calls in dry-run
        assert len(client.api_calls) == 0

    def test_empty_torrent_list(self):
        """Test handling of empty torrent list."""
        from scripts.tag_by_age import tag_by_age

        client = MockClient()
        torrents = []

        # Should not raise an error
        tag_by_age(client, torrents, {}, dry_run=False)

        # Should not have made any API calls
        assert len(client.api_calls) == 0


class TestAutoRemove:
    """Test auto_remove functionality."""

    def test_removes_completed_torrents(self):
        """Test that completed torrents are identified."""
        from scripts.auto_remove import auto_remove

        client = MockClient()
        client.torrents_delete = lambda torrent_hashes, delete_files: client.api_calls.append(("delete", torrent_hashes))

        torrents = [
            MockTorrent("completed1", "hash1", state="complete"),
            MockTorrent("completed2", "hash2", state="complete"),
            MockTorrent("incomplete", "hash3", state="downloading"),
        ]

        # Run auto remove
        auto_remove(client, torrents, dry_run=False)

        # Should have tried to delete 2 completed torrents
        delete_calls = [call for call in client.api_calls if call[0] == "delete"]
        assert len(delete_calls) == 2

    def test_dry_run_no_deletion(self):
        """Test that dry_run mode doesn't delete torrents."""
        from scripts.auto_remove import auto_remove

        client = MockClient()
        client.torrents_delete = lambda torrent_hashes, delete_files: client.api_calls.append(("delete", torrent_hashes))

        torrents = [
            MockTorrent("completed1", "hash1", state="complete"),
            MockTorrent("completed2", "hash2", state="complete"),
        ]

        # Run in dry-run mode
        auto_remove(client, torrents, dry_run=True)

        # Should not have deleted anything
        delete_calls = [call for call in client.api_calls if call[0] == "delete"]
        assert len(delete_calls) == 0


class TestTorrentManagement:
    """Test torrent management functions."""

    def test_pause_torrents_batched(self):
        """Test that pause operation is batched."""
        from scripts.torrent_management import pause_torrents

        client = MockClient()
        client.torrents_pause = lambda torrent_hashes: client.api_calls.append(("pause", torrent_hashes))

        torrents = [
            MockTorrent("torrent1", "hash1"),
            MockTorrent("torrent2", "hash2"),
            MockTorrent("torrent3", "hash3"),
        ]

        # Pause torrents
        pause_torrents(client, torrents, dry_run=False)

        # Should be a single batched API call
        pause_calls = [call for call in client.api_calls if call[0] == "pause"]
        assert len(pause_calls) == 1

        # Should have all 3 hashes in the batch
        hashes = pause_calls[0][1]
        assert len(hashes) == 3

    def test_resume_torrents_batched(self):
        """Test that resume operation is batched."""
        from scripts.torrent_management import resume_torrents

        client = MockClient()
        client.torrents_resume = lambda torrent_hashes: client.api_calls.append(("resume", torrent_hashes))

        torrents = [
            MockTorrent("torrent1", "hash1"),
            MockTorrent("torrent2", "hash2"),
        ]

        # Resume torrents
        resume_torrents(client, torrents, dry_run=False)

        # Should be a single batched API call
        resume_calls = [call for call in client.api_calls if call[0] == "resume"]
        assert len(resume_calls) == 1

    def test_pause_dry_run(self):
        """Test pause in dry-run mode."""
        from scripts.torrent_management import pause_torrents

        client = MockClient()
        client.torrents_pause = lambda torrent_hashes: client.api_calls.append(("pause", torrent_hashes))

        torrents = [MockTorrent("torrent1", "hash1")]

        # Pause in dry-run mode
        pause_torrents(client, torrents, dry_run=True)

        # Should not have made API call
        assert len(client.api_calls) == 0

    def test_empty_torrent_list_handling(self):
        """Test handling of empty torrent lists."""
        from scripts.torrent_management import pause_torrents, resume_torrents

        client = MockClient()
        client.torrents_pause = lambda torrent_hashes: client.api_calls.append(("pause", torrent_hashes))
        client.torrents_resume = lambda torrent_hashes: client.api_calls.append(("resume", torrent_hashes))

        # Should handle empty lists gracefully
        pause_torrents(client, [], dry_run=False)
        resume_torrents(client, [], dry_run=False)

        # Should not have made any API calls
        assert len(client.api_calls) == 0


class TestAutoTMM:
    """Test auto TMM functionality."""

    def test_auto_tmm_batched(self):
        """Test that auto TMM is applied in batch."""
        from scripts.auto_tmm import apply_auto_tmm_per_torrent

        client = MockClient()
        client.torrents_set_auto_management = lambda enable, torrent_hashes: client.api_calls.append(
            ("auto_tmm", enable, torrent_hashes)
        )

        torrents = [
            MockTorrent("torrent1", "hash1"),
            MockTorrent("torrent2", "hash2"),
            MockTorrent("torrent3", "hash3"),
        ]

        # Apply auto TMM
        apply_auto_tmm_per_torrent(client, torrents, dry_run=False)

        # Should be a single batched API call
        tmm_calls = [call for call in client.api_calls if call[0] == "auto_tmm"]
        assert len(tmm_calls) == 1

        # Should enable TMM
        assert tmm_calls[0][1] is True

        # Should have all 3 hashes
        hashes = tmm_calls[0][2]
        assert len(hashes) == 3

    def test_auto_tmm_dry_run(self):
        """Test auto TMM in dry-run mode."""
        from scripts.auto_tmm import apply_auto_tmm_per_torrent

        client = MockClient()
        client.torrents_set_auto_management = lambda enable, torrent_hashes: client.api_calls.append(
            ("auto_tmm", enable, torrent_hashes)
        )

        torrents = [MockTorrent("torrent1", "hash1")]

        # Apply in dry-run mode
        apply_auto_tmm_per_torrent(client, torrents, dry_run=True)

        # Should not have made API call
        assert len(client.api_calls) == 0
