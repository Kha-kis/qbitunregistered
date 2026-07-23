"""Tests for the command-line coordinator."""

import json
from unittest.mock import Mock, patch

import pytest

from qbitunregistered.cli import EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS, main


def _write_config(tmp_path, **overrides):
    config = {
        "host": "localhost:8080",
        "username": "admin",
        "password": "password",
        "dry_run": False,
        "default_unregistered_tag": "unregistered",
        "cross_seeding_tag": "unregistered:crossseeding",
        "target_dir": str(tmp_path),
    }
    config.update(overrides)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _empty_client(tmp_path):
    client = Mock()
    client.torrents.info.return_value = []
    client.application.default_save_path = str(tmp_path)
    client.torrent_categories.categories = {}
    return client


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


def test_invalid_configured_dry_run_aborts_before_connection(tmp_path) -> None:
    config_path = _write_config(tmp_path, dry_run="true")

    with patch("qbitunregistered.cli.create_client") as create_client:
        with pytest.raises(SystemExit) as error:
            main(["--config", str(config_path), "--auto-remove", "--yes"])

    assert error.value.code == EXIT_CONFIG_ERROR
    create_client.assert_not_called()


@pytest.mark.parametrize(
    ("override", "expected"),
    [("--dry-run", True), ("--no-dry-run", False)],
)
def test_cli_dry_run_override_takes_precedence(tmp_path, override, expected) -> None:
    config_path = _write_config(tmp_path, dry_run="invalid")
    client = _empty_client(tmp_path)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        assert main(["--config", str(config_path), override, "--yes"]) == EXIT_SUCCESS

    assert client.auth_log_out.called


def test_blank_cli_api_key_selects_credentials(tmp_path) -> None:
    config_path = _write_config(tmp_path, api_key="qbt_configured")
    client = _empty_client(tmp_path)
    captured_config = {}

    def capture_config(config):
        captured_config.update(config)
        return client

    with (
        patch("qbitunregistered.cli.create_client", side_effect=capture_config),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        assert (
            main(
                [
                    "--config",
                    str(config_path),
                    "--api-key",
                    "",
                    "--username",
                    "cli-user",
                    "--password",
                    "cli-password",
                    "--yes",
                ]
            )
            == EXIT_SUCCESS
        )

    assert captured_config["api_key"] == ""
    assert captured_config["username"] == "cli-user"
    assert captured_config["password"] == "cli-password"


@pytest.mark.parametrize(
    "operation_flag",
    [
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
    ],
)
def test_declining_confirmation_blocks_every_mutating_flag(tmp_path, operation_flag) -> None:
    config_path = _write_config(tmp_path)
    client = _empty_client(tmp_path)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("builtins.input", return_value="n"),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        assert main(["--config", str(config_path), operation_flag]) == EXIT_SUCCESS

    client.torrents_delete.assert_not_called()
    client.torrents_add_tags.assert_not_called()
    client.torrents_set_share_limits.assert_not_called()
    client.torrents_set_auto_management.assert_not_called()
    client.torrents_pause.assert_not_called()
    client.torrents_resume.assert_not_called()
    notifications.assert_not_called()


def test_analyzer_failure_aborts_before_mutation(tmp_path) -> None:
    config_path = _write_config(tmp_path)
    client = _empty_client(tmp_path)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.impact.analyze_impact", side_effect=RuntimeError("analysis failed")),
    ):
        assert main(["--config", str(config_path), "--pause-torrents"]) == EXIT_GENERAL_ERROR

    client.torrents_pause.assert_not_called()
    client.auth_log_out.assert_called_once_with()


def test_combined_preview_targets_are_reused_for_execution(tmp_path, capsys) -> None:
    from qbitunregistered.operations.orphaned import check_files_on_disk

    orphan = tmp_path / "orphan.mkv"
    orphan.write_text("orphan")
    config_path = _write_config(tmp_path)
    client = _empty_client(tmp_path)
    torrent = Mock(hash="completed", save_path=str(tmp_path), category="", tags="", files=[])
    torrent.name = "completed.mkv"
    torrent.state_enum.is_complete = True
    client.torrents.info.return_value = [torrent]

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("builtins.input", return_value="y"),
        patch(
            "qbitunregistered.operations.orphaned.check_files_on_disk",
            wraps=check_files_on_disk,
        ) as scan,
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        assert (
            main(
                [
                    "--config",
                    str(config_path),
                    "--orphaned",
                    "--auto-remove",
                ]
            )
            == EXIT_SUCCESS
        )

    assert scan.call_count == 1
    assert str(orphan) in capsys.readouterr().out
    assert not orphan.exists()
    client.torrents_delete.assert_called_once_with(torrent_hashes=["completed"], delete_files=False)
