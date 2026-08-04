"""Tests for the repository-local deterministic gauntlet evaluator."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import math
import os
import py_compile
import stat
import subprocess
import sys
import tempfile
import tracemalloc
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest

from benchmarks.gauntlet import __main__ as gauntlet_cli
from benchmarks.gauntlet import import_bootstrap
from benchmarks.gauntlet import launcher
from benchmarks.gauntlet import paired
from benchmarks.gauntlet import runner
from benchmarks.gauntlet.baseline import (
    BaselineMeasurement,
    QualityBar,
    QualityBarError,
    compare_result,
    load_quality_bar,
    load_quality_bar_bytes,
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
from benchmarks.gauntlet.identity import (
    RepositoryIdentity,
    RepositoryIdentityError,
    capture_repository_identity,
)
from benchmarks.gauntlet.paired import (
    PAIRED_ORDER,
    PairedGauntletError,
    compare_paired_results,
    run_paired_gauntlet,
)
from benchmarks.gauntlet.runner import (
    DEFAULT_SAMPLES,
    EVALUATOR_VERSION,
    GauntletSafetyError,
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
requires_bound_publication = pytest.mark.skipif(
    not runner._supports_bound_publication(),
    reason="safe paired publication requires descriptor-relative filesystem operations",
)


def _near_name_max_ascii_basename(directory: Path) -> str:
    """Probe a long legal ASCII basename for the test filesystem."""
    suffix = ".json"
    maximum_length = 255
    pathconf = getattr(os, "pathconf", None)
    if pathconf is not None:
        try:
            reported_maximum = pathconf(directory, "PC_NAME_MAX")
        except (OSError, ValueError):
            pass
        else:
            if reported_maximum > 0:
                maximum_length = min(reported_maximum, 4096)

    last_error: OSError | None = None
    for length in range(maximum_length, 127, -1):
        basename = ("r" * (length - len(suffix))) + suffix
        probe = directory / basename
        try:
            descriptor = os.open(
                probe,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except OSError as error:
            last_error = error
            continue
        try:
            os.close(descriptor)
        finally:
            probe.unlink(missing_ok=True)
        return basename

    pytest.fail(f"could not establish a long legal ASCII basename: {last_error}")


@pytest.fixture
def isolated_parent_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    cache_root = tmp_path / "parent-pycache"
    cache_root.mkdir()
    monkeypatch.setattr(gauntlet_cli, "_require_isolated_coordinator", lambda: None)
    monkeypatch.setattr(
        gauntlet_cli,
        "_repository_protected_roots",
        lambda roots: tuple(path.expanduser().resolve() for path in roots),
    )
    monkeypatch.setattr(
        gauntlet_cli,
        "_revalidate_repository_protected_roots",
        lambda _roots, expected: expected,
    )
    monkeypatch.setenv(gauntlet_cli.ISOLATED_PARENT_CACHE_ENV, str(cache_root))
    monkeypatch.setattr(gauntlet_cli.sys, "pycache_prefix", str(cache_root))
    return cache_root


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


def _initialize_gauntlet_test_repository(repository_root: Path) -> None:
    """Create a minimal committed repository with both protected package trees."""
    (repository_root / "benchmarks" / "gauntlet").mkdir(parents=True)
    (repository_root / "qbitunregistered").mkdir()
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (repository_root / "qbitunregistered" / "__init__.py").write_text("", encoding="utf-8")

    _commit_gauntlet_test_repository(repository_root)


def _initialize_paired_test_repository(repository_root: Path) -> None:
    """Create a committed repository containing every paired evaluator input."""
    _initialize_gauntlet_test_repository(repository_root)
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "quality-bar.toml").write_bytes(QUALITY_BAR_PATH.read_bytes())
    (repository_root / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (repository_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "benchmarks/gauntlet", "pyproject.toml", "uv.lock"],
        cwd=repository_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=gauntlet@example.invalid",
            "commit",
            "-qm",
            "paired evaluator inputs",
        ],
        cwd=repository_root,
        check=True,
    )


def _commit_gauntlet_test_repository(repository_root: Path) -> None:
    """Initialize Git and commit the protected Python source trees."""
    subprocess.run(["git", "init", "-q", str(repository_root)], check=True)
    subprocess.run(
        ["git", "add", *import_bootstrap.PROTECTED_PACKAGE_NAMES],
        cwd=repository_root,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=gauntlet@example.invalid",
            "commit",
            "-qm",
            "test fixture",
        ],
        cwd=repository_root,
        check=True,
    )


def _initialize_external_git_worktrees(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create two temporary worktrees whose shared Git directory is external."""
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    common_directory = tmp_path / "common.git"
    subprocess.run(
        [
            "git",
            "init",
            "-q",
            "--separate-git-dir",
            str(common_directory),
            str(control_root),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=gauntlet@example.invalid",
            "commit",
            "--allow-empty",
            "-qm",
            "external metadata fixture",
        ],
        cwd=control_root,
        check=True,
    )
    subprocess.run(["git", "branch", "candidate"], cwd=control_root, check=True)
    subprocess.run(
        ["git", "worktree", "add", "-q", str(candidate_root), "candidate"],
        cwd=control_root,
        check=True,
    )
    return control_root, candidate_root, common_directory.resolve()


def _write_import_bootstrap_fixture(
    repository_root: Path,
    main_source: str,
) -> None:
    """Create protected package sources for an isolated bootstrap subprocess."""
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    qbitunregistered_root = repository_root / "qbitunregistered"
    gauntlet_root.mkdir(parents=True)
    qbitunregistered_root.mkdir()
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (qbitunregistered_root / "__init__.py").write_text("", encoding="utf-8")
    assert import_bootstrap.__file__ is not None
    (gauntlet_root / "import_bootstrap.py").write_bytes(Path(import_bootstrap.__file__).read_bytes())
    (gauntlet_root / "__main__.py").write_text(main_source, encoding="utf-8")


