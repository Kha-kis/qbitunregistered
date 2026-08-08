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
TRACKER_SCENARIO_NAMES = {
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
ISOLATION_COUNTER_KEYS = {
    "filesystem_write_attempts",
    "network_connect_attempts",
    "network_dns_attempts",
    "network_outbound_attempts",
}
COMMON_RESULT_KEYS = {
    "schema",
    "schema_version",
    "evaluator_version",
    "commit",
    "candidate_state",
    "identity_verified",
    "environment",
    "scope",
    "profile_kind",
    "profile",
    "tier",
    "seed",
    "workload",
    "fixture_manifest_digest",
    "intended_action_digest",
    "reconciliation",
    "candidate_counts",
    "endpoint_counters",
    "timed_sample_endpoint_counters",
    "pass_endpoint_counters",
    "mutation_counters",
    "measurement_policy",
    "sample_runtime_seconds",
    "median_runtime_seconds",
    "minimum_runtime_seconds",
    "maximum_runtime_seconds",
    "median_absolute_deviation_seconds",
    "peak_memory_bytes",
}
TRACKER_RESULT_KEYS = {
    "execution_action_digest",
    "isolation_counters",
    "scenarios",
}
ORPHAN_WORKLOAD_KEYS = {
    "torrents",
    "filesystem_files",
    "owned_files",
    "orphan_files",
    "exact_metadata_torrents",
    "bulk_path_torrents",
    "configured_roots",
    "shards",
    "timed_samples",
    "warmup_passes",
    "memory_passes",
}
TRACKER_WORKLOAD_KEYS = {
    "torrents",
    "tracker_records",
    "save_path_groups",
    "exact_message_targets",
    "prefix_message_targets",
    "default_tag_targets",
    "cross_seed_tag_targets",
    "torrent_only_delete_targets",
    "timed_samples",
    "warmup_passes",
    "memory_passes",
}
COMMON_PROFILE_KEYS = {
    "kind",
    "tier",
    "seed",
    "fixture_manifest_digest",
    "intended_action_digest",
    "workload",
    "reconciliation",
    "baseline",
    "targets",
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

    kind: Literal["orphan", "tracker"]
    tier: str
    seed: int
    fixture_manifest_digest: str
    intended_action_digest: str
    execution_action_digest: str | None
    reconciliation: Mapping[str, int | str]
    candidate_counts: Mapping[str, int]
    workload: Mapping[str, int]
    api_budgets: Mapping[str, EndpointBudget]
    allowed_tracker_transports: tuple[tuple[int, int], ...]
    scenario_action_digests: Mapping[str, str]
    isolation_counters: Mapping[str, int]
    baseline: BaselineMeasurement
    runtime_baseline_fraction_max: float
    peak_memory_baseline_fraction_max: float
    relative_mad_max: float
    relative_range_max: float

    @property
    def candidate_count(self) -> int:
        """Return the legacy orphan candidate count for existing callers."""
        return self.candidate_counts["orphan_files"]


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


def _tracker_reconciliation(value: object, description: str) -> dict[str, int | str]:
    table = _table(value, description)
    count_keys = {
        "save_path_group_count",
        "torrent_path_count",
        "unregistered_tracker_count",
        "default_tag_action_count",
        "cross_seed_tag_action_count",
        "torrent_only_delete_action_count",
    }
    if set(table) != {*count_keys, "digest"}:
        raise QualityBarError(f"{description} keys do not match the tracker evaluator schema")
    return {
        **{key: _integer(table[key], f"{description}.{key}", minimum=1) for key in count_keys},
        "digest": _sha256(table["digest"], f"{description}.digest"),
    }


def _tracker_api_evidence(value: object, description: str) -> tuple[tuple[int, int], ...]:
    table = _table(value, description)
    if set(table) != {"ordinary_info", "allowed_transports"}:
        raise QualityBarError(f"{description} keys do not match the tracker evaluator schema")
    if _integer(table["ordinary_info"], f"{description}.ordinary_info") != 0:
        raise QualityBarError(f"{description}.ordinary_info must be zero")
    raw_transports = table["allowed_transports"]
    if not isinstance(raw_transports, list) or len(raw_transports) != 2:
        raise QualityBarError(f"{description}.allowed_transports must contain exactly two pairs")
    transports: list[tuple[int, int]] = []
    for index, raw_transport in enumerate(raw_transports):
        if not isinstance(raw_transport, list) or len(raw_transport) != 2:
            raise QualityBarError(f"{description}.allowed_transports[{index}] must be a pair")
        transports.append(
            (
                _integer(raw_transport[0], f"{description}.allowed_transports[{index}][0]"),
                _integer(raw_transport[1], f"{description}.allowed_transports[{index}][1]"),
            )
        )
    resolved = tuple(transports)
    if resolved[1] != (1, 0) or resolved[0][0] != 0 or resolved[0][1] < 1:
        raise QualityBarError(f"{description}.allowed_transports do not lock exact and bulk responses")
    return resolved


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


def _validated_quality_bar(document: Mapping[str, object]) -> QualityBar:  # noqa: C901
    """Build one fully validated quality bar from a parsed TOML document."""
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
        kind = _string(profile.get("kind"), f"profiles.{profile_name}.kind")
        if kind not in {"orphan", "tracker"}:
            raise QualityBarError(f"profiles.{profile_name}.kind must be 'orphan' or 'tracker'")
        kind_specific_keys = (
            {"candidate_count", "api_budgets"}
            if kind == "orphan"
            else {
                "candidate_counts",
                "execution_action_digest",
                "api_evidence",
                "isolation_counters",
                "scenario_action_digests",
            }
        )
        if set(profile) != COMMON_PROFILE_KEYS | kind_specific_keys:
            raise QualityBarError(f"profiles.{profile_name} profile keys do not match the {kind} schema")
        targets = _table(profile.get("targets"), f"profiles.{profile_name}.targets")
        if kind == "orphan":
            candidate_counts = {
                "orphan_files": _integer(
                    profile.get("candidate_count"),
                    f"profiles.{profile_name}.candidate_count",
                    minimum=1,
                )
            }
            reconciliation = _reconciliation(
                profile.get("reconciliation"),
                f"profiles.{profile_name}.reconciliation",
            )
            api_budgets = _endpoint_budgets(
                profile.get("api_budgets"),
                f"profiles.{profile_name}.api_budgets",
            )
            allowed_tracker_transports: tuple[tuple[int, int], ...] = ()
            scenario_action_digests: dict[str, str] = {}
            execution_action_digest: str | None = None
            isolation_counters: dict[str, int] = {}
        else:
            candidate_counts = _integer_table(
                profile.get("candidate_counts"),
                f"profiles.{profile_name}.candidate_counts",
            )
            if set(candidate_counts) != {
                "default_tag_targets",
                "cross_seed_tag_targets",
                "torrent_only_deletes",
            } or any(value < 1 for value in candidate_counts.values()):
                raise QualityBarError(f"profiles.{profile_name}.candidate_counts keys do not match the tracker schema")
            reconciliation = _tracker_reconciliation(
                profile.get("reconciliation"),
                f"profiles.{profile_name}.reconciliation",
            )
            api_budgets = {}
            allowed_tracker_transports = _tracker_api_evidence(
                profile.get("api_evidence"),
                f"profiles.{profile_name}.api_evidence",
            )
            scenario_table = _table(
                profile.get("scenario_action_digests"),
                f"profiles.{profile_name}.scenario_action_digests",
            )
            if set(scenario_table) != TRACKER_SCENARIO_NAMES:
                raise QualityBarError(f"profiles.{profile_name}.scenario_action_digests keys do not match the tracker schema")
            scenario_action_digests = {
                name: _sha256(value, f"profiles.{profile_name}.scenario_action_digests.{name}")
                for name, value in scenario_table.items()
            }
            execution_action_digest = _sha256(
                profile.get("execution_action_digest"),
                f"profiles.{profile_name}.execution_action_digest",
            )
            isolation_counters = _integer_table(
                profile.get("isolation_counters"),
                f"profiles.{profile_name}.isolation_counters",
            )
            if set(isolation_counters) != ISOLATION_COUNTER_KEYS or any(isolation_counters.values()):
                raise QualityBarError(f"profiles.{profile_name}.isolation_counters must lock every attempt class to zero")
        workload = _integer_table(
            profile.get("workload"),
            f"profiles.{profile_name}.workload",
        )
        expected_workload_keys = ORPHAN_WORKLOAD_KEYS if kind == "orphan" else TRACKER_WORKLOAD_KEYS
        if set(workload) != expected_workload_keys:
            raise QualityBarError(f"profiles.{profile_name}.workload keys do not match the {kind} schema")
        profiles[profile_name] = ProfileQualityBar(
            kind=cast(Literal["orphan", "tracker"], kind),
            tier=_string(profile.get("tier"), f"profiles.{profile_name}.tier"),
            seed=_integer(profile.get("seed"), f"profiles.{profile_name}.seed"),
            fixture_manifest_digest=_sha256(
                profile.get("fixture_manifest_digest"),
                f"profiles.{profile_name}.fixture_manifest_digest",
            ),
            intended_action_digest=_sha256(
                profile.get("intended_action_digest"),
                f"profiles.{profile_name}.intended_action_digest",
            ),
            execution_action_digest=execution_action_digest,
            candidate_counts=candidate_counts,
            workload=workload,
            reconciliation=reconciliation,
            api_budgets=api_budgets,
            allowed_tracker_transports=allowed_tracker_transports,
            scenario_action_digests=scenario_action_digests,
            isolation_counters=isolation_counters,
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


def load_quality_bar_bytes(source: bytes) -> QualityBar:
    """Parse and fully validate one UTF-8 stdlib-TOML quality-bar buffer."""
    try:
        document = tomllib.loads(source.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise QualityBarError("could not load gauntlet quality bar") from error
    return _validated_quality_bar(document)


def load_quality_bar(path: Path) -> QualityBar:
    """Load and fully validate one stdlib-TOML quality bar."""
    try:
        source = path.read_bytes()
    except OSError as error:
        raise QualityBarError("could not load gauntlet quality bar") from error
    return load_quality_bar_bytes(source)


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


def _runtime_reconciliation(
    value: object,
    kind: Literal["orphan", "tracker"],
) -> dict[str, int | str] | None:
    if not isinstance(value, dict):
        return None
    count_keys = (
        {"file_action_count", "empty_directory_count"}
        if kind == "orphan"
        else {
            "save_path_group_count",
            "torrent_path_count",
            "unregistered_tracker_count",
            "default_tag_action_count",
            "cross_seed_tag_action_count",
            "torrent_only_delete_action_count",
        }
    )
    digest_keys = {"file_action_digest", "empty_directory_digest", "digest"} if kind == "orphan" else {"digest"}
    expected_keys = count_keys | digest_keys
    if set(value) != expected_keys:
        return None
    counts: dict[str, int] = {}
    for key in count_keys:
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            return None
        counts[key] = item
    digests: dict[str, str] = {}
    for key in digest_keys:
        item = value[key]
        if not isinstance(item, str) or len(item) != 64 or any(character not in "0123456789abcdef" for character in item):
            return None
        digests[key] = item
    return {**counts, **digests}


def _runtime_tracker_scenarios(
    value: object,
    profile: ProfileQualityBar,
) -> dict[str, object] | None:
    if profile.kind != "tracker":
        return {} if value is None else None
    if not isinstance(value, dict) or set(value) != TRACKER_SCENARIO_NAMES:
        return None
    sanitized: dict[str, object] = {}
    endpoint_keys = {
        "torrents.info",
        "torrents.info.include_trackers",
        "torrents_trackers",
    }
    for name, raw_evidence in value.items():
        if not isinstance(raw_evidence, dict) or set(raw_evidence) != {
            "outcome",
            "action_digest",
            "endpoint_counters",
            "isolation_counters",
        }:
            return None
        action_digest = raw_evidence["action_digest"]
        endpoints = _mapping_of_ints(raw_evidence["endpoint_counters"])
        isolation_counters = _mapping_of_ints(raw_evidence["isolation_counters"])
        if (
            raw_evidence["outcome"] != "pass"
            or action_digest != profile.scenario_action_digests.get(name)
            or endpoints is None
            or set(endpoints) != endpoint_keys
            or isolation_counters != dict(profile.isolation_counters)
        ):
            return None
        sanitized[name] = {
            "outcome": "pass",
            "action_digest": action_digest,
            "endpoint_counters": endpoints,
            "isolation_counters": isolation_counters,
        }
    return sanitized


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
    if result.get("profile_kind") == "tracker":
        isolation_counters = _mapping_of_ints(result.get("isolation_counters"))
        if isolation_counters is None or set(isolation_counters) != ISOLATION_COUNTER_KEYS:
            return _gate("fail", "isolation counters are missing or malformed")
        total += sum(isolation_counters.values())
        scenarios = result.get("scenarios")
        if not isinstance(scenarios, dict) or set(scenarios) != TRACKER_SCENARIO_NAMES:
            return _gate("fail", "scenario isolation counters are missing or malformed")
        for evidence in scenarios.values():
            if not isinstance(evidence, dict):
                return _gate("fail", "scenario isolation counters are missing or malformed")
            scenario_isolation = _mapping_of_ints(evidence.get("isolation_counters"))
            if scenario_isolation is None or set(scenario_isolation) != ISOLATION_COUNTER_KEYS:
                return _gate("fail", "scenario isolation counters are missing or malformed")
            total += sum(scenario_isolation.values())
    if total:
        return _gate("fail", "dry-run mutation evidence is nonzero", actual=total)
    return _gate("pass", "all mutation and isolation counters are zero", actual=0)


def _result_gate(
    result: Mapping[str, object],
    quality_bar: QualityBar,
    profile: ProfileQualityBar,
    profile_name: str,
) -> GateResult:
    expected_keys = COMMON_RESULT_KEYS | (TRACKER_RESULT_KEYS if profile.kind == "tracker" else set())
    if set(result) != expected_keys:
        return _gate("fail", f"top-level result keys do not match the {profile.kind} schema")
    workload = _mapping_of_ints(result.get("workload"))
    candidates = _mapping_of_ints(result.get("candidate_counts"))
    reconciliation = _runtime_reconciliation(result.get("reconciliation"), profile.kind)
    scenarios = _runtime_tracker_scenarios(result.get("scenarios"), profile)
    matches = (
        result.get("schema") == quality_bar.result_schema
        and result.get("schema_version") == quality_bar.evaluator_schema_version
        and result.get("evaluator_version") == quality_bar.evaluator_version
        and result.get("scope") == quality_bar.scope
        and result.get("profile_kind") == profile.kind
        and result.get("profile") == profile_name
        and result.get("tier") == profile.tier
        and result.get("seed") == profile.seed
        and result.get("fixture_manifest_digest") == profile.fixture_manifest_digest
        and result.get("intended_action_digest") == profile.intended_action_digest
        and (profile.kind != "tracker" or result.get("execution_action_digest") == profile.execution_action_digest)
        and (
            profile.kind != "tracker" or _mapping_of_ints(result.get("isolation_counters")) == dict(profile.isolation_counters)
        )
        and reconciliation == dict(profile.reconciliation)
        and scenarios is not None
        and candidates == dict(profile.candidate_counts)
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
        if profile.kind == "tracker":
            if counters is None or set(counters) != {
                "torrents.info",
                "torrents.info.include_trackers",
                "torrents_trackers",
            }:
                return False
            transport = (counters["torrents.info.include_trackers"], counters["torrents_trackers"])
            return counters["torrents.info"] == 0 and transport in profile.allowed_tracker_transports
        if counters is None or set(counters) != set(profile.api_budgets):
            return False
        return all(
            profile.api_budgets[name].minimum <= count <= profile.api_budgets[name].maximum for name, count in counters.items()
        )

    all_passes = [normalized, *timed_samples, pass_counters["warmup"], pass_counters["memory"]]
    if not all(within_budget(item) for item in all_passes):
        return _gate("fail", "one or more per-pass API counts violate the locked budget")
    if profile.kind == "tracker":
        return _gate(
            "pass",
            "all passes use one locked complete tracker transport",
            actual=normalized["torrents_trackers"] if normalized is not None else None,
            target=profile.allowed_tracker_transports[0][1],
        )
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
