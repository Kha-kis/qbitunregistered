"""Mutation, identity, and churn safety checks for the gauntlet boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks.gauntlet import runner
from benchmarks.gauntlet import identity as gauntlet_identity
from benchmarks.gauntlet.fixture_factory import (
    FakeTorrent,
    GauntletProfile,
    build_fixture,
)
from benchmarks.gauntlet.runner import DEFAULT_SAMPLES, evaluate_fixture
from qbitunregistered.cache import clear_cache
from qbitunregistered.cli import EXIT_SUCCESS, main
from qbitunregistered.operations.orphaned import build_orphan_file_plan, check_files_on_disk

SAFETY_PROFILE = GauntletProfile(
    name="safety",
    torrent_count=4,
    file_count=13,
    orphan_count=1,
    exact_metadata_torrent_count=2,
    shard_count=3,
    tier="test",
)


def _filesystem_snapshot(root: Path) -> dict[str, tuple[int, int, int, int, int, str]]:
    snapshot: dict[str, tuple[int, int, int, int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        path_stat = path.lstat()
        content_digest = ""
        if path.is_file() and not path.is_symlink():
            content_digest = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshot[path.relative_to(root).as_posix()] = (
            path_stat.st_mode,
            path_stat.st_size,
            path_stat.st_ino,
            path_stat.st_mtime_ns,
            path_stat.st_ctime_ns,
            content_digest,
        )
    return snapshot


def test_actual_cli_dry_run_keeps_qbittorrent_and_file_contents_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=71)
    before = _filesystem_snapshot(fixture.root)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "host": "localhost:8080",
                "username": "admin",
                "password": "not-a-real-secret",
                "dry_run": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("qbitunregistered.cli.create_client", lambda _config: fixture.client)

    exit_code = main(["--config", str(config_path), "--orphaned", "--dry-run"])

    assert exit_code == EXIT_SUCCESS
    assert fixture.client.mutation_total == 0
    assert fixture.client.logout_count == 1
    assert _filesystem_snapshot(fixture.root) == before
    captured = capsys.readouterr()
    assert "DRY-RUN IMPACT PREVIEW" in captured.out
    assert "would be permanently deleted" in captured.err
    logging.getLogger().handlers.clear()


def test_candidate_identity_hashes_staged_unstaged_and_streamed_untracked_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    untracked = tmp_path / "large.bin"
    untracked.write_bytes(b"a" * (2 * 1024 * 1024))
    staged = [b"staged-v1"]

    def fake_git_output(_repository_root: Path, arguments: list[str]) -> bytes:
        if arguments[:2] == ["rev-parse", "HEAD"]:
            return b"a" * 40
        if arguments[0] == "status":
            return b"?? large.bin\0"
        if arguments[:2] == ["diff", "--cached"]:
            return staged[0]
        if arguments[0] == "diff":
            return b"unstaged-v1"
        if arguments[0] == "ls-files":
            return b"large.bin\0"
        raise AssertionError(arguments)

    monkeypatch.setattr(gauntlet_identity, "_git_output", fake_git_output)

    first = gauntlet_identity.capture_repository_identity(tmp_path)
    staged[0] = b"staged-v2"
    second = gauntlet_identity.capture_repository_identity(tmp_path)
    staged[0] = b"staged-v1"
    untracked.write_bytes(b"b" * (2 * 1024 * 1024))
    third = gauntlet_identity.capture_repository_identity(tmp_path)

    assert first.known
    assert first.diff_sha256 != second.diff_sha256
    assert first.diff_sha256 != third.diff_sha256
    assert str(tmp_path) not in repr(first)


def test_untracked_identity_distinguishes_regular_file_from_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"target-name")

    def fake_git_output(_repository_root: Path, arguments: list[str]) -> bytes:
        if arguments[:2] == ["rev-parse", "HEAD"]:
            return b"a" * 40
        if arguments[0] == "status":
            return b"?? candidate\0"
        if arguments[0] in {"diff"}:
            return b""
        if arguments[0] == "ls-files":
            return b"candidate\0"
        raise AssertionError(arguments)

    monkeypatch.setattr(gauntlet_identity, "_git_output", fake_git_output)
    regular_identity = gauntlet_identity.capture_repository_identity(tmp_path)
    candidate.unlink()
    try:
        candidate.symlink_to("target-name")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a symbolic link: {error}")

    symlink_identity = gauntlet_identity.capture_repository_identity(tmp_path)

    assert regular_identity.known
    assert symlink_identity.known
    assert regular_identity.diff_sha256 != symlink_identity.diff_sha256


def test_same_length_replacement_with_restored_mtime_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=72)
    target = fixture.orphan_files[0]
    original_stat = target.stat()
    original_content = target.read_bytes()
    original_delete = runner.delete_orphaned_files

    def replace_after_dry_run(*args, **kwargs):
        original_delete(*args, **kwargs)
        replacement = bytes([original_content[0] ^ 0xFF]) + original_content[1:]
        target.write_bytes(replacement)
        os.utime(
            target,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

    monkeypatch.setattr(runner, "delete_orphaned_files", replace_after_dry_run)

    with pytest.raises(runner.GauntletSafetyError, match="changed the fixture filesystem"):
        evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert target.stat().st_size == len(original_content)
    assert target.stat().st_mtime_ns == original_stat.st_mtime_ns
    assert fixture.client.mutation_total == 0


def test_failed_current_snapshot_fails_closed_without_mutation(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=73)
    before = _filesystem_snapshot(fixture.root)
    fixture.client.set_torrent_snapshot(ConnectionError("transient failure"))

    with pytest.raises(runner.GauntletSafetyError, match="materialized fixture verification failed"):
        evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert fixture.client.read_counts == {}
    assert fixture.client.mutation_total == 0
    assert _filesystem_snapshot(fixture.root) == before


@pytest.mark.parametrize("snapshot_change", ["alter", "add"])
def test_materialized_verifier_uses_current_client_snapshot_before_measurement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_change: str,
) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=74)
    current_snapshot = list(fixture.initial_torrents)
    if snapshot_change == "alter":
        current_snapshot[0] = replace(current_snapshot[0], name="altered-current-name")
    else:
        source = current_snapshot[-1]
        current_snapshot.append(
            replace(
                source,
                hash="f" * 64,
                name="added-current-torrent",
            )
        )
    fixture.client.set_torrent_snapshot(current_snapshot)

    def unexpected_pipeline(_fixture):
        raise AssertionError("measurement pipeline must not start")

    monkeypatch.setattr(runner, "_execute_pipeline", unexpected_pipeline)

    with pytest.raises(runner.GauntletSafetyError, match="materialized fixture verification failed"):
        evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert fixture.client.read_counts == {}
    assert fixture.client.mutation_total == 0


def test_failed_active_torrent_metadata_fails_closed_without_mutation(
    tmp_path: Path,
) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=79)
    exact_torrent = fixture.initial_torrents[0]
    fixture.client.set_torrent_files(
        exact_torrent.hash,
        OSError("metadata unavailable"),
    )

    with pytest.raises(runner.GauntletSafetyError, match="materialized fixture verification failed"):
        evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert fixture.client.read_counts == {}
    assert fixture.client.mutation_total == 0


def test_same_hash_readd_is_reconciled_from_current_snapshot(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=83)
    replaced_torrent = fixture.initial_torrents[2]
    previously_owned_path = Path(replaced_torrent.content_path)
    newly_owned_path = fixture.orphan_files[0]
    current_snapshot = list(fixture.initial_torrents)
    current_snapshot[2] = FakeTorrent(
        hash=replaced_torrent.hash,
        name=replaced_torrent.name,
        save_path=str(fixture.scan_roots[0]),
        content_path=str(newly_owned_path),
    )
    fixture.client.set_torrent_snapshot(current_snapshot)
    clear_cache()

    orphaned = check_files_on_disk(fixture.client, list(fixture.initial_torrents))
    plan = build_orphan_file_plan(orphaned)

    assert previously_owned_path.resolve() in plan.paths
    assert newly_owned_path.resolve() not in plan.paths
    assert len(plan.files) == 1
    assert fixture.client.mutation_total == 0


def test_malformed_current_snapshot_fails_closed(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=89)
    fixture.client.set_torrent_snapshot({"not": "a torrent sequence"})

    with pytest.raises(runner.GauntletSafetyError, match="materialized fixture verification failed"):
        evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert fixture.client.read_counts == {}
    assert fixture.client.mutation_total == 0


def test_skipping_dry_run_reconciliation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path / "fixture", SAFETY_PROFILE, seed=97)
    monkeypatch.setattr(runner, "delete_orphaned_files", lambda *_args, **_kwargs: None)

    with pytest.raises(
        runner.GauntletSafetyError,
        match="operator-visible reconciliation",
    ):
        evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert fixture.client.mutation_total == 0
