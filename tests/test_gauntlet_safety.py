"""Mutation, identity, and churn safety checks for the gauntlet boundary."""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import os
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

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
from qbitunregistered.file_operations import SafetyCheckError
from qbitunregistered.impact import ImpactAnalysisError, analyze_impact
from qbitunregistered.operations.orphaned import build_orphan_file_plan, check_files_on_disk
from qbitunregistered.operations.unregistered_checks import unregistered_checks

SAFETY_PROFILE = GauntletProfile(
    name="safety",
    torrent_count=4,
    file_count=13,
    orphan_count=1,
    exact_metadata_torrent_count=2,
    shard_count=3,
    tier="test",
)


def _tracker_module(name: str) -> ModuleType:
    try:
        return importlib.import_module(f"benchmarks.gauntlet.{name}")
    except ModuleNotFoundError:
        pytest.fail(f"{name} is not implemented")


def _tracker_safety_profile():
    tracker_fixture = _tracker_module("tracker_fixture")
    return tracker_fixture.TrackerGauntletProfile(
        name="tracker-safety",
        torrent_count=6,
        tracker_record_count=18,
        save_path_group_count=4,
        default_tag_count=2,
        cross_seed_tag_count=2,
        delete_count=1,
        tier="test",
    )


def _tracker_config() -> dict[str, object]:
    return {
        "default_unregistered_tag": "unregistered",
        "cross_seeding_tag": "unregistered:crossseeding",
        "unregistered": ["torrent is not registered", "starts_with:tracker prefix unavailable"],
        "use_delete_tags": True,
        "use_delete_files": False,
        "delete_tags": ["tracker-delete"],
        "delete_files": {"tracker-delete": False},
    }


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


def test_tracker_evaluator_rejects_transient_create_remove_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a dry-run creating and removing a file before final-state validation."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    marker = fixture.root / "transient-mutation"
    real_execute_pipeline = tracker_runner._execute_pipeline

    def create_then_remove(current_fixture):
        result = real_execute_pipeline(current_fixture)
        marker.write_text("transient", encoding="utf-8")
        marker.unlink()
        return result

    monkeypatch.setattr(tracker_runner, "_execute_pipeline", create_then_remove)

    with pytest.raises(runner.GauntletSafetyError, match="filesystem mutation"):
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert not marker.exists()
    assert fixture.client.mutation_total == 0


def test_tracker_evaluator_rejects_write_restore_attempts_independent_of_final_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a dry-run restoring bytes after a write so the final digest appears stable."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    marker = fixture.root / "restored-mutation"
    marker.write_bytes(b"original")
    real_execute_pipeline = tracker_runner._execute_pipeline

    def write_then_restore(current_fixture):
        result = real_execute_pipeline(current_fixture)
        marker.write_bytes(b"changed")
        marker.write_bytes(b"original")
        return result

    monkeypatch.setattr(tracker_runner, "_execute_pipeline", write_then_restore)
    monkeypatch.setattr(tracker_runner, "_filesystem_digest", lambda _root: "stable-final-state")

    with pytest.raises(runner.GauntletSafetyError, match="filesystem mutation"):
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert marker.read_bytes() == b"original"
    assert fixture.client.mutation_total == 0


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


@pytest.mark.parametrize("mode", ["supported", "omitted", "rejected", "malformed"])
def test_tracker_optional_embedded_transport_is_validated_before_normalization(
    tmp_path: Path,
    mode: str,
) -> None:
    """Catch malformed embedded metadata being treated as compatibility fallback."""
    tracker_fixture = _tracker_module("tracker_fixture")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / mode,
        _tracker_safety_profile(),
        seed=201,
        embedded_trackers_mode=mode,
    )
    before = _filesystem_snapshot(fixture.root)
    clear_cache()

    summary = analyze_impact(fixture.client, fixture.initial_torrents, _tracker_config(), ["unregistered"])

    assert summary.unregistered_deletion_plan is not None
    assert fixture.client.read_counts == {
        "torrents.info": 0,
        "torrents.info.include_trackers": 0,
        "torrents_trackers": 6,
    }
    assert fixture.client.mutation_total == 0
    assert _filesystem_snapshot(fixture.root) == before
    if mode == "rejected":
        with pytest.raises(TypeError, match="include_trackers"):
            fixture.client.torrents.info(include_trackers=True)
    else:
        embedded = fixture.client.torrents.info(include_trackers=True)
        if mode == "supported":
            assert all(isinstance(torrent["trackers"], list) for torrent in embedded)
        elif mode == "omitted":
            assert all("trackers" not in torrent for torrent in embedded)
        else:
            assert embedded[0]["trackers"] == {"malformed": True}


