"""Run and compare contemporaneous control/candidate gauntlet evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal, TypedDict, cast

from benchmarks.gauntlet.baseline import (
    ENVIRONMENT_KEYS,
    GateResult,
    ProfileQualityBar,
    QualityBar,
    compare_result,
    load_quality_bar,
)
from benchmarks.gauntlet.identity import (
    RepositoryIdentity,
    capture_repository_identity,
    require_same_identity,
)
from benchmarks.gauntlet.paired_evidence import (
    PairedEvidenceError,
    sanitize_child_result,
)

PAIRED_SCHEMA_NAME = "qbitunregistered.gauntlet.paired-result"
PAIRED_SCHEMA_VERSION = 2
PAIRING_VERSION = "2.0.0"
PAIRED_ORDER: tuple[Literal["control", "candidate"], ...] = (
    "control",
    "candidate",
    "candidate",
    "control",
    "candidate",
    "control",
    "control",
    "candidate",
)
PAIRED_BLOCKS = ((0, 1, 2, 3), (4, 5, 6, 7))
ADJACENT_PAIRS = ((0, 1), (3, 2), (5, 4), (6, 7))
REQUIRED_CHILD_GATES = {
    "identity",
    "measurement_policy",
    "safety",
    "result",
    "api",
    "variance",
}
MAX_CHILD_ARTIFACT_BYTES = 8 * 1024 * 1024
CHILD_TIMEOUT_SECONDS = 3_600
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")


class PairedGauntletError(RuntimeError):
    """Raised when paired evaluation cannot produce trustworthy evidence."""


class PairingComparison(TypedDict):
    """Fail-closed comparison of contemporaneous paired measurements."""

    overall: Literal["pass", "fail"]
    gates: dict[str, GateResult]
    runtime_pair_ratios: list[float]
    memory_pair_ratios: list[float]
    runtime_control_fraction: float | None
    memory_control_fraction: float | None
    role_runtime_relative_ranges: dict[str, float]
    block_runtime_control_fractions: list[float]
    block_memory_control_fractions: list[float]
    role_memory_relative_ranges: dict[str, float]


class PairedRun(TypedDict):
    """One sanitized child result and its fixed ABBA position."""

    position: int
    role: Literal["control", "candidate"]
    result: dict[str, object]


class PairedResult(TypedDict):
    """One versioned paired artifact containing every underlying sample."""

    schema: str
    schema_version: int
    pairing_version: str
    evaluator_digest: str
    quality_bar_digest: str
    dependency_digest: str
    order: list[str]
    profile: str
    seed: int
    identities: dict[str, dict[str, object]]
    thresholds: dict[str, float]
    runs: list[PairedRun]
    comparison: PairingComparison


def _gate(
    status: Literal["pass", "fail", "non_comparable"],
    detail: str,
    *,
    actual: int | float | None = None,
    baseline: int | float | None = None,
    target: int | float | None = None,
) -> GateResult:
    return {
        "status": status,
        "detail": detail,
        "actual": actual,
        "baseline": baseline,
        "target": target,
    }


def _failed_comparison(detail: str) -> PairingComparison:
    gates = {
        name: _gate("non_comparable" if name in {"structure", "evaluator", "identity", "environment"} else "fail", detail)
        for name in (
            "structure",
            "evaluator",
            "identity",
            "environment",
            "oracles",
            "child_gates",
            "paired_drift",
            "memory_drift",
            "runtime",
            "memory",
        )
    }
    return {
        "overall": "fail",
        "gates": gates,
        "runtime_pair_ratios": [],
        "memory_pair_ratios": [],
        "runtime_control_fraction": None,
        "memory_control_fraction": None,
        "role_runtime_relative_ranges": {},
        "block_runtime_control_fractions": [],
        "block_memory_control_fractions": [],
        "role_memory_relative_ranges": {},
    }


def _complete_clean_identity(result: Mapping[str, object]) -> tuple[str, bool, str] | None:
    commit = result.get("commit")
    candidate_state = result.get("candidate_state")
    if not isinstance(candidate_state, dict):
        return None
    clean = candidate_state.get("clean")
    diff_sha256 = candidate_state.get("diff_sha256")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
        or clean is not True
        or not isinstance(diff_sha256, str)
        or len(diff_sha256) != 64
        or any(character not in "0123456789abcdef" for character in diff_sha256)
        or result.get("identity_verified") is not True
    ):
        return None
    return commit, clean, diff_sha256


def _finite_positive(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    resolved = float(value)
    return resolved if math.isfinite(resolved) and resolved > 0 else None


def _validated_child_measurements(
    result: Mapping[str, object],
    *,
    expected_samples: int,
) -> tuple[list[float], float, int] | None:
    raw_samples = result.get("sample_runtime_seconds")
    median = _finite_positive(result.get("median_runtime_seconds"))
    peak_memory = result.get("peak_memory_bytes")
    if (
        not isinstance(raw_samples, list)
        or len(raw_samples) != expected_samples
        or median is None
        or isinstance(peak_memory, bool)
        or not isinstance(peak_memory, int)
        or peak_memory < 1
    ):
        return None
    samples = [_finite_positive(sample) for sample in raw_samples]
    if any(sample is None for sample in samples):
        return None
    resolved_samples = cast(list[float], samples)
    if not math.isclose(statistics.median(resolved_samples), median, rel_tol=1e-12, abs_tol=1e-12):
        return None
    return resolved_samples, median, peak_memory


def _relative_range(values: Sequence[float]) -> float:
    median = statistics.median(values)
    return (max(values) - min(values)) / median


def _profile_quality_bar(quality_bar: QualityBar, profile_name: object) -> ProfileQualityBar | None:
    if not isinstance(profile_name, str):
        return None
    return quality_bar.profiles.get(profile_name)


def compare_paired_results(  # noqa: C901
    runs: Sequence[Mapping[str, object]],
    quality_bar: QualityBar,
) -> PairingComparison:
    """Compare a symmetric crossover, rejecting incomplete or inconsistent evidence."""
    if len(runs) != len(PAIRED_ORDER):
        return _failed_comparison("paired evidence must contain exactly eight crossover runs")
    roles = [run.get("role") for run in runs]
    positions = [run.get("position") for run in runs]
    if roles != list(PAIRED_ORDER) or positions != list(range(len(PAIRED_ORDER))):
        return _failed_comparison("paired evidence order is not the locked ABBA+BAAB sequence")
    results: list[dict[str, object]] = []
    for run in runs:
        try:
            results.append(sanitize_child_result(run.get("result"), quality_bar))
        except PairedEvidenceError as error:
            return _failed_comparison(str(error))

    reference = results[0]
    profile = _profile_quality_bar(quality_bar, reference.get("profile"))
    if profile is None:
        return _failed_comparison("paired evidence uses an unknown profile")
    expected_samples_value = quality_bar.measurement_policy.get("timed_samples")
    if isinstance(expected_samples_value, bool) or not isinstance(expected_samples_value, int):
        return _failed_comparison("quality bar timed sample count is malformed")

    gates: dict[str, GateResult] = {
        "structure": _gate(
            "pass",
            "eight ordered ABBA+BAAB runs retain every child artifact",
        ),
    }
    evaluator_fields = ("schema", "schema_version", "evaluator_version", "scope")
    expected_evaluator = (
        quality_bar.result_schema,
        quality_bar.evaluator_schema_version,
        quality_bar.evaluator_version,
        quality_bar.scope,
    )
    actual_evaluators = [tuple(result.get(field) for field in evaluator_fields) for result in results]
    if any(actual != expected_evaluator for actual in actual_evaluators):
        gates["evaluator"] = _gate("non_comparable", "child evaluator schema, version, or scope differs")
    else:
        gates["evaluator"] = _gate("pass", "child evaluator schema, version, and scope are identical")

    identities = [_complete_clean_identity(result) for result in results]
    control_identities = [identity for role, identity in zip(PAIRED_ORDER, identities, strict=True) if role == "control"]
    candidate_identities = [identity for role, identity in zip(PAIRED_ORDER, identities, strict=True) if role == "candidate"]
    role_identities_stable = len(set(control_identities)) == 1 and len(set(candidate_identities)) == 1
    if any(identity is None for identity in identities) or not role_identities_stable:
        gates["identity"] = _gate("non_comparable", "control or candidate identity is dirty, incomplete, or unstable")
    else:
        gates["identity"] = _gate(
            "pass",
            "both clean repository identities remain stable across the crossover",
        )

    environments = [result.get("environment") for result in results]
    environment = environments[0]
    environment_valid = (
        isinstance(environment, dict)
        and set(environment) == ENVIRONMENT_KEYS
        and all(isinstance(value, str) and value and value != "unknown" for value in environment.values())
    )
    if not environment_valid or any(item != environment for item in environments[1:]):
        gates["environment"] = _gate("non_comparable", "child environments are incomplete or differ")
    else:
        gates["environment"] = _gate("pass", "all child environments are complete and identical")

    oracle_fields = (
        "profile",
        "seed",
        "fixture_manifest_digest",
        "intended_action_digest",
        "reconciliation",
        "candidate_counts",
        "workload",
        "measurement_policy",
    )
    reference_oracles = tuple(reference.get(field) for field in oracle_fields)
    if any(tuple(result.get(field) for field in oracle_fields) != reference_oracles for result in results[1:]):
        gates["oracles"] = _gate("fail", "profile, seed, workload, policy, or safety oracles differ")
    else:
        gates["oracles"] = _gate("pass", "profile, seed, workload, policy, and safety oracles are identical")

    child_reports = [compare_result(result, quality_bar) for result in results]
    child_gates_pass = all(
        report["gates"][gate_name]["status"] == "pass" for report in child_reports for gate_name in REQUIRED_CHILD_GATES
    )
    if child_gates_pass:
        gates["child_gates"] = _gate("pass", "every child correctness, API, safety, and variance gate passes")
    else:
        gates["child_gates"] = _gate("fail", "at least one child correctness, API, safety, or variance gate failed")

    measurements = [_validated_child_measurements(result, expected_samples=expected_samples_value) for result in results]
    if any(measurement is None for measurement in measurements):
        invalid = _failed_comparison("child runtime or memory measurements are malformed")
        invalid["gates"].update(gates)
        return invalid
    resolved = cast(list[tuple[list[float], float, int]], measurements)
    runtime_pair_ratios = [
        resolved[candidate_index][1] / resolved[control_index][1] for control_index, candidate_index in ADJACENT_PAIRS
    ]
    memory_pair_ratios = [
        resolved[candidate_index][2] / resolved[control_index][2] for control_index, candidate_index in ADJACENT_PAIRS
    ]
    control_indexes = [index for index, role in enumerate(PAIRED_ORDER) if role == "control"]
    candidate_indexes = [index for index, role in enumerate(PAIRED_ORDER) if role == "candidate"]
    control_samples = [sample for index in control_indexes for sample in resolved[index][0]]
    candidate_samples = [sample for index in candidate_indexes for sample in resolved[index][0]]
    runtime_ratio = statistics.median(candidate_samples) / statistics.median(control_samples)
    memory_ratio = statistics.median([resolved[index][2] for index in candidate_indexes]) / statistics.median(
        [resolved[index][2] for index in control_indexes]
    )
    block_runtime_control_fractions: list[float] = []
    block_memory_control_fractions: list[float] = []
    for block in PAIRED_BLOCKS:
        block_control_indexes = [index for index in block if PAIRED_ORDER[index] == "control"]
        block_candidate_indexes = [index for index in block if PAIRED_ORDER[index] == "candidate"]
        block_control_samples = [sample for index in block_control_indexes for sample in resolved[index][0]]
        block_candidate_samples = [sample for index in block_candidate_indexes for sample in resolved[index][0]]
        block_runtime_control_fractions.append(
            statistics.median(block_candidate_samples) / statistics.median(block_control_samples)
        )
        block_memory_control_fractions.append(
            statistics.median([resolved[index][2] for index in block_candidate_indexes])
            / statistics.median([resolved[index][2] for index in block_control_indexes])
        )
    role_runtime_relative_ranges = {
        "control": _relative_range([resolved[index][1] for index in control_indexes]),
        "candidate": _relative_range([resolved[index][1] for index in candidate_indexes]),
    }
    runtime_drift = max(role_runtime_relative_ranges.values())
    if runtime_drift <= profile.relative_range_max:
        gates["paired_drift"] = _gate(
            "pass",
            "both roles meet the locked ABBA run-median relative-range limit",
            actual=runtime_drift,
            target=profile.relative_range_max,
        )
    else:
        gates["paired_drift"] = _gate(
            "fail",
            "a role exceeds the locked ABBA run-median relative-range limit",
            actual=runtime_drift,
            target=profile.relative_range_max,
        )
    role_memory_relative_ranges = {
        "control": _relative_range([float(resolved[index][2]) for index in control_indexes]),
        "candidate": _relative_range([float(resolved[index][2]) for index in candidate_indexes]),
    }
    memory_drift = max(role_memory_relative_ranges.values())
    gates["memory_drift"] = _gate(
        "pass" if memory_drift <= profile.relative_range_max else "fail",
        (
            "both roles meet the locked memory-peak relative-range limit"
            if memory_drift <= profile.relative_range_max
            else "a role exceeds the locked memory-peak relative-range limit"
        ),
        actual=memory_drift,
        target=profile.relative_range_max,
    )
    runtime_passes = runtime_ratio <= profile.runtime_baseline_fraction_max and all(
        ratio <= profile.runtime_baseline_fraction_max for ratio in block_runtime_control_fractions
    )
    gates["runtime"] = _gate(
        "pass" if runtime_passes else "fail",
        (
            "pooled and per-block runtime meet the independent target"
            if runtime_passes
            else "pooled or per-block runtime exceeds the independent target"
        ),
        actual=max(runtime_ratio, *block_runtime_control_fractions),
        baseline=1.0,
        target=profile.runtime_baseline_fraction_max,
    )
    memory_passes = memory_ratio <= profile.peak_memory_baseline_fraction_max and all(
        ratio <= profile.peak_memory_baseline_fraction_max for ratio in block_memory_control_fractions
    )
    gates["memory"] = _gate(
        "pass" if memory_passes else "fail",
        (
            "pooled and per-block memory meet the independent target"
            if memory_passes
            else "pooled or per-block memory exceeds the independent target"
        ),
        actual=max(memory_ratio, *block_memory_control_fractions),
        baseline=1.0,
        target=profile.peak_memory_baseline_fraction_max,
    )
    overall: Literal["pass", "fail"] = "pass" if all(gate["status"] == "pass" for gate in gates.values()) else "fail"
    return {
        "overall": overall,
        "gates": gates,
        "runtime_pair_ratios": runtime_pair_ratios,
        "memory_pair_ratios": memory_pair_ratios,
        "runtime_control_fraction": runtime_ratio,
        "memory_control_fraction": memory_ratio,
        "role_runtime_relative_ranges": role_runtime_relative_ranges,
        "block_runtime_control_fractions": block_runtime_control_fractions,
        "block_memory_control_fractions": block_memory_control_fractions,
        "role_memory_relative_ranges": role_memory_relative_ranges,
    }


def _read_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    description: str,
) -> bytes:
    """Read one bounded regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if not isinstance(nofollow, int) or not nofollow:
        raise PairedGauntletError(f"cannot read {description} without no-follow support")
    flags |= nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise PairedGauntletError(f"could not open {description} safely") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size < 1 or before.st_size > maximum_bytes:
            raise PairedGauntletError(f"{description} is not a bounded regular file")
        with os.fdopen(descriptor, "rb", closefd=False) as file_handle:
            payload = file_handle.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
    except OSError as error:
        raise PairedGauntletError(f"could not read {description} safely") from error
    finally:
        os.close(descriptor)
    stable_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if not stable_identity or len(payload) != before.st_size or len(payload) > maximum_bytes:
        raise PairedGauntletError(f"{description} changed or exceeded its size limit")
    return payload


