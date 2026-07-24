"""Tests for the command-line coordinator."""

import json
from unittest.mock import Mock, patch

import pytest

from qbitunregistered.cli import EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, EXIT_SUCCESS, main
from qbitunregistered.operations.unregistered_checks import (
    DeletionAction,
    PlannedTorrentDeletion,
    UnregisteredDeletionPlan,
)


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


def _unregistered_file_deletion_plan(action: DeletionAction) -> UnregisteredDeletionPlan:
    return UnregisteredDeletionPlan(
        deletions=(
            PlannedTorrentDeletion(
                torrent_hash="hash",
                torrent_name="content.mkv",
                matching_tag="unregistered",
                category="movies",
                action=action,
            ),
        ),
        ownership_snapshot=None,
    )


def _destructive_unregistered_config(tmp_path, **overrides):
    return _write_config(
        tmp_path,
        use_delete_tags=True,
        use_delete_files=True,
        delete_tags=["unregistered"],
        delete_files={"unregistered": True},
        **overrides,
    )


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


def test_main_clears_stale_cache_from_previous_execution(tmp_path, capsys) -> None:
    from qbitunregistered.operations.orphaned import _get_default_save_path

    stale_root = tmp_path / "stale"
    stale_root.mkdir()
    stale_file = stale_root / "stale.mkv"
    stale_file.write_text("stale", encoding="utf-8")
    current_root = tmp_path / "current"
    current_root.mkdir()
    current_file = current_root / "current.mkv"
    current_file.write_text("current", encoding="utf-8")
    config_path = _write_config(tmp_path)
    client = _empty_client(stale_root)

    assert _get_default_save_path(client, cache_scope=id(client)) == str(stale_root)
    client.application.default_save_path = str(current_root)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--orphaned",
                "--dry-run",
            ]
        )

    output = capsys.readouterr().out
    assert result == EXIT_SUCCESS
    assert str(current_file) in output
    assert str(stale_file) not in output


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


def test_cli_target_override_satisfies_scheduled_hard_link_validation(tmp_path) -> None:
    config_path = _write_config(
        tmp_path,
        target_dir=None,
        scheduled_times=["09:00"],
        scheduled_operations=["create_hard_links"],
    )
    target_dir = tmp_path / "hard-links"
    client = _empty_client(tmp_path)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.create_hard_links") as create_hard_links,
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--target-dir",
                str(target_dir),
                "--dry-run",
                "--yes",
            ]
        )

    assert result == EXIT_SUCCESS
    create_hard_links.assert_called_once_with(
        str(target_dir),
        [],
        dry_run=True,
        planned_links=None,
    )


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


@pytest.mark.parametrize("cli_value", ["", "   "], ids=["empty", "whitespace"])
@pytest.mark.parametrize("has_config", [True, False], ids=["configured", "missing-config"])
def test_blank_cli_recycle_bin_aborts_before_connection_or_mutation(tmp_path, cli_value, has_config) -> None:
    configured_recycle_bin = tmp_path / "configured-recycle"
    config_path = (
        _write_config(tmp_path, recycle_bin=str(configured_recycle_bin)) if has_config else tmp_path / "missing-config.json"
    )

    with (
        patch("qbitunregistered.cli.create_client") as create_client,
        patch("qbitunregistered.cli.unregistered_checks") as unregistered_checks,
        pytest.raises(SystemExit) as error,
    ):
        main(
            [
                "--config",
                str(config_path),
                "--unregistered",
                "--recycle-bin",
                cli_value,
                "--yes",
            ]
        )

    assert error.value.code == EXIT_CONFIG_ERROR
    create_client.assert_not_called()
    unregistered_checks.assert_not_called()


def test_nonblank_cli_recycle_bin_overrides_config(tmp_path) -> None:
    config_path = _write_config(tmp_path, recycle_bin=str(tmp_path / "configured-recycle"))
    cli_recycle_bin = tmp_path / "cli-recycle"
    client = _empty_client(tmp_path)
    captured_config = {}

    def capture_config(config):
        captured_config.update(config)
        return client

    with (
        patch("qbitunregistered.cli.create_client", side_effect=capture_config),
        patch("qbitunregistered.cli.unregistered_checks", return_value=({}, {})) as unregistered_checks,
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--unregistered",
                "--recycle-bin",
                str(cli_recycle_bin),
                "--dry-run",
                "--yes",
            ]
        )

    assert result == EXIT_SUCCESS
    assert captured_config["recycle_bin"] == str(cli_recycle_bin)
    assert unregistered_checks.call_args.kwargs["recycle_bin"] == str(cli_recycle_bin)


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


