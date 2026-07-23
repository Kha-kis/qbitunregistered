"""Tests for the command-line coordinator."""

import json
from unittest.mock import Mock, patch

import pytest

from qbitunregistered.cli import EXIT_CONFIG_ERROR, EXIT_SUCCESS, main


def test_main_runs_with_minimal_config(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "host": "localhost:8080",
                "username": "admin",
                "password": "password",
                "dry_run": True,
            }
        ),
        encoding="utf-8",
    )
    client = Mock()
    client.torrents.info.return_value = []

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(["--config", str(config_path), "--yes"])

    assert result == EXIT_SUCCESS
    client.auth_log_out.assert_called_once_with()
    notifications.return_value.send_summary.assert_called_once_with({"succeeded": [], "failed": []})


def test_main_reports_missing_config(tmp_path) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--config", str(tmp_path / "missing.json"), "--yes"])

    assert error.value.code == EXIT_CONFIG_ERROR


def test_main_requires_target_for_hard_links(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "host": "localhost:8080",
                "username": "admin",
                "password": "password",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as error:
        main(["--config", str(config_path), "--create-hard-links", "--yes"])

    assert error.value.code == EXIT_CONFIG_ERROR