def _run_import_bootstrap_fixture(
    repository_root: Path,
    dependency_root: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one tracked bootstrap fixture without inherited Python injection."""
    dependency_paths = (str(dependency_root.resolve()),)
    dependency_digest = import_bootstrap.dependency_environment_digest(dependency_paths)
    return subprocess.run(
        [
            sys.executable,
            "-s",
            "-S",
            "-P",
            str(repository_root / "benchmarks" / "gauntlet" / "import_bootstrap.py"),
            str(repository_root),
            json.dumps(dependency_paths),
            import_bootstrap.DEPENDENCY_DIGEST_ARGUMENT,
            dependency_digest,
        ],
        cwd=repository_root.parent,
        env={
            **{key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _set_test_index_flag(
    repository_root: Path,
    relative_path: str,
    option: str,
    expected_tag: str,
) -> None:
    """Set and verify one real Git index-hiding flag for a test source."""
    try:
        subprocess.run(
            ["git", "update-index", option, "--", relative_path],
            cwd=repository_root,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as error:
        diagnostic = (error.stderr or b"").lower()
        if b"not supported" in diagnostic or b"unknown option" in diagnostic:
            pytest.skip(f"Git does not support {option}")
        raise
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "-v", "-z", "--", relative_path],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    assert listed.stdout == f"{expected_tag} {relative_path}\0".encode()


def _staged_source_record(
    relative_path: str,
    *,
    tag: str = "H",
    mode: str = "100644",
    oid: str = "a" * 40,
    stage: str = "0",
) -> bytes:
    """Build one exact `git ls-files -v --stage -z` source record."""
    return f"{tag} {mode} {oid} {stage}\t{relative_path}".encode()


def _head_source_record(
    relative_path: str,
    *,
    mode: str = "100644",
    oid: str = "a" * 40,
) -> bytes:
    """Build one exact ``git ls-tree -r -z`` source record."""
    return f"{mode} blob {oid}\t{relative_path}".encode()


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


def test_quality_bar_bytes_loader_matches_path_loader_and_rejects_invalid_utf8() -> None:
    source = QUALITY_BAR_PATH.read_bytes()

    assert load_quality_bar_bytes(source) == load_quality_bar(QUALITY_BAR_PATH)
    with pytest.raises(QualityBarError, match="could not load"):
        load_quality_bar_bytes(b"\xff")


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


def _verified_quality_bar_fixture(
    revision: str = "e" * 40,
    source_bytes: bytes | None = None,
) -> paired._VerifiedQualityBarSource:
    """Build one commit-pinned canonical quality-bar record for mocked runs."""
    resolved_source = QUALITY_BAR_PATH.read_bytes() if source_bytes is None else source_bytes
    return paired._VerifiedQualityBarSource(
        revision=revision,
        mode="100644",
        oid=hashlib.sha256(resolved_source).hexdigest(),
        source_bytes=resolved_source,
    )


def _installed_dependency_index(
    import_paths: list[Path],
    imported_path: Path,
) -> int:
    matching_paths: list[tuple[int, int]] = []
    for index, import_path in enumerate(import_paths):
        resolved_path = import_path.resolve()
        if imported_path.is_relative_to(resolved_path) and paired.SITE_DIRECTORY_NAMES.intersection(
            part.casefold() for part in resolved_path.parts
        ):
            matching_paths.append((len(resolved_path.parts), index))
    assert matching_paths
    greatest_depth = max(depth for depth, _index in matching_paths)
    most_specific_indexes = [index for depth, index in matching_paths if depth == greatest_depth]
    assert len(most_specific_indexes) == 1
    return most_specific_indexes[0]


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
    dependency_root = tmp_path / "environment" / "site-packages"
    dependency_root.mkdir(parents=True)
    (dependency_root / "dependency.py").write_text("VALUE = 1\n", encoding="utf-8")
    dependency_paths = (str(dependency_root),)
    dependency_environment_identity = import_bootstrap.dependency_environment_digest(dependency_paths)

    monkeypatch.setattr(
        "benchmarks.gauntlet.paired.capture_repository_identity",
        lambda root: identities[root],
    )
    monkeypatch.setattr("benchmarks.gauntlet.paired._evaluator_digest", lambda _root: "d" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._named_files_digest", lambda *_args: "f" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._dependency_import_paths", lambda: dependency_paths)
    monkeypatch.setattr("benchmarks.gauntlet.paired._reject_ignored_python_sources", lambda _root: None)
    monkeypatch.setattr("benchmarks.gauntlet.paired._reject_noncanonical_index_inputs", lambda _root: None)
    monkeypatch.setattr(
        paired,
        "verified_import_bootstrap_source",
        lambda _root, _commit: b"# immutable bootstrap\n",
    )
    quality_bar_source = _verified_quality_bar_fixture()
    quality_bar_verifications: list[tuple[Path, str]] = []

    def verify_quality_bar(root: Path, commit: str) -> paired._VerifiedQualityBarSource:
        quality_bar_verifications.append((root, commit))
        return quality_bar_source

    monkeypatch.setattr(
        paired,
        "_verified_canonical_quality_bar",
        verify_quality_bar,
    )

    def fake_run_child(root: Path, **kwargs):
        role = "control" if root == control_root else "candidate"
        assert kwargs["dependency_paths"] == dependency_paths
        assert kwargs["dependency_environment_digest"] == dependency_environment_identity
        assert kwargs["bootstrap_source"] == b"# immutable bootstrap\n"
        assert kwargs["expected_commit"] == identities[root].commit
        calls.append(role)
        return copy.deepcopy(expected_runs[len(calls) - 1]["result"])

    monkeypatch.setattr("benchmarks.gauntlet.paired._run_child", fake_run_child)
    compare_paired_results_original = paired.compare_paired_results

    def compare_after_revalidation(
        runs: list[paired.PairedRun],
        quality_bar: QualityBar,
    ) -> paired.PairingComparison:
        assert len(quality_bar_verifications) == 2
        return compare_paired_results_original(runs, quality_bar)

    monkeypatch.setattr(paired, "compare_paired_results", compare_after_revalidation)
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
    assert result["dependency_digest"] == paired._bound_dependency_digest(
        "f" * 64,
        dependency_environment_identity,
    )
    assert len(result["quality_bar_digest"]) == 64
    assert result["quality_bar_digest"] == hashlib.sha256(quality_bar_source.source_bytes).hexdigest()
    assert quality_bar_verifications == [
        (REPOSITORY_ROOT, identities[REPOSITORY_ROOT].commit),
        (REPOSITORY_ROOT, identities[REPOSITORY_ROOT].commit),
    ]
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


def test_paired_runner_rejects_noncanonical_samples_before_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unexpected_setup = Mock(side_effect=AssertionError("paired setup unexpectedly started"))
    monkeypatch.setattr(paired, "_require_clean_identity", unexpected_setup)
    monkeypatch.setattr(paired, "_load_canonical_quality_bar", unexpected_setup)
    monkeypatch.setattr(paired, "_dependency_import_paths", unexpected_setup)
    monkeypatch.setattr(paired.tempfile, "TemporaryDirectory", unexpected_setup)
    monkeypatch.setattr(paired, "_run_child", unexpected_setup)

    with pytest.raises(
        PairedGauntletError,
        match=rf"^comparable paired gauntlet runs require exactly {DEFAULT_SAMPLES} timed samples$",
    ):
        run_paired_gauntlet(
            tmp_path / "control",
            tmp_path / "candidate",
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES + 1,
        )

    unexpected_setup.assert_not_called()


def test_paired_runner_rejects_noncanonical_seed_before_dependency_or_child_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    unexpected_dependency_work = Mock(side_effect=AssertionError("dependency work unexpectedly started"))
    unexpected_child = Mock(side_effect=AssertionError("paired child unexpectedly started"))
    monkeypatch.setattr(paired, "_require_clean_identity", lambda _root: identity)
    monkeypatch.setattr(paired, "_reject_unsafe_package_entries_in_roots", lambda _roots: None)
    monkeypatch.setattr(
        paired,
        "_verified_canonical_quality_bar",
        lambda _root, _commit: _verified_quality_bar_fixture(identity.commit),
    )
    monkeypatch.setattr(paired, "_named_files_digest", unexpected_dependency_work)
    monkeypatch.setattr(paired, "_dependency_import_paths", unexpected_dependency_work)
    monkeypatch.setattr(paired, "_current_dependency_environment_digest", unexpected_dependency_work)
    monkeypatch.setattr(paired, "_run_child", unexpected_child)
    canonical_seed = load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed

    with pytest.raises(
        PairedGauntletError,
        match=r"^paired seed must match the canonical profile seed$",
    ):
        run_paired_gauntlet(
            tmp_path / "control",
            tmp_path / "candidate",
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=canonical_seed + 1,
            samples=DEFAULT_SAMPLES,
        )

    unexpected_dependency_work.assert_not_called()
    unexpected_child.assert_not_called()


@pytest.mark.parametrize(
    "exclude_source",
    ("gitignore", "repository_exclude", "configured_global_exclude"),
)
def test_paired_runner_rejects_ignored_python_sources_before_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exclude_source: str,
) -> None:
    orchestrator_root = tmp_path / "orchestrator"
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    for repository_root in (orchestrator_root, control_root, candidate_root):
        _initialize_paired_test_repository(repository_root)

    ignored_relative_path = Path("benchmarks") / "gauntlet" / "private-credential-source.py"
    ignore_pattern = f"/{ignored_relative_path.as_posix()}\n"
    if exclude_source == "gitignore":
        ignore_file = candidate_root / ".gitignore"
        ignore_file.write_text(ignore_pattern, encoding="utf-8")
        subprocess.run(["git", "add", ".gitignore"], cwd=candidate_root, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Gauntlet Test",
                "-c",
                "user.email=gauntlet@example.invalid",
                "commit",
                "-qm",
                "ignore test source",
            ],
            cwd=candidate_root,
            check=True,
        )
    elif exclude_source == "repository_exclude":
        (candidate_root / ".git" / "info" / "exclude").write_text(
            ignore_pattern,
            encoding="utf-8",
        )
    else:
        global_excludes = tmp_path / "configured-global-excludes"
        global_excludes.write_text(ignore_pattern, encoding="utf-8")
        subprocess.run(
            ["git", "config", "core.excludesFile", str(global_excludes)],
            cwd=candidate_root,
            check=True,
        )
    ignored_path = candidate_root / ignored_relative_path
    ignored_path.write_text("PASSWORD = 'must-not-leak'\n", encoding="utf-8")
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=candidate_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""

    child = Mock(side_effect=AssertionError("paired child unexpectedly started"))
    monkeypatch.setattr(paired, "_run_child", child)

    with pytest.raises(PairedGauntletError) as error_info:
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=orchestrator_root,
            profile="quick",
            seed=20_260_729,
            samples=DEFAULT_SAMPLES,
        )

    assert str(error_info.value) == paired.IGNORED_PYTHON_SOURCE_ERROR
    assert ignored_path.name not in str(error_info.value)
    assert str(tmp_path) not in str(error_info.value)
    child.assert_not_called()


@pytest.mark.parametrize(
    ("index_option", "expected_tag"),
    (
        ("--skip-worktree", "S"),
        ("--assume-unchanged", "h"),
    ),
)
@pytest.mark.parametrize(
    "relative_path",
    (
        "benchmarks/gauntlet/quality-bar.toml",
        "pyproject.toml",
        "uv.lock",
    ),
)
def test_paired_runner_rejects_identical_index_hidden_evaluator_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    index_option: str,
    expected_tag: str,
    relative_path: str,
) -> None:
    orchestrator_root = tmp_path / "orchestrator"
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    for repository_root in (orchestrator_root, control_root, candidate_root):
        _initialize_paired_test_repository(repository_root)
        _set_test_index_flag(
            repository_root,
            relative_path,
            index_option,
            expected_tag,
        )
        hidden_input = repository_root / relative_path
        hidden_input.write_bytes(hidden_input.read_bytes() + b"\n# identical hidden edit\n")
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""
        assert capture_repository_identity(repository_root).clean is True

    child = Mock(side_effect=AssertionError("paired child unexpectedly started"))
    monkeypatch.setattr(paired, "_run_child", child)

    with pytest.raises(PairedGauntletError) as error_info:
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=orchestrator_root,
            profile="quick",
            seed=20_260_729,
            samples=DEFAULT_SAMPLES,
        )

    assert str(error_info.value) == paired.NONCANONICAL_INDEX_INPUT_ERROR
    assert relative_path not in str(error_info.value)
    assert str(tmp_path) not in str(error_info.value)
    child.assert_not_called()


@requires_descriptor_no_follow
@pytest.mark.parametrize("child_fails", [False, True])
def test_paired_runner_rechecks_importable_extensions_after_each_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_fails: bool,
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
    monkeypatch.setattr("benchmarks.gauntlet.paired._dependency_import_paths", lambda: ("dependencies",))
    monkeypatch.setattr("benchmarks.gauntlet.paired._reject_ignored_python_sources", lambda _root: None)
    monkeypatch.setattr("benchmarks.gauntlet.paired._reject_noncanonical_index_inputs", lambda _root: None)
    monkeypatch.setattr(
        paired,
        "verified_import_bootstrap_source",
        lambda _root, _commit: b"# immutable bootstrap\n",
    )
    monkeypatch.setattr(
        "benchmarks.gauntlet.paired._current_dependency_environment_digest",
        lambda _paths: "a" * 64,
    )
    monkeypatch.setattr(
        paired,
        "_verified_canonical_quality_bar",
        lambda _root, _commit: _verified_quality_bar_fixture(),
    )
    sanitize_result = Mock(side_effect=AssertionError("unsafe child evidence was accepted"))
    monkeypatch.setattr("benchmarks.gauntlet.paired.sanitize_child_result", sanitize_result)

    def fake_run_child(root: Path, **_kwargs) -> dict[str, object]:
        calls.append(root)
        extension_path = root / "benchmarks" / "gauntlet" / f"runner{paired.EXTENSION_SUFFIXES[0]}"
        extension_path.parent.mkdir(parents=True)
        extension_path.write_bytes(b"late native extension")
        if child_fails:
            raise PairedGauntletError("simulated child failure")
        return copy.deepcopy(expected_runs[0]["result"])

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

    assert calls == [control_root]
    sanitize_result.assert_not_called()


def test_paired_runner_rejects_dependency_environment_tampering_between_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    dependency_root = tmp_path / "environment" / "site-packages"
    dependency_file = dependency_root / "dependency.py"
    control_root.mkdir()
    candidate_root.mkdir()
    dependency_root.mkdir(parents=True)
    dependency_file.write_text("VALUE = 1\n", encoding="utf-8")
    expected_runs = _paired_runs()
    calls: list[Path] = []
    identities = {
        REPOSITORY_ROOT: RepositoryIdentity("e" * 40, True, "e" * 64),
        control_root: RepositoryIdentity("a" * 40, True, "a" * 64),
        candidate_root: RepositoryIdentity("c" * 40, True, "c" * 64),
    }

    monkeypatch.setattr(paired, "capture_repository_identity", lambda root: identities[root])
    monkeypatch.setattr(paired, "_evaluator_digest", lambda _root: "d" * 64)
    monkeypatch.setattr(paired, "_named_files_digest", lambda *_args: "f" * 64)
    monkeypatch.setattr(paired, "_dependency_import_paths", lambda: (str(dependency_root),))
    monkeypatch.setattr(paired, "_reject_ignored_python_sources", lambda _root: None)
    monkeypatch.setattr(paired, "_reject_noncanonical_index_inputs", lambda _root: None)
    monkeypatch.setattr(
        paired,
        "verified_import_bootstrap_source",
        lambda _root, _commit: b"# immutable bootstrap\n",
    )
    real_load_quality_bar = paired._load_canonical_quality_bar
    quality_bar_source = _verified_quality_bar_fixture()
    loaded_quality_bars: list[bytes] = []
    verified_quality_bars: list[tuple[Path, str]] = []

    def load_canonical_quality_bar(source: bytes) -> QualityBar:
        loaded_quality_bars.append(source)
        return real_load_quality_bar(source)

    def verified_canonical_quality_bar(root: Path, commit: str) -> paired._VerifiedQualityBarSource:
        verified_quality_bars.append((root, commit))
        return quality_bar_source

    monkeypatch.setattr(paired, "_load_canonical_quality_bar", load_canonical_quality_bar)
    monkeypatch.setattr(paired, "_verified_canonical_quality_bar", verified_canonical_quality_bar)
    monkeypatch.delattr(paired.os, "O_NOFOLLOW", raising=False)

    def tamper_after_first_child(root: Path, **_kwargs) -> dict[str, object]:
        calls.append(root)
        dependency_file.write_text("VALUE = 2\n", encoding="utf-8")
        return copy.deepcopy(expected_runs[0]["result"])

    monkeypatch.setattr(paired, "_run_child", tamper_after_first_child)

    with pytest.raises(
        PairedGauntletError,
        match=r"^paired dependency environment changed during evaluation$",
    ):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=REPOSITORY_ROOT,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    assert calls == [control_root]
    assert loaded_quality_bars == [quality_bar_source.source_bytes]
    assert verified_quality_bars == [(REPOSITORY_ROOT, identities[REPOSITORY_ROOT].commit)]


@requires_descriptor_no_follow
def test_paired_runner_rejects_same_or_different_evaluator_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr("benchmarks.gauntlet.paired._require_clean_identity", lambda _root: identity)
    monkeypatch.setattr(
        paired,
        "_verified_canonical_quality_bar",
        lambda _root, _commit: _verified_quality_bar_fixture(identity.commit),
    )
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
    monkeypatch.setattr(
        paired,
        "_verified_canonical_quality_bar",
        lambda _root, _commit: _verified_quality_bar_fixture(identity.commit),
    )
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


def test_paired_runner_rejects_package_directory_symlink_before_child_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    roots = [tmp_path / name for name in ("orchestrator", "control", "candidate")]
    for root in roots:
        (root / "benchmarks" / "gauntlet").mkdir(parents=True)
        (root / "qbitunregistered").mkdir()
    orchestrator_root, control_root, candidate_root = roots
    redirect_target = tmp_path / "redirect-target"
    redirect_target.mkdir()
    redirect = candidate_root / "benchmarks" / "gauntlet" / "redirected"
    try:
        redirect.symlink_to(redirect_target, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a directory symbolic link: {error}")
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr(paired, "_require_clean_identity", lambda _root: identity)
    child_calls: list[Path] = []
    monkeypatch.setattr(
        paired,
        "_run_child",
        lambda repository_root, **_kwargs: child_calls.append(repository_root),
    )

    with pytest.raises(PairedGauntletError, match="redirecting entry"):
        run_paired_gauntlet(
            control_root,
            candidate_root,
            orchestrator_root=orchestrator_root,
            profile="quick",
            seed=load_quality_bar(QUALITY_BAR_PATH).profiles["quick"].seed,
            samples=DEFAULT_SAMPLES,
        )

    assert child_calls == []


def test_redirect_detection_includes_windows_reparse_points() -> None:
    reparse_stat = cast(
        os.stat_result,
        SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        ),
    )

    assert paired._entry_is_redirecting(reparse_stat)
    assert import_bootstrap._entry_is_redirecting(reparse_stat)


def test_package_tree_rejects_windows_reparse_point_before_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package_root = tmp_path / "benchmarks"
    package_root.mkdir()
    real_lstat = os.lstat
    reparse_stat = cast(
        os.stat_result,
        SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        ),
    )

    def report_reparse_point(path: os.PathLike[str] | str) -> os.stat_result:
        if Path(path) == package_root:
            return reparse_stat
        return real_lstat(path)

    monkeypatch.setattr(paired.os, "lstat", report_reparse_point)

    with pytest.raises(PairedGauntletError, match="redirecting entry"):
        paired._reject_package_tree_redirects(tmp_path)


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
        paired,
        "_verified_canonical_quality_bar",
        lambda _root, _commit: _verified_quality_bar_fixture(identity.commit),
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


def test_dependency_environment_digest_tracks_paths_and_contents_not_mtime(
    tmp_path: Path,
) -> None:
    dependency_root = tmp_path / "environment" / "site-packages"
    package_root = dependency_root / "package"
    dependency_file = package_root / "module.py"
    package_root.mkdir(parents=True)
    dependency_file.write_text("VALUE = 1\n", encoding="utf-8")
    dependency_paths = (str(dependency_root),)
    first = import_bootstrap.dependency_environment_digest(dependency_paths)
    original_stat = dependency_file.stat()

    os.utime(
        dependency_file,
        ns=(
            original_stat.st_atime_ns,
            original_stat.st_mtime_ns + 1_000_000_000,
        ),
    )
    assert import_bootstrap.dependency_environment_digest(dependency_paths) == first

    dependency_file.write_text("VALUE = 2\n", encoding="utf-8")
    os.utime(
        dependency_file,
        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
    )
    content_changed = import_bootstrap.dependency_environment_digest(dependency_paths)
    assert content_changed != first

    (package_root / "renamed.py").write_text("VALUE = 2\n", encoding="utf-8")
    assert import_bootstrap.dependency_environment_digest(dependency_paths) != content_changed


def test_dependency_environment_digest_rejects_redirecting_entries(
    tmp_path: Path,
) -> None:
    dependency_root = tmp_path / "environment" / "site-packages"
    package_root = dependency_root / "package"
    target = tmp_path / "target.py"
    package_root.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    redirect = package_root / "module.py"
    try:
        redirect.symlink_to(target)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a symbolic link: {error}")

    with pytest.raises(
        import_bootstrap.DependencyEnvironmentError,
        match="redirecting entry",
    ):
        import_bootstrap.dependency_environment_digest((str(dependency_root),))


def test_dependency_environment_digest_rejects_windows_reparse_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_root = tmp_path / "environment" / "site-packages"
    dependency_root.mkdir(parents=True)
    real_lstat = os.lstat
    reparse_stat = cast(
        os.stat_result,
        SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
        ),
    )

    def report_reparse_point(path: os.PathLike[str] | str) -> os.stat_result:
        if Path(path) == dependency_root:
            return reparse_stat
        return real_lstat(path)

    monkeypatch.setattr(import_bootstrap.os, "lstat", report_reparse_point)

    with pytest.raises(
        import_bootstrap.DependencyEnvironmentError,
        match="redirecting entry",
    ):
        import_bootstrap.dependency_environment_digest((str(dependency_root),))


def test_import_bootstrap_maps_valid_tracked_packages_and_modules(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    _write_import_bootstrap_fixture(repository_root, "")
    operations_root = repository_root / "qbitunregistered" / "operations"
    operations_root.mkdir()
    (operations_root / "__init__.py").write_text("", encoding="utf-8")
    (operations_root / "nested.py").write_text("VALUE = 1\n", encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)

    sources = import_bootstrap._tracked_protected_sources(repository_root)
    finder = import_bootstrap._WorktreePackageFinder(repository_root, sources)

    expected = {
        "benchmarks": True,
        "benchmarks.gauntlet": True,
        "benchmarks.gauntlet.__main__": False,
        "qbitunregistered": True,
        "qbitunregistered.operations": True,
        "qbitunregistered.operations.nested": False,
    }
    for fullname, is_package in expected.items():
        spec = finder.find_spec(fullname, [] if "." in fullname else None)
        assert spec is not None
        assert spec.origin == str(sources[fullname].path)
        assert (spec.submodule_search_locations is not None) is is_package
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        assert module.__file__ == str(sources[fullname].path)
        if is_package:
            assert module.__path__ == [str(sources[fullname].path.parent)]
        else:
            assert not hasattr(module, "__path__")
    assert sources["qbitunregistered.operations.nested"].source_bytes == b"VALUE = 1\n"
    with pytest.raises(
        import_bootstrap.ProtectedPackageTreeError,
        match=f"^{import_bootstrap.PROTECTED_IMPORT_ERROR}$",
    ):
        finder.find_spec("Qbitunregistered.operations.nested", [])


def test_verified_child_bootstrap_executes_original_commit_bytes_from_stdin(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    dependency_root = tmp_path / "environment" / "site-packages"
    dependency_root.mkdir(parents=True)
    _write_import_bootstrap_fixture(repository_root, 'print("trusted child")\n')
    _commit_gauntlet_test_repository(repository_root)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bootstrap_path = repository_root / "benchmarks" / "gauntlet" / "import_bootstrap.py"
    trusted_source = import_bootstrap.verified_import_bootstrap_source(
        repository_root,
        expected_commit,
    )
    bootstrap_path.write_text('raise SystemExit("mutable worktree bootstrap ran")\n', encoding="utf-8")
    dependency_paths = (str(dependency_root.resolve()),)
    dependency_digest = import_bootstrap.dependency_environment_digest(dependency_paths)

    completed = subprocess.run(
        [
            sys.executable,
            "-s",
            "-S",
            "-P",
            "-",
            str(repository_root),
            json.dumps(dependency_paths),
            import_bootstrap.EXPECTED_REPOSITORY_COMMIT_ARGUMENT,
            expected_commit,
            import_bootstrap.DEPENDENCY_DIGEST_ARGUMENT,
            dependency_digest,
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        input=trusted_source,
        check=False,
        capture_output=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.splitlines() == [b"trusted child"]
    assert completed.stderr == b""


def test_verified_child_bootstrap_rejects_head_and_index_drift_from_expected_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    _write_import_bootstrap_fixture(repository_root, "")
    _commit_gauntlet_test_repository(repository_root)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    bootstrap_path = repository_root / "benchmarks" / "gauntlet" / "import_bootstrap.py"
    bootstrap_path.write_bytes(bootstrap_path.read_bytes() + b"\n# changed revision\n")
    subprocess.run(["git", "add", str(bootstrap_path)], cwd=repository_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=gauntlet@example.invalid",
            "commit",
            "-qm",
            "changed revision",
        ],
        cwd=repository_root,
        check=True,
    )
    blob_reads: list[str] = []
    real_read_git_blob = import_bootstrap._read_git_blob

    def record_blob_read(root: Path, oid: str) -> bytes:
        blob_reads.append(oid)
        return real_read_git_blob(root, oid)

    monkeypatch.setattr(import_bootstrap, "_read_git_blob", record_blob_read)

    with pytest.raises(
        import_bootstrap.ProtectedPackageTreeError,
        match=f"^{import_bootstrap.PROTECTED_IMPORT_ERROR}$",
    ):
        import_bootstrap.verified_import_bootstrap_source(
            repository_root,
            expected_commit,
        )

    assert blob_reads == []


def test_import_bootstrap_accepts_canonical_nul_terminated_stage_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = b"\0".join(
        (
            _staged_source_record("benchmarks/__init__.py"),
            _staged_source_record(
                "qbitunregistered/__init__.py",
                mode="100755",
                oid="b" * 64,
            ),
            b"",
        )
    )

    def run_git(
        arguments: list[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if arguments[1] == "ls-files":
            stdout = records
        elif arguments[2] == "ls-tree":
            stdout = b"\0".join(
                (
                    _head_source_record("benchmarks/__init__.py"),
                    _head_source_record(
                        "qbitunregistered/__init__.py",
                        mode="100755",
                        oid="b" * 64,
                    ),
                    b"",
                )
            )
        else:
            assert arguments[1:4] == ["--no-replace-objects", "cat-file", "blob"]
            stdout = b"INDEX_SOURCE = True\n"
        return subprocess.CompletedProcess(arguments, 0, stdout=stdout)

    monkeypatch.setattr(import_bootstrap.subprocess, "run", run_git)
    monkeypatch.setattr(import_bootstrap, "_validate_protected_source", lambda *_args: None)

    sources = import_bootstrap._tracked_protected_sources(tmp_path)

    assert sources["benchmarks"].mode == "100644"
    assert sources["benchmarks"].oid == "a" * 40
    assert sources["qbitunregistered"].mode == "100755"
    assert sources["qbitunregistered"].oid == "b" * 64
    assert sources["qbitunregistered"].source_bytes == b"INDEX_SOURCE = True\n"


def test_verified_quality_bar_uses_one_commit_pinned_buffer_during_worktree_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    _initialize_paired_test_repository(repository_root)
    expected_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    quality_bar_path = repository_root / paired._QUALITY_BAR_RELATIVE_PATH
    canonical_bytes = quality_bar_path.read_bytes()
    weakened_bytes = canonical_bytes.replace(
        b"relative_range_max = 0.50",
        b"relative_range_max = 9.00",
        1,
    )
    assert weakened_bytes != canonical_bytes

    verified_source = paired._verified_canonical_quality_bar(
        repository_root,
        expected_commit,
    )
    replacement_oid = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            input=weakened_bytes,
        )
        .stdout.strip()
        .decode("ascii")
    )
    subprocess.run(
        ["git", "replace", verified_source.oid, replacement_oid],
        cwd=repository_root,
        check=True,
    )
    assert (
        subprocess.run(
            ["git", "cat-file", "blob", verified_source.oid],
            cwd=repository_root,
            check=True,
            capture_output=True,
        ).stdout
        == weakened_bytes
    )
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "redirected.git"))
    quality_bar_path.write_bytes(weakened_bytes)
    rebound_source = paired._verified_canonical_quality_bar(
        repository_root,
        expected_commit,
    )
    parsed = paired._load_canonical_quality_bar(rebound_source.source_bytes)
    quality_bar_path.write_bytes(canonical_bytes)

    assert rebound_source == verified_source
    assert parsed.profiles["quick"].relative_range_max == 0.50
    assert hashlib.sha256(rebound_source.source_bytes).hexdigest() == hashlib.sha256(canonical_bytes).hexdigest()

    quality_bar_path.write_bytes(weakened_bytes)
    monkeypatch.delenv("GIT_DIR")
    subprocess.run(
        ["git", "add", paired._QUALITY_BAR_RELATIVE_PATH],
        cwd=repository_root,
        check=True,
    )
    with pytest.raises(
        PairedGauntletError,
        match=f"^{paired._QUALITY_BAR_VERIFICATION_ERROR}$",
    ):
        paired._verified_canonical_quality_bar(
            repository_root,
            expected_commit,
        )


@pytest.mark.parametrize("staged_change", ("mode", "oid"))
def test_import_bootstrap_rejects_protected_index_source_differing_from_head_before_blob_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    staged_change: str,
) -> None:
    repository_root = tmp_path / "repository"
    payload_path = repository_root / "qbitunregistered" / "payload.py"
    _write_import_bootstrap_fixture(repository_root, "")
    payload_path.write_text("ORIGIN = 'head'\n", encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)
    if staged_change == "mode":
        subprocess.run(
            ["git", "update-index", "--chmod=+x", "--", "qbitunregistered/payload.py"],
            cwd=repository_root,
            check=True,
        )
    else:
        payload_path.write_text("ORIGIN = 'staged'\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "--", "qbitunregistered/payload.py"],
            cwd=repository_root,
            check=True,
        )
    blob_reads: list[str] = []

    def record_blob_read(_repository_root: Path, oid: str) -> bytes:
        blob_reads.append(oid)
        return b""

    monkeypatch.setattr(import_bootstrap, "_read_git_blob", record_blob_read)

    with pytest.raises(
        import_bootstrap.ProtectedPackageTreeError,
        match=f"^{import_bootstrap.PROTECTED_IMPORT_ERROR}$",
    ):
        import_bootstrap._tracked_protected_sources(repository_root)

    assert blob_reads == []


def test_protected_loader_ignores_git_replacement_refs(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    payload_path = repository_root / "qbitunregistered" / "payload.py"
    payload_relative = "qbitunregistered/payload.py"
    canonical_side_effect = tmp_path / "canonical-side-effect"
    malicious_side_effect = tmp_path / "malicious-side-effect"
    canonical_source = (
        "\n".join(
            (
                "from pathlib import Path",
                "ORIGIN = 'canonical-index-blob'",
                f"Path({str(canonical_side_effect)!r}).write_text('ran', encoding='utf-8')",
            )
        )
        + "\n"
    )
    malicious_source = (
        "\n".join(
            (
                "from pathlib import Path",
                "ORIGIN = 'malicious-replacement-blob'",
                f"Path({str(malicious_side_effect)!r}).write_text('ran', encoding='utf-8')",
            )
        )
        + "\n"
    )
    _write_import_bootstrap_fixture(repository_root, "")
    payload_path.write_text(canonical_source, encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)
    identity_before = capture_repository_identity(repository_root)
    assert identity_before.known
    assert identity_before.clean is True

    def assert_underlying_repository_is_clean() -> None:
        underlying_status = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
        )
        assert underlying_status.stdout == ""
        replacement_aware_identity = capture_repository_identity(repository_root)
        if replacement_aware_identity != identity_before:
            assert replacement_aware_identity.known
            assert replacement_aware_identity.commit == identity_before.commit
            assert replacement_aware_identity.clean is False

    original_oid = subprocess.run(
        ["git", "rev-parse", f"HEAD:{payload_relative}"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    replacement_oid = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        input=malicious_source,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "replace", original_oid, replacement_oid],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )

    listed = subprocess.run(
        ["git", "ls-files", "--cached", "-v", "--stage", "-z", "--", payload_relative],
        cwd=repository_root,
        check=True,
        capture_output=True,
    )
    assert listed.stdout == f"H 100644 {original_oid} 0\t{payload_relative}\0".encode()
    replaced_blob = subprocess.run(
        ["git", "cat-file", "blob", original_oid],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert replaced_blob.stdout == malicious_source
    assert_underlying_repository_is_clean()

    sources = import_bootstrap._tracked_protected_sources(repository_root)
    assert sources["qbitunregistered.payload"].oid == original_oid
    assert sources["qbitunregistered.payload"].source_bytes == canonical_source.encode()
    finder = import_bootstrap._WorktreePackageFinder(repository_root, sources)
    spec = finder.find_spec("qbitunregistered.payload", [])
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.ORIGIN == "canonical-index-blob"
    assert module.__file__ == str(payload_path)
    assert canonical_side_effect.read_text(encoding="utf-8") == "ran"
    assert not malicious_side_effect.exists()
    assert not (payload_path.parent / "__pycache__").exists()
    assert_underlying_repository_is_clean()


@pytest.mark.parametrize(
    "malformation",
    ("missing-terminator", "double-terminator", "interior-empty-record"),
)
def test_import_bootstrap_rejects_malformed_nul_framing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    malformation: str,
) -> None:
    first = _staged_source_record("benchmarks/__init__.py")
    second = _staged_source_record("qbitunregistered/__init__.py")
    canonical = first + b"\0" + second + b"\0"
    if malformation == "missing-terminator":
        records = canonical[:-1]
    elif malformation == "double-terminator":
        records = canonical + b"\0"
    else:
        records = first + b"\0\0" + second + b"\0"
    monkeypatch.setattr(
        import_bootstrap.subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            0,
            stdout=records,
        ),
    )
    monkeypatch.setattr(import_bootstrap, "_validate_protected_source", lambda *_args: None)

    with pytest.raises(
        import_bootstrap.ProtectedPackageTreeError,
        match=f"^{import_bootstrap.PROTECTED_IMPORT_ERROR}$",
    ):
        import_bootstrap._tracked_protected_sources(tmp_path)


@pytest.mark.parametrize(
    ("index_option", "expected_tag"),
    (
        ("--skip-worktree", "S"),
        ("--assume-unchanged", "h"),
    ),
)
def test_protected_loader_executes_captured_blob_after_post_validation_race(
    tmp_path: Path,
    index_option: str,
    expected_tag: str,
) -> None:
    repository_root = tmp_path / "repository"
    payload_path = repository_root / "qbitunregistered" / "payload.py"
    canonical_side_effect = tmp_path / "canonical-side-effect"
    malicious_side_effect = tmp_path / "malicious-side-effect"
    _write_import_bootstrap_fixture(repository_root, "")
    payload_path.write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "ORIGIN = 'canonical-index-blob'",
                f"Path({str(canonical_side_effect)!r}).write_text('ran', encoding='utf-8')",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _commit_gauntlet_test_repository(repository_root)
    sources = import_bootstrap._tracked_protected_sources(repository_root)
    finder = import_bootstrap._WorktreePackageFinder(repository_root, sources)
    spec = finder.find_spec("qbitunregistered.payload", [])
    assert spec is not None
    assert spec.loader is not None

    _set_test_index_flag(
        repository_root,
        "qbitunregistered/payload.py",
        index_option,
        expected_tag,
    )
    payload_path.write_text(
        "\n".join(
            (
                "from pathlib import Path",
                "ORIGIN = 'malicious-worktree-source'",
                f"Path({str(malicious_side_effect)!r}).write_text('ran', encoding='utf-8')",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.ORIGIN == "canonical-index-blob"
    assert module.__file__ == str(payload_path)
    assert canonical_side_effect.read_text(encoding="utf-8") == "ran"
    assert not malicious_side_effect.exists()
    assert not (payload_path.parent / "__pycache__").exists()
    with pytest.raises(
        import_bootstrap.ProtectedPackageTreeError,
        match=f"^{import_bootstrap.PROTECTED_IMPORT_ERROR}$",
    ):
        finder.validate_sources()


@pytest.mark.parametrize(
    ("index_option", "expected_tag", "clear_option"),
    (
        ("--skip-worktree", "S", "--no-skip-worktree"),
        ("--assume-unchanged", "h", "--no-assume-unchanged"),
    ),
)
@pytest.mark.parametrize("flag_stage", ("before", "during"))
def test_import_bootstrap_rejects_index_hidden_protected_sources(
    tmp_path: Path,
    index_option: str,
    expected_tag: str,
    clear_option: str,
    flag_stage: str,
) -> None:
    repository_root = tmp_path / "repository"
    dependency_root = tmp_path / "environment" / "site-packages"
    payload_path = repository_root / "qbitunregistered" / "payload.py"
    payload_relative = "qbitunregistered/payload.py"
    malicious_side_effect = tmp_path / "malicious-side-effect"
    accepted_result = tmp_path / "accepted-result"
    dependency_root.mkdir(parents=True)
    malicious_source = (
        "from pathlib import Path\n" f"Path({str(malicious_side_effect)!r}).write_text('ran', encoding='utf-8')\n"
    )
    if flag_stage == "during":
        main_source = "\n".join(
            (
                "from pathlib import Path",
                "import subprocess",
                f"repository_root = Path({str(repository_root)!r})",
                (
                    "subprocess.run("
                    f"['git', 'update-index', {index_option!r}, '--', {payload_relative!r}], "
                    "cwd=repository_root, check=True)"
                ),
                f"Path({str(payload_path)!r}).write_text({malicious_source!r}, encoding='utf-8')",
                "import qbitunregistered.payload",
                f"Path({str(accepted_result)!r}).write_text('accepted', encoding='utf-8')",
            )
        )
    else:
        main_source = "\n".join(
            (
                "import qbitunregistered.payload",
                "from pathlib import Path",
                f"Path({str(accepted_result)!r}).write_text('accepted', encoding='utf-8')",
            )
        )
    _write_import_bootstrap_fixture(repository_root, main_source + "\n")
    payload_path.write_text("ORIGIN = 'tracked'\n", encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)
    if flag_stage == "during":
        _set_test_index_flag(
            repository_root,
            payload_relative,
            index_option,
            expected_tag,
        )
        subprocess.run(
            ["git", "update-index", clear_option, "--", payload_relative],
            cwd=repository_root,
            check=True,
        )
    identity_before = capture_repository_identity(repository_root)
    if flag_stage == "before":
        _set_test_index_flag(
            repository_root,
            payload_relative,
            index_option,
            expected_tag,
        )
        payload_path.write_text(malicious_source, encoding="utf-8")
    assert identity_before.known
    assert identity_before.clean is True

    completed = _run_import_bootstrap_fixture(repository_root, dependency_root)

    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""
    assert capture_repository_identity(repository_root) == identity_before
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == import_bootstrap.PROTECTED_IMPORT_ERROR
    assert "Traceback" not in completed.stderr
    assert str(tmp_path) not in completed.stderr
    assert not malicious_side_effect.exists()
    assert not accepted_result.exists()


def test_import_bootstrap_uses_tracked_source_after_native_injection(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    dependency_root = tmp_path / "environment" / "site-packages"
    result_marker = tmp_path / "selected-source"
    native_path = repository_root / "qbitunregistered" / f"payload{paired.EXTENSION_SUFFIXES[0]}"
    dependency_root.mkdir(parents=True)
    _write_import_bootstrap_fixture(
        repository_root,
        "\n".join(
            (
                "from pathlib import Path",
                f"Path({str(native_path)!r}).write_bytes(b'untrusted native payload')",
                "from qbitunregistered import payload",
                f"Path({str(result_marker)!r}).write_text(payload.ORIGIN, encoding='utf-8')",
            )
        )
        + "\n",
    )
    (repository_root / "qbitunregistered" / "payload.py").write_text(
        "ORIGIN = 'tracked-source'\n",
        encoding="utf-8",
    )
    _commit_gauntlet_test_repository(repository_root)

    completed = _run_import_bootstrap_fixture(repository_root, dependency_root)

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert result_marker.read_text(encoding="utf-8") == "tracked-source"
    with pytest.raises(PairedGauntletError, match="importable native extension"):
        paired._reject_importable_extensions(repository_root)


@pytest.mark.parametrize(
    "injection_kind",
    ("native", "windows_pyd", "ignored_python"),
)
def test_import_bootstrap_rejects_untracked_protected_fullnames_without_side_effect(
    tmp_path: Path,
    injection_kind: str,
) -> None:
    repository_root = tmp_path / "repository"
    dependency_root = tmp_path / "environment" / "site-packages"
    side_effect = tmp_path / "untrusted-side-effect"
    dependency_root.mkdir(parents=True)
    _write_import_bootstrap_fixture(
        repository_root,
        "\n".join(
            (
                "import qbitunregistered.payload",
                "from pathlib import Path",
                f"Path({str(side_effect)!r}).write_text('ran', encoding='utf-8')",
            )
        )
        + "\n",
    )
    _commit_gauntlet_test_repository(repository_root)
    if injection_kind == "ignored_python":
        (repository_root / ".git" / "info" / "exclude").write_text(
            "/qbitunregistered/payload.py\n",
            encoding="utf-8",
        )
        (repository_root / "qbitunregistered" / "payload.py").write_text(
            f"from pathlib import Path\nPath({str(side_effect)!r}).write_text('ran', encoding='utf-8')\n",
            encoding="utf-8",
        )
    else:
        suffix = paired.EXTENSION_SUFFIXES[0] if injection_kind == "native" else ".pyd"
        (repository_root / "qbitunregistered" / f"payload{suffix}").write_bytes(
            b"untrusted native payload",
        )

    completed = _run_import_bootstrap_fixture(repository_root, dependency_root)

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == import_bootstrap.PROTECTED_IMPORT_ERROR
    assert "Traceback" not in completed.stderr
    assert str(tmp_path) not in completed.stderr
    assert not side_effect.exists()


def test_import_bootstrap_rejects_casefold_module_collisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oid = b"a" * 40
    tracked_paths = b"\0".join(
        (
            b"H 100644 " + oid + b" 0\tbenchmarks/__init__.py",
            b"H 100644 " + oid + b" 0\tqbitunregistered/__init__.py",
            b"H 100644 " + oid + b" 0\tqbitunregistered/Collision.py",
            b"H 100644 " + oid + b" 0\tqbitunregistered/collision.py",
            b"",
        )
    )
    monkeypatch.setattr(
        import_bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["git", "ls-files"],
            0,
            stdout=tracked_paths,
        ),
    )
    monkeypatch.setattr(
        import_bootstrap,
        "_validate_protected_source",
        lambda *_args: None,
    )

    with pytest.raises(
        import_bootstrap.ProtectedPackageTreeError,
        match=f"^{import_bootstrap.PROTECTED_IMPORT_ERROR}$",
    ):
        import_bootstrap._tracked_protected_sources(tmp_path)


@pytest.mark.parametrize("redirect_stage", ["before", "during"])
def test_import_bootstrap_rejects_package_redirects_inside_each_child(
    tmp_path: Path,
    redirect_stage: str,
) -> None:
    repository_root = tmp_path / "repository"
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    qbitunregistered_root = repository_root / "qbitunregistered"
    dependency_root = tmp_path / "environment" / "site-packages"
    marker = tmp_path / "evaluator-ran"
    redirect_path = (
        repository_root / "benchmarks" / "__init__.py"
        if redirect_stage == "before"
        else qbitunregistered_root / "redirected.py"
    )
    redirect_target = tmp_path / "redirect-target.py"
    gauntlet_root.mkdir(parents=True)
    qbitunregistered_root.mkdir()
    dependency_root.mkdir(parents=True)
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (qbitunregistered_root / "__init__.py").write_text("", encoding="utf-8")
    redirect_path.write_text("", encoding="utf-8")
    redirect_target.write_text("", encoding="utf-8")
    assert import_bootstrap.__file__ is not None
    bootstrap_path = gauntlet_root / "import_bootstrap.py"
    bootstrap_path.write_bytes(Path(import_bootstrap.__file__).read_bytes())
    main_lines = [
        "from pathlib import Path",
        f'Path({str(marker)!r}).write_text("ran", encoding="utf-8")',
    ]
    if redirect_stage == "during":
        main_lines.extend(
            [
                f"redirect = Path({str(redirect_path)!r})",
                "redirect.unlink()",
                f"redirect.symlink_to(Path({str(redirect_target)!r}))",
            ]
        )
    (gauntlet_root / "__main__.py").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)
    dependency_paths = (str(dependency_root.resolve()),)
    expected_digest = import_bootstrap.dependency_environment_digest(dependency_paths)

    # Model a redirect introduced after the coordinator's preflight scan.
    import_bootstrap._validate_protected_package_trees(repository_root)
    if redirect_stage == "before":
        redirect_path.unlink()
        try:
            redirect_path.symlink_to(redirect_target)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"platform cannot create a symbolic link: {error}")
    else:
        probe = tmp_path / "symlink-probe"
        try:
            probe.symlink_to(redirect_target)
        except (NotImplementedError, OSError) as error:
            pytest.skip(f"platform cannot create a symbolic link: {error}")
        probe.unlink()

    completed = subprocess.run(
        [
            sys.executable,
            "-s",
            "-S",
            "-P",
            str(bootstrap_path),
            str(repository_root),
            json.dumps(dependency_paths),
            import_bootstrap.DEPENDENCY_DIGEST_ARGUMENT,
            expected_digest,
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == ("gauntlet protected package tree contains a redirecting entry")
    assert "Traceback" not in completed.stderr
    assert marker.exists() is (redirect_stage == "during")


@pytest.mark.parametrize("tamper_stage", ["before", "during"])
def test_import_bootstrap_rejects_dependency_tampering_without_traceback(
    tmp_path: Path,
    tamper_stage: str,
) -> None:
    repository_root = tmp_path / "repository"
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    qbitunregistered_root = repository_root / "qbitunregistered"
    dependency_root = tmp_path / "environment" / "site-packages"
    dependency_file = dependency_root / "dependency.py"
    marker = tmp_path / "evaluator-ran"
    gauntlet_root.mkdir(parents=True)
    qbitunregistered_root.mkdir()
    dependency_root.mkdir(parents=True)
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (qbitunregistered_root / "__init__.py").write_text("", encoding="utf-8")
    dependency_file.write_text("VALUE = 1\n", encoding="utf-8")
    assert import_bootstrap.__file__ is not None
    bootstrap_path = gauntlet_root / "import_bootstrap.py"
    bootstrap_path.write_bytes(Path(import_bootstrap.__file__).read_bytes())
    main_lines = [
        "from pathlib import Path",
        f'Path({str(marker)!r}).write_text("ran", encoding="utf-8")',
    ]
    if tamper_stage == "during":
        main_lines.extend(
            [
                "import dependency",
                'Path(dependency.__file__).write_text("VALUE = 2\\n", encoding="utf-8")',
            ]
        )
    (gauntlet_root / "__main__.py").write_text("\n".join(main_lines) + "\n", encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)
    dependency_paths = (str(dependency_root.resolve()),)
    expected_digest = import_bootstrap.dependency_environment_digest(dependency_paths)
    if tamper_stage == "before":
        dependency_file.write_text("VALUE = 2\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-s",
            "-S",
            "-P",
            str(bootstrap_path),
            str(repository_root),
            json.dumps(dependency_paths),
            import_bootstrap.DEPENDENCY_DIGEST_ARGUMENT,
            expected_digest,
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == (f"gauntlet dependency environment changed {tamper_stage} evaluation")
    assert "Traceback" not in completed.stderr
    assert marker.exists() is (tamper_stage == "during")


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
    monkeypatch.setenv("PythonWarnings", "ignore")
    monkeypatch.setenv(paired.ISOLATED_PARENT_CACHE_ENV, "/inherited/parent-cache")
    pycache_roots: list[Path] = []
    dependency_paths = paired._dependency_import_paths()
    dependency_environment_identity = import_bootstrap.dependency_environment_digest(dependency_paths)
    bootstrap_source = b"# immutable bootstrap\n"
    expected_commit = "a" * 40

    def fake_run(command, **kwargs):
        assert command[1:4] == ["-s", "-S", "-P"]
        assert command[4] == "-"
        assert command[5] == str(tmp_path)
        assert json.loads(command[6]) == list(dependency_paths)
        assert command[7:9] == [
            import_bootstrap.EXPECTED_REPOSITORY_COMMIT_ARGUMENT,
            expected_commit,
        ]
        assert command[9:11] == [
            import_bootstrap.DEPENDENCY_DIGEST_ARGUMENT,
            dependency_environment_identity,
        ]
        assert kwargs["input"] == bootstrap_source
        environment = kwargs["env"]
        assert environment["PYTHONNOUSERSITE"] == "1"
        assert environment["PYTHONHASHSEED"] == "0"
        assert "PYTHONSTARTUP" not in environment
        assert "PYTHONPATH" not in environment
        assert "PythonWarnings" not in environment
        assert paired.ISOLATED_PARENT_CACHE_ENV not in environment
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
            dependency_paths=dependency_paths,
            dependency_environment_digest=dependency_environment_identity,
            bootstrap_source=bootstrap_source,
            expected_commit=expected_commit,
        )
        for _ in range(2)
    ]

    assert [result["profile"] for result in results] == ["quick", "quick"]
    assert len(set(pycache_roots)) == 2
    assert all(not path.exists() for path in pycache_roots)


def test_controlled_bootstrap_ignores_root_shadows_and_orders_import_paths(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    selected_package = repository_root / "qbitunregistered"
    dependency_root = tmp_path / "environment" / "site-packages"
    installed_package = dependency_root / "qbitunregistered"
    gauntlet_root.mkdir(parents=True)
    selected_package.mkdir()
    installed_package.mkdir(parents=True)
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (selected_package / "__init__.py").write_text('ORIGIN = "selected-worktree"\n', encoding="utf-8")
    (installed_package / "__init__.py").write_text('ORIGIN = "editable-install"\n', encoding="utf-8")
    (dependency_root / "schedule.py").write_text('ORIGIN = "installed-dependency"\n', encoding="utf-8")
    (repository_root / "schedule.py").write_text('ORIGIN = "root-shadow"\n', encoding="utf-8")
    (gauntlet_root / "__main__.py").write_text(
        "\n".join(
            (
                "import sys",
                "import json",
                ("bootstrap_state = " f"sys.modules.get({import_bootstrap.COORDINATOR_BOOTSTRAP_MODULE!r})"),
                ("bootstrap_accepted = bootstrap_state is not None " "and bootstrap_state.accept(__file__)"),
                "import schedule",
                "import statistics",
                "import qbitunregistered",
                "print(json.dumps({",
                '    "bootstrap_accepted": bootstrap_accepted,',
                '    "first_party": qbitunregistered.ORIGIN,',
                '    "first_party_file": qbitunregistered.__file__,',
                (
                    '    "flags": {"no_site": sys.flags.no_site, '
                    '"no_user_site": sys.flags.no_user_site, "safe_path": sys.flags.safe_path},'
                ),
                '    "third_party": getattr(schedule, "ORIGIN", "installed-dependency"),',
                '    "third_party_file": schedule.__file__,',
                '    "statistics_file": statistics.__file__,',
                '    "statistics_marker": getattr(statistics, "ORIGIN", "stdlib"),',
                '    "path": sys.path,',
                "}))",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    assert paired.__file__ is not None
    bootstrap_source = Path(paired.__file__).with_name("import_bootstrap.py")
    (gauntlet_root / "import_bootstrap.py").write_bytes(bootstrap_source.read_bytes())
    assert launcher.__file__ is not None
    (gauntlet_root / "launcher.py").write_bytes(Path(launcher.__file__).read_bytes())

    shadow_source = repository_root / "statistics.py"
    stale_shadow = 'ORIGIN = "root-stale"\n'
    fresh_shadow = 'ORIGIN = "root-fresh"\n'
    assert len(stale_shadow) == len(fresh_shadow)
    source_timestamp = 1_700_000_000
    shadow_source.write_text(stale_shadow, encoding="utf-8")
    os.utime(shadow_source, (source_timestamp, source_timestamp))
    py_compile.compile(
        str(shadow_source),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
    )
    shadow_source.write_text(fresh_shadow, encoding="utf-8")
    os.utime(shadow_source, (source_timestamp, source_timestamp))
    _commit_gauntlet_test_repository(repository_root)

    clean_environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    clean_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    direct = subprocess.run(
        [sys.executable, "-s", "-m", "benchmarks.gauntlet"],
        cwd=repository_root,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert direct.returncode == 0
    direct_result = json.loads(direct.stdout)
    assert direct_result["bootstrap_accepted"] is False
    assert direct_result["statistics_marker"] == "root-stale"
    assert direct_result["third_party"] == "root-shadow"

    dependency_json = json.dumps([str(dependency_root.resolve())])
    dependency_environment_identity = import_bootstrap.dependency_environment_digest((str(dependency_root.resolve()),))
    controlled = subprocess.run(
        [
            sys.executable,
            "-s",
            "-S",
            "-P",
            str(gauntlet_root / "import_bootstrap.py"),
            str(repository_root),
            dependency_json,
            import_bootstrap.DEPENDENCY_DIGEST_ARGUMENT,
            dependency_environment_identity,
        ],
        cwd=repository_root,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert controlled.returncode == 0, controlled.stderr
    controlled_result = json.loads(controlled.stdout)

    launched = subprocess.run(
        [sys.executable, "-I", str(gauntlet_root / "launcher.py")],
        cwd=repository_root,
        env={
            **clean_environment,
            "TMPDIR": str(tmp_path),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert launched.returncode == 0, launched.stderr
    launched_result = json.loads(launched.stdout)

    for result in (controlled_result, launched_result):
        assert result["bootstrap_accepted"] is True
        assert result["flags"] == {
            "no_site": 1,
            "no_user_site": 1,
            "safe_path": True,
        }
        assert result["first_party"] == "selected-worktree"
        assert Path(result["first_party_file"]).is_relative_to(selected_package)
        assert result["third_party"] == "installed-dependency"
        assert Path(result["third_party_file"]) != repository_root / "schedule.py"
        assert result["statistics_marker"] == "stdlib"
        assert Path(result["statistics_file"]) != shadow_source
        import_paths = [Path(value) for value in result["path"]]
        assert repository_root not in import_paths
        third_party_path = Path(result["third_party_file"]).resolve()
        dependency_index = _installed_dependency_index(import_paths, third_party_path)
        stdlib_zip_indexes = [index for index, path in enumerate(import_paths) if path.suffix.casefold() == ".zip"]
        dynamic_library_indexes = [
            index for index, path in enumerate(import_paths) if path.name.casefold() in {"lib-dynload", "dlls"}
        ]
        assert stdlib_zip_indexes
        assert dynamic_library_indexes
        assert max(*stdlib_zip_indexes, *dynamic_library_indexes) < dependency_index


def test_installed_dependency_index_prefers_nested_site_packages(
    tmp_path: Path,
) -> None:
    stdlib_path = tmp_path / "hostedtoolcache" / "Python" / "3.14" / "lib" / "python3.14"
    dependency_path = stdlib_path / "site-packages"
    import_paths = [
        stdlib_path.parent / "python314.zip",
        stdlib_path,
        stdlib_path / "lib-dynload",
        dependency_path,
    ]
    imported_path = dependency_path / "schedule" / "__init__.py"

    dependency_index = _installed_dependency_index(import_paths, imported_path)

    assert dependency_index == 3
    assert import_paths[dependency_index] == dependency_path
    assert max(0, 2) < dependency_index


def test_paired_child_stderr_capture_is_memory_bounded(
    tmp_path: Path,
) -> None:
    stderr_payload = b"a" * (paired.MAX_CHILD_STDERR_BYTES * 3)

    completed, captured, truncated = paired._run_child_with_bounded_stderr(
        [
            sys.executable,
            "-c",
            ("import sys; " f"sys.stderr.buffer.write(b'a' * {len(stderr_payload)}); " "raise SystemExit(7)"),
        ],
        repository_root=tmp_path,
        environment={},
    )

    assert completed.returncode == 7
    assert len(captured) == paired.MAX_CHILD_STDERR_BYTES
    assert captured == stderr_payload[-paired.MAX_CHILD_STDERR_BYTES :]
    assert truncated is True


def test_paired_child_failure_reports_sanitized_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "child.json"
    secret = "sensitive-child-token"
    monkeypatch.setenv("GAUNTLET_API_TOKEN", secret)
    monkeypatch.setenv("DOCKER_AUTH_CONFIG", "opaque-auth-secret")
    stderr_payload = (
        f'\n\x1b[31mfailed at {tmp_path / "private" / "module.py"} token={secret}\x1b[0m\n'
        'password="abc,assignment-tail"\n'
        "mirror=https://operator:password@example.invalid/path\n"
        '{"password":"hunter2"}\n'
        '{"password":"abc\\"secret-tail"}\n'
        "Authorization: Bearer bearer-secret\n"
        "Cookie: session=cookie-secret\n"
        "opaque-auth-secret\n"
        'File "/tmp/private path/module.py", line 7\n'
        'File "\\\\server\\private share\\module.py", line 9\n'
    ).encode()

    def fake_run(command, **kwargs):
        os.write(kwargs["stderr"], stderr_payload)
        return paired.subprocess.CompletedProcess(command, 9)

    monkeypatch.setattr(paired.subprocess, "run", fake_run)

    with pytest.raises(PairedGauntletError) as error_info:
        paired._run_child(
            tmp_path,
            profile="quick",
            seed=20_260_729,
            samples=DEFAULT_SAMPLES,
            output=output,
            dependency_paths=("dependencies",),
            dependency_environment_digest="a" * 64,
            bootstrap_source=b"# immutable bootstrap\n",
            expected_commit="a" * 40,
        )

    message = str(error_info.value)
    assert message.startswith("paired child evaluation failed with exit code 9: ")
    assert "<path>" in message
    assert "token=<redacted>" in message
    assert str(tmp_path) not in message
    assert secret not in message
    assert "operator:password" not in message
    assert "hunter2" not in message
    assert "secret-tail" not in message
    assert "assignment-tail" not in message
    assert "bearer-secret" not in message
    assert "cookie-secret" not in message
    assert "opaque-auth-secret" not in message
    assert "private path" not in message
    assert "private share" not in message
    assert "\x1b" not in message
    assert len(message.encode("utf-8")) <= paired.MAX_CHILD_STDERR_BYTES + 64


def test_paired_child_failure_suppresses_contextless_truncated_stderr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = tmp_path / "child.json"
    secret_fragment = "credential-material-that-must-not-leak"
    stderr_payload = ("password=" + secret_fragment * 200).encode()

    def fake_run(command, **kwargs):
        os.write(kwargs["stderr"], stderr_payload)
        return paired.subprocess.CompletedProcess(command, 9)

    monkeypatch.setattr(paired.subprocess, "run", fake_run)

    with pytest.raises(PairedGauntletError) as error_info:
        paired._run_child(
            tmp_path,
            profile="quick",
            seed=20_260_729,
            samples=DEFAULT_SAMPLES,
            output=output,
            dependency_paths=("dependencies",),
            dependency_environment_digest="a" * 64,
            bootstrap_source=b"# immutable bootstrap\n",
            expected_commit="a" * 40,
        )

    message = str(error_info.value)
    assert message == ("paired child evaluation failed with exit code 9: " "[stderr truncated; diagnostic suppressed]")
    assert secret_fragment not in message
    assert len(message.encode("utf-8")) < 256
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_paired_child_failure_with_empty_stderr_reports_only_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "child.json"

    def fake_run(command, **_kwargs):
        return paired.subprocess.CompletedProcess(command, 4)

    monkeypatch.setattr(paired.subprocess, "run", fake_run)

    with pytest.raises(
        PairedGauntletError,
        match=r"^paired child evaluation failed with exit code 4$",
    ):
        paired._run_child(
            tmp_path,
            profile="quick",
            seed=20_260_729,
            samples=DEFAULT_SAMPLES,
            output=output,
            dependency_paths=("dependencies",),
            dependency_environment_digest="a" * 64,
            bootstrap_source=b"# immutable bootstrap\n",
            expected_commit="a" * 40,
        )


def test_source_launcher_ignores_timestamp_valid_parent_bytecode(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    package_root = repository_root / "benchmarks"
    gauntlet_root = package_root / "gauntlet"
    gauntlet_root.mkdir(parents=True)
    poisoned_sources = (
        (
            package_root / "__init__.py",
            'print("parent-stale")\n',
            'print("parent-fresh")\n',
        ),
        (
            gauntlet_root / "__init__.py",
            'print("package-stale")\n',
            'print("package-fresh")\n',
        ),
        (
            gauntlet_root / "__main__.py",
            'print("main-stale")\n',
            'print("main-fresh")\n',
        ),
    )
    source_timestamp = 1_700_000_000
    for source, stale_source, fresh_source in poisoned_sources:
        assert len(stale_source) == len(fresh_source)
        source.write_text(stale_source, encoding="utf-8")
        os.utime(source, (source_timestamp, source_timestamp))
        py_compile.compile(
            str(source),
            doraise=True,
            invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP,
        )
        source.write_text(fresh_source, encoding="utf-8")
        os.utime(source, (source_timestamp, source_timestamp))

    clean_environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    clean_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    direct = subprocess.run(
        [sys.executable, "-m", "benchmarks.gauntlet"],
        cwd=repository_root,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert direct.returncode == 0
    assert direct.stdout.splitlines() == [
        "parent-stale",
        "package-stale",
        "main-stale",
    ]

    launcher_path = gauntlet_root / "launcher.py"
    assert launcher.__file__ is not None
    launcher_path.write_text(
        Path(launcher.__file__).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    bootstrap_path = gauntlet_root / "import_bootstrap.py"
    bootstrap_path.write_bytes(Path(launcher.__file__).with_name("import_bootstrap.py").read_bytes())
    qbitunregistered_root = repository_root / "qbitunregistered"
    qbitunregistered_root.mkdir()
    (qbitunregistered_root / "__init__.py").write_text("", encoding="utf-8")
    _commit_gauntlet_test_repository(repository_root)
    cache_parent = tmp_path / "cache-parent"
    cache_parent.mkdir()
    invocation_root = tmp_path / "invocation"
    invocation_root.mkdir()
    launched_environment = {
        **clean_environment,
        "PYTHONPATH": "/inherited/injection",
        "TMPDIR": str(cache_parent),
    }
    launched = subprocess.run(
        [sys.executable, "-I", str(launcher_path)],
        cwd=invocation_root,
        env=launched_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert launched.returncode == 0
    assert launched.stdout.splitlines() == [
        "parent-fresh",
        "package-fresh",
        "main-fresh",
    ]
    assert list(cache_parent.iterdir()) == []


def test_source_launcher_rejects_modified_worktree_bootstrap_before_execution(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    qbitunregistered_root = repository_root / "qbitunregistered"
    invocation_root = tmp_path / "invocation"
    marker = tmp_path / "modified-bootstrap-ran"
    gauntlet_root.mkdir(parents=True)
    qbitunregistered_root.mkdir()
    invocation_root.mkdir()
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__main__.py").write_text('print("canonical evidence")\n', encoding="utf-8")
    (qbitunregistered_root / "__init__.py").write_text("", encoding="utf-8")
    assert launcher.__file__ is not None
    launcher_path = gauntlet_root / "launcher.py"
    launcher_path.write_bytes(Path(launcher.__file__).read_bytes())
    bootstrap_path = gauntlet_root / "import_bootstrap.py"
    bootstrap_path.write_bytes(Path(launcher.__file__).with_name("import_bootstrap.py").read_bytes())
    _commit_gauntlet_test_repository(repository_root)
    subprocess.run(
        ["git", "config", "core.autocrlf", "true"],
        cwd=repository_root,
        check=True,
    )
    bootstrap_path.unlink()
    subprocess.run(
        ["git", "checkout", "--", launcher.BOOTSTRAP_RELATIVE_PATH],
        cwd=repository_root,
        check=True,
    )
    assert b"\r\n" in bootstrap_path.read_bytes()
    clean_environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}
    clean_environment["PYTHONDONTWRITEBYTECODE"] = "1"

    clean = subprocess.run(
        [sys.executable, "-I", str(launcher_path)],
        cwd=invocation_root,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert clean.returncode == 0
    assert clean.stdout.strip() == "canonical evidence"
    assert clean.stderr == ""

    bootstrap_path.write_text(
        "\n".join(
            (
                "from pathlib import Path",
                f"Path({str(marker)!r}).write_text('ran', encoding='utf-8')",
                "print('fabricated evidence')",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    status = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=repository_root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == " M benchmarks/gauntlet/import_bootstrap.py\n"

    modified = subprocess.run(
        [sys.executable, "-I", str(launcher_path)],
        cwd=invocation_root,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert modified.returncode == 1
    assert modified.stdout == ""
    assert modified.stderr.strip() == "gauntlet import bootstrap could not be verified"
    assert "Traceback" not in modified.stderr
    assert str(repository_root) not in modified.stderr
    assert not marker.exists()

    redirected_repository = tmp_path / "redirected-repository"
    redirected_bootstrap = redirected_repository / "benchmarks" / "gauntlet" / "import_bootstrap.py"
    redirected_bootstrap.parent.mkdir(parents=True)
    (redirected_repository / "qbitunregistered").mkdir()
    (redirected_repository / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (redirected_repository / "qbitunregistered" / "__init__.py").write_text("", encoding="utf-8")
    redirected_bootstrap.write_bytes(bootstrap_path.read_bytes())
    _commit_gauntlet_test_repository(redirected_repository)
    redirected_environment = {
        **clean_environment,
        "GIT_DIR": str(redirected_repository / ".git"),
        "GIT_WORK_TREE": str(repository_root),
    }

    redirected = subprocess.run(
        [sys.executable, "-I", str(launcher_path)],
        cwd=invocation_root,
        env=redirected_environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert redirected.returncode == 1
    assert redirected.stdout == ""
    assert redirected.stderr.strip() == "gauntlet import bootstrap could not be verified"
    assert "Traceback" not in redirected.stderr
    assert str(repository_root) not in redirected.stderr
    assert not marker.exists()


def test_source_launcher_resolves_git_directories_with_sanitized_crlf_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    admin_directory = tmp_path / "admin"
    common_directory = tmp_path / "common"
    repository_root.mkdir()
    admin_directory.mkdir()
    common_directory.mkdir()
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "redirected.git"))
    run = Mock(
        return_value=Mock(
            returncode=0,
            stdout=os.fsencode(admin_directory) + b"\r\n" + os.fsencode(common_directory) + b"\r\n",
            stderr=b"",
        )
    )
    monkeypatch.setattr(launcher.subprocess, "run", run)

    assert launcher._repository_git_directories(repository_root) == (
        admin_directory.resolve(),
        common_directory.resolve(),
    )
    assert run.call_args.args[0] == [
        "git",
        "--no-replace-objects",
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
    ]
    assert all(not key.upper().startswith("GIT_") for key in run.call_args.kwargs["env"])

    run.return_value = Mock(returncode=129, stdout=b"", stderr=b"unsupported option")
    with pytest.raises(SystemExit) as error_info:
        launcher._repository_git_directories(repository_root)
    assert str(error_info.value) == launcher.REPOSITORY_METADATA_ERROR


def test_source_launcher_rejects_git_metadata_cache_root_before_creating_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root, candidate_root, common_directory = _initialize_external_git_worktrees(tmp_path)
    listing_before = tuple(sorted(str(path.relative_to(common_directory)) for path in common_directory.rglob("*")))
    temporary_directory = Mock(side_effect=AssertionError("bytecode cache unexpectedly created"))
    trusted_bootstrap = Mock(side_effect=AssertionError("bootstrap unexpectedly read"))
    monkeypatch.setattr(launcher, "_require_isolated_startup", lambda: None)
    monkeypatch.setattr(launcher.tempfile, "gettempdir", lambda: str(common_directory))
    monkeypatch.setattr(launcher.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(launcher, "_trusted_bootstrap_source", trusted_bootstrap)

    with pytest.raises(SystemExit, match="bytecode cache directory must be outside"):
        launcher.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
            ]
        )

    temporary_directory.assert_not_called()
    trusted_bootstrap.assert_not_called()
    assert tuple(sorted(str(path.relative_to(common_directory)) for path in common_directory.rglob("*"))) == listing_before


@pytest.mark.parametrize("changed_interface", [None, "descriptor", "path"])
def test_worktree_bootstrap_compares_ctime_only_within_stat_interface(
    monkeypatch: pytest.MonkeyPatch,
    changed_interface: str | None,
) -> None:
    payload = b"trusted bootstrap\n"

    def file_stat(*, ctime_ns: int) -> os.stat_result:
        return cast(
            os.stat_result,
            SimpleNamespace(
                st_dev=7,
                st_ino=11,
                st_mode=stat.S_IFREG | 0o600,
                st_size=len(payload),
                st_file_attributes=0,
                st_mtime_ns=13,
                st_ctime_ns=ctime_ns,
            ),
        )

    path_before = file_stat(ctime_ns=17)
    descriptor_before = file_stat(ctime_ns=19)
    path_after = file_stat(ctime_ns=23 if changed_interface == "path" else 17)
    descriptor_after = file_stat(ctime_ns=29 if changed_interface == "descriptor" else 19)
    monkeypatch.setattr(launcher.os, "lstat", Mock(side_effect=(path_before, path_after)))
    monkeypatch.setattr(launcher.os, "open", Mock(return_value=31))
    monkeypatch.setattr(launcher.os, "fstat", Mock(side_effect=(descriptor_before, descriptor_after)))
    monkeypatch.setattr(launcher.os, "read", Mock(side_effect=(payload, b"")))
    monkeypatch.setattr(launcher.os, "close", Mock())

    if changed_interface is None:
        assert launcher._read_worktree_bootstrap(Path("bootstrap.py")) == payload
    else:
        with pytest.raises(SystemExit, match=launcher.BOOTSTRAP_VERIFICATION_ERROR):
            launcher._read_worktree_bootstrap(Path("bootstrap.py"))


@pytest.mark.parametrize(
    ("worktree_source", "trusted_source", "expected"),
    [
        (b"first\nsecond\n", b"first\nsecond\n", True),
        (b"first\r\nsecond\r\n", b"first\nsecond\n", True),
        (b"first\r\nsecond\n", b"first\nsecond\n", False),
        (b"first\r\nchanged\r\n", b"first\nsecond\n", False),
        (b"first\r\nsecond\r\n", b"first\r\nsecond\r\n", True),
        (b"first\r\r\nsecond\r\r\n", b"first\r\nsecond\r\n", False),
    ],
)
def test_bootstrap_checkout_bytes_allow_only_exact_or_whole_file_crlf(
    worktree_source: bytes,
    trusted_source: bytes,
    expected: bool,
) -> None:
    assert launcher._bootstrap_checkout_matches(worktree_source, trusted_source) is expected


@pytest.mark.parametrize(("child_returncode", "expected_returncode"), [(2, 2), (-15, 143)])
def test_source_launcher_strips_injection_spawns_once_and_cleans_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    child_returncode: int,
    expected_returncode: int,
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(launcher, "_require_isolated_startup", lambda: None)
    monkeypatch.setenv("PYTHONPATH", "/inherited/injection")
    monkeypatch.setenv("PythonStartup", "/inherited/startup.py")
    monkeypatch.setenv("pythonwarnings", "ignore")
    monkeypatch.setenv("git_dir", "/redirected/git-dir")
    monkeypatch.setenv("GiT_WoRk_TrEe", "/redirected/worktree")
    monkeypatch.setenv(
        launcher.ISOLATED_PARENT_CACHE_ENV.lower(),
        "/inherited/parent-cache",
    )
    trusted_bootstrap = b"# trusted bootstrap\n"
    monkeypatch.setattr(
        launcher,
        "_trusted_bootstrap_source",
        lambda _repository_root: trusted_bootstrap,
    )
    monkeypatch.setattr(
        launcher,
        "_repository_protected_roots",
        lambda roots: tuple(path.resolve() for path in roots),
    )
    calls: list[list[str]] = []

    def record_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        assert kwargs["input"] == trusted_bootstrap
        environment = kwargs["env"]
        assert "PYTHONPATH" not in environment
        assert "PythonStartup" not in environment
        assert "pythonwarnings" not in environment
        assert "git_dir" not in environment
        assert "GiT_WoRk_TrEe" not in environment
        assert launcher.ISOLATED_PARENT_CACHE_ENV.lower() not in environment
        cache_root = Path(environment["PYTHONPYCACHEPREFIX"])
        assert cache_root.is_dir()
        assert environment[launcher.ISOLATED_PARENT_CACHE_ENV] == str(cache_root)
        assert environment["PYTHONHASHSEED"] == "0"
        assert environment["PYTHONNOUSERSITE"] == "1"
        return subprocess.CompletedProcess(command, child_returncode)

    monkeypatch.setattr(launcher.subprocess, "run", record_run)

    returncode = launcher.main(["--profile", "quick"])

    assert returncode == expected_returncode
    assert len(calls) == 1
    assert calls[0][:7] == [
        sys.executable,
        "-s",
        "-S",
        "-P",
        "-",
        str(REPOSITORY_ROOT),
        json.dumps(launcher._dependency_import_paths()),
    ]
    assert calls[0][7:] == ["--profile", "quick"]
    assert list(tmp_path.iterdir()) == []


def test_source_launcher_requires_isolated_interpreter() -> None:
    if sys.flags.isolated:
        pytest.skip("test runner already uses isolated interpreter mode")

    with pytest.raises(SystemExit, match="must be started with python -I"):
        launcher._require_isolated_startup()


def test_direct_paired_module_rejects_spoofed_cache_markers_before_root_shadow(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "repository"
    gauntlet_root = repository_root / "benchmarks" / "gauntlet"
    qbitunregistered_root = repository_root / "qbitunregistered"
    gauntlet_root.mkdir(parents=True)
    qbitunregistered_root.mkdir()
    (repository_root / "benchmarks" / "__init__.py").write_text("", encoding="utf-8")
    (gauntlet_root / "__init__.py").write_text("", encoding="utf-8")
    (qbitunregistered_root / "__init__.py").write_text("", encoding="utf-8")
    for source_name in ("__main__.py", "baseline.py", "identity.py"):
        source = REPOSITORY_ROOT / "benchmarks" / "gauntlet" / source_name
        (gauntlet_root / source_name).write_bytes(source.read_bytes())
    shadow_marker = tmp_path / "statistics-shadow-ran"
    shadow_source = repository_root / "statistics.py"
    shadow_source.write_text(
        "from pathlib import Path\n" f"Path({str(shadow_marker)!r}).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    _commit_gauntlet_test_repository(repository_root)
    subprocess.run(["git", "add", "statistics.py"], cwd=repository_root, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Gauntlet Test",
            "-c",
            "user.email=gauntlet@example.invalid",
            "commit",
            "--amend",
            "--no-edit",
            "-q",
        ],
        cwd=repository_root,
        check=True,
    )
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    cache_root = tmp_path / "spoofed-cache"
    control_root.mkdir()
    candidate_root.mkdir()
    cache_root.mkdir()
    secret = "operator-secret-value"
    environment = {
        **{key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")},
        "PYTHONPYCACHEPREFIX": str(cache_root),
        gauntlet_cli.ISOLATED_PARENT_CACHE_ENV: str(cache_root),
        "GAUNTLET_SECRET": secret,
    }

    argument_forms = (
        [
            "--paired-control",
            str(control_root),
            "--paired-candidate",
            str(candidate_root),
        ],
        [
            f"--paired-control={control_root}",
            f"--paired-candidate={candidate_root}",
        ],
    )
    invocations: tuple[tuple[list[str], dict[str, str]], ...] = (
        ([], environment),
        (
            ["-s", "-S", "-P"],
            {
                **environment,
                "PYTHONPATH": str(repository_root),
            },
        ),
    )
    for interpreter_arguments, invocation_environment in invocations:
        for arguments in argument_forms:
            completed = subprocess.run(
                [
                    sys.executable,
                    *interpreter_arguments,
                    "-m",
                    "benchmarks.gauntlet",
                    *arguments,
                ],
                cwd=repository_root,
                env=invocation_environment,
                check=False,
                capture_output=True,
                text=True,
            )

            assert completed.returncode == 1
            assert completed.stdout == ""
            assert completed.stderr.strip() == "paired gauntlet must be started with benchmarks/gauntlet/launcher.py"
            assert "Traceback" not in completed.stderr
            assert str(repository_root) not in completed.stderr
            assert secret not in completed.stderr
    assert not shadow_marker.exists()


def test_direct_nonpaired_module_entry_remains_available() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "benchmarks.gauntlet", "--help"],
        cwd=REPOSITORY_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "Run the deterministic qbitunregistered safety gauntlet." in completed.stdout
    assert completed.stderr == ""


@requires_bound_publication
def test_source_launcher_reports_paired_failure_without_traceback_or_paths() -> None:
    assert launcher.__file__ is not None

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(Path(launcher.__file__)),
            "--paired-control",
            str(REPOSITORY_ROOT),
            "--paired-candidate",
            str(REPOSITORY_ROOT),
        ],
        cwd=REPOSITORY_ROOT.parent,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == "paired control and candidate must be isolated worktrees"
    assert "Traceback" not in completed.stderr
    assert str(REPOSITORY_ROOT) not in completed.stderr


def test_paired_cli_rejects_unisolated_or_mismatched_parent_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(gauntlet_cli, "_require_isolated_coordinator", lambda: None)
    monkeypatch.setattr(
        gauntlet_cli,
        "_repository_protected_roots",
        lambda roots: tuple(path.resolve() for path in roots),
    )
    monkeypatch.setenv(gauntlet_cli.ISOLATED_PARENT_CACHE_ENV, str(cache_root))
    monkeypatch.setattr(
        gauntlet_cli.sys,
        "pycache_prefix",
        str(tmp_path / "different-cache"),
    )
    run_calls: list[tuple[object, ...]] = []

    def record_paired_run(*args, **_kwargs):
        run_calls.append(args)
        return {"comparison": {"overall": "fail"}}

    monkeypatch.setattr(paired, "run_paired_gauntlet", record_paired_run)

    with pytest.raises(SystemExit, match="missing, mismatched, or unsafe"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
            ]
        )

    assert run_calls == []


def test_paired_cli_translates_sanitized_child_failure_without_chaining(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    sensitive_path = tmp_path / "private" / "child.py"
    sanitized_message = "paired child evaluation failed with exit code 9: token=<redacted> failed at <path>"
    monkeypatch.setattr(
        runner,
        "bind_output_directory",
        lambda *_args, **_kwargs: nullcontext(None),
    )

    def fail_paired_run(*_args, **_kwargs):
        try:
            raise OSError(f"child failed at {sensitive_path}")
        except OSError as error:
            raise PairedGauntletError(sanitized_message) from error

    monkeypatch.setattr(paired, "run_paired_gauntlet", fail_paired_run)

    with pytest.raises(SystemExit) as error_info:
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
            ]
        )

    assert str(error_info.value) == sanitized_message
    assert error_info.value.__cause__ is None
    assert error_info.value.__suppress_context__ is True
    assert str(sensitive_path) not in str(error_info.value)


@pytest.mark.parametrize(
    "quality_bar_text",
    [
        "schema_version = [\n",
        "schema_version = 0\n",
    ],
    ids=["malformed-toml", "invalid-schema"],
)
def test_paired_cli_translates_invalid_canonical_quality_bar_without_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
    quality_bar_text: str,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    quality_bar_path = tmp_path / "private" / "quality-bar.toml"
    quality_bar_path.parent.mkdir()
    quality_bar_path.write_text(quality_bar_text, encoding="utf-8")
    quality_bar_source = quality_bar_path.read_bytes()

    with pytest.raises(PairedGauntletError) as domain_error_info:
        paired._load_canonical_quality_bar(quality_bar_source)

    assert isinstance(domain_error_info.value.__cause__, QualityBarError)

    def load_invalid_quality_bar(*_args, **_kwargs):
        paired._load_canonical_quality_bar(quality_bar_source)

    bound_directory = object()
    monkeypatch.setattr(
        runner,
        "bind_output_directory",
        lambda *_args, **_kwargs: nullcontext(bound_directory),
    )
    monkeypatch.setattr(paired, "run_paired_gauntlet", load_invalid_quality_bar)

    with pytest.raises(SystemExit) as error_info:
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
            ]
        )

    assert str(error_info.value) == ("paired canonical quality bar is malformed or does not match the evaluator schema")
    assert error_info.value.__cause__ is None
    assert error_info.value.__suppress_context__ is True
    assert str(quality_bar_path) not in str(error_info.value)


@pytest.mark.parametrize(
    ("unexpected_error", "expected_type"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(23), SystemExit),
        (RuntimeError("unexpected quality-bar failure"), RuntimeError),
    ],
)
def test_paired_quality_bar_boundary_preserves_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
    unexpected_error: BaseException,
    expected_type: type[BaseException],
) -> None:
    def fail_unexpectedly(_source: bytes):
        raise unexpected_error

    monkeypatch.setattr(paired, "load_quality_bar_bytes", fail_unexpectedly)

    with pytest.raises(expected_type) as error_info:
        paired._load_canonical_quality_bar(b"quality-bar")

    assert error_info.value is unexpected_error


def test_paired_cli_translates_changed_repository_identity_without_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    expected_identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    changed_identity = RepositoryIdentity("c" * 40, False, "d" * 64)
    captured_identities = iter((expected_identity, changed_identity))
    monkeypatch.setattr(
        gauntlet_cli,
        "capture_repository_identity",
        lambda _root: next(captured_identities),
    )
    monkeypatch.setattr(
        runner,
        "bind_output_directory",
        lambda *_args, **_kwargs: nullcontext(None),
    )
    monkeypatch.setattr(
        paired,
        "run_paired_gauntlet",
        lambda *_args, **_kwargs: {"comparison": {"overall": "fail"}},
    )

    with pytest.raises(SystemExit) as error_info:
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
            ]
        )

    assert str(error_info.value) == "paired repository identity changed during evaluation"
    assert error_info.value.__cause__ is None
    assert error_info.value.__suppress_context__ is True
    assert str(tmp_path) not in str(error_info.value)


def test_paired_domain_translates_repository_identity_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = RepositoryIdentity("a" * 40, True, "b" * 64)
    changed = RepositoryIdentity("c" * 40, False, "d" * 64)
    monkeypatch.setattr(
        paired,
        "capture_repository_identity",
        lambda _root: changed,
    )

    with pytest.raises(
        PairedGauntletError,
        match=r"^paired repository identity changed during evaluation$",
    ) as error_info:
        paired._require_unchanged_identity(expected, tmp_path)

    assert isinstance(error_info.value.__cause__, RepositoryIdentityError)


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [
        (KeyboardInterrupt(), KeyboardInterrupt),
        (SystemExit(17), SystemExit),
        (RuntimeError("unexpected evaluator error"), RuntimeError),
    ],
)
def test_paired_cli_does_not_swallow_unexpected_or_control_flow_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
    error: BaseException,
    expected_type: type[BaseException],
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    monkeypatch.setattr(
        runner,
        "bind_output_directory",
        lambda *_args, **_kwargs: nullcontext(None),
    )

    def fail_paired_run(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(paired, "run_paired_gauntlet", fail_paired_run)

    with pytest.raises(expected_type) as error_info:
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
            ]
        )

    assert error_info.value is error


def test_repository_protected_roots_include_normal_git_metadata_from_relative_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository_root)], check=True)
    redirected_root = tmp_path / "redirected"
    subprocess.run(["git", "init", "-q", str(redirected_root)], check=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(redirected_root / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected_root))

    protected_roots = gauntlet_cli._repository_protected_roots((Path("repository"),))

    assert protected_roots == (
        repository_root.resolve(),
        (repository_root / ".git").resolve(),
    )


def test_repository_git_directories_are_sanitized_strict_and_windows_portable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    admin_directory = tmp_path / "admin"
    common_directory = tmp_path / "common"
    admin_alias = tmp_path / "admin-alias"
    repository_root.mkdir()
    admin_directory.mkdir()
    common_directory.mkdir()
    try:
        admin_alias.symlink_to(admin_directory, target_is_directory=True)
    except (NotImplementedError, OSError):
        admin_alias = admin_directory
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "redirected.git"))
    monkeypatch.setenv("git_work_tree", str(tmp_path / "redirected-worktree"))
    completed = Mock(
        returncode=0,
        stdout=os.fsencode(admin_alias) + b"\r\n" + os.fsencode(common_directory) + b"\r\n",
        stderr=b"",
    )
    run = Mock(return_value=completed)
    monkeypatch.setattr(gauntlet_cli.subprocess, "run", run)
    assert gauntlet_cli._repository_git_directories(repository_root) == (
        admin_directory.resolve(),
        common_directory.resolve(),
    )
    command = run.call_args.args[0]
    options = run.call_args.kwargs
    assert command == [
        "git",
        "rev-parse",
        "--path-format=absolute",
        "--git-dir",
        "--git-common-dir",
    ]
    assert options["cwd"] == repository_root
    assert all(not key.upper().startswith("GIT_") for key in options["env"])
    assert options["capture_output"] is True
    assert options["timeout"] == 10

    valid_lines = os.fsencode(admin_directory) + b"\n" + os.fsencode(common_directory) + b"\n"
    invalid_results = (
        Mock(returncode=1, stdout=valid_lines, stderr=b""),
        Mock(returncode=0, stdout=valid_lines, stderr=b"warning\n"),
        Mock(returncode=0, stdout=os.fsencode(admin_directory) + b"\n", stderr=b""),
        Mock(returncode=0, stdout=valid_lines + os.fsencode(admin_directory) + b"\n", stderr=b""),
        Mock(returncode=0, stdout=b"relative.git\nrelative-common.git\n", stderr=b""),
        Mock(returncode=0, stdout=b"\0invalid\n" + os.fsencode(common_directory) + b"\n", stderr=b""),
        Mock(
            returncode=0, stdout=os.fsencode(admin_directory) + b"\n" + os.fsencode(tmp_path / "missing") + b"\n", stderr=b""
        ),
    )
    for invalid_result in invalid_results:
        run.return_value = invalid_result
        with pytest.raises(SystemExit) as error_info:
            gauntlet_cli._repository_git_directories(repository_root)
        assert str(error_info.value) == gauntlet_cli._REPOSITORY_METADATA_ERROR


def test_paired_cli_rejects_output_inside_external_common_git_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root, candidate_root, common_directory = _initialize_external_git_worktrees(tmp_path)
    cache_root = tmp_path / "parent-pycache"
    cache_root.mkdir()
    proposed_output = common_directory / "paired-result.json"
    status_before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=control_root,
        check=True,
        capture_output=True,
    ).stdout
    protected_roots = gauntlet_cli._repository_protected_roots((control_root, candidate_root))
    child = Mock(side_effect=AssertionError("paired child unexpectedly started"))
    monkeypatch.setattr(gauntlet_cli, "_require_isolated_coordinator", lambda: None)
    monkeypatch.setenv(gauntlet_cli.ISOLATED_PARENT_CACHE_ENV, str(cache_root))
    monkeypatch.setattr(gauntlet_cli.sys, "pycache_prefix", str(cache_root))
    monkeypatch.setattr(paired, "run_paired_gauntlet", child)

    with pytest.raises(SystemExit, match="outside both evaluated repositories"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--output",
                str(proposed_output),
            ]
        )

    child.assert_not_called()
    assert common_directory in protected_roots
    assert (common_directory / "worktrees" / candidate_root.name).resolve() in protected_roots
    assert not proposed_output.exists()
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=control_root,
            check=True,
            capture_output=True,
        ).stdout
        == status_before
    )


@requires_bound_publication
def test_paired_cli_uses_stable_git_metadata_roots_for_bound_validation_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control_root, candidate_root, common_directory = _initialize_external_git_worktrees(tmp_path)
    cache_root = tmp_path / "parent-pycache"
    output_root = tmp_path / "output"
    cache_root.mkdir()
    output_root.mkdir()
    output_path = output_root / "paired-result.json"
    original_protected_roots = gauntlet_cli._repository_protected_roots
    protected_root_calls: list[tuple[Path, ...]] = []
    bound_roots: list[tuple[Path, ...]] = []

    def record_protected_roots(repository_roots: tuple[Path, ...]) -> tuple[Path, ...]:
        protected_root_calls.append(repository_roots)
        return original_protected_roots(repository_roots)

    def record_write(
        _serialized_result: str,
        output: Path | None = None,
        *,
        default_directory: Path | None = None,
        bound_directory: runner.BoundOutputDirectory | None = None,
    ) -> Path:
        assert default_directory is None
        assert bound_directory is not None
        bound_roots.append(bound_directory.protected_roots)
        assert output == output_path
        return output_path

    monkeypatch.setattr(gauntlet_cli, "_require_isolated_coordinator", lambda: None)
    monkeypatch.setattr(gauntlet_cli, "_repository_protected_roots", record_protected_roots)
    monkeypatch.setenv(gauntlet_cli.ISOLATED_PARENT_CACHE_ENV, str(cache_root))
    monkeypatch.setattr(gauntlet_cli.sys, "pycache_prefix", str(cache_root))
    monkeypatch.setattr(
        paired,
        "run_paired_gauntlet",
        lambda *_args, **_kwargs: {"comparison": {"overall": "fail"}},
    )
    monkeypatch.setattr(runner, "write_serialized_result", record_write)

    assert (
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
        == gauntlet_cli.COMPARISON_FAILED_EXIT
    )

    assert len(protected_root_calls) == 3
    assert protected_root_calls[1] == protected_root_calls[2]
    assert len(bound_roots) == 1
    assert common_directory in bound_roots[0]
    assert (common_directory / "worktrees" / candidate_root.name).resolve() in bound_roots[0]
    assert not output_path.exists()


def test_repository_protected_roots_revalidation_fails_closed_on_change_or_resolution_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    expected = (repository_root.resolve(), tmp_path / "original.git")
    changed = (repository_root.resolve(), tmp_path / "changed.git")
    monkeypatch.setattr(gauntlet_cli, "_repository_protected_roots", lambda _roots: changed)

    with pytest.raises(SystemExit) as changed_error:
        gauntlet_cli._revalidate_repository_protected_roots((repository_root,), expected)
    assert str(changed_error.value) == gauntlet_cli._REPOSITORY_METADATA_CHANGED_ERROR

    def fail_resolution(_roots: object) -> tuple[Path, ...]:
        raise SystemExit(gauntlet_cli._REPOSITORY_METADATA_ERROR)

    monkeypatch.setattr(gauntlet_cli, "_repository_protected_roots", fail_resolution)
    with pytest.raises(SystemExit) as resolution_error:
        gauntlet_cli._revalidate_repository_protected_roots((repository_root,), expected)
    assert str(resolution_error.value) == gauntlet_cli._REPOSITORY_METADATA_CHANGED_ERROR
    assert resolution_error.value.__cause__ is None


def test_cli_rejects_output_symlink_entry_inside_invoking_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    (repository_root / "benchmarks" / "gauntlet").mkdir(parents=True)
    monkeypatch.setattr(
        gauntlet_cli,
        "__file__",
        str(repository_root / "benchmarks" / "gauntlet" / "__main__.py"),
    )
    monkeypatch.setattr(
        gauntlet_cli,
        "_repository_protected_roots",
        lambda roots: tuple(path.resolve() for path in roots),
    )
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr(
        gauntlet_cli,
        "capture_repository_identity",
        lambda _root: identity,
    )
    protected_path = tmp_path / "protected.json"
    protected_content = "protected external content\n"
    protected_path.write_text(protected_content, encoding="utf-8")
    output_path = repository_root / "result.json"
    try:
        output_path.symlink_to(protected_path)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a file symbolic link: {error}")
    run_calls: list[tuple[object, ...]] = []

    def record_gauntlet_run(*args, **_kwargs):
        run_calls.append(args)
        return {}

    monkeypatch.setattr(runner, "run_gauntlet", record_gauntlet_run)

    with pytest.raises(SystemExit, match="outside the repository"):
        gauntlet_cli.main(["--output", str(output_path)])

    assert run_calls == []
    assert output_path.is_symlink()
    assert protected_path.read_text(encoding="utf-8") == protected_content


def test_cli_rejects_default_output_directory_inside_invoking_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    (repository_root / "benchmarks" / "gauntlet").mkdir(parents=True)
    monkeypatch.setattr(
        gauntlet_cli,
        "__file__",
        str(repository_root / "benchmarks" / "gauntlet" / "__main__.py"),
    )
    monkeypatch.setattr(
        gauntlet_cli,
        "_repository_protected_roots",
        lambda roots: tuple(path.resolve() for path in roots),
    )
    monkeypatch.setattr(tempfile, "tempdir", str(repository_root))
    identity = RepositoryIdentity("a" * 40, True, "b" * 64)
    monkeypatch.setattr(
        gauntlet_cli,
        "capture_repository_identity",
        lambda _root: identity,
    )
    run_calls: list[tuple[object, ...]] = []

    def record_gauntlet_run(*args, **_kwargs):
        run_calls.append(args)
        return {}

    monkeypatch.setattr(runner, "run_gauntlet", record_gauntlet_run)

    with pytest.raises(SystemExit, match="outside the repository"):
        gauntlet_cli.main([])

    assert run_calls == []
    assert list(repository_root.iterdir()) == [repository_root / "benchmarks"]


def test_paired_cli_requires_both_worktrees_and_external_output(
    tmp_path: Path,
    isolated_parent_cache: Path,
) -> None:
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


def test_paired_cli_compare_symlink_loop_fails_with_canonical_path_free_error(
    tmp_path: Path,
) -> None:
    control_root = tmp_path / "control-secret"
    candidate_root = tmp_path / "candidate-secret"
    subprocess.run(["git", "init", "-q", str(control_root)], check=True)
    subprocess.run(["git", "init", "-q", str(candidate_root)], check=True)
    first_link = tmp_path / "private-compare-a"
    second_link = tmp_path / "private-compare-b"
    try:
        first_link.symlink_to(second_link)
        second_link.symlink_to(first_link)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a symbolic-link loop: {error}")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")}

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(REPOSITORY_ROOT / "benchmarks" / "gauntlet" / "launcher.py"),
            "--paired-control",
            str(control_root),
            "--paired-candidate",
            str(candidate_root),
            "--compare",
            str(first_link),
        ],
        cwd=REPOSITORY_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr.strip() == ("paired gauntlet requires the invoking checkout's canonical quality bar")
    assert "Traceback" not in completed.stderr
    assert "secret" not in completed.stderr
    assert str(tmp_path) not in completed.stderr


def test_paired_cli_compare_resolve_error_is_canonical_and_path_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    private_compare = tmp_path / "private-credential-quality-bar.toml"
    control_root.mkdir()
    candidate_root.mkdir()
    original_resolve = Path.resolve

    def selective_resolve(path: Path, strict: bool = False) -> Path:
        if path == private_compare:
            raise OSError(f"could not resolve secret path {path}")
        return original_resolve(path, strict=strict)

    child = Mock(side_effect=AssertionError("paired child unexpectedly started"))
    monkeypatch.setattr(Path, "resolve", selective_resolve)
    monkeypatch.setattr(paired, "run_paired_gauntlet", child)

    with pytest.raises(SystemExit) as error_info:
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--compare",
                str(private_compare),
            ]
        )

    assert str(error_info.value) == ("paired gauntlet requires the invoking checkout's canonical quality bar")
    assert error_info.value.__cause__ is None
    assert "secret" not in str(error_info.value)
    assert str(tmp_path) not in str(error_info.value)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    child.assert_not_called()


def test_paired_cli_resolves_relative_worktrees_before_output_containment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
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


@requires_bound_publication
def test_paired_cli_atomically_replaces_external_output_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
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


@requires_bound_publication
def test_paired_cli_rejects_directory_output_before_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    output_root = tmp_path / "output"
    output_path = output_root / "result.json"
    control_root.mkdir()
    candidate_root.mkdir()
    output_path.mkdir(parents=True)
    run_calls: list[tuple[object, ...]] = []

    def record_paired_run(*args, **_kwargs):
        run_calls.append(args)
        return {"comparison": {"overall": "fail"}}

    monkeypatch.setattr(paired, "run_paired_gauntlet", record_paired_run)

    with pytest.raises(SystemExit) as error_info:
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

    assert str(error_info.value) == ("result output must be missing, a regular file, or a symbolic link")
    assert error_info.value.__cause__ is None
    assert error_info.value.__suppress_context__ is True
    assert str(output_path) not in str(error_info.value)
    assert run_calls == []
    assert output_path.is_dir()
    assert list(output_path.iterdir()) == []
    assert list(output_root.iterdir()) == [output_path]


@requires_bound_publication
def test_paired_cli_translates_publication_race_and_cleans_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    output_root = tmp_path / "output"
    output_path = output_root / "result.json"
    control_root.mkdir()
    candidate_root.mkdir()
    output_root.mkdir()
    run_calls: list[tuple[object, ...]] = []

    def record_paired_run(*args, **_kwargs):
        run_calls.append(args)
        return {"comparison": {"overall": "fail"}}

    def substitute_directory_then_fail(*_args, **_kwargs):
        output_path.mkdir()
        raise IsADirectoryError("publication target became a directory")

    monkeypatch.setattr(paired, "run_paired_gauntlet", record_paired_run)
    monkeypatch.setattr(runner.os, "link", substitute_directory_then_fail)

    with pytest.raises(SystemExit) as error_info:
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

    assert str(error_info.value) == "could not publish the bound result safely"
    assert error_info.value.__cause__ is None
    assert error_info.value.__suppress_context__ is True
    assert str(output_path) not in str(error_info.value)
    assert len(run_calls) == 1
    assert output_path.is_dir()
    assert list(output_path.iterdir()) == []
    assert list(output_root.iterdir()) == [output_path]


def test_paired_cli_rejects_output_symlink_entry_inside_evaluated_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
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
    output_path = candidate_root / "result.json"
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
    assert protected_path.read_text(encoding="utf-8") == protected_content


def test_paired_cli_rejects_default_output_directory_inside_evaluated_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(candidate_root))
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
            ]
        )

    assert run_calls == []
    assert list(candidate_root.iterdir()) == []


@requires_bound_publication
def test_paired_cli_rejects_retargeted_output_ancestor_before_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
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


@requires_bound_publication
def test_paired_cli_rejects_raced_bound_directory_without_protected_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    outside_container = tmp_path / "outside"
    outside_results = outside_container / "results"
    outside_results.mkdir(parents=True)
    saved_container = tmp_path / "saved-outside"
    unsafe_container = candidate_root / "unsafe"
    unsafe_results = unsafe_container / "results"
    unsafe_results.mkdir(parents=True)
    output_path = outside_results / "result.json"

    def swap_to_unsafe_directory(*_args, **_kwargs):
        outside_container.rename(saved_container)
        outside_container.symlink_to(unsafe_container, target_is_directory=True)
        return {"comparison": {"overall": "fail"}}

    monkeypatch.setattr(paired, "run_paired_gauntlet", swap_to_unsafe_directory)

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

    assert list((saved_container / "results").iterdir()) == []
    assert list(unsafe_results.iterdir()) == []


@requires_bound_publication
def test_paired_cli_fails_closed_without_existing_output_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_parent_cache: Path,
) -> None:
    control_root = tmp_path / "control"
    candidate_root = tmp_path / "candidate"
    control_root.mkdir()
    candidate_root.mkdir()
    missing_directory = tmp_path / "missing" / "output"
    monkeypatch.setattr(
        paired,
        "run_paired_gauntlet",
        lambda *_args, **_kwargs: {"comparison": {"overall": "fail"}},
    )

    with pytest.raises(SystemExit, match="could not bind the validated result directory"):
        gauntlet_cli.main(
            [
                "--paired-control",
                str(control_root),
                "--paired-candidate",
                str(candidate_root),
                "--output",
                str(missing_directory / "result.json"),
            ]
        )

    assert not missing_directory.exists()


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


def test_omitted_output_uses_bound_validated_temporary_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validated_directory = tmp_path / "validated"
    changed_default_directory = tmp_path / "changed-default"
    validated_directory.mkdir()
    changed_default_directory.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(changed_default_directory))

    output = runner.write_serialized_result(
        "validated artifact\n",
        default_directory=validated_directory,
    )

    assert output.parent == validated_directory
    assert output.read_text(encoding="utf-8") == "validated artifact\n"
    assert list(changed_default_directory.iterdir()) == []


@requires_bound_publication
def test_bound_omitted_output_uses_unique_file_in_validated_directory(
    tmp_path: Path,
) -> None:
    with runner.bind_output_directory(tmp_path) as bound_directory:
        output = runner.write_serialized_result(
            "bound artifact\n",
            default_directory=tmp_path,
            bound_directory=bound_directory,
        )

    assert output.parent == tmp_path
    assert output.name.startswith("qbitunregistered-gauntlet-")
    assert output.read_text(encoding="utf-8") == "bound artifact\n"


@requires_bound_publication
def test_bound_omitted_output_preserves_concurrent_replacement_before_identity_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_bound_file_identity = runner._bound_file_identity
    replaced = False

    def replace_before_identity_check(
        bound_directory: runner.BoundOutputDirectory,
        name: str,
    ) -> tuple[int, int, int] | None:
        nonlocal replaced
        if name.startswith("qbitunregistered-gauntlet-") and name.endswith(".json") and not replaced:
            replacement = tmp_path / "concurrent-result"
            replacement.write_text("concurrent artifact\n", encoding="utf-8")
            os.replace(replacement, tmp_path / name)
            replaced = True
        return real_bound_file_identity(bound_directory, name)

    monkeypatch.setattr(runner, "_bound_file_identity", replace_before_identity_check)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        with pytest.raises(GauntletSafetyError):
            runner.write_serialized_result(
                "unaccepted artifact\n",
                default_directory=tmp_path,
                bound_directory=bound_directory,
            )

    assert replaced is True
    outputs = [path for path in tmp_path.iterdir() if path.name.endswith(".json")]
    assert len(outputs) == 1
    assert outputs[0].read_text(encoding="utf-8") == "concurrent artifact\n"
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "concurrent artifact\n"
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


@requires_bound_publication
def test_bound_omitted_output_does_not_clobber_replacement_before_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_link = os.link
    replaced = False

    def replace_before_staging_link(source, destination, **kwargs) -> None:
        nonlocal replaced
        if str(source).endswith(".tmp"):
            replacement = tmp_path / "concurrent-result"
            replacement.write_text("concurrent artifact\n", encoding="utf-8")
            os.replace(replacement, tmp_path / destination)
            replaced = True
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(runner.os, "link", replace_before_staging_link)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        with pytest.raises(
            GauntletSafetyError,
            match=r"^bound result output changed during publication$",
        ):
            runner.write_serialized_result(
                "unaccepted artifact\n",
                default_directory=tmp_path,
                bound_directory=bound_directory,
            )

    assert replaced is True
    outputs = [path for path in tmp_path.iterdir() if path.name.endswith(".json")]
    assert len(outputs) == 1
    assert outputs[0].read_text(encoding="utf-8") == "concurrent artifact\n"
    assert all(not path.name.endswith((".tmp", ".backup")) for path in tmp_path.iterdir())


@requires_bound_publication
def test_bound_omitted_output_retains_uncertain_leaf_when_identity_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_fstat = os.fstat

    with runner.bind_output_directory(tmp_path) as bound_directory:

        def fail_allocator_fstat(descriptor: int) -> os.stat_result:
            if descriptor != bound_directory.descriptor:
                reservation = next(tmp_path.glob("qbitunregistered-gauntlet-*.json"))
                replacement = tmp_path / "concurrent-result"
                replacement.write_text("concurrent artifact\n", encoding="utf-8")
                os.replace(replacement, reservation)
                raise OSError("allocator fstat failed")
            return real_fstat(descriptor)

        monkeypatch.setattr(runner.os, "fstat", fail_allocator_fstat)

        with pytest.raises(OSError, match=r"^allocator fstat failed$"):
            runner.write_serialized_result(
                "unaccepted artifact\n",
                default_directory=tmp_path,
                bound_directory=bound_directory,
            )

    assert all(not path.name.endswith((".json", ".tmp")) for path in tmp_path.iterdir())
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "concurrent artifact\n"


@requires_bound_publication
def test_bound_output_leaf_validation_allows_replaceable_entry_types(
    tmp_path: Path,
) -> None:
    regular = tmp_path / "regular.json"
    regular.write_text("regular\n", encoding="utf-8")
    hard_link = tmp_path / "hard-link.json"
    try:
        hard_link.hardlink_to(regular)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a file hard link: {error}")
    directory_target = tmp_path / "directory-target"
    directory_target.mkdir()
    directory_symlink = tmp_path / "directory-symlink.json"
    dangling_symlink = tmp_path / "dangling-symlink.json"
    try:
        directory_symlink.symlink_to(directory_target, target_is_directory=True)
        dangling_symlink.symlink_to(tmp_path / "missing-target")
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a symbolic link: {error}")

    with runner.bind_output_directory(tmp_path) as bound_directory:
        for output_name in (
            "missing.json",
            regular.name,
            hard_link.name,
            directory_symlink.name,
            dangling_symlink.name,
        ):
            runner.validate_bound_output_leaf(bound_directory, output_name)
        runner.write_serialized_result(
            "replacement artifact\n",
            hard_link,
            bound_directory=bound_directory,
        )

    assert regular.read_text(encoding="utf-8") == "regular\n"
    assert hard_link.read_text(encoding="utf-8") == "replacement artifact\n"
    assert not hard_link.samefile(regular)


@requires_bound_publication
@pytest.mark.parametrize("entry_kind", ["directory", "fifo"])
def test_bound_output_leaf_validation_rejects_nonreplaceable_entries(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    output = tmp_path / "result.json"
    if entry_kind == "directory":
        output.mkdir()
    else:
        make_fifo = getattr(os, "mkfifo", None)
        if make_fifo is None:
            pytest.skip("platform cannot create a FIFO")
        try:
            make_fifo(output)
        except OSError as error:
            pytest.skip(f"platform cannot create a FIFO: {error}")

    with runner.bind_output_directory(tmp_path) as bound_directory:
        with pytest.raises(
            GauntletSafetyError,
            match=r"^result output must be missing, a regular file, or a symbolic link$",
        ):
            runner.validate_bound_output_leaf(bound_directory, output.name)

    assert output.exists()
    assert not output.is_symlink()
    assert list(tmp_path.iterdir()) == [output]


@requires_bound_publication
@pytest.mark.parametrize("target_kind", ["directory", "dangling"])
def test_bound_publication_replaces_symlink_without_following_target(
    tmp_path: Path,
    target_kind: str,
) -> None:
    output = tmp_path / "result.json"
    if target_kind == "directory":
        target = tmp_path / "target"
        target.mkdir()
        target_is_directory = True
    else:
        target = tmp_path / "missing-target"
        target_is_directory = False
    try:
        output.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a symbolic link: {error}")

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        written_path = runner.write_serialized_result(
            "replacement artifact\n",
            output,
            bound_directory=bound_directory,
        )

    assert written_path == output
    assert not output.is_symlink()
    assert output.read_text(encoding="utf-8") == "replacement artifact\n"
    if target_kind == "directory":
        assert target.is_dir()
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


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


def test_explicit_publication_supports_near_name_max_output_and_cleans_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / _near_name_max_ascii_basename(tmp_path)
    output.write_text("previous artifact\n", encoding="utf-8")
    serialized_result = runner.serialize_result(
        {
            "schema_version": SCHEMA_VERSION,
            "intended_action_digest": "a" * 64,
        }
    )

    written_path = runner.write_serialized_result(serialized_result, output)

    assert written_path == output
    assert output.read_text(encoding="utf-8") == serialized_result
    assert json.loads(output.read_text(encoding="utf-8"))["intended_action_digest"] == "a" * 64
    assert list(tmp_path.iterdir()) == [output]

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(runner.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        runner.write_serialized_result("failed replacement\n", output)

    assert output.read_text(encoding="utf-8") == serialized_result
    assert json.loads(output.read_text(encoding="utf-8"))["intended_action_digest"] == "a" * 64
    assert list(tmp_path.iterdir()) == [output]


@requires_bound_publication
def test_bound_publication_supports_near_name_max_output_and_cleans_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / _near_name_max_ascii_basename(tmp_path)
    output.write_text("previous artifact\n", encoding="utf-8")
    serialized_result = runner.serialize_result(
        {
            "schema_version": SCHEMA_VERSION,
            "intended_action_digest": "b" * 64,
        }
    )

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        written_path = runner.write_serialized_result(
            serialized_result,
            output,
            bound_directory=bound_directory,
        )

        assert written_path == output
        assert output.read_text(encoding="utf-8") == serialized_result
        assert json.loads(output.read_text(encoding="utf-8"))["intended_action_digest"] == "b" * 64
        assert list(tmp_path.iterdir()) == [output]

        real_link = os.link

        def fail_publication_link(source, destination, **kwargs) -> None:
            if str(source).endswith(".tmp"):
                raise OSError("link failed")
            real_link(source, destination, **kwargs)

        monkeypatch.setattr(runner.os, "link", fail_publication_link)
        with pytest.raises(
            GauntletSafetyError,
            match="could not publish the bound result safely",
        ):
            runner.write_serialized_result(
                "failed replacement\n",
                output,
                bound_directory=bound_directory,
            )

    assert output.read_text(encoding="utf-8") == serialized_result
    assert json.loads(output.read_text(encoding="utf-8"))["intended_action_digest"] == "b" * 64
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == serialized_result
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


@requires_bound_publication
@pytest.mark.parametrize("output_state", ("missing", "existing"))
def test_bound_publication_preserves_leaf_created_or_replaced_between_backup_and_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_state: str,
) -> None:
    output = tmp_path / "result.json"
    if output_state == "existing":
        output.write_text("previous artifact\n", encoding="utf-8")
    real_backup = runner._backup_bound_output

    def backup_then_replace(
        bound_directory: runner.BoundOutputDirectory,
        output_name: str,
    ) -> str | None:
        backup_name = real_backup(bound_directory, output_name)
        concurrent = tmp_path / "concurrent.json"
        concurrent.write_text("concurrent artifact\n", encoding="utf-8")
        os.replace(concurrent, output)
        return backup_name

    monkeypatch.setattr(runner, "_backup_bound_output", backup_then_replace)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        with pytest.raises(GauntletSafetyError):
            runner.write_serialized_result(
                "replacement artifact\n",
                output,
                bound_directory=bound_directory,
            )

    assert output.read_text(encoding="utf-8") == "concurrent artifact\n"
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    if output_state == "existing":
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "previous artifact\n"
    else:
        assert backups == []
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


@requires_bound_publication
def test_bound_publication_restores_special_leaf_raced_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    make_fifo = getattr(os, "mkfifo", None)
    if make_fifo is None:
        pytest.skip("platform cannot create a FIFO")
    output = tmp_path / "result.json"
    output.write_text("previous artifact\n", encoding="utf-8")
    real_backup = runner._backup_bound_output

    def replace_with_fifo_then_backup(
        bound_directory: runner.BoundOutputDirectory,
        output_name: str,
    ) -> str | None:
        output.unlink()
        try:
            make_fifo(output)
        except OSError as error:
            pytest.skip(f"platform cannot create a FIFO: {error}")
        return real_backup(bound_directory, output_name)

    monkeypatch.setattr(runner, "_backup_bound_output", replace_with_fifo_then_backup)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        with pytest.raises(
            GauntletSafetyError,
            match=r"^result output changed to a nonreplaceable entry during publication$",
        ):
            runner.write_serialized_result(
                "replacement artifact\n",
                output,
                bound_directory=bound_directory,
            )

    assert stat.S_ISFIFO(output.lstat().st_mode)
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    assert len(backups) == 1
    assert stat.S_ISFIFO(backups[0].lstat().st_mode)
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


@requires_bound_publication
@pytest.mark.parametrize("output_state", ("missing", "existing"))
def test_bound_explicit_publication_uses_descriptor_relative_no_clobber_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_state: str,
) -> None:
    output = tmp_path / "result.json"
    if output_state == "existing":
        output.write_text("previous artifact\n", encoding="utf-8")
    real_rename = os.rename
    real_link = os.link
    rename_calls: list[tuple[str, str, int | None, int | None]] = []
    link_calls: list[tuple[str, str, int | None, int | None, bool]] = []

    def record_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        rename_calls.append((source, destination, src_dir_fd, dst_dir_fd))
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def record_link(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        link_calls.append((source, destination, src_dir_fd, dst_dir_fd, follow_symlinks))
        real_link(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr(runner.os, "rename", record_rename)
    monkeypatch.setattr(runner.os, "link", record_link)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        written_path = runner.write_serialized_result(
            "replacement artifact\n",
            output,
            bound_directory=bound_directory,
        )

    assert written_path == output
    assert output.read_text(encoding="utf-8") == "replacement artifact\n"
    assert len(rename_calls) == 1
    detached_source, detached_destination, rename_source_fd, rename_destination_fd = rename_calls[0]
    assert detached_source == output.name
    assert detached_destination.endswith(".backup")
    assert rename_source_fd == rename_destination_fd
    assert rename_source_fd is not None
    assert len(link_calls) == 1
    staged_source, installed_destination, link_source_fd, link_destination_fd, follow_symlinks = link_calls[0]
    assert staged_source.endswith(".tmp")
    assert installed_destination == output.name
    assert link_source_fd == link_destination_fd == rename_source_fd
    assert follow_symlinks is False
    assert list(tmp_path.iterdir()) == [output]


@requires_bound_publication
@pytest.mark.parametrize("output_state", ("missing", "existing"))
def test_bound_explicit_publication_rolls_back_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_state: str,
) -> None:
    output = tmp_path / "result.json"
    if output_state == "existing":
        output.write_text("previous artifact\n", encoding="utf-8")
    real_link = os.link

    def fail_staging_link(source, destination, **kwargs) -> None:
        if str(source).endswith(".tmp"):
            raise OSError("link failed")
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(runner.os, "link", fail_staging_link)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        with pytest.raises(
            GauntletSafetyError,
            match=r"^could not publish the bound result safely$",
        ):
            runner.write_serialized_result(
                "replacement artifact\n",
                output,
                bound_directory=bound_directory,
            )

    if output_state == "existing":
        assert output.read_text(encoding="utf-8") == "previous artifact\n"
    else:
        assert not output.exists()
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    if output_state == "existing":
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "previous artifact\n"
    else:
        assert backups == []
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


@requires_bound_publication
def test_bound_explicit_rollback_retains_prior_backup_after_restored_leaf_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "result.json"
    output.write_text("previous artifact\n", encoding="utf-8")
    real_link = os.link
    real_bound_file_identity = runner._bound_file_identity
    replaced = False

    def fail_staging_link(source, destination, **kwargs) -> None:
        if str(source).endswith(".tmp"):
            raise OSError("link failed")
        real_link(source, destination, **kwargs)

    def replace_after_restored_identity_read(
        bound_directory: runner.BoundOutputDirectory,
        name: str,
    ) -> tuple[int, int, int] | None:
        nonlocal replaced
        identity = real_bound_file_identity(bound_directory, name)
        if name == output.name and identity is not None and not replaced:
            replacement = tmp_path / "concurrent-result"
            replacement.write_text("concurrent artifact\n", encoding="utf-8")
            os.replace(replacement, output)
            replaced = True
        return identity

    monkeypatch.setattr(runner.os, "link", fail_staging_link)
    monkeypatch.setattr(runner, "_bound_file_identity", replace_after_restored_identity_read)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        with pytest.raises(
            GauntletSafetyError,
            match=r"^could not publish the bound result safely$",
        ):
            runner.write_serialized_result(
                "replacement artifact\n",
                output,
                bound_directory=bound_directory,
            )

    assert replaced is True
    assert output.read_text(encoding="utf-8") == "concurrent artifact\n"
    backups = [path for path in tmp_path.iterdir() if path.name.endswith(".backup")]
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "previous artifact\n"
    assert all(not path.name.endswith(".tmp") for path in tmp_path.iterdir())


@requires_bound_publication
def test_bound_explicit_publication_preserves_output_when_staging_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "result.json"
    output.write_text("previous artifact\n", encoding="utf-8")

    def fail_fsync(_descriptor: int) -> None:
        raise OSError("fsync failed")

    monkeypatch.setattr(runner.os, "fsync", fail_fsync)

    with runner.bind_output_directory(tmp_path) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        with pytest.raises(OSError, match=r"^fsync failed$"):
            runner.write_serialized_result(
                "replacement artifact\n",
                output,
                bound_directory=bound_directory,
            )

    assert output.read_text(encoding="utf-8") == "previous artifact\n"
    assert list(tmp_path.iterdir()) == [output]


@requires_bound_publication
def test_bound_publication_ignores_ancestor_retarget_during_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    safe_directory = tmp_path / "safe"
    protected_directory = tmp_path / "protected"
    safe_directory.mkdir()
    protected_directory.mkdir()
    output_alias = tmp_path / "current"
    try:
        output_alias.symlink_to(safe_directory, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"platform cannot create a directory symbolic link: {error}")
    output = safe_directory / "result.json"
    real_link = os.link

    def retarget_then_replace(source, destination, **kwargs):
        output_alias.unlink()
        output_alias.symlink_to(protected_directory, target_is_directory=True)
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(runner.os, "link", retarget_then_replace)

    with runner.bind_output_directory(safe_directory) as bound_directory:
        written_path = runner.write_serialized_result(
            "bound artifact\n",
            output,
            bound_directory=bound_directory,
        )

    assert written_path == output
    assert output.read_text(encoding="utf-8") == "bound artifact\n"
    assert list(protected_directory.iterdir()) == []
    assert not (output_alias / "result.json").exists()


@requires_bound_publication
def test_bound_publication_rejects_repeated_retarget_matching_fd_identity(
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "candidate"
    unsafe_results = candidate_root / "unsafe" / "results"
    unsafe_results.mkdir(parents=True)
    outside_container = tmp_path / "outside"
    outside_results = outside_container / "results"
    outside_results.mkdir(parents=True)
    saved_container = tmp_path / "saved-outside"
    outside_container.rename(saved_container)
    outside_container.symlink_to(candidate_root / "unsafe", target_is_directory=True)
    descriptor = os.open(outside_results, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    bound_directory = runner.BoundOutputDirectory(
        outside_results,
        descriptor,
        (candidate_root,),
    )
    try:
        with pytest.raises(
            GauntletSafetyError,
            match="changed before publication",
        ):
            runner.write_serialized_result(
                "unsafe artifact\n",
                outside_results / "result.json",
                bound_directory=bound_directory,
            )
    finally:
        os.close(descriptor)

    assert list((saved_container / "results").iterdir()) == []
    assert list(unsafe_results.iterdir()) == []


@requires_bound_publication
@pytest.mark.parametrize("output_state", ["omitted", "missing", "existing"])
def test_bound_publication_rolls_back_directory_move_after_descriptor_relative_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_state: str,
) -> None:
    candidate_root = tmp_path / "candidate"
    _initialize_gauntlet_test_repository(candidate_root)
    identity_before = capture_repository_identity(candidate_root)
    assert identity_before.clean is True
    outside_results = tmp_path / "outside-results"
    outside_results.mkdir()
    moved_results = candidate_root / "published-results"
    output = outside_results / "result.json"
    if output_state == "existing":
        output.write_text("previous artifact\n", encoding="utf-8")
    real_rename = os.rename
    real_link = os.link

    def publish_then_move_directory(source, destination, **kwargs):
        real_rename(source, destination, **kwargs)
        if output_state == "omitted" and str(source).endswith(".tmp"):
            real_rename(outside_results, moved_results)

    def link_then_move_directory(source, destination, **kwargs):
        real_link(source, destination, **kwargs)
        if str(source).endswith(".tmp"):
            real_rename(outside_results, moved_results)

    monkeypatch.setattr(runner.os, "rename", publish_then_move_directory)
    monkeypatch.setattr(runner.os, "link", link_then_move_directory)

    with runner.bind_output_directory(
        outside_results,
        protected_roots=(candidate_root,),
    ) as bound_directory:
        with pytest.raises(
            GauntletSafetyError,
            match=r"^validated result directory changed during publication$",
        ):
            if output_state == "omitted":
                runner.write_serialized_result(
                    "unaccepted artifact\n",
                    default_directory=outside_results,
                    bound_directory=bound_directory,
                )
            else:
                runner.validate_bound_output_leaf(bound_directory, output.name)
                runner.write_serialized_result(
                    "unaccepted artifact\n",
                    output,
                    bound_directory=bound_directory,
                )

    assert not outside_results.exists()
    if output_state in {"omitted", "missing"}:
        assert list(moved_results.iterdir()) == []
        assert capture_repository_identity(candidate_root) == identity_before
    else:
        moved_output = moved_results / output.name
        assert moved_output.read_text(encoding="utf-8") == "previous artifact\n"
        backups = [path for path in moved_results.iterdir() if path.name.endswith(".backup")]
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "previous artifact\n"
        assert all(not path.name.endswith(".tmp") for path in moved_results.iterdir())
        identity_after = capture_repository_identity(candidate_root)
        assert identity_after.commit == identity_before.commit
        assert identity_after.clean is False
        assert identity_after.diff_sha256 != identity_before.diff_sha256


@requires_bound_publication
@pytest.mark.parametrize("output_state", ["missing", "existing"])
def test_bound_publication_preserves_concurrent_replacement_after_directory_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_state: str,
) -> None:
    candidate_root = tmp_path / "candidate"
    _initialize_gauntlet_test_repository(candidate_root)
    outside_results = tmp_path / "outside-results"
    outside_results.mkdir()
    moved_results = candidate_root / "published-results"
    output = outside_results / "result.json"
    if output_state == "existing":
        output.write_text("previous artifact\n", encoding="utf-8")
    real_rename = os.rename
    real_link = os.link

    def publish_move_and_replace(source, destination, **kwargs):
        real_link(source, destination, **kwargs)
        if not str(source).endswith(".tmp"):
            return
        real_rename(outside_results, moved_results)
        concurrent_output = moved_results / "concurrent-result"
        concurrent_output.write_text("concurrent artifact\n", encoding="utf-8")
        real_rename(concurrent_output, moved_results / output.name)

    monkeypatch.setattr(runner.os, "link", publish_move_and_replace)

    with runner.bind_output_directory(
        outside_results,
        protected_roots=(candidate_root,),
    ) as bound_directory:
        runner.validate_bound_output_leaf(bound_directory, output.name)
        with pytest.raises(
            GauntletSafetyError,
            match=r"^bound result changed before rollback$",
        ):
            runner.write_serialized_result(
                "unaccepted artifact\n",
                output,
                bound_directory=bound_directory,
            )

    moved_output = moved_results / output.name
    assert moved_output.read_text(encoding="utf-8") == "concurrent artifact\n"
    backups = [path for path in moved_results.iterdir() if path.name.endswith(".backup")]
    if output_state == "existing":
        assert sorted(path.read_text(encoding="utf-8") for path in backups) == [
            "concurrent artifact\n",
            "previous artifact\n",
        ]
    else:
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == "concurrent artifact\n"
    assert all(not path.name.endswith(".tmp") for path in moved_results.iterdir())


def test_bound_publication_fails_closed_without_descriptor_relative_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "_supports_bound_publication", lambda: False)

    with pytest.raises(
        GauntletSafetyError,
        match="safe descriptor-relative result publication is unavailable",
    ):
        with runner.bind_output_directory(tmp_path):
            pytest.fail("unsupported publication must not yield a directory")

    assert list(tmp_path.iterdir()) == []


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