def test_yes_mode_reports_unregistered_recycle_failure(tmp_path) -> None:
    """Scheduled-style execution reports a preserved torrent as a failure."""
    from qbitunregistered.file_operations import SafetyCheckError

    config_path = _write_config(tmp_path)
    client = _empty_client(tmp_path)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch(
            "qbitunregistered.cli.unregistered_checks",
            side_effect=SafetyCheckError("Could not safely recycle all files; torrent preserved"),
        ),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(["--config", str(config_path), "--unregistered", "--yes"])

    assert result == EXIT_GENERAL_ERROR
    notifications.return_value.send_summary.assert_called_once_with({"succeeded": [], "failed": ["Unregistered checks"]})
    client.auth_log_out.assert_called_once_with()


@pytest.mark.parametrize(
    "action",
    [DeletionAction.PERMANENT_DELETE, DeletionAction.RECYCLE_FILES],
    ids=["permanent", "recycle"],
)
def test_combined_hard_link_failure_blocks_destructive_unregistered_cleanup(tmp_path, action) -> None:
    config_path = _destructive_unregistered_config(tmp_path)
    client = _empty_client(tmp_path)
    plan = _unregistered_file_deletion_plan(action)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.build_unregistered_deletion_plan", return_value=plan),
        patch("qbitunregistered.cli.create_hard_links", side_effect=OSError("link failed")),
        patch("qbitunregistered.cli.unregistered_checks") as unregistered_checks,
        patch("qbitunregistered.cli.auto_remove") as auto_remove,
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--unregistered",
                "--auto-remove",
                "--yes",
            ]
        )

    assert result == EXIT_GENERAL_ERROR
    unregistered_checks.assert_not_called()
    client.torrents_delete.assert_not_called()
    client.torrents_add_tags.assert_not_called()
    auto_remove.assert_called_once_with(client, [], False)
    notifications.return_value.send_summary.assert_called_once_with(
        {
            "succeeded": ["Auto remove"],
            "failed": [
                "Create hard links",
                "Unregistered checks (blocked: hard-link creation failed)",
            ],
        }
    )


@pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
def test_combined_destructive_cleanup_runs_hard_links_first(tmp_path, dry_run) -> None:
    config_path = _destructive_unregistered_config(tmp_path)
    client = _empty_client(tmp_path)
    plan = _unregistered_file_deletion_plan(DeletionAction.PERMANENT_DELETE)
    calls = []

    def record_hard_links(*_args, **_kwargs):
        calls.append("hard-links")

    def record_unregistered(*_args, **_kwargs):
        calls.append("unregistered")
        return {}, {}

    argv = [
        "--config",
        str(config_path),
        "--orphaned",
        "--create-hard-links",
        "--unregistered",
        "--yes",
    ]
    if dry_run:
        argv.append("--dry-run")

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.build_unregistered_deletion_plan", return_value=plan),
        patch(
            "qbitunregistered.cli.delete_orphaned_files",
            side_effect=lambda *_args, **_kwargs: calls.append("orphaned"),
        ),
        patch("qbitunregistered.cli.create_hard_links", side_effect=record_hard_links) as create_hard_links,
        patch("qbitunregistered.cli.unregistered_checks", side_effect=record_unregistered) as unregistered_checks,
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(argv)

    assert result == EXIT_SUCCESS
    assert calls == ["orphaned", "hard-links", "unregistered"]
    assert create_hard_links.call_args.kwargs["dry_run"] is dry_run
    assert unregistered_checks.call_args.kwargs["dry_run"] is dry_run
    assert unregistered_checks.call_args.kwargs["deletion_plan"] is plan


def test_combined_torrent_only_cleanup_keeps_existing_operation_order(tmp_path) -> None:
    config_path = _destructive_unregistered_config(tmp_path)
    client = _empty_client(tmp_path)
    plan = UnregisteredDeletionPlan(deletions=(), ownership_snapshot=None)
    calls = []

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.build_unregistered_deletion_plan", return_value=plan),
        patch("qbitunregistered.cli.create_hard_links", side_effect=lambda *_args, **_kwargs: calls.append("hard-links")),
        patch(
            "qbitunregistered.cli.unregistered_checks",
            side_effect=lambda *_args, **_kwargs: (calls.append("unregistered") or ({}, {})),
        ),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--unregistered",
                "--yes",
            ]
        )

    assert result == EXIT_SUCCESS
    assert calls == ["unregistered", "hard-links"]


