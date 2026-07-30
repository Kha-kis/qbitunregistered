"""Tests for the repository-local deterministic gauntlet evaluator."""

from __future__ import annotations

import copy
import json
import math
import tempfile
import tracemalloc
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from benchmarks.gauntlet import __main__ as gauntlet_cli
from benchmarks.gauntlet import paired
from benchmarks.gauntlet import runner
from benchmarks.gauntlet.baseline import (
    BaselineMeasurement,
    compare_result,
    load_quality_bar,
)
from benchmarks.gauntlet.fixture_factory import (
    FULL_PROFILE,
    PROFILES,
    QUICK_PROFILE,
    FakeBulkTorrent,
    GauntletProfile,
    build_blueprint,
    build_fixture,
    expected_endpoint_budgets,
    expected_endpoint_counters,
    materialized_fixture_digest,
)
from benchmarks.gauntlet.identity import RepositoryIdentity
from benchmarks.gauntlet.paired import (
    PAIRED_ORDER,
    PairedGauntletError,
    compare_paired_results,
    run_paired_gauntlet,
)
from benchmarks.gauntlet.runner import (
    DEFAULT_SAMPLES,
    EVALUATOR_VERSION,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    evaluate_fixture,
    run_gauntlet,
    write_result,
)

QUALITY_BAR_PATH = Path(__file__).parents[1] / "benchmarks" / "gauntlet" / "quality-bar.toml"
REPOSITORY_ROOT = QUALITY_BAR_PATH.parents[2]
TINY_PROFILE = GauntletProfile(
    name="tiny",
    torrent_count=6,
    file_count=23,
    orphan_count=1,
    exact_metadata_torrent_count=3,
    shard_count=4,
    tier="test",
)
requires_descriptor_no_follow = pytest.mark.skipif(
    not getattr(paired.os, "O_NOFOLLOW", 0),
    reason="contemporaneous paired execution requires descriptor no-follow support",
)


def _test_environment() -> dict[str, str]:
    return {
        "cpu_affinity_digest": "a" * 64,
        "effective_cpu_count": "4",
        "filesystem_block_size": "4096",
        "filesystem_id": "b" * 64,
        "filesystem_type": "tmpfs",
        "implementation": "CPython",
        "kernel_release": "test-kernel",
        "logical_cpu_count": "4",
        "machine": "x86_64",
        "operating_system": "Linux",
        "processor": "test",
        "python": "3.12.0",
    }


def _valid_quick_result() -> dict[str, Any]:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    profile = quality_bar.profiles["quick"]
    timed_sample_count = quality_bar.measurement_policy["timed_samples"]
    assert isinstance(timed_sample_count, int)
    measurement_policy = {
        key: value
        for key, value in quality_bar.measurement_policy.items()
        if key not in {"timed_samples", "warmup_passes", "memory_passes"}
    }
    counters = {name: budget.maximum for name, budget in profile.api_budgets.items()}
    mutations = {
        "filesystem": 0,
        "qbittorrent": 0,
        "torrents_add_tags": 0,
        "torrents_delete": 0,
        "torrents_pause": 0,
        "torrents_remove_tags": 0,
        "torrents_resume": 0,
        "torrents_set_auto_management": 0,
        "torrents_set_share_limits": 0,
        "torrents_tags": 0,
    }
    return {
        "schema": quality_bar.result_schema,
        "schema_version": quality_bar.evaluator_schema_version,
        "evaluator_version": quality_bar.evaluator_version,
        "scope": quality_bar.scope,
        "commit": "a" * 40,
        "candidate_state": {"clean": True, "diff_sha256": "b" * 64},
        "identity_verified": True,
        "environment": _test_environment(),
        "profile": "quick",
        "tier": "round",
        "seed": profile.seed,
        "fixture_manifest_digest": profile.fixture_manifest_digest,
        "intended_action_digest": profile.intended_action_digest,
        "reconciliation": dict(profile.reconciliation),
        "candidate_counts": {"orphan_files": profile.candidate_count},
        "workload": dict(profile.workload),
        "endpoint_counters": counters,
        "timed_sample_endpoint_counters": [dict(counters) for _ in range(timed_sample_count)],
        "pass_endpoint_counters": {"warmup": dict(counters), "memory": dict(counters)},
        "mutation_counters": mutations,
        "measurement_policy": measurement_policy,
        "sample_runtime_seconds": [1.0] * timed_sample_count,
        "median_runtime_seconds": 1.0,
        "minimum_runtime_seconds": 1.0,
        "maximum_runtime_seconds": 1.0,
        "median_absolute_deviation_seconds": 0.0,
        "peak_memory_bytes": 1,
    }


def _paired_runs(
    *,
    control_runtimes: tuple[float, float, float, float] = (2.0, 2.0, 2.0, 2.0),
    candidate_runtimes: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 0.8),
    control_memory: tuple[int, int, int, int] = (1_000, 1_000, 1_000, 1_000),
    candidate_memory: tuple[int, int, int, int] = (1_100, 1_100, 1_100, 1_100),
) -> list[dict[str, Any]]:
    values = (
        ("control", control_runtimes[0], control_memory[0], "a"),
        ("candidate", candidate_runtimes[0], candidate_memory[0], "c"),
        ("candidate", candidate_runtimes[1], candidate_memory[1], "c"),
        ("control", control_runtimes[1], control_memory[1], "a"),
        ("candidate", candidate_runtimes[2], candidate_memory[2], "c"),
        ("control", control_runtimes[2], control_memory[2], "a"),
        ("control", control_runtimes[3], control_memory[3], "a"),
        ("candidate", candidate_runtimes[3], candidate_memory[3], "c"),
    )
    runs: list[dict[str, Any]] = []
    for position, (role, runtime, memory, commit_character) in enumerate(values):
        result = _valid_quick_result()
        samples = [runtime] * DEFAULT_SAMPLES
        result.update(
            {
                "commit": commit_character * 40,
                "candidate_state": {"clean": True, "diff_sha256": commit_character * 64},
                "sample_runtime_seconds": samples,
                "median_runtime_seconds": runtime,
                "minimum_runtime_seconds": runtime,
                "maximum_runtime_seconds": runtime,
                "median_absolute_deviation_seconds": 0.0,
                "peak_memory_bytes": memory,
            }
        )
        runs.append({"position": position, "role": role, "result": result})
    return runs


