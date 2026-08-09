"""Mutation, identity, and churn safety checks for the gauntlet boundary."""

from __future__ import annotations

import errno
import hashlib
import importlib
import json
import logging
import os
import socket
import stat
import sys
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


def test_tracker_boundary_rejects_preopened_os_write_outside_root(tmp_path: Path) -> None:
    """Reject an outside regular fd before os.write can bypass audit events."""
    tracker_runner = _tracker_module("tracker_runner")
    marker = tmp_path / "outside-preopened-os-write"
    marker.write_bytes(b"original")
    descriptor = os.open(marker, os.O_RDWR)
    body_called = False
    try:
        with pytest.raises(runner.GauntletSafetyError, match="regular file descriptor"):
            with tracker_runner._ProductionBoundaryAudit():
                body_called = True
                os.write(descriptor, b"changed")
    finally:
        os.close(descriptor)

    assert body_called is False
    assert marker.read_bytes() == b"original"


def test_tracker_boundary_rejects_preopened_file_object_write(tmp_path: Path) -> None:
    """Reject a buffered file object before its unaudited write method runs."""
    tracker_runner = _tracker_module("tracker_runner")
    marker = tmp_path / "outside-preopened-file-object"
    marker.write_bytes(b"original")

    with marker.open("r+b") as stream:
        with pytest.raises(runner.GauntletSafetyError, match="regular file descriptor"):
            with tracker_runner._ProductionBoundaryAudit():
                stream.write(b"changed")

    assert marker.read_bytes() == b"original"


def test_tracker_boundary_rejects_preopened_read_only_regular_fd(tmp_path: Path) -> None:
    """Reject read-only regular fds because access mode is not portable evidence."""
    tracker_runner = _tracker_module("tracker_runner")
    marker = tmp_path / "outside-preopened-read"
    marker.write_bytes(b"safe")
    descriptor = os.open(marker, os.O_RDONLY)
    try:
        with pytest.raises(runner.GauntletSafetyError, match="regular file descriptor"):
            with tracker_runner._ProductionBoundaryAudit():
                pass
    finally:
        os.close(descriptor)


def test_tracker_boundary_allows_inside_read_and_preopened_pipe_socket(tmp_path: Path) -> None:
    """Keep audited read acquisition and non-regular IPC descriptors available."""
    tracker_runner = _tracker_module("tracker_runner")
    marker = tmp_path / "inside-read"
    marker.write_bytes(b"config")
    read_fd, write_fd = os.pipe()
    first, second = socket.socketpair()
    try:
        with tracker_runner._ProductionBoundaryAudit():
            assert marker.read_bytes() == b"config"
    finally:
        first.close()
        second.close()
        os.close(read_fd)
        os.close(write_fd)


def test_tracker_boundary_allows_only_regular_stdio_identity_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allow redirected output duplicates only when fstat identity is equal."""
    tracker_runner = _tracker_module("tracker_runner")
    regular = os.stat_result((stat.S_IFREG, 41, 7, 1, 0, 0, 0, 0, 0, 0))
    pipe = os.stat_result((stat.S_IFIFO, 42, 7, 1, 0, 0, 0, 0, 0, 0))
    stats = {1: regular, 2: pipe, 9: regular}
    monkeypatch.setattr(tracker_runner, "_open_descriptor_numbers", lambda: iter((9,)), raising=False)
    monkeypatch.setattr(tracker_runner.os, "fstat", lambda descriptor: stats[descriptor])

    tracker_runner._assert_safe_preexisting_descriptors()


def test_tracker_boundary_linux_inventory_tolerates_disappearing_fd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore only EBADF when a proc descriptor closes after enumeration."""
    tracker_runner = _tracker_module("tracker_runner")
    monkeypatch.setattr(tracker_runner.sys, "platform", "linux")
    monkeypatch.setattr(tracker_runner.os, "listdir", lambda _path: ["0", "1", "2", "77", "not-an-fd"])
    real_fstat = os.fstat

    def disappearing_fstat(descriptor: int):
        if descriptor == 77:
            raise OSError(errno.EBADF, "closed")
        return real_fstat(descriptor)

    monkeypatch.setattr(tracker_runner.os, "fstat", disappearing_fstat)

    tracker_runner._assert_safe_preexisting_descriptors()


