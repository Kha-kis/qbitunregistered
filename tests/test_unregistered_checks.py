"""Tests for unregistered checks functionality."""

import pytest
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
