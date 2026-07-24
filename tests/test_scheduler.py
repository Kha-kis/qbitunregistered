"""Tests for the scheduler command."""

import os
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
        cwd=None,
    )


def test_run_script_uses_explicit_execution_cwd() -> None:
    config_path = Path("/tmp/custom-qbitunregistered.json")
    checkout_path = Path("/tmp/qbitunregistered-checkout")

    with patch("qbitunregistered.scheduler.subprocess.run") as run:
        run.return_value.stdout = ""
        run_script(config_path, execution_cwd=checkout_path)

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
        cwd=checkout_path,
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


def test_scheduler_propagates_legacy_execution_cwd(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    checkout_path = tmp_path / "checkout"
    config_path.write_text(
        '{"host":"localhost:8080","username":"admin","password":"password",'
        '"scheduled_times":["09:00"],"scheduled_operations":["orphaned"]}',
        encoding="utf-8",
    )

    with (
        patch("qbitunregistered.scheduler.schedule.every") as every,
        patch("qbitunregistered.scheduler.schedule.run_pending", side_effect=KeyboardInterrupt),
    ):
        assert main(["--config", str(config_path)], execution_cwd=checkout_path) == 0

    every.return_value.day.at.return_value.do.assert_called_once_with(
        run_script,
        config_path,
        ["orphaned"],
        execution_cwd=checkout_path,
    )


def test_compatibility_wrapper_uses_adjacent_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wrapper_path = Path(__file__).resolve().parents[1] / "scheduler.py"
    monkeypatch.chdir(tmp_path)

    with patch("qbitunregistered.scheduler.main", return_value=0) as scheduler_main:
        with pytest.raises(SystemExit) as exit_info:
            runpy.run_path(str(wrapper_path), run_name="__main__")

    assert exit_info.value.code == 0
    scheduler_main.assert_called_once_with(
        default_config_path=wrapper_path.with_name("config.json"),
        execution_cwd=wrapper_path.parent,
    )


def test_compatibility_wrapper_starts_without_install_from_outside_checkout(tmp_path: Path) -> None:
    """The root wrapper imports its checkout package when invoked by absolute path."""
    wrapper_path = Path(__file__).resolve().parents[1] / "scheduler.py"
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"host":"localhost:8080","username":"admin","password":"password","scheduled_times":[]}',
        encoding="utf-8",
    )
    dependency_stubs = tmp_path / "dependency-stubs"
    dependency_stubs.mkdir()
    (dependency_stubs / "schedule.py").write_text("", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(dependency_stubs)

    result = subprocess.run(
        [
            sys.executable,
            "-S",
            str(wrapper_path),
            "--config",
            str(config_path),
        ],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "No scheduled_times found" in result.stdout