def test_fake_qbittorrent_bulk_response_embeds_exact_files_without_endpoint_calls(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=107)
    source_torrent = fixture.initial_torrents[0]
    expected_files = fixture.client.torrents_files(torrent_hash=source_torrent.hash)
    fixture.client.reset_read_counts()

    snapshot = fixture.client.torrents.info(include_files=True)

    assert isinstance(snapshot, list)
    assert all(isinstance(torrent, FakeBulkTorrent) for torrent in snapshot)
    exact_torrent = next(torrent for torrent in snapshot if torrent.hash == source_torrent.hash)
    assert exact_torrent["files"] == expected_files
    assert exact_torrent["files"] is not expected_files
    second_snapshot = fixture.client.torrents.info(include_files=True)
    assert isinstance(second_snapshot, list)
    second_exact = next(
        torrent for torrent in second_snapshot if isinstance(torrent, FakeBulkTorrent) and torrent.hash == source_torrent.hash
    )
    assert second_exact is not exact_torrent
    assert second_exact["files"] == exact_torrent["files"]
    assert second_exact["files"] is not exact_torrent["files"]
    assert fixture.client.read_counts == {"torrents.info": 2}


def test_fake_exact_metadata_allocates_fresh_decoded_responses(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=108)
    torrent_hash = fixture.initial_torrents[0].hash
    fixture.client.reset_read_counts()

    first = fixture.client.torrents_files(torrent_hash=torrent_hash)
    second = fixture.client.torrents_files(torrent_hash=torrent_hash)

    assert first == second
    assert first is not second
    assert first[0] is not second[0]
    assert fixture.client.read_counts == {"torrents_files": 2}


def test_fake_qbittorrent_legacy_and_unsupported_bulk_modes_preserve_fallback(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=109)
    fixture.client.set_bulk_files_mode("legacy_missing")
    snapshot = fixture.client.torrents.info(include_files=True)

    assert isinstance(snapshot, list)
    assert all(isinstance(torrent, FakeBulkTorrent) and "files" not in torrent for torrent in snapshot)
    for torrent in fixture.initial_torrents[: TINY_PROFILE.exact_metadata_torrent_count]:
        fixture.client.torrents_files(torrent_hash=torrent.hash)
    assert fixture.client.read_counts == {
        "torrents.info": 1,
        "torrents_files": TINY_PROFILE.exact_metadata_torrent_count,
    }

    fixture.client.reset_read_counts()
    fixture.client.set_bulk_files_mode("unsupported")
    with pytest.raises(TypeError, match="unsupported"):
        fixture.client.torrents.info(include_files=True)
    assert fixture.client.torrents.info() == list(fixture.initial_torrents)
    assert fixture.client.read_counts == {"torrents.info": 2}


def test_fake_qbittorrent_malformed_bulk_mode_is_explicit(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=113)
    fixture.client.set_bulk_files_mode("malformed")

    snapshot = fixture.client.torrents.info(include_files=True)

    assert isinstance(snapshot, list)
    assert any(isinstance(torrent, FakeBulkTorrent) and isinstance(torrent["files"], dict) for torrent in snapshot)
    assert fixture.client.read_counts["torrents_files"] == 0


def test_paired_comparison_passes_supported_api_reduction_and_retains_all_samples() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    runs = _paired_runs()
    for run in runs:
        if run["role"] == "candidate":
            result = run["result"]
            for counters in [
                result["endpoint_counters"],
                *result["timed_sample_endpoint_counters"],
                *result["pass_endpoint_counters"].values(),
            ]:
                counters["torrents_files"] = 0

    comparison = compare_paired_results(runs, quality_bar)

    assert comparison["overall"] == "pass"
    assert comparison["runtime_pair_ratios"] == pytest.approx([0.4] * 4)
    assert comparison["memory_pair_ratios"] == pytest.approx([1.1] * 4)
    assert comparison["runtime_control_fraction"] == pytest.approx(0.4)
    assert comparison["memory_control_fraction"] == pytest.approx(1.1)
    assert comparison["block_runtime_control_fractions"] == pytest.approx([0.4, 0.4])
    assert comparison["block_memory_control_fractions"] == pytest.approx([1.1, 1.1])
    assert comparison["role_runtime_relative_ranges"] == {
        "control": 0.0,
        "candidate": 0.0,
    }
    assert comparison["role_memory_relative_ranges"] == {
        "control": 0.0,
        "candidate": 0.0,
    }
    assert len([sample for run in runs for sample in run["result"]["sample_runtime_seconds"]]) == 40
    control_positions = [run["position"] for run in runs if run["role"] == "control"]
    candidate_positions = [run["position"] for run in runs if run["role"] == "candidate"]
    assert sum(control_positions) == sum(candidate_positions) == 14


@pytest.mark.parametrize(
    "mutate",
    [
        lambda runs: runs.reverse(),
        lambda runs: runs[1]["result"].update({"environment": {**_test_environment(), "processor": "other"}}),
        lambda runs: runs[2]["result"]["candidate_state"].update({"clean": False}),
        lambda runs: runs[1]["result"].update({"seed": 0}),
        lambda runs: runs[1]["result"].update({"sample_runtime_seconds": [math.nan] * 5}),
        lambda runs: runs[1]["result"].update({"unknown": "ignored-secret"}),
        lambda runs: runs[1]["result"]["environment"].update({"unknown": "ignored-secret"}),
    ],
)
def test_paired_comparison_fails_closed_on_order_identity_environment_or_parsing(mutate) -> None:
    runs = _paired_runs()
    mutate(runs)

    comparison = compare_paired_results(runs, load_quality_bar(QUALITY_BAR_PATH))

    assert comparison["overall"] == "fail"