def test_confirmed_hard_link_source_substitution_blocks_unregistered_deletion(tmp_path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    target_dir = tmp_path / "hard-links"
    target_dir.mkdir()
    source = downloads / "content.mkv"
    source.write_text("confirmed", encoding="utf-8")
    config_path = _destructive_unregistered_config(tmp_path, target_dir=str(target_dir))
    client = _empty_client(downloads)
    torrent = Mock(hash="hash", save_path=str(downloads), category="movies", tags="unregistered")
    torrent.name = source.name
    torrent.state_enum.is_complete = True
    torrent.trackers = []
    client.torrents.info.return_value = [torrent]
    client.torrents_files.return_value = [{"name": source.name}]

    def substitute_source(_prompt):
        source.unlink()
        source.write_text("replacement", encoding="utf-8")
        return "y"

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("builtins.input", side_effect=substitute_source),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--unregistered",
            ]
        )

    assert result == EXIT_GENERAL_ERROR
    assert source.read_text(encoding="utf-8") == "replacement"
    assert not (target_dir / "movies" / source.name).exists()
    client.torrents_delete.assert_not_called()
    client.torrents_add_tags.assert_not_called()
    notifications.return_value.send_summary.assert_called_once_with(
        {
            "succeeded": [],
            "failed": [
                "Create hard links",
                "Unregistered checks (blocked: hard-link creation failed)",
            ],
        }
    )


@pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
def test_combined_cleanup_preserves_content_through_created_hard_link(tmp_path, use_recycle_bin) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    target_dir = tmp_path / "hard-links"
    target_dir.mkdir()
    source = downloads / "content.mkv"
    source.write_text("preserved", encoding="utf-8")
    recycle_bin = tmp_path / "recycle" if use_recycle_bin else None
    config_path = _destructive_unregistered_config(
        tmp_path,
        target_dir=str(target_dir),
        recycle_bin=str(recycle_bin) if recycle_bin else None,
    )
    client = _empty_client(downloads)
    torrent = Mock(hash="hash", save_path=str(downloads), category="movies", tags="unregistered")
    torrent.name = source.name
    torrent.state_enum.is_complete = True
    torrent.trackers = []
    client.torrents.info.return_value = [torrent]
    client.torrents_files.return_value = [{"name": source.name}]

    if not use_recycle_bin:

        def simulate_qbittorrent_file_deletion(*, torrent_hashes, delete_files):
            assert torrent_hashes == ["hash"]
            assert delete_files is True
            source.unlink()

        client.torrents_delete.side_effect = simulate_qbittorrent_file_deletion

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--unregistered",
                "--yes",
            ]
        )

    hard_link = target_dir / "movies" / source.name
    assert result == EXIT_SUCCESS
    assert hard_link.read_text(encoding="utf-8") == "preserved"
    assert not source.exists()
    client.torrents_delete.assert_called_once_with(
        torrent_hashes=["hash"],
        delete_files=not use_recycle_bin,
    )


@pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["execute", "dry-run"])
def test_existing_unrelated_hard_link_target_blocks_destructive_cleanup(
    tmp_path,
    use_recycle_bin,
    dry_run,
) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    target_dir = tmp_path / "hard-links"
    existing_target = target_dir / "movies" / "content.mkv"
    existing_target.parent.mkdir(parents=True)
    existing_target.write_text("unrelated", encoding="utf-8")
    source = downloads / existing_target.name
    source.write_text("sole source", encoding="utf-8")
    recycle_bin = tmp_path / "recycle" if use_recycle_bin else None
    config_path = _destructive_unregistered_config(
        tmp_path,
        target_dir=str(target_dir),
        recycle_bin=str(recycle_bin) if recycle_bin else None,
    )
    client = _empty_client(downloads)
    torrent = Mock(hash="hash", save_path=str(downloads), category="movies", tags="unregistered")
    torrent.name = source.name
    torrent.state_enum.is_complete = True
    torrent.trackers = []
    client.torrents.info.return_value = [torrent]
    client.torrents_files.return_value = [{"name": source.name}]
    argv = [
        "--config",
        str(config_path),
        "--create-hard-links",
        "--unregistered",
        "--yes",
    ]
    if dry_run:
        argv.append("--dry-run")

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(argv)

    assert result == EXIT_GENERAL_ERROR
    assert source.read_text(encoding="utf-8") == "sole source"
    assert existing_target.read_text(encoding="utf-8") == "unrelated"
    assert source.stat().st_ino != existing_target.stat().st_ino
    client.torrents_delete.assert_not_called()
    client.torrents_add_tags.assert_not_called()
    assert recycle_bin is None or not recycle_bin.exists()
    notifications.return_value.send_summary.assert_called_once_with(
        {
            "succeeded": [],
            "failed": [
                "Create hard links",
                "Unregistered checks (blocked: hard-link creation failed)",
            ],
        }
    )


@pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
def test_existing_correct_hard_link_allows_destructive_cleanup(tmp_path, use_recycle_bin) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    target_dir = tmp_path / "hard-links"
    hard_link = target_dir / "movies" / "content.mkv"
    hard_link.parent.mkdir(parents=True)
    source = downloads / hard_link.name
    source.write_text("preserved", encoding="utf-8")
    hard_link.hardlink_to(source)
    recycle_bin = tmp_path / "recycle" if use_recycle_bin else None
    config_path = _destructive_unregistered_config(
        tmp_path,
        target_dir=str(target_dir),
        recycle_bin=str(recycle_bin) if recycle_bin else None,
    )
    client = _empty_client(downloads)
    torrent = Mock(hash="hash", save_path=str(downloads), category="movies", tags="unregistered")
    torrent.name = source.name
    torrent.state_enum.is_complete = True
    torrent.trackers = []
    client.torrents.info.return_value = [torrent]
    client.torrents_files.return_value = [{"name": source.name}]

    if not use_recycle_bin:
        client.torrents_delete.side_effect = lambda **_kwargs: source.unlink()

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--unregistered",
                "--yes",
            ]
        )

    assert result == EXIT_SUCCESS
    assert not source.exists()
    assert hard_link.read_text(encoding="utf-8") == "preserved"
    client.torrents_delete.assert_called_once_with(
        torrent_hashes=["hash"],
        delete_files=not use_recycle_bin,
    )


