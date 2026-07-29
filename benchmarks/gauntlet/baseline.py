"""Load and compare versioned gauntlet quality bars using the standard library."""

from __future__ import annotations

import math
import statistics
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, TypedDict, cast

GateStatus = Literal["pass", "fail", "pending", "non_comparable"]
MUTATION_COUNTER_KEYS = {
    "filesystem",
    "qbittorrent",
    "torrents_add_tags",
    "torrents_delete",
    "torrents_pause",
    "torrents_remove_tags",
    "torrents_resume",
    "torrents_set_auto_management",
    "torrents_set_share_limits",
    "torrents_tags",
}
ENVIRONMENT_KEYS = {
    "cpu_affinity_digest",
    "effective_cpu_count",
    "filesystem_block_size",
    "filesystem_id",
    "filesystem_type",
    "implementation",
    "kernel_release",
    "logical_cpu_count",
    "machine",
    "operating_system",
    "processor",
    "python",
}


class QualityBarError(ValueError):
    """Raised when a quality-bar TOML file is malformed or incomplete."""


class GateResult(TypedDict):
    """One comparison gate with baseline and target kept distinct."""

    status: GateStatus
    detail: str
    actual: int | float | None
    baseline: int | float | None
    target: int | float | None


class ComparisonReport(TypedDict):
    """Sanitized result of comparing one evaluator artifact to a quality bar."""

    quality_bar_schema_version: int
    overall: Literal["pass", "fail", "pending"]
    gates: dict[str, GateResult]


@dataclass(frozen=True, slots=True)
class BaselineMeasurement:
    """Accepted measurement state for one profile."""

    status: Literal["pending_clean_evaluator_commit", "measured"]
    median_runtime_seconds: float | None
    peak_memory_bytes: int | None
    environment: Mapping[str, str] | None


@dataclass(frozen=True, slots=True)
class EndpointBudget:
    """Inclusive per-pass read limits for one endpoint."""

    minimum: int
    maximum: int


@dataclass(frozen=True, slots=True)
class ProfileQualityBar:
    """Correctness oracle, baseline, and independent targets for one profile."""

    seed: int
    fixture_manifest_digest: str
    intended_action_digest: str
    reconciliation: Mapping[str, int | str]
    candidate_count: int
    workload: Mapping[str, int]
    api_budgets: Mapping[str, EndpointBudget]
    baseline: BaselineMeasurement
    runtime_baseline_fraction_max: float
    peak_memory_baseline_fraction_max: float
    relative_mad_max: float
    relative_range_max: float


@dataclass(frozen=True, slots=True)
class QualityBar:
    """Fully validated repository quality-bar configuration."""

    schema_version: int
    evaluator_schema_version: int
    evaluator_version: str
    result_schema: str
    scope: str
    measurement_policy: Mapping[str, object]
    profiles: Mapping[str, ProfileQualityBar]


