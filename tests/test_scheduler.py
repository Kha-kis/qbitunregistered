"""Tests for the scheduler command."""

import runpy
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from qbitunregistered.scheduler import main, run_script


def test_run_script_forwards_config_path() -> None:
    config_path = Path("/tmp/custom-qbitunregistered.json")

    with patch("qbitunregistered.scheduler.subprocess.run") as run:
        run.return_value.stdout = ""
        run_script(config_path)

    run.assert_called_once_with(
        [
            sys.executable,
            "-m",
            "qbitunregistered",
            "--config",
            str(config_path),
            "--yes",
        ],
        timeout=3600,
        check=True,
        capture_output=True,
        text=True,
    )


def test_run_script_handles_failure() -> None:
    error = subprocess.CalledProcessError(2, ["qbitunregistered"], stderr="bad config")

    with patch("qbitunregistered.scheduler.subprocess.run", side_effect=error):
        run_script("/tmp/config.json")


def test_run_script_forwards_every_configured_operation() -> None:
    operations = [
        "orphaned",
        "unregistered",
        "tag_by_tracker",
        "seeding_management",
        "auto_tmm",
        "pause_torrents",
        "resume_torrents",
        "auto_remove",
        "create_hard_links",
        "tag_by_age",
        "tag_by_cross_seed",
    ]

    with patch("qbitunregistered.scheduler.subprocess.run") as run:
        run.return_value.stdout = ""
        run_script("/tmp/config.json", operations)

    command = run.call_args.args[0]
    assert command[-1] == "--yes"
    for expected_flag in [
        "--orphaned",
        "--unregistered",
        "--tag-by-tracker",
        "--seeding-management",
        "--auto-tmm",
        "--pause-torrents",
        "--resume-torrents",
        "--auto-remove",
        "--create-hard-links",
        "--tag-by-age",
        "--tag-by-cross-seed",
    ]:
        assert expected_flag in command


def test_scheduler_uses_working_directory_config_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.json").write_text(
        '{"host":"localhost:8080","username":"admin","password":"password","scheduled_times":[]}',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0


def test_scheduler_rejects_times_without_operations(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"host":"localhost:8080","username":"admin","password":"password",'
        '"scheduled_times":["09:00"],"scheduled_operations":[]}',
        encoding="utf-8",
    )

    assert main(["--config", str(config_path)]) == 1


def test_scheduler_rejects_scheduled_hard_links_without_target_dir(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"host":"localhost:8080","username":"admin","password":"password",'
        '"scheduled_times":["09:00"],"scheduled_operations":["create_hard_links"]}',
        encoding="utf-8",
    )

    with patch("qbitunregistered.scheduler.schedule.every") as every:
        assert main(["--config", str(config_path)]) == 1

    every.assert_not_called()


def test_compatibility_wrapper_uses_adjacent_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper_path = Path(__file__).resolve().parents[1] / "scheduler.py"
    monkeypatch.chdir(tmp_path)

    with patch("qbitunregistered.scheduler.main", return_value=0) as scheduler_main:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(wrapper_path), run_name="__main__")

    assert exit_info.value.code == 0
    scheduler_main.assert_called_once_with(
        default_config_path=wrapper_path.with_name("config.json"),
    )
