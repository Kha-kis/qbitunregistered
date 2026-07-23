"""Tests for tag operations."""

from datetime import datetime
from collections import defaultdict
from unittest.mock import Mock, patch


class MockTorrent:
    """Mock torrent object for testing."""

    def __init__(self, name, hash_val, creation_date=None, state="complete"):
        self.name = name
        self.hash = hash_val
        # Convert datetime to timestamp for added_on
        creation_dt = creation_date or datetime.now()
        self.added_on = int(creation_dt.timestamp())

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
        from qbitunregistered.operations.tag_by_age import tag_by_age
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
        from qbitunregistered.operations.tag_by_age import tag_by_age

        client = MockClient()
        torrents = [MockTorrent("test", "hash1", datetime.now())]

        # Run in dry-run mode
        tag_by_age(client, torrents, {}, dry_run=True)

        # Should not have made any API calls in dry-run
        assert len(client.api_calls) == 0

    def test_empty_torrent_list(self):
        """Test handling of empty torrent list."""
        from qbitunregistered.operations.tag_by_age import tag_by_age

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
        from qbitunregistered.operations.auto_remove import auto_remove

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
        from qbitunregistered.operations.auto_remove import auto_remove

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
        from qbitunregistered.operations.torrent_management import pause_torrents

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
        from qbitunregistered.operations.torrent_management import resume_torrents

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
        from qbitunregistered.operations.torrent_management import pause_torrents

        client = MockClient()
        client.torrents_pause = lambda torrent_hashes: client.api_calls.append(("pause", torrent_hashes))

        torrents = [MockTorrent("torrent1", "hash1")]

        # Pause in dry-run mode
        pause_torrents(client, torrents, dry_run=True)

        # Should not have made API call
        assert len(client.api_calls) == 0

    def test_empty_torrent_list_handling(self):
        """Test handling of empty torrent lists."""
        from qbitunregistered.operations.torrent_management import pause_torrents, resume_torrents

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
        from qbitunregistered.operations.auto_tmm import apply_auto_tmm_per_torrent

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
        from qbitunregistered.operations.auto_tmm import apply_auto_tmm_per_torrent

        client = MockClient()
        client.torrents_set_auto_management = lambda enable, torrent_hashes: client.api_calls.append(
            ("auto_tmm", enable, torrent_hashes)
        )

        torrents = [MockTorrent("torrent1", "hash1")]

        # Apply in dry-run mode
        apply_auto_tmm_per_torrent(client, torrents, dry_run=True)

        # Should not have made API call
        assert len(client.api_calls) == 0


class TestTrackerTagging:
    """Test tracker-based batching."""

    def test_batches_tags_and_limits(self):
        from qbitunregistered.operations.tag_by_tracker import tag_by_tracker

        client = Mock()
        torrents = [MockTorrent("one", "hash1"), MockTorrent("two", "hash2")]
        tracker_config = {
            "tag": "example",
            "seed_time_limit": "120",
            "seed_ratio_limit": "2.5",
        }

        with patch(
            "qbitunregistered.operations.tag_by_tracker.find_tracker_config",
            return_value=tracker_config,
        ):
            tag_by_tracker(client, torrents, {}, dry_run=False)

        client.torrents_add_tags.assert_called_once_with(torrent_hashes=["hash1", "hash2"], tags="example")
        client.torrents_set_share_limits.assert_called_once_with(
            torrent_hashes=["hash1", "hash2"],
            ratio_limit=2.5,
            seeding_time_limit=120,
        )

    def test_dry_run_does_not_mutate(self):
        from qbitunregistered.operations.tag_by_tracker import tag_by_tracker

        client = Mock()
        with patch(
            "qbitunregistered.operations.tag_by_tracker.find_tracker_config",
            return_value={"tag": "example"},
        ):
            tag_by_tracker(client, [MockTorrent("one", "hash1")], {}, dry_run=True)

        client.torrents_add_tags.assert_not_called()


class TestCrossSeedTagging:
    """Test file-structure based cross-seed tagging."""

    def test_groups_cross_seeded_and_unique_torrents(self):
        from qbitunregistered.operations.tag_cross_seeding import tag_cross_seeds

        client = Mock()
        torrents = [
            Mock(name="one", hash="hash1", save_path="/data"),
            Mock(name="two", hash="hash2", save_path="/data"),
            Mock(name="unique", hash="hash3", save_path="/other"),
        ]

        with patch(
            "qbitunregistered.operations.tag_cross_seeding.fetch_torrent_files",
            side_effect=[
                [{"name": "shared.mkv"}],
                [{"name": "shared.mkv"}],
                [{"name": "unique.mkv"}],
            ],
        ):
            tag_cross_seeds(client, torrents)

        client.torrents_remove_tags.assert_any_call(torrent_hashes=["hash1", "hash2"], tags="not-cross-seeding")
        client.torrents_add_tags.assert_any_call(torrent_hashes=["hash1", "hash2"], tags="cross-seed")
        client.torrents_add_tags.assert_any_call(torrent_hashes=["hash3"], tags="not-cross-seeding")

    def test_dry_run_does_not_mutate(self):
        from qbitunregistered.operations.tag_cross_seeding import tag_cross_seeds

        client = Mock()
        torrent = Mock(name="one", hash="hash1", save_path="/data")
        with patch(
            "qbitunregistered.operations.tag_cross_seeding.fetch_torrent_files",
            return_value=[{"name": "one.mkv"}],
        ):
            tag_cross_seeds(client, [torrent], dry_run=True)

        client.torrents_add_tags.assert_not_called()
        client.torrents_remove_tags.assert_not_called()