def _table(value: object, description: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise QualityBarError(f"{description} must be a TOML table")
    return cast(dict[str, Any], value)


def _string(value: object, description: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualityBarError(f"{description} must be a non-empty string")
    return value


def _integer(value: object, description: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise QualityBarError(f"{description} must be an integer >= {minimum}")
    return value


def _positive_float(value: object, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualityBarError(f"{description} must be a positive number")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved <= 0:
        raise QualityBarError(f"{description} must be a positive finite number")
    return resolved


def _integer_table(value: object, description: str) -> dict[str, int]:
    table = _table(value, description)
    return {_string(key, f"{description} key"): _integer(item, f"{description}.{key}") for key, item in table.items()}


def _sha256(value: object, description: str) -> str:
    resolved = _string(value, description)
    if len(resolved) != 64 or any(character not in "0123456789abcdef" for character in resolved):
        raise QualityBarError(f"{description} must be a lowercase SHA-256 digest")
    return resolved


def _endpoint_budgets(value: object, description: str) -> dict[str, EndpointBudget]:
    table = _table(value, description)
    expected = {
        "application.default_save_path",
        "torrent_categories.categories",
        "torrents.info",
        "torrents_files",
    }
    if set(table) != expected:
        raise QualityBarError(f"{description} endpoints do not match the evaluator schema")
    budgets: dict[str, EndpointBudget] = {}
    for endpoint, raw_budget in table.items():
        budget = _table(raw_budget, f"{description}.{endpoint}")
        if set(budget) != {"minimum", "maximum"}:
            raise QualityBarError(f"{description}.{endpoint} keys are malformed")
        minimum = _integer(budget["minimum"], f"{description}.{endpoint}.minimum")
        maximum = _integer(budget["maximum"], f"{description}.{endpoint}.maximum")
        if minimum > maximum:
            raise QualityBarError(f"{description}.{endpoint} minimum exceeds maximum")
        budgets[endpoint] = EndpointBudget(minimum, maximum)
    return budgets


def _reconciliation(value: object, description: str) -> dict[str, int | str]:
    table = _table(value, description)
    expected = {
        "file_action_count",
        "empty_directory_count",
        "file_action_digest",
        "empty_directory_digest",
        "digest",
    }
    if set(table) != expected:
        raise QualityBarError(f"{description} keys do not match the evaluator schema")
    return {
        "file_action_count": _integer(table["file_action_count"], f"{description}.file_action_count", minimum=1),
        "empty_directory_count": _integer(
            table["empty_directory_count"],
            f"{description}.empty_directory_count",
            minimum=1,
        ),
        "file_action_digest": _sha256(table["file_action_digest"], f"{description}.file_action_digest"),
        "empty_directory_digest": _sha256(
            table["empty_directory_digest"],
            f"{description}.empty_directory_digest",
        ),
        "digest": _sha256(table["digest"], f"{description}.digest"),
    }


def _baseline_measurement(value: object, description: str) -> BaselineMeasurement:
    table = _table(value, description)
    status = _string(table.get("status"), f"{description}.status")
    if status == "pending_clean_evaluator_commit":
        return BaselineMeasurement(
            status="pending_clean_evaluator_commit",
            median_runtime_seconds=None,
            peak_memory_bytes=None,
            environment=None,
        )
    if status != "measured":
        raise QualityBarError(f"{description}.status must be 'pending_clean_evaluator_commit' or 'measured'")
    environment_table = _table(table.get("environment"), f"{description}.environment")
    environment = {
        _string(key, f"{description}.environment key"): _string(
            item,
            f"{description}.environment.{key}",
        )
        for key, item in environment_table.items()
    }
    return BaselineMeasurement(
        status="measured",
        median_runtime_seconds=_positive_float(
            table.get("median_runtime_seconds"),
            f"{description}.median_runtime_seconds",
        ),
        peak_memory_bytes=_integer(
            table.get("peak_memory_bytes"),
            f"{description}.peak_memory_bytes",
            minimum=1,
        ),
        environment=environment,
    )


def load_quality_bar(path: Path) -> QualityBar:
    """Load and fully validate one stdlib-TOML quality bar."""
    try:
        with path.open("rb") as quality_bar_file:
            document = tomllib.load(quality_bar_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise QualityBarError("could not load gauntlet quality bar") from error

    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1)
    evaluator_schema_version = _integer(
        document.get("evaluator_schema_version"),
        "evaluator_schema_version",
        minimum=1,
    )
    evaluator_version = _string(document.get("evaluator_version"), "evaluator_version")
    result_schema = _string(document.get("result_schema"), "result_schema")
    scope = _string(document.get("scope"), "scope")
    measurement_policy = _table(document.get("measurement_policy"), "measurement_policy")
    expected_policy_keys = {
        "timed_samples",
        "warmup_passes",
        "memory_passes",
        "sequence",
        "timed_samples_traced",
        "memory_pass_timed",
        "application_cache",
        "fixture_metadata",
        "os_page_cache",
        "sample_rejection",
    }
    if set(measurement_policy) != expected_policy_keys:
        raise QualityBarError("measurement_policy keys do not match the evaluator schema")
    for key in ("timed_samples", "warmup_passes", "memory_passes"):
        _integer(measurement_policy[key], f"measurement_policy.{key}", minimum=1)
    for key in ("timed_samples_traced", "memory_pass_timed"):
        if not isinstance(measurement_policy[key], bool):
            raise QualityBarError(f"measurement_policy.{key} must be a boolean")
    for key in (
        "sequence",
        "application_cache",
        "fixture_metadata",
        "os_page_cache",
        "sample_rejection",
    ):
        _string(measurement_policy[key], f"measurement_policy.{key}")

    profile_tables = _table(document.get("profiles"), "profiles")
    if not profile_tables:
        raise QualityBarError("quality bar must define at least one profile")
    profiles: dict[str, ProfileQualityBar] = {}
    for profile_name, raw_profile in profile_tables.items():
        profile = _table(raw_profile, f"profiles.{profile_name}")
        targets = _table(profile.get("targets"), f"profiles.{profile_name}.targets")
        profiles[profile_name] = ProfileQualityBar(
            seed=_integer(profile.get("seed"), f"profiles.{profile_name}.seed"),
            fixture_manifest_digest=_sha256(
                profile.get("fixture_manifest_digest"),
                f"profiles.{profile_name}.fixture_manifest_digest",
            ),
            intended_action_digest=_sha256(
                profile.get("intended_action_digest"),
                f"profiles.{profile_name}.intended_action_digest",
            ),
            candidate_count=_integer(
                profile.get("candidate_count"),
                f"profiles.{profile_name}.candidate_count",
                minimum=1,
            ),
            workload=_integer_table(
                profile.get("workload"),
                f"profiles.{profile_name}.workload",
            ),
            reconciliation=_reconciliation(
                profile.get("reconciliation"),
                f"profiles.{profile_name}.reconciliation",
            ),
            api_budgets=_endpoint_budgets(
                profile.get("api_budgets"),
                f"profiles.{profile_name}.api_budgets",
            ),
            baseline=_baseline_measurement(
                profile.get("baseline"),
                f"profiles.{profile_name}.baseline",
            ),
            runtime_baseline_fraction_max=_positive_float(
                targets.get("runtime_baseline_fraction_max"),
                f"profiles.{profile_name}.targets.runtime_baseline_fraction_max",
            ),
            peak_memory_baseline_fraction_max=_positive_float(
                targets.get("peak_memory_baseline_fraction_max"),
                f"profiles.{profile_name}.targets.peak_memory_baseline_fraction_max",
            ),
            relative_mad_max=_positive_float(
                targets.get("relative_mad_max"),
                f"profiles.{profile_name}.targets.relative_mad_max",
            ),
            relative_range_max=_positive_float(
                targets.get("relative_range_max"),
                f"profiles.{profile_name}.targets.relative_range_max",
            ),
        )
    return QualityBar(
        schema_version=schema_version,
        evaluator_schema_version=evaluator_schema_version,
        evaluator_version=evaluator_version,
        result_schema=result_schema,
        scope=scope,
        measurement_policy=dict(measurement_policy),
        profiles=profiles,
    )


def _gate(
    status: GateStatus,
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


def _mapping_of_ints(value: object, *, nonnegative: bool = True) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    resolved: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(item, int) or (nonnegative and item < 0):
            return None
        resolved[key] = item
    return resolved


def _runtime_reconciliation(value: object) -> dict[str, int | str] | None:
    if not isinstance(value, dict):
        return None
    expected_keys = {
        "file_action_count",
        "empty_directory_count",
        "file_action_digest",
        "empty_directory_digest",
        "digest",
    }
    if set(value) != expected_keys:
        return None
    counts: dict[str, int] = {}
    for key in ("file_action_count", "empty_directory_count"):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            return None
        counts[key] = item
    digests: dict[str, str] = {}
    for key in ("file_action_digest", "empty_directory_digest", "digest"):
        item = value[key]
        if not isinstance(item, str) or len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
            return None
        digests[key] = item
    return {**counts, **digests}


def _measurement_policy_matches(
    result: Mapping[str, object],
    quality_bar: QualityBar,
) -> bool:
    workload = _mapping_of_ints(result.get("workload"))
    policy = result.get("measurement_policy")
    if workload is None or not isinstance(policy, dict):
        return False
    expected = quality_bar.measurement_policy
    count_keys = {"timed_samples", "warmup_passes", "memory_passes"}
    expected_emitted_policy = {key: value for key, value in expected.items() if key not in count_keys}
    return (
        workload.get("timed_samples") == expected["timed_samples"]
        and workload.get("warmup_passes") == expected["warmup_passes"]
        and workload.get("memory_passes") == expected["memory_passes"]
        and policy == expected_emitted_policy
    )


def _identity_gate(result: Mapping[str, object]) -> GateResult:
    candidate_state = result.get("candidate_state")
    if not isinstance(candidate_state, dict):
        return _gate("non_comparable", "candidate identity is missing")
    clean = candidate_state.get("clean")
    diff_digest = candidate_state.get("diff_sha256")
    commit = result.get("commit")
    if (
        not isinstance(commit, str)
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
        or not isinstance(clean, bool)
        or not isinstance(diff_digest, str)
        or len(diff_digest) != 64
        or any(character not in "0123456789abcdef" for character in diff_digest)
        or result.get("identity_verified") is not True
    ):
        return _gate("non_comparable", "candidate identity is unknown or unverified")
    return _gate("pass", "candidate identity is complete and stable")


def _safety_gate(result: Mapping[str, object]) -> GateResult:
    mutations = _mapping_of_ints(result.get("mutation_counters"))
    if mutations is None or set(mutations) != MUTATION_COUNTER_KEYS:
        return _gate("fail", "mutation counters are missing or malformed")
    total = sum(mutations.values())
    if total:
        return _gate("fail", "dry-run mutation evidence is nonzero", actual=total)
    return _gate("pass", "all mutation counters are zero", actual=0)


def _result_gate(
    result: Mapping[str, object],
    quality_bar: QualityBar,
    profile: ProfileQualityBar,
    profile_name: str,
) -> GateResult:
    workload = _mapping_of_ints(result.get("workload"))
    candidates = _mapping_of_ints(result.get("candidate_counts"))
    reconciliation = _runtime_reconciliation(result.get("reconciliation"))
    matches = (
        result.get("schema") == quality_bar.result_schema
        and result.get("schema_version") == quality_bar.evaluator_schema_version
        and result.get("evaluator_version") == quality_bar.evaluator_version
        and result.get("scope") == quality_bar.scope
        and result.get("profile") == profile_name
        and result.get("seed") == profile.seed
        and result.get("fixture_manifest_digest") == profile.fixture_manifest_digest
        and result.get("intended_action_digest") == profile.intended_action_digest
        and reconciliation == dict(profile.reconciliation)
        and candidates == {"orphan_files": profile.candidate_count}
        and workload == dict(profile.workload)
    )
    if not matches:
        return _gate("fail", "result or fixture oracle does not match the quality bar")
    return _gate("pass", "result and fixture oracle match the quality bar")


def _api_gate(
    result: Mapping[str, object],
    profile: ProfileQualityBar,
    *,
    timed_sample_count: int,
) -> GateResult:
    normalized = _mapping_of_ints(result.get("endpoint_counters"))
    timed_samples = result.get("timed_sample_endpoint_counters")
    pass_counters = result.get("pass_endpoint_counters")
    if not isinstance(timed_samples, list) or not isinstance(pass_counters, dict):
        return _gate("fail", "per-pass API counters are missing")
    if len(timed_samples) != timed_sample_count or set(pass_counters) != {"warmup", "memory"}:
        return _gate(
            "fail",
            "API evidence does not contain the locked timed, warmup, and memory passes",
        )

    def within_budget(value: object) -> bool:
        counters = _mapping_of_ints(value)
        if counters is None or set(counters) != set(profile.api_budgets):
            return False
        return all(
            profile.api_budgets[name].minimum <= count <= profile.api_budgets[name].maximum for name, count in counters.items()
        )

    all_passes = [normalized, *timed_samples, pass_counters["warmup"], pass_counters["memory"]]
    if not all(within_budget(item) for item in all_passes):
        return _gate("fail", "one or more per-pass API counts violate the locked budget")
    maximum = profile.api_budgets["torrents_files"].maximum
    return _gate(
        "pass",
        "all passes remain within the locked API budgets",
        actual=normalized["torrents_files"] if normalized is not None else None,
        target=maximum,
    )


def _environment_gate(
    result: Mapping[str, object],
    baseline: BaselineMeasurement,
) -> GateResult:
    environment = result.get("environment")
    if (
        not isinstance(environment, dict)
        or set(environment) != ENVIRONMENT_KEYS
        or any(not isinstance(value, str) or not value or value == "unknown" for value in environment.values())
        or any(
            not environment[key].isdigit() or int(environment[key]) <= 0
            for key in ("effective_cpu_count", "filesystem_block_size", "logical_cpu_count")
        )
        or any(
            len(environment[key]) != 64 or any(character not in "0123456789abcdef" for character in environment[key])
            for key in ("cpu_affinity_digest", "filesystem_id")
        )
    ):
        return _gate("non_comparable", "required environment evidence is missing or unknown")
    if baseline.status != "measured":
        return _gate("pending", "clean-commit baseline environment is not recorded")
    if environment != baseline.environment:
        return _gate("non_comparable", "environment differs from the recorded baseline")
    return _gate("pass", "environment matches the recorded baseline")


def _performance_gate(
    actual_value: object,
    baseline_value: int | float | None,
    target_fraction: float,
    description: str,
) -> GateResult:
    if isinstance(actual_value, bool) or not isinstance(actual_value, (int, float)):
        return _gate("fail", f"{description} result is missing or malformed")
    actual = float(actual_value)
    if not math.isfinite(actual) or actual <= 0:
        return _gate("fail", f"{description} result must be positive and finite")
    if baseline_value is None:
        return _gate("pending", f"clean-commit {description} baseline is not recorded")
    target = baseline_value * target_fraction
    status: GateStatus = "pass" if actual <= target else "fail"
    return _gate(
        status,
        f"{description} {'meets' if status == 'pass' else 'exceeds'} the independent target",
        actual=actual,
        baseline=baseline_value,
        target=target,
    )


def _variance_gate(
    result: Mapping[str, object],
    profile: ProfileQualityBar,
    *,
    timed_sample_count: int,
) -> GateResult:
    samples = result.get("sample_runtime_seconds")
    if (
        not isinstance(samples, list)
        or len(samples) != timed_sample_count
        or any(
            isinstance(sample, bool)
            or not isinstance(sample, (int, float))
            or not math.isfinite(float(sample))
            or float(sample) <= 0
            for sample in samples
        )
    ):
        return _gate(
            "fail",
            "runtime samples must match the locked count and be positive finite values",
        )
    resolved = [float(sample) for sample in samples]
    median = statistics.median(resolved)
    minimum = min(resolved)
    maximum = max(resolved)
    mad = statistics.median(abs(sample - median) for sample in resolved)
    reported = (
        result.get("median_runtime_seconds"),
        result.get("minimum_runtime_seconds"),
        result.get("maximum_runtime_seconds"),
        result.get("median_absolute_deviation_seconds"),
    )
    expected = (median, minimum, maximum, mad)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not math.isclose(float(value), expected_value, rel_tol=1e-12, abs_tol=0.0)
        for value, expected_value in zip(reported, expected, strict=True)
    ):
        return _gate("fail", "reported runtime statistics are malformed or inconsistent")
    relative_mad = mad / median
    relative_range = (maximum - minimum) / median
    maximum_normalized_variance = max(
        relative_mad / profile.relative_mad_max,
        relative_range / profile.relative_range_max,
    )
    if maximum_normalized_variance > 1.0:
        return _gate(
            "fail",
            "runtime sample variance exceeds the locked limits",
            actual=maximum_normalized_variance,
            target=1.0,
        )
    return _gate(
        "pass",
        "runtime sample variance remains within the locked limits",
        actual=maximum_normalized_variance,
        target=1.0,
    )


def compare_result(
    result: Mapping[str, object],
    quality_bar: QualityBar,
) -> ComparisonReport:
    """Compare one result against structural, safety, API, and performance gates."""
    profile_name = result.get("profile")
    if not isinstance(profile_name, str) or profile_name not in quality_bar.profiles:
        raise QualityBarError("result profile is not defined by the quality bar")
    profile = quality_bar.profiles[profile_name]
    timed_sample_count = cast(int, quality_bar.measurement_policy["timed_samples"])
    policy_gate = (
        _gate("pass", "measurement and cache policy matches the quality bar")
        if _measurement_policy_matches(result, quality_bar)
        else _gate("non_comparable", "measurement or cache policy differs from the quality bar")
    )
    gates: dict[str, GateResult] = {
        "identity": _identity_gate(result),
        "measurement_policy": policy_gate,
        "environment": _environment_gate(result, profile.baseline),
        "safety": _safety_gate(result),
        "result": _result_gate(result, quality_bar, profile, profile_name),
        "api": _api_gate(
            result,
            profile,
            timed_sample_count=timed_sample_count,
        ),
        "variance": _variance_gate(
            result,
            profile,
            timed_sample_count=timed_sample_count,
        ),
        "runtime": _performance_gate(
            result.get("median_runtime_seconds"),
            profile.baseline.median_runtime_seconds,
            profile.runtime_baseline_fraction_max,
            "runtime",
        ),
        "memory": _performance_gate(
            result.get("peak_memory_bytes"),
            profile.baseline.peak_memory_bytes,
            profile.peak_memory_baseline_fraction_max,
            "memory",
        ),
    }
    statuses = {gate["status"] for gate in gates.values()}
    if statuses & {"fail", "non_comparable"}:
        overall: Literal["pass", "fail", "pending"] = "fail"
    elif "pending" in statuses:
        overall = "pending"
    else:
        overall = "pass"
    return {
        "quality_bar_schema_version": quality_bar.schema_version,
        "overall": overall,
        "gates": gates,
    }
