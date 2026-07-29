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
    GauntletProfile,
    build_blueprint,
    build_fixture,
    expected_endpoint_budgets,
    expected_endpoint_counters,
    materialized_fixture_digest,
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
TINY_PROFILE = GauntletProfile(
    name="tiny",
    torrent_count=6,
    file_count=23,
    orphan_count=1,
    exact_metadata_torrent_count=3,
    shard_count=4,
    tier="test",
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


def test_comparison_rejects_unknown_identity_and_reports_pending_baseline() -> None:
    quality_bar = load_quality_bar(QUALITY_BAR_PATH)
    quick_profile = quality_bar.profiles["quick"]
    pending_quick_profile = replace(
        quick_profile,
        baseline=BaselineMeasurement(
            status="pending_clean_evaluator_commit",
            median_runtime_seconds=None,
            peak_memory_bytes=None,
            environment=None,
        ),
    )
    pending_quality_bar = replace(
        quality_bar,
        profiles={**quality_bar.profiles, "quick": pending_quick_profile},
    )
    result = _valid_quick_result()
    result["commit"] = "unknown"
    result["candidate_state"] = {"clean": None, "diff_sha256": "unknown"}
    report = compare_result(result, pending_quality_bar)

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
        pending_quality_bar,
    )
    assert mismatched_report["gates"]["measurement_policy"]["status"] == "non_comparable"

    missing_policy_result = copy.deepcopy(result)
    missing_policy_result["measurement_policy"].pop("sequence")
    assert (
        compare_result(
            missing_policy_result,
            pending_quality_bar,
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
            pending_quality_bar,
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