def test_combined_destructive_dry_run_preserves_source_and_creates_no_link(tmp_path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    target_dir = tmp_path / "hard-links"
    target_dir.mkdir()
    source = downloads / "content.mkv"
    source.write_text("untouched", encoding="utf-8")
    config_path = _destructive_unregistered_config(tmp_path, target_dir=str(target_dir))
    client = _empty_client(downloads)
    torrent = Mock(hash="hash", save_path=str(downloads), category="movies", tags="unregistered")
    torrent.name = source.name
    torrent.state_enum.is_complete = True
    torrent.trackers = []
    client.torrents.info.return_value = [torrent]
    client.torrents_files.return_value = [{"name": source.name}]

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(
            [
                "--config",
                str(config_path),
                "--create-hard-links",
                "--unregistered",
                "--dry-run",
                "--yes",
            ]
        )

    assert result == EXIT_SUCCESS
    assert source.read_text(encoding="utf-8") == "untouched"
    assert not (target_dir / "movies" / source.name).exists()
    client.torrents_delete.assert_not_called()
    client.torrents_add_tags.assert_not_called()
    notifications.return_value.send_summary.assert_called_once_with(
        {
            "succeeded": ["Create hard links", "Unregistered checks"],
            "failed": [],
        }
    )


def test_yes_mode_reports_incomplete_orphan_cleanup_as_failure(tmp_path) -> None:
    """Scheduled orphan cleanup cannot notify success after a skipped target."""
    from qbitunregistered.file_operations import SafetyCheckError

    downloads = tmp_path / "downloads"
    downloads.mkdir()
    orphan = downloads / "orphan.mkv"
    orphan.write_text("orphan", encoding="utf-8")
    config_path = _write_config(tmp_path)
    client = _empty_client(downloads)
    failure = SafetyCheckError("Orphan cleanup incomplete: 0 of 1 planned files were deleted; 1 remain. See logs for details.")

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.delete_orphaned_files", side_effect=failure),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(["--config", str(config_path), "--orphaned", "--yes"])

    assert result == EXIT_GENERAL_ERROR
    notifications.return_value.send_summary.assert_called_once_with(
        {
            "succeeded": [],
            "failed": [f"Orphaned files check: {failure}"],
        }
    )
    assert orphan.read_text(encoding="utf-8") == "orphan"
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
    client.torrents_files.return_value = []

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


def test_orphan_recycle_confirmation_preview_describes_move(tmp_path, capsys) -> None:
    """Confirmation describes recycling without deletion or freed-space claims."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    orphan = downloads / "orphan.mkv"
    orphan.write_bytes(b"x" * (2 * 1024**2))
    recycle_bin = tmp_path / "recycle"
    config_path = _write_config(tmp_path, recycle_bin=str(recycle_bin))
    client = _empty_client(downloads)

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("builtins.input", return_value="n"),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(["--config", str(config_path), "--orphaned"])

    output = capsys.readouterr().out
    assert result == EXIT_SUCCESS
    assert "Orphaned files to MOVE TO RECYCLE BIN: 1" in output
    assert "Data to move to recycle bin: 2.00 MB" in output
    assert "Orphaned files to DELETE" not in output
    assert "Disk space to free" not in output
    assert orphan.read_bytes() == b"x" * (2 * 1024**2)
    assert not recycle_bin.exists()
    notifications.assert_not_called()


@pytest.mark.parametrize(
    ("use_recycle_bin", "dry_run", "expected_result"),
    [
        (False, False, "Orphaned files check: 1 file permanently deleted"),
        (False, True, "Orphaned files check: 1 file would be permanently deleted"),
        (True, False, "Orphaned files check: 1 file moved to recycle bin"),
        (True, True, "Orphaned files check: 1 file would be moved to recycle bin"),
    ],
    ids=["permanent-execute", "permanent-dry-run", "recycle-execute", "recycle-dry-run"],
)
def test_orphan_notification_summary_matches_execution_mode(
    tmp_path,
    use_recycle_bin,
    dry_run,
    expected_result,
) -> None:
    """Operation notifications describe the action that did or would occur."""
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    orphan = downloads / "orphan.mkv"
    orphan.write_text("orphan", encoding="utf-8")
    recycle_bin = tmp_path / "recycle" if use_recycle_bin else None
    config_path = _write_config(tmp_path, recycle_bin=str(recycle_bin) if recycle_bin else None)
    client = _empty_client(downloads)
    argv = ["--config", str(config_path), "--orphaned", "--yes"]
    if dry_run:
        argv.append("--dry-run")

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("qbitunregistered.cli.NotificationManager") as notifications,
    ):
        result = main(argv)

    assert result == EXIT_SUCCESS
    notifications.return_value.send_summary.assert_called_once_with({"succeeded": [expected_result], "failed": []})
    if dry_run:
        assert orphan.read_text(encoding="utf-8") == "orphan"
        assert recycle_bin is None or not recycle_bin.exists()
    elif recycle_bin is not None:
        assert not orphan.exists()
        assert len(list(recycle_bin.rglob(orphan.name))) == 1
    else:
        assert not orphan.exists()


@pytest.mark.parametrize("use_recycle_bin", [False, True], ids=["permanent", "recycle"])
def test_orphan_owner_added_during_confirmation_blocks_all_mutation(tmp_path, use_recycle_bin) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    claimed_orphan = downloads / "claimed.mkv"
    other_orphan = downloads / "other.mkv"
    claimed_orphan.write_text("claimed", encoding="utf-8")
    other_orphan.write_text("other", encoding="utf-8")
    recycle_bin = tmp_path / "recycle" if use_recycle_bin else None
    config_path = _write_config(tmp_path, recycle_bin=str(recycle_bin) if recycle_bin else None)
    client = _empty_client(downloads)

    new_owner = Mock(hash="new-owner", save_path=str(downloads))

    def add_owner_during_confirmation(_prompt):
        client.torrents.info.return_value = [new_owner]
        client.torrents_files.return_value = [{"name": claimed_orphan.name}]
        return "y"

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("builtins.input", side_effect=add_owner_during_confirmation),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(["--config", str(config_path), "--orphaned"])

    assert result == EXIT_GENERAL_ERROR
    assert claimed_orphan.read_text(encoding="utf-8") == "claimed"
    assert other_orphan.read_text(encoding="utf-8") == "other"
    if recycle_bin is not None:
        assert not recycle_bin.exists()
    client.torrents_files.assert_called_once_with("new-owner")


def test_orphan_execution_does_not_add_files_newly_orphaned_during_confirmation(tmp_path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    confirmed_orphan = downloads / "confirmed.mkv"
    newly_orphaned = downloads / "newly-orphaned.mkv"
    confirmed_orphan.write_text("confirmed", encoding="utf-8")
    newly_orphaned.write_text("tracked", encoding="utf-8")
    config_path = _write_config(tmp_path)
    client = _empty_client(downloads)

    tracked_file = Mock()
    tracked_file.name = newly_orphaned.name
    initial_owner = Mock(hash="initial-owner", save_path=str(downloads), files=[tracked_file])
    client.torrents.info.return_value = [initial_owner]

    def remove_owner_during_confirmation(_prompt):
        client.torrents.info.return_value = []
        return "y"

    with (
        patch("qbitunregistered.cli.create_client", return_value=client),
        patch("builtins.input", side_effect=remove_owner_during_confirmation),
        patch("qbitunregistered.cli.NotificationManager"),
    ):
        result = main(["--config", str(config_path), "--orphaned"])

    assert result == EXIT_SUCCESS
    assert not confirmed_orphan.exists()
    assert newly_orphaned.read_text(encoding="utf-8") == "tracked"
