"""Pytest configuration and fixtures for qbitunregistered tests."""
import pytest
import json
from pathlib import Path


@pytest.fixture
def valid_config():
    """Provide a valid configuration dictionary for testing."""
    return {
        'host': 'localhost:8080',
        'username': 'admin',
        'password': 'password',
        'dry_run': True,
        'default_unregistered_tag': 'unregistered',
        'cross_seeding_tag': 'unregistered:crossseeding',
        'other_issues_tag': 'issue',
        'use_delete_tags': False,
        'use_delete_files': False,
        'delete_tags': ['unregistered', 'unregistered:crossseeding'],
        'delete_files': {
            'unregistered': False,
            'unregistered:crossseeding': False
        },
        'exclude_files': ['*.!qB', '*_unpackerred'],
        'exclude_dirs': ['/path/to/exclude/*', '/data/torrents/temp/'],
        'unregistered': [
            'This torrent does not exist',
            'Unregistered torrent',
            'Torrent not found',
            'starts_with:Trump'
        ],
        'target_dir': '/path/to/target/dir',
        'auto_tmm_enabled': True,
        'tracker_tags': {
            'aither': {
                'tag': 'AITHER',
                'seed_time_limit': 100,
                'seed_ratio_limit': 1
            }
        },
        'scheduled_times': ['09:00', '15:00', '21:00']
    }


@pytest.fixture
def minimal_config():
    """Provide a minimal valid configuration."""
    return {
        'host': 'localhost:8080',
        'username': 'admin',
        'password': 'password'
    }


@pytest.fixture
def unregistered_patterns():
    """Provide common unregistered patterns for testing."""
    return [
        'Unregistered torrent',
        'Torrent not found',
        'This torrent does not exist',
        'starts_with:Trump',
        'starts_with:Error'
    ]


@pytest.fixture
def temp_config_file(tmp_path, valid_config):
    """Create a temporary config file for testing."""
    config_path = tmp_path / "config.json"
    with open(config_path, 'w') as f:
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