def _file_digest(path: Path, description: str) -> str:
    return hashlib.sha256(
        _read_regular_file(
            path,
            maximum_bytes=MAX_CHILD_ARTIFACT_BYTES,
            description=description,
        )
    ).hexdigest()


def _named_files_digest(repository_root: Path, names: Sequence[str]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            _read_regular_file(
                repository_root / name,
                maximum_bytes=MAX_CHILD_ARTIFACT_BYTES,
                description="repository identity input",
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _evaluator_digest(repository_root: Path) -> str:
    """Hash evaluator sources and its quality bar without recording host paths."""
    evaluator_root = repository_root / "benchmarks" / "gauntlet"
    files = sorted(
        path
        for path in evaluator_root.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "quality-bar.toml")
    )
    if not files:
        raise PairedGauntletError("repository does not contain the gauntlet evaluator")
    digest = hashlib.sha256()
    for path in files:
        relative = path.relative_to(repository_root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            _read_regular_file(
                path,
                maximum_bytes=MAX_CHILD_ARTIFACT_BYTES,
                description="evaluator source",
            )
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _require_clean_identity(repository_root: Path) -> RepositoryIdentity:
    identity = capture_repository_identity(repository_root)
    if not identity.known or identity.clean is not True:
        raise PairedGauntletError("paired repositories must have clean, complete Git identities")
    return identity


def _run_child(
    repository_root: Path,
    *,
    profile: str,
    seed: int,
    samples: int,
    output: Path,
) -> dict[str, object]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTHON")}
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    command = [
        sys.executable,
        "-s",
        "-m",
        "benchmarks.gauntlet",
        "--profile",
        profile,
        "--seed",
        str(seed),
        "--samples",
        str(samples),
        "--output",
        str(output),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=repository_root,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PairedGauntletError("paired child evaluation could not run") from error
    if completed.returncode != 0:
        raise PairedGauntletError("paired child evaluation failed")
    try:
        result = json.loads(
            _read_regular_file(
                output,
                maximum_bytes=MAX_CHILD_ARTIFACT_BYTES,
                description="paired child artifact",
            )
        )
    except json.JSONDecodeError as error:
        raise PairedGauntletError("paired child artifact could not be read") from error
    if not isinstance(result, dict):
        raise PairedGauntletError("paired child artifact is malformed")
    return cast(dict[str, object], result)


def _identity_payload(identity: RepositoryIdentity) -> dict[str, object]:
    return {
        "commit": identity.commit,
        "clean": identity.clean,
        "diff_sha256": identity.diff_sha256,
    }


def _identity_tuple(identity: RepositoryIdentity) -> tuple[str, bool, str]:
    if identity.clean is not True:
        raise PairedGauntletError("paired repository identity is not clean")
    return identity.commit, True, identity.diff_sha256


def run_paired_gauntlet(
    control_root: Path,
    candidate_root: Path,
    *,
    orchestrator_root: Path,
    profile: str,
    seed: int,
    samples: int,
) -> PairedResult:
    """Run clean worktrees under the invoking checkout's canonical evaluator."""
    orchestrator_root = orchestrator_root.expanduser().resolve()
    control_root = control_root.expanduser().resolve()
    candidate_root = candidate_root.expanduser().resolve()
    if control_root == candidate_root:
        raise PairedGauntletError("paired control and candidate must be isolated worktrees")
    orchestrator_identity = _require_clean_identity(orchestrator_root)
    control_identity = _require_clean_identity(control_root)
    candidate_identity = _require_clean_identity(candidate_root)
    canonical_quality_bar = orchestrator_root / "benchmarks" / "gauntlet" / "quality-bar.toml"
    quality_bar = load_quality_bar(canonical_quality_bar)
    quality_bar_digest = _file_digest(
        canonical_quality_bar,
        "canonical quality bar",
    )
    orchestrator_digest = _evaluator_digest(orchestrator_root)
    control_digest = _evaluator_digest(control_root)
    candidate_digest = _evaluator_digest(candidate_root)
    if len({orchestrator_digest, control_digest, candidate_digest}) != 1:
        raise PairedGauntletError("orchestrator and paired worktrees do not contain the identical evaluator")
    orchestrator_dependency_digest = _named_files_digest(
        orchestrator_root,
        DEPENDENCY_FILES,
    )
    control_dependency_digest = _named_files_digest(control_root, DEPENDENCY_FILES)
    candidate_dependency_digest = _named_files_digest(candidate_root, DEPENDENCY_FILES)
    if (
        len(
            {
                orchestrator_dependency_digest,
                control_dependency_digest,
                candidate_dependency_digest,
            }
        )
        != 1
    ):
        raise PairedGauntletError("orchestrator and paired worktrees do not have identical dependency locks")
    if profile not in quality_bar.profiles:
        raise PairedGauntletError("paired profile is not present in the quality bar")

    roots = {"control": control_root, "candidate": candidate_root}
    paired_runs: list[PairedRun] = []
    with tempfile.TemporaryDirectory(prefix="qbitunregistered-gauntlet-paired-") as temporary_root:
        for position, role in enumerate(PAIRED_ORDER):
            output = Path(temporary_root) / f"run-{position}.json"
            try:
                result = sanitize_child_result(
                    _run_child(
                        roots[role],
                        profile=profile,
                        seed=seed,
                        samples=samples,
                        output=output,
                    ),
                    quality_bar,
                )
            except PairedEvidenceError as error:
                raise PairedGauntletError("paired child evidence failed strict validation") from error
            paired_runs.append(
                {
                    "position": position,
                    "role": role,
                    "result": result,
                }
            )
    expected_identities = {
        "control": _identity_tuple(control_identity),
        "candidate": _identity_tuple(candidate_identity),
    }
    if any(_complete_clean_identity(run["result"]) != expected_identities[run["role"]] for run in paired_runs):
        raise PairedGauntletError("paired child identity does not match its requested worktree")
    require_same_identity(
        orchestrator_identity,
        capture_repository_identity(orchestrator_root),
    )
    require_same_identity(control_identity, capture_repository_identity(control_root))
    require_same_identity(candidate_identity, capture_repository_identity(candidate_root))
    if (
        _evaluator_digest(orchestrator_root) != orchestrator_digest
        or _evaluator_digest(control_root) != control_digest
        or _evaluator_digest(candidate_root) != candidate_digest
        or _named_files_digest(orchestrator_root, DEPENDENCY_FILES) != orchestrator_dependency_digest
        or _named_files_digest(control_root, DEPENDENCY_FILES) != control_dependency_digest
        or _named_files_digest(candidate_root, DEPENDENCY_FILES) != candidate_dependency_digest
        or _file_digest(canonical_quality_bar, "canonical quality bar") != quality_bar_digest
    ):
        raise PairedGauntletError("evaluator, quality bar, or dependency identity changed during execution")
    comparison = compare_paired_results(paired_runs, quality_bar)
    profile_bar = quality_bar.profiles[profile]
    return {
        "schema": PAIRED_SCHEMA_NAME,
        "schema_version": PAIRED_SCHEMA_VERSION,
        "pairing_version": PAIRING_VERSION,
        "evaluator_digest": orchestrator_digest,
        "quality_bar_digest": quality_bar_digest,
        "dependency_digest": orchestrator_dependency_digest,
        "order": list(PAIRED_ORDER),
        "profile": profile,
        "seed": seed,
        "identities": {
            "orchestrator": _identity_payload(orchestrator_identity),
            "control": _identity_payload(control_identity),
            "candidate": _identity_payload(candidate_identity),
        },
        "thresholds": {
            "runtime_control_fraction_max": profile_bar.runtime_baseline_fraction_max,
            "memory_control_fraction_max": profile_bar.peak_memory_baseline_fraction_max,
            "paired_runtime_relative_range_max": profile_bar.relative_range_max,
            "paired_memory_relative_range_max": profile_bar.relative_range_max,
        },
        "runs": paired_runs,
        "comparison": comparison,
    }


__all__ = [
    "PAIRED_ORDER",
    "PAIRED_SCHEMA_NAME",
    "PAIRED_SCHEMA_VERSION",
    "PAIRING_VERSION",
    "PairedGauntletError",
    "compare_paired_results",
    "run_paired_gauntlet",
]
