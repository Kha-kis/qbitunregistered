"""Strict validation and reconstruction of paired-gauntlet child evidence."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import cast

from benchmarks.gauntlet.baseline import (
    ENVIRONMENT_KEYS,
    MUTATION_COUNTER_KEYS,
    QualityBar,
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
WORKLOAD_KEYS = {
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
RECONCILIATION_KEYS = {
    "file_action_count",
    "empty_directory_count",
    "file_action_digest",
    "empty_directory_digest",
    "digest",
}
ENDPOINT_KEYS = {
    "application.default_save_path",
    "torrent_categories.categories",
    "torrents.info",
    "torrents_files",
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


def _endpoint_mapping(value: object, description: str) -> dict[str, int]:
    return _integer_mapping(value, ENDPOINT_KEYS, description)


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


def _sanitize_reconciliation(value: object) -> dict[str, object]:
    reconciliation = _exact_mapping(value, RECONCILIATION_KEYS, "reconciliation")
    return {
        "file_action_count": _integer(
            reconciliation["file_action_count"],
            "reconciliation.file_action_count",
            minimum=1,
        ),
        "empty_directory_count": _integer(
            reconciliation["empty_directory_count"],
            "reconciliation.empty_directory_count",
            minimum=1,
        ),
        "file_action_digest": _digest(
            reconciliation["file_action_digest"],
            "reconciliation.file_action_digest",
        ),
        "empty_directory_digest": _digest(
            reconciliation["empty_directory_digest"],
            "reconciliation.empty_directory_digest",
        ),
        "digest": _digest(reconciliation["digest"], "reconciliation.digest"),
    }


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


def sanitize_child_result(
    value: object,
    quality_bar: QualityBar,
) -> dict[str, object]:
    """Validate and reconstruct one child artifact without retaining unknown data."""
    result = _exact_mapping(value, CHILD_RESULT_KEYS, "child result")
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
    workload = _integer_mapping(result["workload"], WORKLOAD_KEYS, "workload")
    reconciliation = _sanitize_reconciliation(result["reconciliation"])
    candidate_counts = _integer_mapping(
        result["candidate_counts"],
        {"orphan_files"},
        "candidate_counts",
        minimum=1,
    )
    identity_verified = result["identity_verified"]
    if not isinstance(identity_verified, bool):
        raise PairedEvidenceError("identity_verified must be a boolean")
    return {
        "schema": quality_bar.result_schema,
        "schema_version": quality_bar.evaluator_schema_version,
        "evaluator_version": quality_bar.evaluator_version,
        "commit": _hex_identifier(result["commit"], "commit", length=40),
        "candidate_state": _sanitize_candidate_state(result["candidate_state"]),
        "identity_verified": identity_verified,
        "environment": environment,
        "scope": quality_bar.scope,
        "profile": profile_name,
        "tier": _bounded_string(result["tier"], "tier", maximum=64),
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
        "endpoint_counters": _endpoint_mapping(
            result["endpoint_counters"],
            "endpoint_counters",
        ),
        "timed_sample_endpoint_counters": [
            _endpoint_mapping(item, f"timed_sample_endpoint_counters[{index}]") for index, item in enumerate(timed_counters)
        ],
        "pass_endpoint_counters": {
            "warmup": _endpoint_mapping(
                pass_counters["warmup"],
                "pass_endpoint_counters.warmup",
            ),
            "memory": _endpoint_mapping(
                pass_counters["memory"],
                "pass_endpoint_counters.memory",
            ),
        },
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


__all__ = ["PairedEvidenceError", "sanitize_child_result"]
