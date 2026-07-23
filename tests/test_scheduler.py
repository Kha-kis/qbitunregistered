"""Tests for the scheduler command."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from qbitunregistered.scheduler import run_script


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