def test_paired_comparison_rejects_child_variance_and_cross_pair_drift() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    varying = _paired_runs()
    varying[1]["result"].update(
        {
            "sample_runtime_seconds": [0.4, 0.4, 0.8, 1.2, 1.2],
            "median_runtime_seconds": 0.8,
            "minimum_runtime_seconds": 0.4,
            "maximum_runtime_seconds": 1.2,
            "median_absolute_deviation_seconds": 0.4,
        }
    )
    assert compare_paired_results(varying, quality_bar)["gates"]["child_gates"]["status"] == "fail"

    drifting = _paired_runs(candidate_runtimes=(0.8, 1.6, 0.8, 1.6))
    comparison = compare_paired_results(drifting, quality_bar)
    assert comparison["gates"]["paired_drift"]["status"] == "fail"
    assert comparison["overall"] == "fail"


def test_paired_comparison_gates_each_block_and_memory_robustness() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    slow_second_block = _paired_runs(
        candidate_runtimes=(0.8, 0.8, 1.2, 1.2),
    )
    comparison = compare_paired_results(slow_second_block, quality_bar)
    assert comparison["runtime_control_fraction"] == pytest.approx(0.5)
    assert comparison["block_runtime_control_fractions"] == pytest.approx([0.4, 0.6])
    assert comparison["gates"]["runtime"]["status"] == "fail"

    memory_block_failure = _paired_runs(
        candidate_memory=(1_100, 1_100, 1_400, 1_400),
    )
    comparison = compare_paired_results(memory_block_failure, quality_bar)
    assert comparison["block_memory_control_fractions"] == pytest.approx([1.1, 1.4])
    assert comparison["gates"]["memory"]["status"] == "fail"

    memory_drift_failure = _paired_runs(
        candidate_memory=(500, 500, 1_500, 1_500),
    )
    comparison = compare_paired_results(memory_drift_failure, quality_bar)
    assert comparison["gates"]["memory_drift"]["status"] == "fail"


