"""Pytest configuration and fixtures for qbitunregistered tests."""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
from collections.abc import Generator

import pytest

from qbitunregistered.cache import clear_cache

_WINDOWS_CRT_DESCRIPTOR_LIMIT = 8192
_WINDOWS_REGULAR_DESCRIPTOR_BASELINE = pytest.StashKey[dict[int, os.stat_result]]()


def _diagnostic_descriptor_stat(descriptor: int) -> os.stat_result | None:
    """Return one CRT descriptor stat for the temporary Windows diagnostic."""
    try:
        return os.fstat(descriptor)
    except OSError as error:
        if error.errno == errno.EBADF:
            return None
        raise


def _collect_windows_unsafe_regular_descriptors() -> dict[int, os.stat_result]:
    """Collect regular CRT descriptors rejected by the tracker boundary."""
    stdout_stderr = tuple(
        descriptor_stat for descriptor in (1, 2) if (descriptor_stat := _diagnostic_descriptor_stat(descriptor)) is not None
    )
    descriptors: dict[int, os.stat_result] = {}
    for descriptor in range(3, _WINDOWS_CRT_DESCRIPTOR_LIMIT):
        descriptor_stat = _diagnostic_descriptor_stat(descriptor)
        if descriptor_stat is None or not stat.S_ISREG(descriptor_stat.st_mode):
            continue
        if any(os.path.samestat(descriptor_stat, stdio_stat) for stdio_stat in stdout_stderr):
            continue
        descriptors[descriptor] = descriptor_stat
    return descriptors


def _new_windows_regular_descriptors(
    before: dict[int, os.stat_result],
    after: dict[int, os.stat_result],
) -> dict[int, os.stat_result]:
    """Return descriptor numbers opened or rebound to another regular file."""
    return {
        descriptor: descriptor_stat
        for descriptor, descriptor_stat in after.items()
        if (previous := before.get(descriptor)) is None or not os.path.samestat(descriptor_stat, previous)
    }


def _format_windows_descriptor_metadata(descriptor: int, descriptor_stat: os.stat_result) -> str:
    """Format path-free metadata for one leaked Windows descriptor."""
    stdin_stat = _diagnostic_descriptor_stat(0)
    stdio_aliases = "0" if stdin_stat is not None and os.path.samestat(descriptor_stat, stdin_stat) else "none"
    try:
        inheritable = str(os.get_inheritable(descriptor)).lower()
    except OSError:
        inheritable = "unknown"
    return f"descriptor={descriptor}; stdio_aliases={stdio_aliases}; inheritable={inheritable}"


def _format_windows_descriptor_failure(
    nodeid: str,
    descriptor: int,
    descriptor_stat: os.stat_result,
) -> str:
    """Format the temporary diagnostic failure without descriptor-backed paths."""
    metadata = _format_windows_descriptor_metadata(descriptor, descriptor_stat)
    return f"nodeid={nodeid}; {metadata}"


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Snapshot regular descriptors before each Windows test sets up fixtures."""
    if sys.platform == "win32":
        item.stash[_WINDOWS_REGULAR_DESCRIPTOR_BASELINE] = _collect_windows_unsafe_regular_descriptors()


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(
    item: pytest.Item,
    nextitem: pytest.Item | None,
) -> Generator[None, None, None]:
    """Fail after fixture teardown when a Windows test leaked a regular fd."""
    del nextitem
    yield
    if sys.platform != "win32":
        return
    before = item.stash.get(_WINDOWS_REGULAR_DESCRIPTOR_BASELINE, {})
    after = _collect_windows_unsafe_regular_descriptors()
    leaked = _new_windows_regular_descriptors(before, after)
    if not leaked:
        return
    descriptor = min(leaked)
    pytest.fail(
        _format_windows_descriptor_failure(item.nodeid, descriptor, leaked[descriptor]),
        pytrace=False,
    )


@pytest.fixture(autouse=True)
def isolate_execution_cache():
    """Prevent execution-scoped API metadata from leaking between tests."""
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def valid_config():
    """Provide a valid configuration dictionary for testing."""
    return {
        "host": "localhost:8080",
        "username": "admin",
        "password": "password",
        "dry_run": True,
        "default_unregistered_tag": "unregistered",
        "cross_seeding_tag": "unregistered:crossseeding",
        "other_issues_tag": "issue",
        "use_delete_tags": False,
        "use_delete_files": False,
        "delete_tags": ["unregistered", "unregistered:crossseeding"],
        "delete_files": {"unregistered": False, "unregistered:crossseeding": False},
        "exclude_files": ["*.!qB", "*_unpackerred"],
        "exclude_dirs": ["/path/to/exclude/*", "/data/torrents/temp/"],
        "unregistered": ["This torrent does not exist", "Unregistered torrent", "Torrent not found", "starts_with:Trump"],
        "target_dir": "/path/to/target/dir",
        "auto_tmm_enabled": True,
        "tracker_tags": {"aither": {"tag": "AITHER", "seed_time_limit": 100, "seed_ratio_limit": 1}},
        "scheduled_times": ["09:00", "15:00", "21:00"],
    }


@pytest.fixture
def minimal_config():
    """Provide a minimal valid configuration."""
    return {"host": "localhost:8080", "username": "admin", "password": "password"}


@pytest.fixture
def unregistered_patterns():
    """Provide common unregistered patterns for testing."""
    return [
        "Unregistered torrent",
        "Torrent not found",
        "This torrent does not exist",
        "starts_with:Trump",
        "starts_with:Error",
    ]


@pytest.fixture
def temp_config_file(tmp_path, valid_config):
    """Create a temporary config file for testing."""
    config_path = tmp_path / "config.json"
    with open(config_path, "w") as f:
        json.dump(valid_config, f, indent=2)
    return config_path


@pytest.fixture
def temp_directory_structure(tmp_path):
    """Create a temporary directory structure for testing file operations."""
    # Create directory structure
    dirs = [
        tmp_path / "torrents" / "completed",
        tmp_path / "torrents" / "incomplete",
        tmp_path / "torrents" / "temp",
    ]
    for dir_path in dirs:
        dir_path.mkdir(parents=True, exist_ok=True)

    # Create some files
    files = [
        tmp_path / "torrents" / "completed" / "movie.mkv",
        tmp_path / "torrents" / "completed" / "movie.srt",
        tmp_path / "torrents" / "incomplete" / "download.tmp",
        tmp_path / "torrents" / "temp" / "temp_file.txt",
    ]
    for file_path in files:
        file_path.touch()

    return tmp_path


@pytest.fixture
def mock_torrent_file():
    """Mock torrent file object."""

    class MockFile:
        def __init__(self, name):
            self.name = name

    return MockFile


@pytest.fixture
def mock_torrent(mock_torrent_file):
    """Mock torrent object."""

    class MockTorrent:
        def __init__(self, name, save_path, files, trackers=None):
            self.name = name
            self.save_path = save_path
            self.files = [mock_torrent_file(f) for f in files]
            self.trackers = trackers or []
            self.hash = f"hash_{name}"
            self.tags = []

    return MockTorrent


@pytest.fixture
def mock_tracker():
    """Mock tracker object."""

    class MockTracker:
        def __init__(self, msg, status=2):
            self.msg = msg
            self.status = status

    return MockTracker