def test_tracker_active_exact_failure_and_same_hash_readd_fail_closed(tmp_path: Path) -> None:
    """Catch an unavailable active torrent being mistaken for a proven disappearance."""
    tracker_fixture = _tracker_module("tracker_fixture")
    for readded in (False, True):
        fixture = tracker_fixture.build_tracker_fixture(tmp_path / str(readded), _tracker_safety_profile(), seed=203)
        before = _filesystem_snapshot(fixture.root)
        failed = fixture.initial_torrents[0]
        fixture.client.set_exact_trackers(failed.hash, OSError("exact tracker read failed"))
        if readded:
            snapshot = list(fixture.initial_torrents)
            snapshot[0] = replace(failed, name="same-hash-readded")
            fixture.client.set_torrent_snapshot(snapshot)
        clear_cache()

        with pytest.raises(ImpactAnalysisError, match="Could not analyze"):
            analyze_impact(fixture.client, fixture.initial_torrents, _tracker_config(), ["unregistered"])

        assert fixture.client.mutation_total == 0
        assert _filesystem_snapshot(fixture.root) == before


def test_tracker_failed_exact_read_accepts_only_proven_disappearance(tmp_path: Path) -> None:
    """Catch a failed exact read becoming permission to act without a fresh absence proof."""
    tracker_fixture = _tracker_module("tracker_fixture")
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / "fixture", _tracker_safety_profile(), seed=205)
    before = _filesystem_snapshot(fixture.root)
    failed = fixture.initial_torrents[0]
    fixture.client.set_exact_trackers(failed.hash, OSError("removed during scan"))
    fixture.client.set_torrent_snapshot(fixture.initial_torrents[1:])
    clear_cache()

    summary = analyze_impact(fixture.client, fixture.initial_torrents, _tracker_config(), ["unregistered"])

    assert summary.unregistered_deletion_plan is not None
    assert summary.unregistered_deletion_plan.confirmed_absent_hashes == (failed.hash,)
    assert failed.hash not in {item for hashes in summary.torrents_to_tag.values() for item in hashes}
    assert fixture.client.read_counts["torrents.info"] == 1
    assert fixture.client.mutation_total == 0
    assert _filesystem_snapshot(fixture.root) == before


@pytest.mark.parametrize(
    "snapshot",
    [
        {"malformed": "mapping"},
        [SimpleNamespace(hash=""), SimpleNamespace(hash="valid")],
        [SimpleNamespace(hash="duplicate"), SimpleNamespace(hash="duplicate")],
        [SimpleNamespace(), SimpleNamespace(hash="valid")],
    ],
)
def test_tracker_malformed_refresh_hashes_fail_closed(tmp_path: Path, snapshot: object) -> None:
    """Catch malformed or duplicate refresh hashes authorizing disappearance."""
    tracker_fixture = _tracker_module("tracker_fixture")
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / "fixture", _tracker_safety_profile(), seed=207)
    before = _filesystem_snapshot(fixture.root)
    fixture.client.set_exact_trackers(fixture.initial_torrents[0].hash, OSError("metadata unavailable"))
    fixture.client.set_torrent_snapshot(snapshot)
    clear_cache()

    with pytest.raises(ImpactAnalysisError, match="Could not analyze"):
        analyze_impact(fixture.client, fixture.initial_torrents, _tracker_config(), ["unregistered"])

    assert fixture.client.mutation_total == 0
    assert _filesystem_snapshot(fixture.root) == before


