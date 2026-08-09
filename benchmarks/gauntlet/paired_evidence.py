"""Strict validation and reconstruction of paired-gauntlet child evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from benchmarks.gauntlet.baseline import (
    ENVIRONMENT_KEYS,
    ISOLATION_COUNTER_KEYS,
    MUTATION_COUNTER_KEYS,
    QualityBar,
    TrackerScenarioRole,
    derive_tracker_artifact_role,
    tracker_scenarios_match_role_contracts,
)

CHILD_RESULT_KEYS = {
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
ORPHAN_RECONCILIATION_KEYS = {
    "file_action_count",
    "empty_directory_count",
    "file_action_digest",
    "empty_directory_digest",
    "digest",
}
TRACKER_RECONCILIATION_KEYS = {
    "save_path_group_count",
    "torrent_path_count",
    "unregistered_tracker_count",
    "default_tag_action_count",
    "cross_seed_tag_action_count",
    "torrent_only_delete_action_count",
    "digest",
}
ORPHAN_ENDPOINT_KEYS = {
    "application.default_save_path",
    "torrent_categories.categories",
    "torrents.info",
    "torrents_files",
}
TRACKER_ENDPOINT_KEYS = {
    "torrents.info",
    "torrents.info.include_trackers",
    "torrents_trackers",
}
MEASUREMENT_POLICY_KEYS = {
    "sequence",
    "timed_samples_traced",
    "memory_pass_timed",
    "application_cache",
    "fixture_metadata",
    "os_page_cache",
    "sample_rejection",
}


class PairedEvidenceError(ValueError):
    """Raised when a child artifact contains unsafe or malformed evidence."""


def _finite_nonnegative(value: object, description: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PairedEvidenceError(f"{description} must be a non-negative finite number")
    resolved = float(value)
    if not math.isfinite(resolved) or resolved < 0:
        raise PairedEvidenceError(f"{description} must be a non-negative finite number")
    return resolved


def _exact_mapping(value: object, keys: set[str], description: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise PairedEvidenceError(f"{description} keys do not match the paired schema")
    return cast(Mapping[str, object], value)


def _bounded_string(value: object, description: str, *, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 or character in "/\\" for character in value)
    ):
        raise PairedEvidenceError(f"{description} is not safe bounded metadata")
    return value


def _hex_identifier(value: object, description: str, *, length: int) -> str:
    resolved = _bounded_string(value, description, maximum=length)
    if len(resolved) != length or any(character not in "0123456789abcdef" for character in resolved):
        raise PairedEvidenceError(f"{description} is not a lowercase hexadecimal identifier")
    return resolved


def _digest(value: object, description: str) -> str:
    return _hex_identifier(value, description, length=64)


def _integer(value: object, description: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PairedEvidenceError(f"{description} must be an integer >= {minimum}")
    return value


def _integer_mapping(
    value: object,
    keys: set[str],
    description: str,
    *,
    minimum: int = 0,
) -> dict[str, int]:
    mapping = _exact_mapping(value, keys, description)
    return {key: _integer(mapping[key], f"{description}.{key}", minimum=minimum) for key in sorted(keys)}


def _endpoint_mapping(value: object, keys: set[str], description: str) -> dict[str, int]:
    return _integer_mapping(value, keys, description)


def _sanitize_candidate_state(value: object) -> dict[str, object]:
    state = _exact_mapping(value, {"clean", "diff_sha256"}, "candidate_state")
    clean = state["clean"]
    if not isinstance(clean, bool):
        raise PairedEvidenceError("candidate_state.clean must be a boolean")
    return {
        "clean": clean,
        "diff_sha256": _digest(state["diff_sha256"], "candidate_state.diff_sha256"),
    }


def _sanitize_environment(value: object) -> dict[str, str]:
    environment = _exact_mapping(value, ENVIRONMENT_KEYS, "environment")
    return {key: _bounded_string(environment[key], f"environment.{key}") for key in sorted(ENVIRONMENT_KEYS)}


def _sanitize_reconciliation(value: object, *, profile_kind: str) -> dict[str, object]:
    keys = ORPHAN_RECONCILIATION_KEYS if profile_kind == "orphan" else TRACKER_RECONCILIATION_KEYS
    reconciliation = _exact_mapping(value, keys, "reconciliation")
    sanitized: dict[str, object] = {}
    for key in sorted(keys - {"digest", "file_action_digest", "empty_directory_digest"}):
        sanitized[key] = _integer(reconciliation[key], f"reconciliation.{key}", minimum=1)
    for key in sorted(keys & {"digest", "file_action_digest", "empty_directory_digest"}):
        sanitized[key] = _digest(reconciliation[key], f"reconciliation.{key}")
    return sanitized


def _sanitize_measurement_policy(
    value: object,
    quality_bar: QualityBar,
) -> dict[str, object]:
    policy = _exact_mapping(value, MEASUREMENT_POLICY_KEYS, "measurement_policy")
    sanitized: dict[str, object] = {}
    for key in sorted(MEASUREMENT_POLICY_KEYS):
        item = policy[key]
        if key in {"timed_samples_traced", "memory_pass_timed"}:
            if not isinstance(item, bool):
                raise PairedEvidenceError(f"measurement_policy.{key} must be a boolean")
            sanitized[key] = item
        else:
            sanitized[key] = _bounded_string(item, f"measurement_policy.{key}")
    expected = {
        key: item
        for key, item in quality_bar.measurement_policy.items()
        if key not in {"timed_samples", "warmup_passes", "memory_passes"}
    }
    if sanitized != expected:
        raise PairedEvidenceError("measurement_policy does not match the canonical quality bar")
    return sanitized


def _sanitize_scenarios(
    value: object,
    quality_bar: QualityBar,
    profile_name: str,
    role: TrackerScenarioRole,
) -> dict[str, object]:
    profile = quality_bar.profiles[profile_name]
    scenarios = _exact_mapping(value, set(profile.scenario_action_digests), "scenarios")
    sanitized: dict[str, object] = {}
    for name in sorted(profile.scenario_action_digests):
        raw_evidence = _exact_mapping(
            scenarios[name],
            {
                "outcome",
                "action_digest",
                "endpoint_counters",
                "exit_code",
                "terminal_phase",
                "observation_order",
                "mutation_counters",
                "isolation_counters",
            },
            f"scenarios.{name}",
        )
        if raw_evidence["outcome"] != "pass":
            raise PairedEvidenceError(f"scenarios.{name}.outcome must be pass")
        action_digest = _digest(raw_evidence["action_digest"], f"scenarios.{name}.action_digest")
        if action_digest != profile.scenario_action_digests[name]:
            raise PairedEvidenceError(f"scenarios.{name}.action_digest is not canonical")
        isolation_counters = _integer_mapping(
            raw_evidence["isolation_counters"],
            ISOLATION_COUNTER_KEYS,
            f"scenarios.{name}.isolation_counters",
        )
        if isolation_counters != dict(profile.isolation_counters):
            raise PairedEvidenceError(f"scenarios.{name}.isolation_counters are not canonical")
        mutation_counters = _integer_mapping(
            raw_evidence["mutation_counters"],
            MUTATION_COUNTER_KEYS,
            f"scenarios.{name}.mutation_counters",
        )
        if any(mutation_counters.values()):
            raise PairedEvidenceError(f"scenarios.{name}.mutation_counters must be zero")
        exit_code = _integer(raw_evidence["exit_code"], f"scenarios.{name}.exit_code")
        if exit_code not in {0, 1}:
            raise PairedEvidenceError(f"scenarios.{name}.exit_code is not a locked CLI result")
        terminal_phase = _bounded_string(
            raw_evidence["terminal_phase"],
            f"scenarios.{name}.terminal_phase",
            maximum=32,
        )
        raw_order = raw_evidence["observation_order"]
        if not isinstance(raw_order, list) or raw_order not in [["preview"], ["preview", "execution"]]:
            raise PairedEvidenceError(f"scenarios.{name}.observation_order is not a locked CLI phase order")
        sanitized_evidence = {
            "outcome": "pass",
            "action_digest": action_digest,
            "endpoint_counters": _endpoint_mapping(
                raw_evidence["endpoint_counters"],
                TRACKER_ENDPOINT_KEYS,
                f"scenarios.{name}.endpoint_counters",
            ),
            "exit_code": exit_code,
            "terminal_phase": terminal_phase,
            "observation_order": list(raw_order),
            "mutation_counters": mutation_counters,
            "isolation_counters": isolation_counters,
        }
        sanitized[name] = sanitized_evidence
    if not tracker_scenarios_match_role_contracts(
        sanitized,
        quality_bar.tracker_scenario_contracts,
        role,
    ):
        raise PairedEvidenceError("scenarios do not match the artifact transport role")
    return sanitized


def sanitize_child_result(  # noqa: C901
    value: object,
    quality_bar: QualityBar,
) -> dict[str, object]:
    """Validate and reconstruct one child artifact without retaining unknown data."""
    if not isinstance(value, dict):
        raise PairedEvidenceError("child result keys do not match the paired schema")
    raw_profile_kind = value.get("profile_kind")
    result_keys = CHILD_RESULT_KEYS | (TRACKER_RESULT_KEYS if raw_profile_kind == "tracker" else set())
    result = _exact_mapping(value, result_keys, "child result")
    if (
        result["schema"] != quality_bar.result_schema
        or result["schema_version"] != quality_bar.evaluator_schema_version
        or result["evaluator_version"] != quality_bar.evaluator_version
        or result["scope"] != quality_bar.scope
    ):
        raise PairedEvidenceError("child evaluator schema, version, or scope differs")
    profile_name = _bounded_string(result["profile"], "profile", maximum=64)
    if profile_name not in quality_bar.profiles:
        raise PairedEvidenceError("child profile is not canonical")
    canonical_profile = quality_bar.profiles[profile_name]
    profile_kind = _bounded_string(result["profile_kind"], "profile_kind", maximum=16)
    if profile_kind != canonical_profile.kind:
        raise PairedEvidenceError("child profile kind is not canonical")
    tier = _bounded_string(result["tier"], "tier", maximum=64)
    if tier != canonical_profile.tier:
        raise PairedEvidenceError("child profile tier is not canonical")
    workload_keys = ORPHAN_WORKLOAD_KEYS if profile_kind == "orphan" else TRACKER_WORKLOAD_KEYS
    endpoint_keys = ORPHAN_ENDPOINT_KEYS if profile_kind == "orphan" else TRACKER_ENDPOINT_KEYS
    candidate_keys = (
        {"orphan_files"}
        if profile_kind == "orphan"
        else {
            "default_tag_targets",
            "cross_seed_tag_targets",
            "torrent_only_deletes",
        }
    )
    expected_samples = quality_bar.measurement_policy.get("timed_samples")
    if isinstance(expected_samples, bool) or not isinstance(expected_samples, int):
        raise PairedEvidenceError("canonical timed sample count is malformed")

    raw_samples = result["sample_runtime_seconds"]
    if not isinstance(raw_samples, list) or len(raw_samples) != expected_samples:
        raise PairedEvidenceError("child runtime sample count is malformed")
    samples = [_finite_nonnegative(sample, f"sample_runtime_seconds[{index}]") for index, sample in enumerate(raw_samples)]
    if any(sample <= 0 for sample in samples):
        raise PairedEvidenceError("child runtime samples must be positive")

    timed_counters = result["timed_sample_endpoint_counters"]
    if not isinstance(timed_counters, list) or len(timed_counters) != expected_samples:
        raise PairedEvidenceError("timed sample endpoint counters are malformed")
    pass_counters = _exact_mapping(
        result["pass_endpoint_counters"],
        {"warmup", "memory"},
        "pass_endpoint_counters",
    )
    environment = _sanitize_environment(result["environment"])
    workload = _integer_mapping(result["workload"], workload_keys, "workload")
    reconciliation = _sanitize_reconciliation(result["reconciliation"], profile_kind=profile_kind)
    candidate_counts = _integer_mapping(
        result["candidate_counts"],
        candidate_keys,
        "candidate_counts",
        minimum=1,
    )
    identity_verified = result["identity_verified"]
    if not isinstance(identity_verified, bool):
        raise PairedEvidenceError("identity_verified must be a boolean")
    endpoint_counters = _endpoint_mapping(
        result["endpoint_counters"],
        endpoint_keys,
        "endpoint_counters",
    )
    timed_endpoint_counters = [
        _endpoint_mapping(item, endpoint_keys, f"timed_sample_endpoint_counters[{index}]")
        for index, item in enumerate(timed_counters)
    ]
    pass_endpoint_counters = {
        "warmup": _endpoint_mapping(
            pass_counters["warmup"],
            endpoint_keys,
            "pass_endpoint_counters.warmup",
        ),
        "memory": _endpoint_mapping(
            pass_counters["memory"],
            endpoint_keys,
            "pass_endpoint_counters.memory",
        ),
    }
    artifact_role: TrackerScenarioRole | None = None
    if profile_kind == "tracker":
        torrent_count = canonical_profile.workload["torrents"]
        artifact_role = derive_tracker_artifact_role(endpoint_counters, torrent_count)
        other_primary_counters = [*timed_endpoint_counters, *pass_endpoint_counters.values()]
        if artifact_role is None or any(
            derive_tracker_artifact_role(item, torrent_count) != artifact_role for item in other_primary_counters
        ):
            raise PairedEvidenceError("tracker primary evidence does not have one canonical transport role")
    sanitized_result: dict[str, object] = {
        "schema": quality_bar.result_schema,
        "schema_version": quality_bar.evaluator_schema_version,
        "evaluator_version": quality_bar.evaluator_version,
        "commit": _hex_identifier(result["commit"], "commit", length=40),
        "candidate_state": _sanitize_candidate_state(result["candidate_state"]),
        "identity_verified": identity_verified,
        "environment": environment,
        "scope": quality_bar.scope,
        "profile_kind": profile_kind,
        "profile": profile_name,
        "tier": tier,
        "seed": _integer(result["seed"], "seed"),
        "workload": workload,
        "fixture_manifest_digest": _digest(
            result["fixture_manifest_digest"],
            "fixture_manifest_digest",
        ),
        "intended_action_digest": _digest(
            result["intended_action_digest"],
            "intended_action_digest",
        ),
        "reconciliation": reconciliation,
        "candidate_counts": candidate_counts,
        "endpoint_counters": endpoint_counters,
        "timed_sample_endpoint_counters": timed_endpoint_counters,
        "pass_endpoint_counters": pass_endpoint_counters,
        "mutation_counters": _integer_mapping(
            result["mutation_counters"],
            MUTATION_COUNTER_KEYS,
            "mutation_counters",
        ),
        "measurement_policy": _sanitize_measurement_policy(
            result["measurement_policy"],
            quality_bar,
        ),
        "sample_runtime_seconds": samples,
        "median_runtime_seconds": _finite_nonnegative(
            result["median_runtime_seconds"],
            "median_runtime_seconds",
        ),
        "minimum_runtime_seconds": _finite_nonnegative(
            result["minimum_runtime_seconds"],
            "minimum_runtime_seconds",
        ),
        "maximum_runtime_seconds": _finite_nonnegative(
            result["maximum_runtime_seconds"],
            "maximum_runtime_seconds",
        ),
        "median_absolute_deviation_seconds": _finite_nonnegative(
            result["median_absolute_deviation_seconds"],
            "median_absolute_deviation_seconds",
        ),
        "peak_memory_bytes": _integer(
            result["peak_memory_bytes"],
            "peak_memory_bytes",
            minimum=1,
        ),
    }
    if profile_kind == "tracker":
        if artifact_role is None:
            raise PairedEvidenceError("tracker artifact transport role is missing")
        execution_action_digest = _digest(
            result["execution_action_digest"],
            "execution_action_digest",
        )
        if execution_action_digest != canonical_profile.execution_action_digest:
            raise PairedEvidenceError("execution_action_digest is not canonical")
        isolation_counters = _integer_mapping(
            result["isolation_counters"],
            ISOLATION_COUNTER_KEYS,
            "isolation_counters",
        )
        if isolation_counters != dict(canonical_profile.isolation_counters):
            raise PairedEvidenceError("isolation_counters are not canonical")
        sanitized_result["execution_action_digest"] = execution_action_digest
        sanitized_result["isolation_counters"] = isolation_counters
        sanitized_result["scenarios"] = _sanitize_scenarios(
            result["scenarios"],
            quality_bar,
            profile_name,
            artifact_role,
        )
    return sanitized_result


__all__ = ["PairedEvidenceError", "sanitize_child_result"]
