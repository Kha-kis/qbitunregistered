"""Run and compare contemporaneous control/candidate gauntlet evaluations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from typing import Literal, TypedDict, cast

from benchmarks.gauntlet.baseline import (
    ENVIRONMENT_KEYS,
    GateResult,
    ProfileQualityBar,
    QualityBar,
    QualityBarError,
    compare_result,
    load_quality_bar_bytes,
)
from benchmarks.gauntlet.identity import (
    RepositoryIdentity,
    RepositoryIdentityError,
    capture_repository_identity,
    require_same_identity,
)
from benchmarks.gauntlet.import_bootstrap import (
    DEPENDENCY_DIGEST_ARGUMENT,
    EXPECTED_REPOSITORY_COMMIT_ARGUMENT,
    DependencyEnvironmentError,
    ProtectedPackageTreeError,
    dependency_environment_digest,
    verified_import_bootstrap_source,
)
from benchmarks.gauntlet.paired_evidence import (
    PairedEvidenceError,
    sanitize_child_result,
)
from benchmarks.gauntlet.runner import DEFAULT_SAMPLES

PAIRED_SCHEMA_NAME = "qbitunregistered.gauntlet.paired-result"
PAIRED_SCHEMA_VERSION = 2
PAIRING_VERSION = "2.1.0"
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
MAX_CHILD_STDERR_BYTES = 4 * 1024
CHILD_TIMEOUT_SECONDS = 3_600
CHILD_STDERR_DRAIN_TIMEOUT_SECONDS = 5
DEPENDENCY_FILES = ("pyproject.toml", "uv.lock")
PROTECTED_PACKAGE_NAMES = ("benchmarks", "qbitunregistered")
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})
IMPORTABLE_EXTENSION_ERROR = "repository contains an importable native extension in a protected package tree"
REDIRECTING_PACKAGE_ENTRY_ERROR = "repository contains a redirecting entry in a protected package tree"
IGNORED_PYTHON_SOURCE_ERROR = "repository contains an ignored Python source in a protected package tree"
NONCANONICAL_INDEX_INPUT_ERROR = "repository contains hidden or noncanonical evaluator inputs"
ISOLATED_PARENT_CACHE_ENV = "QBITUNREGISTERED_GAUNTLET_PARENT_PYCACHE"
_QUALITY_BAR_RELATIVE_PATH = "benchmarks/gauntlet/quality-bar.toml"
_REGULAR_BLOB_MODES = frozenset({b"100644", b"100755"})
_QUALITY_BAR_VERIFICATION_ERROR = "paired canonical quality bar could not be verified"
_SECRET_ASSIGNMENT_PATTERN = re.compile(r"(?i)\b(api[\s_-]?key|password|passwd|token|secret)\b\s*[:=]\s*[^\r\n]*")
_SECRET_JSON_PATTERN = re.compile(r"""(?ix)
    (
        ["'](?:api[\s_-]?key|password|passwd|token|secret)["']
        \s*:\s*
    )
    (?:
        "(?:\\.|[^"\\\r\n])*"
        |
        '(?:\\.|[^'\\\r\n])*'
        |
        [^,}\s]+
    )
    """)
_SENSITIVE_HEADER_PATTERN = re.compile(r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*$")
_URL_USERINFO_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_QUOTED_ABSOLUTE_PATH_PATTERN = re.compile(r"""(?x)
    (?P<quote>["'])
    (?:
        /
        |
        [a-zA-Z]:[\\/]
        |
        \\\\
    )
    [^"'\r\n]+
    (?P=quote)
    """)
_UNC_PATH_PATTERN = re.compile(r"(?<![\w])\\\\[^\r\n,;)]*")
_WINDOWS_ABSOLUTE_PATH_PATTERN = re.compile(r"(?i)(?<![\w])[a-z]:[\\/][^\s:;,)]*")
_POSIX_ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![\w])/(?:[^/\s]+/)*[^/\s:;,)]*")
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


@dataclass(frozen=True, slots=True)
class _VerifiedQualityBarSource:
    """One quality-bar blob bound to an exact revision and stage-0 index."""

    revision: str
    mode: str
    oid: str
    source_bytes: bytes


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
    evaluator_files = sorted(
        path
        for path in evaluator_root.rglob("*")
        if path.is_file() and (path.suffix == ".py" or path.name == "quality-bar.toml")
    )
    if not evaluator_files:
        raise PairedGauntletError("repository does not contain the gauntlet evaluator")
    files = [evaluator_root.parent / "__init__.py", *evaluator_files]
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


def _reject_importable_extensions(repository_root: Path) -> None:
    """Reject native modules that can shadow repository package sources."""
    extension_suffixes = tuple(EXTENSION_SUFFIXES)
    if not extension_suffixes:
        raise PairedGauntletError("Python does not expose native extension suffixes")

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for package_name in PROTECTED_PACKAGE_NAMES:
            if any(os.path.lexists(repository_root / f"{package_name}{suffix}") for suffix in extension_suffixes):
                raise PairedGauntletError(IMPORTABLE_EXTENSION_ERROR)
            package_root = repository_root / package_name
            if not package_root.is_dir():
                continue
            for _current_root, _directory_names, file_names in os.walk(
                package_root,
                topdown=True,
                onerror=raise_walk_error,
                followlinks=False,
            ):
                if any(file_name.endswith(extension_suffixes) for file_name in file_names):
                    raise PairedGauntletError(IMPORTABLE_EXTENSION_ERROR)
    except OSError as error:
        raise PairedGauntletError("could not inspect repository package trees for native extensions") from error


def _entry_is_redirecting(file_stat: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(reparse_point and file_attributes & reparse_point)


def _reject_package_tree_redirects(repository_root: Path) -> None:
    """Reject symlinks and Windows reparse points in protected packages."""

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for package_name in PROTECTED_PACKAGE_NAMES:
            package_root = repository_root / package_name
            try:
                package_stat = os.lstat(package_root)
            except FileNotFoundError:
                continue
            if _entry_is_redirecting(package_stat):
                raise PairedGauntletError(REDIRECTING_PACKAGE_ENTRY_ERROR)
            if not stat.S_ISDIR(package_stat.st_mode):
                continue
            for current_root, directory_names, file_names in os.walk(
                package_root,
                topdown=True,
                onerror=raise_walk_error,
                followlinks=False,
            ):
                for name in (*directory_names, *file_names):
                    if _entry_is_redirecting(os.lstat(Path(current_root) / name)):
                        raise PairedGauntletError(REDIRECTING_PACKAGE_ENTRY_ERROR)
    except OSError as error:
        raise PairedGauntletError("could not inspect repository package trees for redirecting entries") from error


def _reject_unsafe_package_entries_in_roots(repository_roots: Sequence[Path]) -> None:
    for repository_root in repository_roots:
        _reject_package_tree_redirects(repository_root)
        _reject_importable_extensions(repository_root)


def _reject_ignored_python_sources(repository_root: Path) -> None:
    """Reject Python sources hidden from clean-worktree identity checks."""
    try:
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
                "--",
                *PROTECTED_PACKAGE_NAMES,
            ],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PairedGauntletError("could not inspect ignored repository package sources") from error
    if completed.returncode != 0:
        raise PairedGauntletError("could not inspect ignored repository package sources")
    if any(
        Path(os.fsdecode(relative_path)).suffix.casefold() == ".py"
        for relative_path in completed.stdout.split(b"\0")
        if relative_path
    ):
        raise PairedGauntletError(IGNORED_PYTHON_SOURCE_ERROR)


def _is_evaluator_index_input(relative_path: bytes) -> bool:
    return (
        relative_path == b"benchmarks/__init__.py"
        or relative_path in {name.encode("ascii") for name in DEPENDENCY_FILES}
        or (
            relative_path.startswith(b"benchmarks/gauntlet/")
            and (relative_path.endswith(b".py") or relative_path == b"benchmarks/gauntlet/quality-bar.toml")
        )
    )


def _reject_noncanonical_index_inputs(repository_root: Path) -> None:
    """Reject evaluator inputs hidden from ordinary Git identity checks."""
    try:
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "-v",
                "--stage",
                "-z",
                "--",
                "benchmarks/__init__.py",
                "benchmarks/gauntlet",
                *DEPENDENCY_FILES,
            ],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PairedGauntletError(NONCANONICAL_INDEX_INPUT_ERROR) from error
    output = completed.stdout
    if completed.returncode != 0 or len(output) > MAX_CHILD_ARTIFACT_BYTES or not output.endswith(b"\0") or b"\0\0" in output:
        raise PairedGauntletError(NONCANONICAL_INDEX_INPUT_ERROR)

    required = {
        b"benchmarks/__init__.py",
        b"benchmarks/gauntlet/quality-bar.toml",
        *(name.encode("ascii") for name in DEPENDENCY_FILES),
    }
    seen: set[bytes] = set()
    for record in output[:-1].split(b"\0"):
        metadata, separator, relative_path = record.partition(b"\t")
        fields = metadata.split()
        if separator != b"\t":
            raise PairedGauntletError(NONCANONICAL_INDEX_INPUT_ERROR)
        if not _is_evaluator_index_input(relative_path):
            continue
        if len(fields) != 4 or len(fields[0]) != 1:
            raise PairedGauntletError(NONCANONICAL_INDEX_INPUT_ERROR)
        index_status, mode, object_id, stage = fields
        if (
            relative_path in seen
            or index_status != b"H"
            or mode not in {b"100644", b"100755"}
            or stage != b"0"
            or re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", object_id) is None
        ):
            raise PairedGauntletError(NONCANONICAL_INDEX_INPUT_ERROR)
        seen.add(relative_path)
        required.discard(relative_path)
    if required:
        raise PairedGauntletError(NONCANONICAL_INDEX_INPUT_ERROR)


def _require_clean_identity(repository_root: Path) -> RepositoryIdentity:
    identity = capture_repository_identity(repository_root)
    if not identity.known or identity.clean is not True:
        raise PairedGauntletError("paired repositories must have clean, complete Git identities")
    _reject_ignored_python_sources(repository_root)
    _reject_noncanonical_index_inputs(repository_root)
    return identity


def _dependency_import_paths() -> tuple[str, ...]:
    """Return ordinary installed-package paths without editable source roots."""
    dependency_paths: list[str] = []
    for value in sys.path:
        if not value:
            continue
        try:
            path = Path(value)
            resolved_path = path.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise PairedGauntletError("paired dependency import path could not be resolved") from error
        if (
            path.is_absolute()
            and resolved_path.is_dir()
            and SITE_DIRECTORY_NAMES.intersection(part.casefold() for part in resolved_path.parts)
        ):
            resolved_value = str(resolved_path)
            if resolved_value not in dependency_paths:
                dependency_paths.append(resolved_value)
    return tuple(dependency_paths)


def _bound_dependency_digest(lock_digest: str, environment_digest: str) -> str:
    digest = hashlib.sha256()
    digest.update(b"lock\0")
    digest.update(lock_digest.encode("ascii"))
    digest.update(b"\nenvironment\0")
    digest.update(environment_digest.encode("ascii"))
    return digest.hexdigest()


def _current_dependency_environment_digest(dependency_paths: Sequence[str]) -> str:
    try:
        return dependency_environment_digest(dependency_paths)
    except DependencyEnvironmentError as error:
        raise PairedGauntletError("paired dependency environment could not be verified") from error


def _require_dependency_environment(
    dependency_paths: Sequence[str],
    expected_digest: str,
) -> None:
    if _current_dependency_environment_digest(dependency_paths) != expected_digest:
        raise PairedGauntletError("paired dependency environment changed during evaluation")


def _require_unchanged_identity(
    expected: RepositoryIdentity,
    repository_root: Path,
) -> None:
    try:
        actual = capture_repository_identity(repository_root)
        if not actual.known or actual.clean is not True:
            raise RepositoryIdentityError("repository identity is incomplete")
        require_same_identity(
            expected,
            actual,
        )
    except RepositoryIdentityError as error:
        raise PairedGauntletError("paired repository identity changed during evaluation") from error
    _reject_ignored_python_sources(repository_root)
    _reject_noncanonical_index_inputs(repository_root)


def _retain_stderr_tail(
    read_descriptor: int,
    captured: bytearray,
    overflowed: list[bool],
    read_errors: list[OSError],
) -> None:
    try:
        with os.fdopen(read_descriptor, "rb") as stderr_reader:
            while chunk := stderr_reader.read(64 * 1024):
                if len(chunk) >= MAX_CHILD_STDERR_BYTES:
                    captured[:] = chunk[-MAX_CHILD_STDERR_BYTES:]
                    overflowed[0] = True
                    continue
                excess = len(captured) + len(chunk) - MAX_CHILD_STDERR_BYTES
                if excess > 0:
                    del captured[:excess]
                    overflowed[0] = True
                captured.extend(chunk)
    except OSError as error:
        read_errors.append(error)


def _run_child_with_bounded_stderr(
    command: Sequence[str],
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    bootstrap_source: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[bytes], bytes, bool]:
    try:
        read_descriptor, write_descriptor = os.pipe()
    except OSError as error:
        raise PairedGauntletError("paired child diagnostic capture could not start") from error
    captured = bytearray()
    overflowed = [False]
    read_errors: list[OSError] = []
    stderr_thread = threading.Thread(
        target=_retain_stderr_tail,
        args=(read_descriptor, captured, overflowed, read_errors),
        daemon=True,
        name="gauntlet-child-stderr",
    )
    try:
        stderr_thread.start()
    except BaseException:
        os.close(read_descriptor)
        os.close(write_descriptor)
        raise
    try:
        completed = subprocess.run(
            command,
            cwd=repository_root,
            env=environment,
            input=bootstrap_source,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=write_descriptor,
            timeout=CHILD_TIMEOUT_SECONDS,
        )
    finally:
        os.close(write_descriptor)
        stderr_thread.join(CHILD_STDERR_DRAIN_TIMEOUT_SECONDS)
    if stderr_thread.is_alive() or read_errors:
        raise PairedGauntletError("paired child diagnostic capture failed")
    return completed, bytes(captured), overflowed[0]


def _sanitize_child_stderr(
    stderr: bytes,
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    truncated: bool,
) -> str:
    if truncated:
        # A discarded prefix may have contained the label that makes the
        # retained bytes recognizable as a credential, so the tail is unsafe.
        return "[stderr truncated; diagnostic suppressed]"
    text = stderr.decode("utf-8", errors="replace")
    text = _ANSI_ESCAPE_PATTERN.sub("", text)
    for sensitive_value in (
        str(repository_root),
        *(
            value
            for key, value in environment.items()
            if value
            and any(
                marker in key.upper()
                for marker in ("AUTH", "COOKIE", "CREDENTIAL", "KEY", "PASSWORD", "PASSWD", "SECRET", "TOKEN")
            )
        ),
    ):
        if len(sensitive_value) >= 4:
            text = text.replace(sensitive_value, "<redacted>")
    text = _SECRET_JSON_PATTERN.sub(lambda match: f'{match.group(1)}"<redacted>"', text)
    text = _SECRET_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = _SENSITIVE_HEADER_PATTERN.sub(lambda match: f"{match.group(1)}<redacted>", text)
    text = _URL_USERINFO_PATTERN.sub(r"\1<redacted>@", text)
    text = _QUOTED_ABSOLUTE_PATH_PATTERN.sub('"<path>"', text)
    text = _UNC_PATH_PATTERN.sub("<path>", text)
    text = _WINDOWS_ABSOLUTE_PATH_PATTERN.sub("<path>", text)
    text = _POSIX_ABSOLUTE_PATH_PATTERN.sub("<path>", text)
    text = "".join(character if character in "\n\t" or character.isprintable() else " " for character in text)
    text = " ".join(text.split())
    if not text:
        return ""
    maximum_text_bytes = MAX_CHILD_STDERR_BYTES
    encoded_text = text.encode("utf-8")[-maximum_text_bytes:]
    return encoded_text.decode("utf-8", errors="ignore")


def _run_child(
    repository_root: Path,
    *,
    profile: str,
    seed: int,
    samples: int,
    output: Path,
    dependency_paths: Sequence[str],
    dependency_environment_digest: str,
    bootstrap_source: bytes,
    expected_commit: str,
) -> dict[str, object]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON") and key.upper() != ISOLATED_PARENT_CACHE_ENV
    }
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    command = [
        sys.executable,
        "-s",
        "-S",
        "-P",
        "-",
        str(repository_root),
        json.dumps(dependency_paths),
        EXPECTED_REPOSITORY_COMMIT_ARGUMENT,
        expected_commit,
        DEPENDENCY_DIGEST_ARGUMENT,
        dependency_environment_digest,
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
        with tempfile.TemporaryDirectory(prefix="qbitunregistered-gauntlet-pycache-") as pycache_name:
            pycache_root = Path(pycache_name).resolve()
            if pycache_root.is_relative_to(repository_root.expanduser().resolve()):
                raise PairedGauntletError("paired child bytecode cache must be outside its worktree")
            environment["PYTHONPYCACHEPREFIX"] = str(pycache_root)
            completed, captured_stderr, stderr_truncated = _run_child_with_bounded_stderr(
                command,
                repository_root=repository_root,
                environment=environment,
                bootstrap_source=bootstrap_source,
            )
    except (OSError, subprocess.SubprocessError) as error:
        raise PairedGauntletError("paired child evaluation could not run") from error
    if completed.returncode != 0:
        diagnostic = _sanitize_child_stderr(
            captured_stderr,
            repository_root=repository_root,
            environment=environment,
            truncated=stderr_truncated,
        )
        detail = f": {diagnostic}" if diagnostic else ""
        raise PairedGauntletError(f"paired child evaluation failed with exit code {completed.returncode}{detail}")
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


def _load_canonical_quality_bar(source: bytes) -> QualityBar:
    try:
        return load_quality_bar_bytes(source)
    except QualityBarError as error:
        raise PairedGauntletError(
            "paired canonical quality bar is malformed or does not match the evaluator schema"
        ) from error


def _quality_bar_git_output(repository_root: Path, arguments: Sequence[str]) -> bytes:
    """Return strict Git output without inherited repository selection."""
    try:
        completed = subprocess.run(
            ["git", "--no-replace-objects", *arguments],
            cwd=repository_root,
            check=False,
            env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR) from error
    if completed.returncode != 0 or completed.stderr:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    return completed.stdout


def _single_git_record(output: bytes) -> tuple[bytes, bytes]:
    """Parse one exact NUL-terminated Git metadata-and-path record."""
    if not output.endswith(b"\0") or output.count(b"\0") != 1:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    try:
        metadata, path = output[:-1].split(b"\t", 1)
    except ValueError as error:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR) from error
    if not metadata or not path:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    return metadata, path


def _verified_canonical_quality_bar(
    repository_root: Path,
    expected_commit: str,
) -> _VerifiedQualityBarSource:
    """Return quality-bar bytes bound to the captured commit and stage-0 blob."""
    if len(expected_commit) not in {40, 64} or any(character not in "0123456789abcdef" for character in expected_commit):
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)

    encoded_path = _QUALITY_BAR_RELATIVE_PATH.encode("ascii")
    index_metadata, index_path = _single_git_record(
        _quality_bar_git_output(
            repository_root,
            (
                "ls-files",
                "--cached",
                "-v",
                "--stage",
                "-z",
                "--",
                _QUALITY_BAR_RELATIVE_PATH,
            ),
        )
    )
    index_fields = index_metadata.split(b" ")
    if len(index_fields) != 4 or any(not field for field in index_fields):
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    index_status, index_mode, index_oid, index_stage = index_fields
    if (
        index_path != encoded_path
        or index_status != b"H"
        or index_mode not in _REGULAR_BLOB_MODES
        or index_stage != b"0"
        or len(index_oid) not in {40, 64}
        or any(character not in b"0123456789abcdef" for character in index_oid)
    ):
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)

    head_metadata, head_path = _single_git_record(
        _quality_bar_git_output(
            repository_root,
            (
                "ls-tree",
                "--full-tree",
                "-z",
                expected_commit,
                "--",
                _QUALITY_BAR_RELATIVE_PATH,
            ),
        )
    )
    head_fields = head_metadata.split(b" ")
    if len(head_fields) != 3 or any(not field for field in head_fields):
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    head_mode, head_type, head_oid = head_fields
    if head_path != encoded_path or head_type != b"blob" or head_mode != index_mode or head_oid != index_oid:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    try:
        mode = index_mode.decode("ascii")
        oid = index_oid.decode("ascii")
    except UnicodeDecodeError as error:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR) from error
    source_bytes = _quality_bar_git_output(repository_root, ("cat-file", "blob", oid))
    if len(source_bytes) > MAX_CHILD_ARTIFACT_BYTES:
        raise PairedGauntletError(_QUALITY_BAR_VERIFICATION_ERROR)
    return _VerifiedQualityBarSource(
        revision=expected_commit,
        mode=mode,
        oid=oid,
        source_bytes=source_bytes,
    )


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
    if samples != DEFAULT_SAMPLES:
        raise PairedGauntletError(f"comparable paired gauntlet runs require exactly {DEFAULT_SAMPLES} timed samples")
    orchestrator_root = orchestrator_root.expanduser().resolve()
    control_root = control_root.expanduser().resolve()
    candidate_root = candidate_root.expanduser().resolve()
    if control_root == candidate_root:
        raise PairedGauntletError("paired control and candidate must be isolated worktrees")
    orchestrator_identity = _require_clean_identity(orchestrator_root)
    control_identity = _require_clean_identity(control_root)
    candidate_identity = _require_clean_identity(candidate_root)
    repository_roots = (orchestrator_root, control_root, candidate_root)
    _reject_unsafe_package_entries_in_roots(repository_roots)
    quality_bar_source = _verified_canonical_quality_bar(
        orchestrator_root,
        orchestrator_identity.commit,
    )
    quality_bar = _load_canonical_quality_bar(quality_bar_source.source_bytes)
    if profile not in quality_bar.profiles:
        raise PairedGauntletError("paired profile is not present in the quality bar")
    if isinstance(seed, bool) or not isinstance(seed, int) or seed != quality_bar.profiles[profile].seed:
        raise PairedGauntletError("paired seed must match the canonical profile seed")
    quality_bar_digest = hashlib.sha256(quality_bar_source.source_bytes).hexdigest()
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
    dependency_paths = _dependency_import_paths()
    dependency_environment_identity = _current_dependency_environment_digest(dependency_paths)
    bound_dependency_digest = _bound_dependency_digest(
        orchestrator_dependency_digest,
        dependency_environment_identity,
    )

    roots = {"control": control_root, "candidate": candidate_root}
    identities = {"control": control_identity, "candidate": candidate_identity}
    paired_runs: list[PairedRun] = []
    with tempfile.TemporaryDirectory(prefix="qbitunregistered-gauntlet-paired-") as temporary_root:
        for position, role in enumerate(PAIRED_ORDER):
            output = Path(temporary_root) / f"run-{position}.json"
            child_root = roots[role]
            child_identity = identities[role]
            _require_unchanged_identity(child_identity, child_root)
            _reject_unsafe_package_entries_in_roots((child_root,))
            _reject_ignored_python_sources(child_root)
            _reject_noncanonical_index_inputs(child_root)
            try:
                bootstrap_source = verified_import_bootstrap_source(
                    child_root,
                    child_identity.commit,
                )
            except ProtectedPackageTreeError as error:
                raise PairedGauntletError("paired child bootstrap could not be verified") from error
            try:
                child_result = _run_child(
                    child_root,
                    profile=profile,
                    seed=seed,
                    samples=samples,
                    output=output,
                    dependency_paths=dependency_paths,
                    dependency_environment_digest=dependency_environment_identity,
                    bootstrap_source=bootstrap_source,
                    expected_commit=child_identity.commit,
                )
            finally:
                _require_unchanged_identity(child_identity, child_root)
                _reject_unsafe_package_entries_in_roots((child_root,))
                _require_dependency_environment(
                    dependency_paths,
                    dependency_environment_identity,
                )
                _reject_ignored_python_sources(child_root)
                _reject_noncanonical_index_inputs(child_root)
                try:
                    current_bootstrap_source = verified_import_bootstrap_source(
                        child_root,
                        child_identity.commit,
                    )
                except ProtectedPackageTreeError as error:
                    raise PairedGauntletError("paired child bootstrap could not be verified") from error
                if current_bootstrap_source != bootstrap_source:
                    raise PairedGauntletError("paired child bootstrap changed during evaluation")
            try:
                result = sanitize_child_result(child_result, quality_bar)
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
    _require_unchanged_identity(orchestrator_identity, orchestrator_root)
    _require_unchanged_identity(control_identity, control_root)
    _require_unchanged_identity(candidate_identity, candidate_root)
    _reject_unsafe_package_entries_in_roots(repository_roots)
    current_quality_bar_source = _verified_canonical_quality_bar(
        orchestrator_root,
        orchestrator_identity.commit,
    )
    if (
        _evaluator_digest(orchestrator_root) != orchestrator_digest
        or _evaluator_digest(control_root) != control_digest
        or _evaluator_digest(candidate_root) != candidate_digest
        or _named_files_digest(orchestrator_root, DEPENDENCY_FILES) != orchestrator_dependency_digest
        or _named_files_digest(control_root, DEPENDENCY_FILES) != control_dependency_digest
        or _named_files_digest(candidate_root, DEPENDENCY_FILES) != candidate_dependency_digest
        or current_quality_bar_source != quality_bar_source
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
        "dependency_digest": bound_dependency_digest,
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