@pytest.mark.parametrize("change", ["disappear", "delete-tag"])
def test_tracker_mutating_preflight_churn_raises_before_fake_mutation(tmp_path: Path, change: str) -> None:
    """Catch confirmed delete targets mutating after disappearance or delete-tag churn."""
    tracker_fixture = _tracker_module("tracker_fixture")
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / change, _tracker_safety_profile(), seed=211)
    before = _filesystem_snapshot(fixture.root)
    clear_cache()
    config = _tracker_config()
    summary = analyze_impact(fixture.client, fixture.initial_torrents, config, ["unregistered"])
    assert summary.unregistered_deletion_plan is not None
    current = list(fixture.initial_torrents)
    if change == "disappear":
        current = current[1:]
    else:
        current[0] = replace(current[0], tags="")
    fixture.client.set_torrent_snapshot(current)

    with pytest.raises(SafetyCheckError, match="no longer available|Delete tag changed"):
        unregistered_checks(
            fixture.client,
            fixture.initial_torrents,
            config,
            True,
            ["tracker-delete"],
            {"tracker-delete": False},
            False,
            deletion_plan=summary.unregistered_deletion_plan,
        )

    assert fixture.client.mutation_total == 0
    assert _filesystem_snapshot(fixture.root) == before


def test_tracker_dry_run_reuses_preview_snapshot_after_tracker_change(tmp_path: Path) -> None:
    """Catch execution silently switching to tracker metadata changed after preview."""
    tracker_fixture = _tracker_module("tracker_fixture")
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / "fixture", _tracker_safety_profile(), seed=223)
    before = _filesystem_snapshot(fixture.root)
    config = _tracker_config()
    clear_cache()
    summary = analyze_impact(fixture.client, fixture.initial_torrents, config, ["unregistered"])
    assert summary.unregistered_deletion_plan is not None
    changed_hash = fixture.initial_torrents[0].hash
    fixture.client.set_exact_trackers(changed_hash, fixture.client.trackers_by_hash[fixture.initial_torrents[-1].hash])

    _paths, counts = unregistered_checks(
        fixture.client,
        fixture.initial_torrents,
        config,
        True,
        ["tracker-delete"],
        {"tracker-delete": False},
        True,
        deletion_plan=summary.unregistered_deletion_plan,
    )

    assert sum(counts.values()) == 4
    assert fixture.client.read_counts["torrents_trackers"] == 6
    assert fixture.client.mutation_total == 0
    assert _filesystem_snapshot(fixture.root) == before


def test_tracker_semantic_matrix_emits_only_normalized_sanitized_pass_evidence(tmp_path: Path) -> None:
    """Catch scenario normalization before transport-specific safety validation."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / "fixture", _tracker_safety_profile(), seed=227)

    scenarios = tracker_runner.evaluate_tracker_scenarios(fixture)

    assert set(scenarios) == {
        "complete_embedded",
        "omitted_embedded_fallback",
        "rejected_embedded_fallback",
        "malformed_embedded_transport_aware",
        "malformed_exact_fail_closed",
        "proven_disappearance",
        "same_hash_readd_fail_closed",
        "malformed_refresh_fail_closed",
        "duplicate_refresh_fail_closed",
        "delete_disappearance_preflight",
        "delete_tag_change_preflight",
        "tracker_change_snapshot_bound",
    }
    for evidence in scenarios.values():
        assert set(evidence) == {"outcome", "action_digest", "endpoint_counters"}
        assert evidence["outcome"] == "pass"
        assert len(evidence["action_digest"]) == 64
        assert set(evidence["endpoint_counters"]) == {
            "torrents.info",
            "torrents.info.include_trackers",
            "torrents_trackers",
        }
        assert all(isinstance(count, int) and count >= 0 for count in evidence["endpoint_counters"].values())
    assert fixture.client.mutation_total == 0


def test_tracker_actual_cli_unregistered_dry_run_keeps_all_state_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catch the genuine CLI path mutating state or bypassing unregistered preview."""
    tracker_fixture = _tracker_module("tracker_fixture")
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / "fixture", _tracker_safety_profile(), seed=229)
    before = _filesystem_snapshot(fixture.root)
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "host": "localhost:8080",
                "username": "admin",
                "password": "not-a-real-secret",
                "dry_run": True,
                **_tracker_config(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("qbitunregistered.cli.create_client", lambda _config: fixture.client)

    exit_code = main(["--config", str(config_path), "--unregistered", "--dry-run"])

    assert exit_code == EXIT_SUCCESS
    assert fixture.client.mutation_total == 0
    assert fixture.client.logout_count == 1
    assert _filesystem_snapshot(fixture.root) == before
    captured = capsys.readouterr()
    assert "DRY-RUN IMPACT PREVIEW" in captured.out
    assert "Torrents to TAG: 4" in captured.out
    logging.getLogger().handlers.clear()