@requires_descriptor_no_follow
def test_paired_runner_uses_crossover_and_emits_all_bound_identities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    expected_runs = _paired_runs()
    calls: list[str] = []
    identities = {
        REPOSITORY_ROOT: RepositoryIdentity("e" * 40, True, "e" * 64),
        control_root: RepositoryIdentity("a" * 40, True, "a" * 64),
        candidate_root: RepositoryIdentity("c" * 40, True, "c" * 64),
    }

    monkeypatch.setattr(
        "benchmarks.gauntlet.paired.capture_repository_identity",
        lambda root: identities[root],
    )
    monkeypatch.setattr("benchmarks.gauntlet.paired._evaluator_digest", lambda _root: "d" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._named_files_digest", lambda *_args: "f" * 64)

    def fake_run_child(root: Path, **_kwargs):
        role = "control" if root == control_root else "candidate"
        calls.append(role)
        return copy.deepcopy(expected_runs[len(calls) - 1]["result"])

    monkeypatch.setattr("benchmarks.gauntlet.paired._run_child", fake_run_child)
    result = run_paired_gauntlet(
        control_root,
        candidate_root,
        orchestrator_root=REPOSITORY_ROOT,
        profile="quick",
        seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
        samples=DEFAULT_SAMPLES,
    )

    assert calls == list(PAIRED_ORDER)
    assert result["identities"]["orchestrator"]["commit"] == "e" * 40
    assert result["identities"]["control"]["commit"] == "a" * 40
    assert result["identities"]["candidate"]["commit"] == "c" * 40
    assert result["dependency_digest"] == "f" * 64
    assert len(result["quality_bar_digest"]) == 64
    assert len(result["evaluator_digest"]) == 64
    assert result["thresholds"] == {
        "runtime_control_fraction_max": 0.50,
        "memory_control_fraction_max": 1.25,
        "paired_runtime_relative_range_max": 0.50,
        "paired_memory_relative_range_max": 0.50,
    }
    assert len(result["runs"]) == 8
    retained_sample_count = 0
    for run in result["runs"]:
        samples = run["result"]["sample_runtime_seconds"]
        assert isinstance(samples, list)
        retained_sample_count += len(samples)
    assert retained_sample_count == 40


@requires_descriptor_no_follow
def test_paired_runner_rechecks_importable_extensions_after_crossover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    expected_runs = _paired_runs()
    calls: list[Path] = []
    identities = {
        REPOSITORY_ROOT: RepositoryIdentity("e" * 40, True, "e" * 64),
        control_root: RepositoryIdentity("a" * 40, True, "a" * 64),
        candidate_root: RepositoryIdentity("c" * 40, True, "c" * 64),
    }

    monkeypatch.setattr(
        "benchmarks.gauntlet.paired.capture_repository_identity",
        lambda root: identities[root],
    )
    monkeypatch.setattr("benchmarks.gauntlet.paired._evaluator_digest", lambda _root: "d" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._named_files_digest", lambda *_args: "f" * 64)

    def fake_run_child(root: Path, **_kwargs) -> dict[str, object]:
        calls.append(root)
        if len(calls) == len(PAIRED_ORDER):
            extension_path = candidate_root / "benchmarks" / "gauntlet" / f"runner{paired.EXTENSION_SUFFIXES[0]}"
            extension_path.parent.mkdir(parents=True)
            extension_path.write_bytes(b"late native extension")
        return copy.deepcopy(expected_runs[len(calls) - 1]["result"])

    monkeypatch.setattr("benchmarks.gauntlet.paired._run_child", fake_run_child)

    with pytest.raises(PairedGauntletError, match="importable native extension"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    assert len(calls) == len(PAIRED_ORDER)


@requires_descriptor_no_follow
def test_paired_runner_rejects_same_or_different_evaluator_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._require_clean_identity", lambda _root: identity)
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)

    with pytest.raises(PairedGauntletError, match="isolated"):
        run_paired_gauntlet(
            tmp_path,
            tmp_path,
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=quality_bar.profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    monkeypatch.setattr(
        "benchmarks.gauntlet.paired._evaluator_digest",
        lambda root: "c" * 64 if root == candidate_root else "a" * 64,
    )
    monkeypatch.setattr("benchmarks.gauntlet.paired._named_files_digest", lambda *_args: "d" * 64)
    with pytest.raises(PairedGauntletError, match="identical evaluator"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=quality_bar.profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )


@requires_descriptor_no_follow
def test_paired_runner_rejects_different_parent_package_initializers_before_child_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = [tmp_path / name for name in ("orchestrator", "control", "candidate")]
    for root in roots:
        evaluator_root = root / "benchmarks" / "gauntlet"
        evaluator_root.mkdir(parents=True)
        (evaluator_root.parent / "__init__.py").write_text('"""Trusted benchmark package."""\n', encoding="utf-8")
        (evaluator_root / "__init__.py").write_text('"""Trusted gauntlet package."""\n', encoding="utf-8")
        (evaluator_root / "quality-bar.toml").write_bytes(QUALITY_BAR_PATH.read_bytes())
    orchestrator_root, control_root, candidate_root = roots
    (candidate_root / "benchmarks" / "__init__.py").write_text(
        '"""Different executable parent initializer."""\n',
        encoding="utf-8",
    )
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._require_clean_identity", lambda _root: identity)
    monkeypatch.setattr("benchmarks.gauntlet.paired._named_files_digest", lambda *_args: "d" * 64)
    child_calls: list[Path] = []

    def record_child(repository_root: Path, **_kwargs) -> dict[str, object]:
        child_calls.append(repository_root)
        return {}

    monkeypatch.setattr("benchmarks.gauntlet.paired._run_child", record_child)

    with pytest.raises(PairedGauntletError, match="identical evaluator"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=orchestrator_root,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    assert child_calls == []


@pytest.mark.parametrize(
    "extension_path",
    [
        Path("benchmarks") / "gauntlet" / f"runner{paired.EXTENSION_SUFFIXES[0]}",
        Path("benchmarks") / f"__init__{paired.EXTENSION_SUFFIXES[0]}",
    ],
)
def test_paired_runner_rejects_importable_extensions_before_child_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension_path: Path,
) -> None:
    roots = [tmp_path / name for name in ("orchestrator", "control", "candidate")]
    for root in roots:
        (root / "benchmarks" / "gauntlet").mkdir(parents=True)
        (root / "qbitunregistered").mkdir()
        paired._reject_importable_extensions(root)
    orchestrator_root, control_root, candidate_root = roots
    shadowing_extension = candidate_root / extension_path
    shadowing_extension.parent.mkdir(parents=True, exist_ok=True)
    shadowing_extension.write_bytes(b"ignored native extension")
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._require_clean_identity", lambda _root: identity)
    child_calls: list[Path] = []

    def record_child(repository_root: Path, **_kwargs) -> dict[str, object]:
        child_calls.append(repository_root)
        return {}

    monkeypatch.setattr("benchmarks.gauntlet.paired._run_child", record_child)

    with pytest.raises(PairedGauntletError, match="importable native extension"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=orchestrator_root,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    assert child_calls == []


@requires_descriptor_no_follow
def test_paired_runner_rejects_dependency_and_orchestrator_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr(
        "benchmarks.gauntlet.paired._require_clean_identity",
        lambda _root: identity,
    )
    monkeypatch.setattr(
        "benchmarks.gauntlet.paired._evaluator_digest",
        lambda _root: "c" * 64,
    )
    monkeypatch.setattr(
        "benchmarks.gauntlet.paired._named_files_digest",
        lambda root, _names: "d" * 64 if root == candidate_root else "e" * 64,
    )

    with pytest.raises(PairedGauntletError, match="dependency locks"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    def reject_orchestrator(root: Path) -> RepositoryIdentity:
        if root == REPOSITORY_ROOT:
            raise PairedGauntletError("paired repositories must have clean identities")
        return identity

    monkeypatch.setattr(
        "benchmarks.gauntlet.paired._require_clean_identity",
        reject_orchestrator,
    )
    with pytest.raises(PairedGauntletError, match="clean identities"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )


def test_paired_bounded_reader_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "artifact.json"
    try:
        link.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a symbolic link: {error}")
    if not getattr(paired.os, "O_NOFOLLOW", 0):
        pytest.skip("platform does not expose O_NOFOLLOW")

    with pytest.raises(PairedGauntletError, match="open paired child artifact safely"):
        paired._read_regular_file(
            link,
            maximum_bytes=1024,
            description="paired child artifact",
        )


def test_paired_bounded_reader_fails_closed_without_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "artifact.json"
    artifact.write_text("{}", encoding="utf-8")
    monkeypatch.delattr(paired.os, "O_NOFOLLOW", raising=False)

    with pytest.raises(PairedGauntletError, match="without no-follow support"):
        paired._read_regular_file(
            artifact,
            maximum_bytes=1024,
            description="paired child artifact",
        )


@requires_descriptor_no_follow
def test_paired_child_uses_isolated_python_environment_and_fresh_bytecode_caches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "child.json"
    inherited_pycache = tmp_path / "inherited-pycache"
    monkeypatch.setenv("PYTHONSTARTUP", "/private/injection.py")
    monkeypatch.setenv("PYTHONPATH", "/private/injection")
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(inherited_pycache))
    pycache_roots: list[Path] = []

    def fake_run(command, **kwargs):
        assert command[1:3] == ["-s", "-m"]
        environment = kwargs["env"]
        assert environment["PYTHONNOUSERSITE"] == "1"
        assert environment["PYTHONHASHSEED"] == "0"
        assert "PYTHONSTARTUP" not in environment
        assert "PYTHONPATH" not in environment
        pycache_root = Path(environment["PYTHONPYCACHEPREFIX"])
        assert pycache_root.is_dir()
        assert not pycache_root.is_relative_to(tmp_path.resolve())
        assert pycache_root != inherited_pycache
        pycache_roots.append(pycache_root)
        output.write_text(json.dumps(_valid_quick_result()), encoding="utf-8")
        return paired.subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(paired.subprocess, "run", fake_run)

    results = [
        paired._run_child(
            tmp_path,
            profile="quick",
            seed=20_260_729,
            samples=DEFAULT_SAMPLES,
            output=output,
        )
        for _ in range(2)
    ]

    assert [result["profile"] for result in results] == ["quick", "quick"]
    assert len(set(pycache_roots)) == 2
    assert all(not path.exists() for path in pycache_roots)


def test_paired_cli_requires_both_worktrees_and_external_output(tmp_path: Path) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()

    with pytest.raises(SystemExit, match="must be supplied together"):
        gauntlet_cli.main(["--paired-control", str(control_root)])

    with pytest.raises(SystemExit, match="outside both evaluated repositories"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--output",
                str(candidate_root / "result.json"),
            ]
        )

    custom_quality_bar = tmp_path / "custom-quality-bar.toml"
    custom_quality_bar.write_text(
        QUALITY_BAR_PATH.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="canonical quality bar"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--compare",
                str(custom_quality_bar),
            ]
        )


def test_paired_cli_resolves_relative_worktrees_before_output_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    invocation_root = tmp_path / "invocation"
    control_root.mkdir()
    candidate_root.mkdir()
    invocation_root.mkdir()
    monkeypatch.chdir(invocation_root)
    run_calls: list[tuple[object, ...]] = []

    def record_paired_run(*args, **_kwargs):
        run_calls.append(args)
        return {}

    monkeypatch.setattr(paired, "run_paired_gauntlet", record_paired_run)

    with pytest.raises(SystemExit, match="outside both evaluated repositories"):
        gauntlet_cli.main(
            [
                "--paired-control",
                "../control",
                "--paired-candidate",
                "../candidate",
                "--output",
                "../control/result.json",
            ]
        )

    assert run_calls == []


def test_paired_cli_atomically_replaces_external_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    output_root = tmp_path / "output"
    control_root.mkdir()
    candidate_root.mkdir()
    output_root.mkdir()
    protected_path = output_root / "protected.json"
    protected_content = "protected external content\n"
    protected_path.write_text(protected_content, encoding="utf-8")
    output_path = output_root / "result.json"
    try:
        output_path.symlink_to(protected_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a file symbolic link: {error}")
    run_calls: list[tuple[object, ...]] = []

    def record_paired_run(*args, **_kwargs):
        run_calls.append(args)
        return {"comparison": {"overall": "fail"}}

    monkeypatch.setattr(paired, "run_paired_gauntlet", record_paired_run)

    exit_code = gauntlet_cli.main(
        [
            "--paired-control",
            str(control_root),
            "--paired-candidate",
            str(candidate_root),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == gauntlet_cli.COMPARISON_FAILED_EXIT
    assert len(run_calls) == 1
    assert protected_path.read_text(encoding="utf-8") == protected_content
    assert not output_path.is_symlink()
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"comparison": {"overall": "fail"}}


def test_paired_cli_rejects_output_symlink_into_evaluated_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    output_root = tmp_path / "output"
    control_root.mkdir()
    candidate_root.mkdir()
    output_root.mkdir()
    protected_path = candidate_root / "protected.json"
    protected_path.write_text("protected candidate content\n", encoding="utf-8")
    output_path = output_root / "result.json"
    try:
        output_path.symlink_to(protected_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a file symbolic link: {error}")
    run_calls: list[tuple[object, ...]] = []

    def record_paired_run(*args, **_kwargs):
        run_calls.append(args)
        return {"comparison": {"overall": "fail"}}

    monkeypatch.setattr(paired, "run_paired_gauntlet", record_paired_run)

    with pytest.raises(SystemExit, match="outside both evaluated repositories"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--output",
                str(output_path),
            ]
        )

    assert run_calls == []
    assert output_path.is_symlink()
    assert protected_path.read_text(encoding="utf-8") == "protected candidate content\n"


def test_paired_cli_rejects_retargeted_output_ancestor_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    output_root = tmp_path / "output"
    control_root.mkdir()
    candidate_root.mkdir()
    output_root.mkdir()
    first_parent = output_root / "first"
    second_parent = output_root / "second"
    first_parent.mkdir()
    second_parent.mkdir()
    protected_path = output_root / "protected.json"
    protected_content = "protected external content\n"
    protected_path.write_text(protected_content, encoding="utf-8")
    output_parent = output_root / "current"
    try:
        (first_parent / "result.json").symlink_to(protected_path)
        (second_parent / "result.json").symlink_to(protected_path)
        output_parent.symlink_to(first_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a directory symbolic link: {error}")
    output_path = output_parent / "result.json"
    write_calls: list[Path | None] = []

    def retarget_output_ancestor(*_args, **_kwargs):
        output_parent.unlink()
        output_parent.symlink_to(second_parent, target_is_directory=True)
        return {"comparison": {"overall": "fail"}}

    def record_write(_serialized_result: str, output: Path | None = None) -> Path:
        write_calls.append(output)
        return output_path

    monkeypatch.setattr(paired, "run_paired_gauntlet", retarget_output_ancestor)
    monkeypatch.setattr(runner, "write_serialized_result", record_write)

    with pytest.raises(SystemExit, match="changed or became unsafe"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--output",
                str(output_path),
            ]
        )

    assert write_calls == []
    assert protected_path.read_text(encoding="utf-8") == protected_content
    assert (first_parent / "result.json").is_symlink()
    assert (second_parent / "result.json").is_symlink()


def test_locked_profiles_match_live_reference_shape() -> None:
    assert (
        QUICK_PROFILE.torrent_count,
        QUICK_PROFILE.file_count,
        QUICK_PROFILE.orphan_count,
        QUICK_PROFILE.exact_metadata_torrent_count,
    ) == (1_200, 9_400, 1, 674)
    assert (
        FULL_PROFILE.torrent_count,
        FULL_PROFILE.file_count,
        FULL_PROFILE.orphan_count,
        FULL_PROFILE.exact_metadata_torrent_count,
    ) == (12_000, 94_000, 3, 6_739)
    assert QUICK_PROFILE.configured_root_count == FULL_PROFILE.configured_root_count == 3


def test_fixture_canonicalizes_fake_api_paths_before_bulk_ownership(
    tmp_path: Path,
) -> None:
    canonical_parent = tmp_path / "canonical"
    canonical_parent.mkdir()
    aliased_parent = tmp_path / "alias"
    try:
        aliased_parent.symlink_to(canonical_parent, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a directory symbolic link: {error}")

    fixture = build_fixture(aliased_parent / "fixture", TINY_PROFILE, seed=17)

    assert fixture.root == (canonical_parent / "fixture").resolve()
    for torrent in fixture.initial_torrents:
        save_path = Path(torrent.save_path)
        content_path = Path(torrent.content_path)
        assert save_path == save_path.resolve()
        assert content_path == content_path.resolve()

    result = evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert result["candidate_counts"] == {"orphan_files": 1}


@pytest.mark.parametrize("profile_name", ["quick", "full"])
def test_versioned_quality_bar_locks_known_fixture_oracle(profile_name: str) -> None:
    profile = PROFILES[profile_name]
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    oracle = quality_bar.profiles[profile_name]
    blueprint = build_blueprint(profile, oracle.seed)

    assert blueprint.manifest_digest == oracle.fixture_manifest_digest
    assert blueprint.intended_action_digest == oracle.intended_action_digest
    assert len(blueprint.orphans) == oracle.candidate_count
    assert {name: (budget.minimum, budget.maximum) for name, budget in expected_endpoint_budgets(profile).items()} == {
        name: (budget.minimum, budget.maximum) for name, budget in oracle.api_budgets.items()
    }
    assert {
        "file_action_count": blueprint.reconciliation.file_action_count,
        "empty_directory_count": blueprint.reconciliation.empty_directory_count,
        "file_action_digest": blueprint.reconciliation.file_action_digest,
        "empty_directory_digest": blueprint.reconciliation.empty_directory_digest,
        "digest": blueprint.reconciliation.digest,
    } == oracle.reconciliation
    assert sum(len(torrent.files) for torrent in blueprint.torrents) + len(blueprint.orphans) == profile.file_count


def test_measurement_phases_never_trace_timed_samples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=41)
    trace_states: list[bool] = []
    execute_pipeline = runner._execute_pipeline

    def record_trace_state(current_fixture):
        trace_states.append(tracemalloc.is_tracing())
        return execute_pipeline(current_fixture)

    monkeypatch.setattr(runner, "_execute_pipeline", record_trace_state)

    result = evaluate_fixture(fixture, samples=DEFAULT_SAMPLES)

    assert trace_states == [False] * (1 + DEFAULT_SAMPLES) + [True]
    assert len(result["sample_runtime_seconds"]) == DEFAULT_SAMPLES
    assert result["minimum_runtime_seconds"] <= result["median_runtime_seconds"]
    assert result["median_runtime_seconds"] <= result["maximum_runtime_seconds"]
    assert result["median_absolute_deviation_seconds"] >= 0
    assert result["measurement_policy"] == {
        "sequence": "one warmup, five untraced timed samples, one traced untimed memory pass",
        "timed_samples_traced": False,
        "memory_pass_timed": False,
        "application_cache": "cleared before every pass",
        "fixture_metadata": "materialized once and reused after the explicit warmup",
        "os_page_cache": "not flushed; timed and memory passes are warm",
        "sample_rejection": "none; all five timed samples are retained",
    }


def test_result_schema_has_normalized_per_pass_api_evidence(tmp_path: Path) -> None:
    result = run_gauntlet(
        TINY_PROFILE,
        seed=19,
        samples=DEFAULT_SAMPLES,
        repository_root=tmp_path,
    )
    expected_api = expected_endpoint_counters(TINY_PROFILE)
    serialized = json.dumps(result, sort_keys=True)

    assert result["schema"] == SCHEMA_NAME
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["evaluator_version"] == EVALUATOR_VERSION
    assert result["commit"] == "unknown"
    assert result["candidate_state"] == {"clean": None, "diff_sha256": "unknown"}
    assert result["identity_verified"] is True
    assert result["candidate_counts"] == {"orphan_files": 1}
    assert result["endpoint_counters"] == expected_api
    assert result["timed_sample_endpoint_counters"] == [expected_api] * DEFAULT_SAMPLES
    assert result["pass_endpoint_counters"] == {
        "warmup": expected_api,
        "memory": expected_api,
    }
    assert result["mutation_counters"]["filesystem"] == 0
    assert result["mutation_counters"]["qbittorrent"] == 0
    assert result["workload"] == {
        "torrents": 6,
        "filesystem_files": 23,
        "owned_files": 22,
        "orphan_files": 1,
        "exact_metadata_torrents": 3,
        "bulk_path_torrents": 3,
        "configured_roots": 3,
        "shards": 4,
        "timed_samples": 5,
        "warmup_passes": 1,
        "memory_passes": 1,
    }
    assert len(result["fixture_manifest_digest"]) == 64
    assert len(result["intended_action_digest"]) == 64
    assert result["reconciliation"]["file_action_count"] == 1
    assert result["reconciliation"]["empty_directory_count"] == 1
    assert str(tmp_path) not in serialized
    assert "qbitunregistered-gauntlet-fixture-" not in serialized


def test_nonlocked_sample_count_is_rejected_before_measurement(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=43)

    with pytest.raises(ValueError, match="exactly 5 timed samples"):
        evaluate_fixture(fixture, samples=3)

    assert fixture.client.read_counts == {}
    assert fixture.client.mutation_total == 0


def test_canonical_quality_bar_reports_pending_baselines_without_weakening_targets() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    for profile in quality_bar.profiles.values():
        assert profile.baseline == BaselineMeasurement(
            status="pending_clean_evaluator_commit",
            median_runtime_seconds=None,
            peak_memory_bytes=None,
            environment=None,
        )
        assert profile.runtime_baseline_fraction_max == 0.50
        assert profile.peak_memory_baseline_fraction_max == 1.25

    result = _valid_quick_result()
    pending_report = compare_result(result, quality_bar)

    assert pending_report["overall"] == "pending"
    assert pending_report["gates"]["identity"]["status"] == "pass"
    assert pending_report["gates"]["environment"]["status"] == "pending"
    assert pending_report["gates"]["runtime"]["status"] == "pending"
    assert pending_report["gates"]["memory"]["status"] == "pending"

    result["commit"] = "unknown"
    result["candidate_state"] = {"clean": None, "diff_sha256": "unknown"}
    report = compare_result(result, quality_bar)

    assert report["overall"] == "fail"
    assert report["gates"]["identity"]["status"] == "non_comparable"
    assert report["gates"]["measurement_policy"]["status"] == "pass"
    assert report["gates"]["environment"]["status"] == "pending"
    assert report["gates"]["safety"]["status"] == "pass"
    assert report["gates"]["result"]["status"] == "pass"
    assert report["gates"]["api"]["status"] == "pass"
    assert report["gates"]["variance"]["status"] == "pass"
    assert report["gates"]["runtime"]["status"] == "pending"
    assert report["gates"]["memory"]["status"] == "pending"

    mismatched_result = dict(result)
    mismatched_result["measurement_policy"] = {
        **result["measurement_policy"],
        "os_page_cache": "different",
    }
    mismatched_report = compare_result(
        mismatched_result,
        quality_bar,
    )
    assert mismatched_report["gates"]["measurement_policy"]["status"] == "non_comparable"

    missing_policy_result = copy.deepcopy(result)
    missing_policy_result["measurement_policy"].pop("sequence")
    assert (
        compare_result(
            missing_policy_result,
            quality_bar,
        )["gates"][
            "measurement_policy"
        ]["status"]
        == "non_comparable"
    )

    extra_policy_result = copy.deepcopy(result)
    extra_policy_result["measurement_policy"]["unexpected"] = "value"
    assert (
        compare_result(
            extra_policy_result,
            quality_bar,
        )["gates"][
            "measurement_policy"
        ]["status"]
        == "non_comparable"
    )


def test_missing_platform_environment_capabilities_fail_non_comparable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(runner.os, "sched_getaffinity", raising=False)
    monkeypatch.delattr(runner.os, "statvfs", raising=False)

    result = run_gauntlet(
        TINY_PROFILE,
        seed=103,
        samples=DEFAULT_SAMPLES,
        repository_root=tmp_path,
    )

    assert result["environment"]["cpu_affinity_digest"] == "unknown"
    assert result["environment"]["effective_cpu_count"] == "unknown"
    assert result["environment"]["filesystem_block_size"] == "unknown"
    assert result["environment"]["filesystem_id"] == "unknown"

    comparable_shape = _valid_quick_result()
    comparable_shape["environment"] = result["environment"]
    report = compare_result(comparable_shape, load_quality_bar(QUALITY_BAR_PATH))
    assert report["gates"]["environment"]["status"] == "non_comparable"


def test_baseline_measurements_remain_distinct_from_stricter_targets() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    quick = quality_bar.profiles["quick"]
    environment = _test_environment()
    measured_quick = replace(
        quick,
        baseline=BaselineMeasurement(
            status="measured",
            median_runtime_seconds=100.0,
            peak_memory_bytes=1_000,
            environment=environment,
        ),
    )
    measured_quality_bar = replace(
        quality_bar,
        profiles={**quality_bar.profiles, "quick": measured_quick},
    )
    report = compare_result(
        {
            "profile": "quick",
            "environment": environment,
            "median_runtime_seconds": 60.0,
            "peak_memory_bytes": 1_200,
        },
        measured_quality_bar,
    )

    assert report["gates"]["runtime"] == {
        "status": "fail",
        "detail": "runtime exceeds the independent target",
        "actual": 60.0,
        "baseline": 100.0,
        "target": 50.0,
    }
    assert report["gates"]["memory"] == {
        "status": "pass",
        "detail": "memory meets the independent target",
        "actual": 1_200.0,
        "baseline": 1_000,
        "target": 1_250.0,
    }
    mismatched_environment = compare_result(
        {
            "profile": "quick",
            "environment": {**environment, "processor": "different"},
            "median_runtime_seconds": 60.0,
            "peak_memory_bytes": 1_200,
        },
        measured_quality_bar,
    )
    assert mismatched_environment["gates"]["environment"]["status"] == "non_comparable"


@pytest.mark.parametrize(
    ("mutation", "gate"),
    [
        (lambda result: result["mutation_counters"].pop("filesystem"), "safety"),
        (lambda result: result["mutation_counters"].update({"unknown": 0}), "safety"),
        (lambda result: result["mutation_counters"].update({"filesystem": -1}), "safety"),
        (lambda result: result.update({"schema": "wrong"}), "result"),
        (lambda result: result.update({"scope": "wrong"}), "result"),
        (lambda result: result.update({"fixture_manifest_digest": "x" * 64}), "result"),
        (lambda result: result["reconciliation"].update({"file_action_count": True}), "result"),
        (lambda result: result.update({"peak_memory_bytes": -1}), "memory"),
        (lambda result: result.update({"median_runtime_seconds": math.nan}), "runtime"),
    ],
)
def test_comparator_rejects_malformed_or_adversarial_evidence(mutation, gate: str) -> None:
    result = _valid_quick_result()
    mutation(result)

    report = compare_result(result, load_quality_bar(QUALITY_BAR_PATH))

    assert report["gates"][gate]["status"] in {"fail", "non_comparable"}


def test_api_budget_allows_safe_exact_metadata_reduction_and_rejects_growth() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    result = _valid_quick_result()
    for counters in [
        result["endpoint_counters"],
        *result["timed_sample_endpoint_counters"],
        *result["pass_endpoint_counters"].values(),
    ]:
        counters["torrents_files"] = 0
    assert compare_result(result, quality_bar)["gates"]["api"]["status"] == "pass"

    grown = copy.deepcopy(result)
    grown["timed_sample_endpoint_counters"][2]["torrents_files"] = 675
    assert compare_result(grown, quality_bar)["gates"]["api"]["status"] == "fail"

    unknown = copy.deepcopy(result)
    unknown["endpoint_counters"]["unknown"] = 0
    assert compare_result(unknown, quality_bar)["gates"]["api"]["status"] == "fail"


@pytest.mark.parametrize(
    ("samples", "median", "expected"),
    [
        ([1.0, 1.0, 1.0, 1.0], 1.0, "fail"),
        ([1.0, 1.0, math.inf, 1.0, 1.0], 1.0, "fail"),
        ([1.0, 1.0, 1.0, 1.0, 1.0], 2.0, "fail"),
        ([1.0, 1.0, 1.0, 1.0, 2.0], 1.0, "fail"),
    ],
)
def test_variance_gate_rejects_bad_samples_or_statistics(
    samples: list[float],
    median: float,
    expected: str,
) -> None:
    result = _valid_quick_result()
    result["sample_runtime_seconds"] = samples
    result["median_runtime_seconds"] = median
    if len(samples) == 5 and all(math.isfinite(value) for value in samples):
        result["minimum_runtime_seconds"] = min(samples)
        result["maximum_runtime_seconds"] = max(samples)
        result["median_absolute_deviation_seconds"] = 0.0

    report = compare_result(result, load_quality_bar(QUALITY_BAR_PATH))

    assert report["gates"]["variance"]["status"] == expected


def test_variance_gate_reports_normalized_evidence_for_mad_only_failure() -> None:
    result = _valid_quick_result()
    samples = [0.84, 0.84, 1.0, 1.16, 1.16]
    result["sample_runtime_seconds"] = samples
    result["median_runtime_seconds"] = 1.0
    result["minimum_runtime_seconds"] = 0.84
    result["maximum_runtime_seconds"] = 1.16
    result["median_absolute_deviation_seconds"] = 0.16

    report = compare_result(result, load_quality_bar(QUALITY_BAR_PATH))

    variance_gate = report["gates"]["variance"]
    assert variance_gate["status"] == "fail"
    actual = variance_gate["actual"]
    target = variance_gate["target"]
    assert actual is not None
    assert target is not None
    assert actual == pytest.approx(0.16 / 0.15)
    assert target == 1.0
    assert actual > target


def test_cli_comparison_mode_exits_nonzero_and_writes_gate_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    available_profiles = {
        "quick": replace(TINY_PROFILE, name="quick"),
        "full": FULL_PROFILE,
    }
    monkeypatch.setattr(runner, "PROFILES", available_profiles)
    output = tmp_path / "result.json"

    exit_code = gauntlet_cli.main(
        [
            "--profile",
            "quick",
            "--samples",
            "5",
            "--output",
            str(output),
            "--compare",
            str(QUALITY_BAR_PATH),
        ]
    )

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == gauntlet_cli.COMPARISON_FAILED_EXIT
    assert capsys.readouterr().out.strip() == str(output.resolve())
    assert artifact["comparison"]["overall"] == "fail"
    assert set(artifact["comparison"]["gates"]) == {
        "identity",
        "measurement_policy",
        "environment",
        "safety",
        "result",
        "api",
        "variance",
        "runtime",
        "memory",
    }


def test_omitted_output_uses_a_unique_system_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    output = write_result({"schema_version": SCHEMA_VERSION})

    assert output.parent == tmp_path
    assert output.name.startswith("qbitunregistered-gauntlet-")
    assert json.loads(output.read_text(encoding="utf-8")) == {"schema_version": SCHEMA_VERSION}


def test_explicit_output_replaces_hard_link_without_mutating_protected_file(
    tmp_path: Path,
) -> None:
    protected_path = tmp_path / "protected.json"
    protected_content = "protected repository content\n"
    protected_path.write_text(protected_content, encoding="utf-8")
    protected_identity = protected_path.stat()
    output = tmp_path / "result.json"
    try:
        output.hardlink_to(protected_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a file hard link: {error}")
    assert output.samefile(protected_path)
    serialized_result = runner.serialize_result({"schema_version": SCHEMA_VERSION})

    written_path = runner.write_serialized_result(serialized_result, output)

    assert written_path == output.resolve()
    assert protected_path.read_text(encoding="utf-8") == protected_content
    assert (protected_path.stat().st_dev, protected_path.stat().st_ino) == (
        protected_identity.st_dev,
        protected_identity.st_ino,
    )
    assert output.read_text(encoding="utf-8") == serialized_result
    assert not output.samefile(protected_path)


def test_explicit_output_replaces_symlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    protected_path = tmp_path / "protected.json"
    protected_content = "protected repository content\n"
    protected_path.write_text(protected_content, encoding="utf-8")
    output = tmp_path / "result.json"
    try:
        output.symlink_to(protected_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a file symbolic link: {error}")
    serialized_result = runner.serialize_result({"schema_version": SCHEMA_VERSION})

    written_path = runner.write_serialized_result(serialized_result, output)

    assert written_path == output.resolve()
    assert not output.is_symlink()
    assert protected_path.read_text(encoding="utf-8") == protected_content
    assert output.read_text(encoding="utf-8") == serialized_result


def test_explicit_output_cleans_staging_file_when_publication_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "result.json"
    output.write_text("previous artifact\n", encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(runner.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        runner.write_serialized_result("replacement artifact\n", output)

    assert output.read_text(encoding="utf-8") == "previous artifact\n"
    assert list(tmp_path.iterdir()) == [output]


def test_explicit_output_cleans_staging_file_when_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "result.json"
    output.write_text("previous artifact\n", encoding="utf-8")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(runner.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="fsync failed"):
        runner.write_serialized_result("replacement artifact\n", output)

    assert output.read_text(encoding="utf-8") == "previous artifact\n"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.gauntlet_full
@pytest.mark.slow
def test_full_profile_is_explicit_and_representative() -> None:
    result = run_gauntlet("full", seed=20_260_729, samples=DEFAULT_SAMPLES)

    assert result["candidate_counts"] == {"orphan_files": 3}
    assert result["endpoint_counters"]["torrents_files"] == 6_739
    assert result["mutation_counters"]["filesystem"] == 0
    assert result["mutation_counters"]["qbittorrent"] == 0
    assert result["workload"]["filesystem_files"] == 94_000


def test_profiles_mapping_exposes_only_locked_cli_profiles() -> None:
    assert set(PROFILES) == {"quick", "full"}


def test_materialized_manifest_detects_filesystem_and_api_metadata_changes(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path / "fixture", TINY_PROFILE, seed=101)
    assert materialized_fixture_digest(fixture) == fixture.expected_manifest_digest

    fixture.owned_files[0].write_bytes(b"altered")
    assert materialized_fixture_digest(fixture) != fixture.expected_manifest_digest

    fixture = build_fixture(tmp_path / "fixture-api", TINY_PROFILE, seed=102)
    fixture.client.set_torrent_files(fixture.initial_torrents[0].hash, [])
    assert materialized_fixture_digest(fixture) != fixture.expected_manifest_digest