def test_tracker_boundary_windows_scans_documented_crt_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inspect the last possible Python CRT descriptor on Windows."""
    tracker_runner = _tracker_module("tracker_runner")
    monkeypatch.setattr(tracker_runner.sys, "platform", "win32")

    descriptors = tracker_runner._open_descriptor_numbers()

    assert next(descriptors) == 3
    assert list(descriptors)[-1] == 8191


def test_tracker_boundary_unsupported_platform_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never claim descriptor isolation without a complete enumerator."""
    tracker_runner = _tracker_module("tracker_runner")
    monkeypatch.setattr(tracker_runner.sys, "platform", "darwin")

    with pytest.raises(runner.GauntletSafetyError, match="descriptor inventory unsupported"):
        with tracker_runner._ProductionBoundaryAudit():
            pytest.fail("production body must not run")


def test_tracker_boundary_inventory_failure_unwinds_active_nested_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Activate before inventory and pop only the failed nested frame."""
    tracker_runner = _tracker_module("tracker_runner")
    audit = tracker_runner._ProductionBoundaryAudit()
    calls = 0

    def fail_second_inventory() -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            sys.audit("open", "redacted", "w", os.O_WRONLY)

    monkeypatch.setattr(tracker_runner, "_assert_safe_preexisting_descriptors", fail_second_inventory, raising=False)

    with pytest.raises(runner.GauntletSafetyError, match="filesystem write"):
        with audit:
            with pytest.raises(runner.GauntletSafetyError, match="filesystem write"):
                with audit:
                    pytest.fail("nested body must not run")
            assert tracker_runner._ACTIVE_PRODUCTION_AUDITS == [audit]
            assert len(audit._activation_totals) == 1

    assert tracker_runner._ACTIVE_PRODUCTION_AUDITS == []
    assert audit._activation_totals == []


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

    with pytest.raises(runner.GauntletSafetyError, match="filesystem write"):
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

    with pytest.raises(runner.GauntletSafetyError, match="filesystem write"):
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert marker.read_bytes() == b"original"
    assert fixture.client.mutation_total == 0


def test_tracker_evaluator_rejects_transient_writes_outside_fixture_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch root-scoped auditing treating an outside dry-run write as harmless."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    marker = tmp_path / "outside-transient-mutation"
    real_execute_pipeline = tracker_runner._execute_pipeline

    def create_then_remove_outside(current_fixture):
        result = real_execute_pipeline(current_fixture)
        marker.write_text("transient", encoding="utf-8")
        marker.unlink()
        return result

    monkeypatch.setattr(tracker_runner, "_execute_pipeline", create_then_remove_outside)

    with pytest.raises(runner.GauntletSafetyError, match="filesystem write") as error:
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert str(marker) not in str(error.value)
    assert not marker.exists()


def test_tracker_evaluator_rejects_descriptor_relative_write_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch descriptor-relative os.open bypassing lexical path resolution."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    outside = tmp_path / "descriptor-target"
    outside.mkdir()
    directory_fd = os.open(outside, os.O_RDONLY)
    real_execute_pipeline = tracker_runner._execute_pipeline

    def write_relative_to_descriptor(current_fixture):
        result = real_execute_pipeline(current_fixture)
        descriptor = os.open("transient", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=directory_fd)
        os.close(descriptor)
        os.unlink("transient", dir_fd=directory_fd)
        return result

    monkeypatch.setattr(tracker_runner, "_execute_pipeline", write_relative_to_descriptor)
    try:
        with pytest.raises(runner.GauntletSafetyError, match="filesystem write"):
            tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)
    finally:
        os.close(directory_fd)

    assert not (outside / "transient").exists()


def test_tracker_semantic_scenario_rejects_transient_filesystem_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch semantic production calls running outside the mutation audit."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    marker = tmp_path / "scenario-transient-mutation"
    real_analyze_impact = tracker_runner.analyze_impact

    def analyze_with_transient_write(*args, **kwargs):
        marker.write_text("transient", encoding="utf-8")
        marker.unlink()
        return real_analyze_impact(*args, **kwargs)

    monkeypatch.setattr(tracker_runner, "analyze_impact", analyze_with_transient_write)

    with pytest.raises(runner.GauntletSafetyError, match="filesystem write") as error:
        tracker_runner.evaluate_tracker_scenarios(fixture)

    assert str(marker) not in str(error.value)
    assert not marker.exists()


def test_tracker_evaluator_rejects_primary_network_connect_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a measured production call attempting an external connection."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    real_execute_pipeline = tracker_runner._execute_pipeline

    def connect_before_pipeline(current_fixture):
        sys.audit("socket.connect", object(), ("forbidden.example.invalid", 443))
        return real_execute_pipeline(current_fixture)

    monkeypatch.setattr(tracker_runner, "_execute_pipeline", connect_before_pipeline)

    with pytest.raises(runner.GauntletSafetyError, match="network connect") as error:
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert "forbidden.example.invalid" not in str(error.value)


def test_tracker_evaluator_rejects_primary_connectionless_send_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a measured production call attempting connectionless outbound I/O."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    real_execute_pipeline = tracker_runner._execute_pipeline

    def send_before_pipeline(current_fixture):
        sys.audit("socket.sendto", object(), ("forbidden.example.invalid", 443))
        return real_execute_pipeline(current_fixture)

    monkeypatch.setattr(tracker_runner, "_execute_pipeline", send_before_pipeline)

    with pytest.raises(runner.GauntletSafetyError, match="network outbound") as error:
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert "forbidden.example.invalid" not in str(error.value)


def test_tracker_semantic_scenario_rejects_network_dns_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a semantic production call attempting DNS resolution."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    real_analyze_impact = tracker_runner.analyze_impact

    def resolve_before_analysis(*args, **kwargs):
        sys.audit("socket.getaddrinfo", "forbidden.example.invalid", 443, 0, 0, 0)
        return real_analyze_impact(*args, **kwargs)

    monkeypatch.setattr(tracker_runner, "analyze_impact", resolve_before_analysis)

    with pytest.raises(runner.GauntletSafetyError, match="network dns") as error:
        tracker_runner.evaluate_tracker_scenarios(fixture)

    assert "forbidden.example.invalid" not in str(error.value)


@pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"), reason="socket.sendmsg is unavailable")
def test_tracker_semantic_scenario_rejects_connectionless_sendmsg_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch a semantic scenario attempting vectorized connectionless outbound I/O."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    real_analyze_impact = tracker_runner.analyze_impact

    def sendmsg_before_analysis(*args, **kwargs):
        sys.audit("socket.sendmsg", object(), (b"blocked",), (), ("forbidden.example.invalid", 443))
        return real_analyze_impact(*args, **kwargs)

    monkeypatch.setattr(tracker_runner, "analyze_impact", sendmsg_before_analysis)

    with pytest.raises(runner.GauntletSafetyError, match="network outbound") as error:
        tracker_runner.evaluate_tracker_scenarios(fixture)

    assert "forbidden.example.invalid" not in str(error.value)


def test_tracker_shadow_execution_rejects_same_path_hash_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch execution tagging a healthy same-path hash while preserving counts."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    profile = _tracker_safety_profile()
    seed = 20_260_729
    fixture = tracker_fixture.build_tracker_fixture(tmp_path / "tracker-fixture", profile, seed=seed)
    cross_seed_hash = hashlib.sha256(f"gauntlet:tracker:torrent:{seed}:2".encode("ascii")).hexdigest()
    healthy_pair_hash = hashlib.sha256(f"gauntlet:tracker:torrent:{seed}:4".encode("ascii")).hexdigest()
    real_add_tags = tracker_fixture.FakeTrackerClient.torrents_add_tags

    def add_tags_with_same_path_swap(client, *args, **kwargs):
        torrent_hashes = list(kwargs.get("torrent_hashes", args[0] if args else ()))
        tags = kwargs.get("tags", args[1] if len(args) > 1 else ())
        if tags == ["unregistered:crossseeding"] and cross_seed_hash in torrent_hashes:
            torrent_hashes[torrent_hashes.index(cross_seed_hash)] = healthy_pair_hash
        return real_add_tags(client, torrent_hashes=torrent_hashes, tags=tags)

    monkeypatch.setattr(tracker_fixture.FakeTrackerClient, "torrents_add_tags", add_tags_with_same_path_swap)

    with pytest.raises(runner.GauntletSafetyError, match="execution action"):
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)


def test_tracker_shadow_execution_rejects_network_connect_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the untimed mutating shadow escaping network isolation."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    real_add_tags = tracker_fixture.FakeTrackerClient.torrents_add_tags

    def connect_before_adding_tags(client, *args, **kwargs):
        sys.audit("socket.connect", object(), ("shadow.example.invalid", 443))
        return real_add_tags(client, *args, **kwargs)

    monkeypatch.setattr(tracker_fixture.FakeTrackerClient, "torrents_add_tags", connect_before_adding_tags)

    with pytest.raises(runner.GauntletSafetyError, match="network connect") as error:
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert "shadow.example.invalid" not in str(error.value)


def test_tracker_shadow_execution_rejects_connectionless_send_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the untimed mutating shadow attempting connectionless outbound I/O."""
    tracker_fixture = _tracker_module("tracker_fixture")
    tracker_runner = _tracker_module("tracker_runner")
    fixture = tracker_fixture.build_tracker_fixture(
        tmp_path / "tracker-fixture",
        _tracker_safety_profile(),
        seed=20_260_729,
    )
    real_add_tags = tracker_fixture.FakeTrackerClient.torrents_add_tags

    def send_before_adding_tags(client, *args, **kwargs):
        sys.audit("socket.sendto", object(), ("shadow.example.invalid", 443))
        return real_add_tags(client, *args, **kwargs)

    monkeypatch.setattr(tracker_fixture.FakeTrackerClient, "torrents_add_tags", send_before_adding_tags)

    with pytest.raises(runner.GauntletSafetyError, match="network outbound") as error:
        tracker_runner.evaluate_tracker_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert "shadow.example.invalid" not in str(error.value)


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
    delete_index = next(
        index
        for index, torrent in enumerate(current)
        if "tracker-delete" in {tag.strip() for tag in torrent.tags.split(",") if tag.strip()}
    )
    if change == "disappear":
        current.pop(delete_index)
    else:
        current[delete_index] = replace(current[delete_index], tags="")
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
        assert set(evidence) == {
            "outcome",
            "action_digest",
            "endpoint_counters",
            "exit_code",
            "terminal_phase",
            "observation_order",
            "mutation_counters",
            "isolation_counters",
        }
        assert evidence["outcome"] == "pass"
        assert len(evidence["action_digest"]) == 64
        assert set(evidence["endpoint_counters"]) == {
            "torrents.info",
            "torrents.info.include_trackers",
            "torrents_trackers",
        }
        assert all(isinstance(count, int) and count >= 0 for count in evidence["endpoint_counters"].values())
        assert evidence["exit_code"] in {0, 1}
        assert evidence["terminal_phase"] in {
            "execution_complete",
            "preview_fail_closed",
            "execution_fail_closed",
        }
        assert evidence["observation_order"] in [["preview"], ["preview", "execution"]]
        assert all(count == 0 for count in evidence["mutation_counters"].values())
        assert evidence["isolation_counters"] == {
            "filesystem_write_attempts": 0,
            "network_connect_attempts": 0,
            "network_dns_attempts": 0,
            "network_outbound_attempts": 0,
        }
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
