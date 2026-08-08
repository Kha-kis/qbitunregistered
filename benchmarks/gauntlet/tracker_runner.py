"""Run the deterministic unregistered tracker preview and dry-run evaluator."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol, TypedDict, cast

from benchmarks.gauntlet.fixture_factory import MUTATION_COUNTER_KEYS
from benchmarks.gauntlet.runner import (
    DEFAULT_SAMPLES,
    GauntletSafetyError,
    MeasurementPolicy,
    PassEndpointCounters,
    _filesystem_digest,
    _measurement_policy,
)
from benchmarks.gauntlet.tracker_fixture import (
    TRACKER_READ_ENDPOINTS,
    EmbeddedTrackersMode,
    TrackerGauntletFixture,
    TrackerGauntletProfile,
    build_tracker_fixture,
    expected_tracker_action_digest,
    expected_tracker_action_records,
)
from qbitunregistered.cache import clear_cache
from qbitunregistered.file_operations import SafetyCheckError
from qbitunregistered.impact import ImpactAnalysisError, ImpactSummary, analyze_impact
from qbitunregistered.operations.unregistered_checks import (
    DeletionAction,
    unregistered_checks,
)
from qbitunregistered.types import TorrentInfo

DEFAULT_TAG = "unregistered"
CROSS_SEED_TAG = "unregistered:crossseeding"
DELETE_TAG = "tracker-delete"


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


class TrackerWorkloadResult(TypedDict):
    """Every locked tracker workload and measurement dimension."""

    torrents: int
    tracker_records: int
    save_path_groups: int
    exact_message_targets: int
    prefix_message_targets: int
    default_tag_targets: int
    cross_seed_tag_targets: int
    torrent_only_delete_targets: int
    timed_samples: int
    warmup_passes: int
    memory_passes: int


class TrackerReconciliationEvidence(TypedDict):
    """Sanitized execution results that do not expose fixture paths."""

    save_path_group_count: int
    torrent_path_count: int
    unregistered_tracker_count: int
    default_tag_action_count: int
    cross_seed_tag_action_count: int
    torrent_only_delete_action_count: int
    digest: str


class TrackerEvaluationResult(TypedDict):
    """Measurements and safety evidence for one tracker fixture."""

    profile_kind: str
    profile: str
    tier: str
    seed: int
    workload: TrackerWorkloadResult
    fixture_manifest_digest: str
    intended_action_digest: str
    reconciliation: TrackerReconciliationEvidence
    scenarios: dict[str, TrackerScenarioEvidence]
    candidate_counts: dict[str, int]
    endpoint_counters: dict[str, int]
    timed_sample_endpoint_counters: list[dict[str, int]]
    pass_endpoint_counters: PassEndpointCounters
    mutation_counters: dict[str, int]
    measurement_policy: MeasurementPolicy
    sample_runtime_seconds: list[float]
    median_runtime_seconds: float
    minimum_runtime_seconds: float
    maximum_runtime_seconds: float
    median_absolute_deviation_seconds: float
    peak_memory_bytes: int


class TrackerScenarioEvidence(TypedDict):
    """One sanitized semantic-safety scenario outcome."""

    outcome: str
    action_digest: str
    endpoint_counters: dict[str, int]


@dataclass(frozen=True, slots=True)
class _TrackerPipelineResult:
    summary: ImpactSummary
    torrent_file_paths: dict[str, list[str]]
    unregistered_counts: dict[str, int]
    operator_counts: dict[str, int]


@dataclass(frozen=True, slots=True)
class _TrackerPassEvidence:
    action_digest: str
    reconciliation: TrackerReconciliationEvidence
    candidate_counts: dict[str, int]
    endpoint_counters: dict[str, int]


_ACTIVE_FILESYSTEM_AUDITS: list[_FilesystemMutationAudit] = []
_FILESYSTEM_AUDIT_HOOK_INSTALLED = False
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_MUTATING_PATH_EVENTS: Mapping[str, tuple[tuple[int, int | None], ...]] = {
    "os.chmod": ((0, 2),),
    "os.chown": ((0, 3),),
    "os.link": ((0, 2), (1, 3)),
    "os.mkdir": ((0, 2),),
    "os.remove": ((0, 1),),
    "os.rename": ((0, 2), (1, 3)),
    "os.rmdir": ((0, 1),),
    "os.symlink": ((1, 2),),
    "os.truncate": ((0, None),),
    "os.utime": ((0, 3),),
}


@dataclass(slots=True)
class _FilesystemMutationAudit:
    """Count filesystem mutation attempts lexically or physically inside one root."""

    root: Path
    attempt_count: int = 0

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    def __enter__(self) -> _FilesystemMutationAudit:
        _install_filesystem_audit_hook()
        _ACTIVE_FILESYSTEM_AUDITS.append(self)
        return self

    def __exit__(self, _error_type: object, _error: object, _traceback: object) -> None:
        _ACTIVE_FILESYSTEM_AUDITS.remove(self)

    def observe(self, event: str, arguments: tuple[object, ...]) -> None:
        """Record one audited mutation event when any target belongs to this root."""
        if event == "open":
            if len(arguments) >= 3 and _open_requests_write(arguments[1], arguments[2]):
                self._record_path(arguments[0], None)
            return
        for path_index, directory_fd_index in _MUTATING_PATH_EVENTS.get(event, ()):
            directory_fd = (
                arguments[directory_fd_index]
                if directory_fd_index is not None and directory_fd_index < len(arguments)
                else None
            )
            if path_index < len(arguments) and self._path_is_within_root(arguments[path_index], directory_fd):
                self.attempt_count += 1
                return

    def _record_path(self, raw_path: object, directory_fd: object) -> None:
        if self._path_is_within_root(raw_path, directory_fd):
            self.attempt_count += 1

    def _path_is_within_root(self, raw_path: object, directory_fd: object) -> bool:
        if not isinstance(raw_path, (str, bytes, os.PathLike)):
            return False
        path = Path(os.fsdecode(raw_path))
        if not path.is_absolute():
            base = Path.cwd()
            if isinstance(directory_fd, int) and directory_fd >= 0:
                try:
                    base = Path(f"/proc/self/fd/{directory_fd}").resolve(strict=True)
                except OSError:
                    return False
            path = base / path
        lexical_path = Path(os.path.abspath(path))
        try:
            resolved_path = path.resolve(strict=False)
        except OSError:
            resolved_path = lexical_path
        return _is_relative_to(lexical_path, self.root) or _is_relative_to(resolved_path, self.root)


def _is_relative_to(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _open_requests_write(raw_mode: object, raw_flags: object) -> bool:
    if isinstance(raw_mode, str) and any(marker in raw_mode for marker in "wax+"):
        return True
    return isinstance(raw_flags, int) and bool(raw_flags & _WRITE_OPEN_FLAGS)


def _filesystem_audit_hook(event: str, arguments: tuple[object, ...]) -> None:
    for audit in tuple(_ACTIVE_FILESYSTEM_AUDITS):
        audit.observe(event, arguments)


def _install_filesystem_audit_hook() -> None:
    global _FILESYSTEM_AUDIT_HOOK_INSTALLED
    if not _FILESYSTEM_AUDIT_HOOK_INSTALLED:
        sys.addaudithook(_filesystem_audit_hook)
        _FILESYSTEM_AUDIT_HOOK_INSTALLED = True


class _TrackerReconciliationCapture(logging.Handler):
    """Capture only sanitized counts from operator-visible dry-run messages."""

    _TAG_PATTERN = re.compile(r"^\[Dry Run\] Would add tag '([^']+)' to ([0-9]+) torrents$")

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.default_tag_actions = 0
        self.cross_seed_tag_actions = 0
        self.torrent_only_deletions = 0

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        match = self._TAG_PATTERN.fullmatch(message)
        if match is not None:
            tag, raw_count = match.groups()
            if tag == DEFAULT_TAG:
                self.default_tag_actions += int(raw_count)
            elif tag == CROSS_SEED_TAG:
                self.cross_seed_tag_actions += int(raw_count)
            return
        if message.startswith("[Dry Run] Would delete torrent '") and message.endswith("' but keep its files."):
            self.torrent_only_deletions += 1

    def counts(self) -> dict[str, int]:
        """Return the exact action counts visible to an operator."""
        return {
            "default_tag_actions": self.default_tag_actions,
            "cross_seed_tag_actions": self.cross_seed_tag_actions,
            "torrent_only_deletions": self.torrent_only_deletions,
        }


def _digest_record(digest: _Digest, record: object) -> None:
    digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def tracker_config() -> dict[str, object]:
    """Return the locked non-file-deleting unregistered configuration."""
    return {
        "default_unregistered_tag": DEFAULT_TAG,
        "cross_seeding_tag": CROSS_SEED_TAG,
        "unregistered": ["torrent is not registered", "starts_with:tracker prefix unavailable"],
        "use_delete_tags": True,
        "use_delete_files": False,
        "delete_tags": [DELETE_TAG],
        "delete_files": {DELETE_TAG: False},
    }


def _production_torrents(fixture: TrackerGauntletFixture) -> Sequence[TorrentInfo]:
    """Expose frozen evaluator torrents through the production read protocol."""
    return cast(Sequence[TorrentInfo], fixture.initial_torrents)


def tracker_fixture_manifest_digest(fixture: TrackerGauntletFixture) -> str:
    """Hash the complete sanitized tracker fixture without retaining URLs."""
    profile = fixture.profile
    digest = hashlib.sha256()
    _digest_record(
        digest,
        {
            "name": profile.name,
            "seed": fixture.seed,
            "torrent_count": profile.torrent_count,
            "tracker_record_count": profile.tracker_record_count,
            "save_path_group_count": profile.save_path_group_count,
            "default_tag_count": profile.default_tag_count,
            "cross_seed_tag_count": profile.cross_seed_tag_count,
            "delete_count": profile.delete_count,
            "tier": profile.tier,
        },
    )
    tracker_count = 0
    seen_hashes: set[str] = set()
    for torrent in fixture.initial_torrents:
        if torrent.hash in seen_hashes:
            raise GauntletSafetyError("tracker fixture contains a duplicate torrent hash")
        seen_hashes.add(torrent.hash)
        save_path_group = Path(torrent.save_path).name
        if not re.fullmatch(r"group-[0-9]{5}", save_path_group):
            raise GauntletSafetyError("tracker fixture contains an unsafe save-path group")
        _digest_record(
            digest,
            {
                "hash": torrent.hash,
                "name": torrent.name,
                "save_path_group": save_path_group,
                "tags": torrent.tags,
            },
        )
        trackers = fixture.client.trackers_by_hash.get(torrent.hash)
        if not isinstance(trackers, list) or len(trackers) != 3:
            raise GauntletSafetyError("tracker fixture does not contain three real trackers per torrent")
        for tracker in trackers:
            if not isinstance(tracker, dict):
                raise GauntletSafetyError("tracker fixture contains malformed tracker metadata")
            _digest_record(digest, {"torrent_hash": torrent.hash, **tracker})
            tracker_count += 1
    if len(seen_hashes) != profile.torrent_count or tracker_count != profile.tracker_record_count:
        raise GauntletSafetyError("tracker fixture workload does not match its profile")
    return digest.hexdigest()


def _action_records(summary: ImpactSummary) -> list[dict[str, str]]:
    records = [
        {"action": "add_tag", "tag": tag, "torrent_hash": torrent_hash}
        for tag, hashes in summary.torrents_to_tag.items()
        for torrent_hash in hashes
    ]
    plan = summary.unregistered_deletion_plan
    if plan is None:
        raise GauntletSafetyError("tracker preview did not produce a deletion plan")
    for deletion in plan.deletions:
        if deletion.action is not DeletionAction.TORRENT_ONLY:
            raise GauntletSafetyError("tracker fixture unexpectedly planned a filesystem deletion")
        records.append(
            {
                "action": "delete_torrent_only",
                "tag": deletion.matching_tag,
                "torrent_hash": deletion.torrent_hash,
            }
        )
    return sorted(records, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))


def _intended_action_digest(summary: ImpactSummary) -> str:
    digest = hashlib.sha256()
    for record in _action_records(summary):
        _digest_record(digest, record)
    return digest.hexdigest()


def _candidate_counts(summary: ImpactSummary) -> dict[str, int]:
    plan = summary.unregistered_deletion_plan
    return {
        "default_tag_targets": len(summary.torrents_to_tag.get(DEFAULT_TAG, [])),
        "cross_seed_tag_targets": len(summary.torrents_to_tag.get(CROSS_SEED_TAG, [])),
        "torrent_only_deletes": len(plan.deletions) if plan is not None else 0,
    }


def _reconciliation_evidence(
    fixture: TrackerGauntletFixture,
    pipeline: _TrackerPipelineResult,
) -> TrackerReconciliationEvidence:
    digest = hashlib.sha256()
    group_count = 0
    torrent_count = 0
    unregistered_count = 0
    for save_path, torrent_hashes in sorted(pipeline.torrent_file_paths.items()):
        try:
            relative = Path(save_path).relative_to(fixture.root)
        except ValueError as error:
            raise GauntletSafetyError("tracker reconciliation path escaped the fixture root") from error
        if len(relative.parts) != 1 or not re.fullmatch(r"group-[0-9]{5}", relative.name):
            raise GauntletSafetyError("tracker reconciliation path is not a fixture group")
        tracker_hits = pipeline.unregistered_counts.get(save_path, 0)
        _digest_record(
            digest,
            {
                "path": relative.name,
                "torrent_count": len(torrent_hashes),
                "unregistered_tracker_count": tracker_hits,
            },
        )
        group_count += 1
        torrent_count += len(torrent_hashes)
        unregistered_count += tracker_hits
    default_actions = pipeline.operator_counts["default_tag_actions"]
    cross_actions = pipeline.operator_counts["cross_seed_tag_actions"]
    delete_actions = pipeline.operator_counts["torrent_only_deletions"]
    for record in (
        {"action": "add_tag", "tag": DEFAULT_TAG, "count": default_actions},
        {"action": "add_tag", "tag": CROSS_SEED_TAG, "count": cross_actions},
        {"action": "delete_torrent_only", "tag": DELETE_TAG, "count": delete_actions},
    ):
        _digest_record(digest, record)
    return {
        "save_path_group_count": group_count,
        "torrent_path_count": torrent_count,
        "unregistered_tracker_count": unregistered_count,
        "default_tag_action_count": default_actions,
        "cross_seed_tag_action_count": cross_actions,
        "torrent_only_delete_action_count": delete_actions,
        "digest": digest.hexdigest(),
    }


def validate_tracker_endpoint_counts(
    endpoint_counts: Mapping[str, int],
    profile: TrackerGauntletProfile,
) -> None:
    """Accept only one complete exact or one complete embedded tracker transport."""
    if set(endpoint_counts) != set(TRACKER_READ_ENDPOINTS):
        raise GauntletSafetyError("tracker API evidence does not match the locked endpoint schema")
    if any(isinstance(count, bool) or not isinstance(count, int) or count < 0 for count in endpoint_counts.values()):
        raise GauntletSafetyError("tracker API evidence contains malformed counts")
    if endpoint_counts["torrents.info"] != 0:
        raise GauntletSafetyError("tracker API evidence contains an ordinary torrent snapshot")
    transport = (
        endpoint_counts["torrents.info.include_trackers"],
        endpoint_counts["torrents_trackers"],
    )
    if transport not in {(0, profile.torrent_count), (1, 0)}:
        raise GauntletSafetyError("tracker API evidence is partial, redundant, or outside the locked budget")


def _prepare_pass(fixture: TrackerGauntletFixture) -> None:
    clear_cache()
    fixture.client.reset_read_counts()


def _execute_pipeline(fixture: TrackerGauntletFixture) -> _TrackerPipelineResult:
    config = tracker_config()
    summary = analyze_impact(fixture.client, _production_torrents(fixture), config, ["unregistered"])
    plan = summary.unregistered_deletion_plan
    if plan is None:
        raise GauntletSafetyError("tracker preview did not produce a deletion plan")
    capture = _TrackerReconciliationCapture()
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(capture)
    root_logger.setLevel(logging.INFO)
    try:
        torrent_file_paths, unregistered_counts = unregistered_checks(
            fixture.client,
            _production_torrents(fixture),
            config,
            True,
            [DELETE_TAG],
            {DELETE_TAG: False},
            True,
            deletion_plan=plan,
        )
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(previous_level)
    return _TrackerPipelineResult(
        summary=summary,
        torrent_file_paths=torrent_file_paths,
        unregistered_counts=unregistered_counts,
        operator_counts=capture.counts(),
    )


def _validate_unchanged_state(
    fixture: TrackerGauntletFixture,
    initial_filesystem_digest: str,
    filesystem_audit: _FilesystemMutationAudit,
) -> None:
    if filesystem_audit.attempt_count:
        raise GauntletSafetyError("tracker dry-run attempted a filesystem mutation")
    if fixture.client.mutation_total:
        raise GauntletSafetyError("tracker dry-run attempted a qBittorrent mutation")
    if _filesystem_digest(fixture.root) != initial_filesystem_digest:
        raise GauntletSafetyError("tracker dry-run changed the fixture filesystem")


def _validate_pass(
    fixture: TrackerGauntletFixture,
    pipeline: _TrackerPipelineResult,
    initial_filesystem_digest: str,
    filesystem_audit: _FilesystemMutationAudit,
) -> _TrackerPassEvidence:
    _validate_unchanged_state(fixture, initial_filesystem_digest, filesystem_audit)
    endpoint_counters = dict(fixture.client.read_counts)
    validate_tracker_endpoint_counts(endpoint_counters, fixture.profile)
    candidate_counts = _candidate_counts(pipeline.summary)
    expected_candidates = {
        "default_tag_targets": fixture.profile.default_tag_count,
        "cross_seed_tag_targets": fixture.profile.cross_seed_tag_count,
        "torrent_only_deletes": fixture.profile.delete_count,
    }
    if candidate_counts != expected_candidates:
        raise GauntletSafetyError("tracker candidates did not match the fixture oracle")
    action_records = _action_records(pipeline.summary)
    expected_action_records = list(expected_tracker_action_records(fixture.profile, fixture.seed))
    if action_records != expected_action_records:
        raise GauntletSafetyError("tracker action records did not match the independent fixture oracle")
    action_digest = _intended_action_digest(pipeline.summary)
    if action_digest != expected_tracker_action_digest(fixture.profile, fixture.seed):
        raise GauntletSafetyError("tracker action digest did not match the independent fixture oracle")
    reconciliation = _reconciliation_evidence(fixture, pipeline)
    expected_reconciliation_counts = (
        fixture.profile.save_path_group_count,
        fixture.profile.torrent_count,
        fixture.profile.default_tag_count + fixture.profile.cross_seed_tag_count,
        fixture.profile.default_tag_count,
        fixture.profile.cross_seed_tag_count,
        fixture.profile.delete_count,
    )
    actual_reconciliation_counts = (
        reconciliation["save_path_group_count"],
        reconciliation["torrent_path_count"],
        reconciliation["unregistered_tracker_count"],
        reconciliation["default_tag_action_count"],
        reconciliation["cross_seed_tag_action_count"],
        reconciliation["torrent_only_delete_action_count"],
    )
    if actual_reconciliation_counts != expected_reconciliation_counts:
        raise GauntletSafetyError("tracker dry-run reconciliation did not match the fixture oracle")
    return _TrackerPassEvidence(
        action_digest=action_digest,
        reconciliation=reconciliation,
        candidate_counts=candidate_counts,
        endpoint_counters=endpoint_counters,
    )


def _checked_pipeline_pass(
    fixture: TrackerGauntletFixture,
    initial_filesystem_digest: str,
    filesystem_audit: _FilesystemMutationAudit,
) -> _TrackerPassEvidence:
    _prepare_pass(fixture)
    try:
        pipeline = _execute_pipeline(fixture)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        _validate_unchanged_state(fixture, initial_filesystem_digest, filesystem_audit)
        raise
    finally:
        clear_cache()
    return _validate_pass(fixture, pipeline, initial_filesystem_digest, filesystem_audit)


def _timed_pipeline_pass(
    fixture: TrackerGauntletFixture,
    initial_filesystem_digest: str,
    filesystem_audit: _FilesystemMutationAudit,
) -> tuple[_TrackerPassEvidence, float]:
    _prepare_pass(fixture)
    started_at = time.perf_counter()
    try:
        pipeline = _execute_pipeline(fixture)
    except (KeyboardInterrupt, SystemExit):
        clear_cache()
        raise
    except Exception:
        _validate_unchanged_state(fixture, initial_filesystem_digest, filesystem_audit)
        clear_cache()
        raise
    elapsed_seconds = time.perf_counter() - started_at
    clear_cache()
    return _validate_pass(fixture, pipeline, initial_filesystem_digest, filesystem_audit), elapsed_seconds


def _memory_pipeline_pass(
    fixture: TrackerGauntletFixture,
    initial_filesystem_digest: str,
    filesystem_audit: _FilesystemMutationAudit,
) -> tuple[_TrackerPassEvidence, int]:
    _prepare_pass(fixture)
    tracemalloc.start()
    try:
        pipeline = _execute_pipeline(fixture)
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        _validate_unchanged_state(fixture, initial_filesystem_digest, filesystem_audit)
        raise
    finally:
        tracemalloc.stop()
        clear_cache()
    return _validate_pass(fixture, pipeline, initial_filesystem_digest, filesystem_audit), peak_bytes


def _scenario_digest(name: str, outcome: str) -> str:
    digest = hashlib.sha256()
    _digest_record(digest, {"scenario": name, "safety": outcome})
    return digest.hexdigest()


def _scenario_endpoint_counters(fixture: TrackerGauntletFixture) -> dict[str, int]:
    counters = dict(fixture.client.read_counts)
    if set(counters) != set(TRACKER_READ_ENDPOINTS) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counters.values()
    ):
        raise GauntletSafetyError("tracker scenario endpoint evidence is malformed")
    return counters


def _validate_scenario_unchanged(fixture: TrackerGauntletFixture, before: str) -> None:
    if fixture.client.mutation_total:
        raise GauntletSafetyError("tracker safety scenario attempted a qBittorrent mutation")
    if _filesystem_digest(fixture.root) != before:
        raise GauntletSafetyError("tracker safety scenario changed the fixture filesystem")


def evaluate_tracker_scenarios(  # noqa: C901
    _fixture: TrackerGauntletFixture,
) -> dict[str, TrackerScenarioEvidence]:
    """Run the transport-aware semantic matrix outside measured execution."""
    scenario_profile = TrackerGauntletProfile(
        name="tracker-scenarios",
        torrent_count=6,
        tracker_record_count=18,
        save_path_group_count=4,
        default_tag_count=2,
        cross_seed_tag_count=2,
        delete_count=1,
        tier="scenario",
    )
    evidence: dict[str, TrackerScenarioEvidence] = {}

    def record(
        name: str,
        fixture: TrackerGauntletFixture,
        before: str,
        *,
        action_digest: str | None = None,
    ) -> None:
        _validate_scenario_unchanged(fixture, before)
        counters = _scenario_endpoint_counters(fixture)
        evidence[name] = {
            "outcome": "pass",
            "action_digest": action_digest or _scenario_digest(name, "fail_closed"),
            "endpoint_counters": counters,
        }

    with tempfile.TemporaryDirectory(prefix="qbitunregistered-tracker-scenarios-") as temporary_root:
        scenario_root = Path(temporary_root)
        compatibility_modes: tuple[tuple[str, EmbeddedTrackersMode], ...] = (
            ("complete_embedded", "supported"),
            ("omitted_embedded_fallback", "omitted"),
            ("rejected_embedded_fallback", "rejected"),
            ("malformed_embedded_transport_aware", "malformed"),
        )
        for offset, (name, mode) in enumerate(compatibility_modes):
            fixture = build_tracker_fixture(
                scenario_root / name,
                scenario_profile,
                30_000 + offset,
                embedded_trackers_mode=mode,
            )
            before = _filesystem_digest(fixture.root)
            clear_cache()
            try:
                summary = analyze_impact(
                    fixture.client,
                    _production_torrents(fixture),
                    tracker_config(),
                    ["unregistered"],
                )
            except ImpactAnalysisError:
                counters = _scenario_endpoint_counters(fixture)
                if mode != "malformed" or counters["torrents.info.include_trackers"] != 1:
                    raise GauntletSafetyError(
                        "tracker compatibility scenario failed without consuming malformed bulk metadata"
                    )
                record(name, fixture, before)
            else:
                counters = _scenario_endpoint_counters(fixture)
                transport = (counters["torrents.info.include_trackers"], counters["torrents_trackers"])
                if mode == "supported" and transport not in {(0, 6), (1, 0)}:
                    raise GauntletSafetyError("supported tracker scenario used an incomplete transport")
                if mode in {"omitted", "rejected"} and transport not in {(0, 6), (1, 6)}:
                    raise GauntletSafetyError("tracker compatibility fallback evidence is incomplete")
                if mode == "malformed" and transport != (0, 6):
                    raise GauntletSafetyError("malformed embedded metadata was normalized without a safe exact-only control")
                record(name, fixture, before, action_digest=_intended_action_digest(summary))

        fixture = build_tracker_fixture(
            scenario_root / "malformed-exact",
            scenario_profile,
            30_010,
            embedded_trackers_mode="omitted",
        )
        before = _filesystem_digest(fixture.root)
        fixture.client.set_exact_trackers(fixture.initial_torrents[0].hash, None)
        clear_cache()
        try:
            analyze_impact(fixture.client, _production_torrents(fixture), tracker_config(), ["unregistered"])
        except ImpactAnalysisError:
            record("malformed_exact_fail_closed", fixture, before)
        else:
            raise GauntletSafetyError("malformed exact tracker metadata did not fail closed")

        fixture = build_tracker_fixture(
            scenario_root / "disappearance",
            scenario_profile,
            30_011,
            embedded_trackers_mode="omitted",
        )
        before = _filesystem_digest(fixture.root)
        failed = fixture.initial_torrents[0]
        fixture.client.set_exact_trackers(failed.hash, OSError("removed"))
        fixture.client.set_torrent_snapshot(fixture.initial_torrents[1:])
        clear_cache()
        summary = analyze_impact(
            fixture.client,
            _production_torrents(fixture),
            tracker_config(),
            ["unregistered"],
        )
        plan = summary.unregistered_deletion_plan
        if plan is None or plan.confirmed_absent_hashes != (failed.hash,):
            raise GauntletSafetyError("tracker disappearance scenario lacked an exact fresh absence proof")
        record("proven_disappearance", fixture, before, action_digest=_intended_action_digest(summary))

        fixture = build_tracker_fixture(
            scenario_root / "readd",
            scenario_profile,
            30_012,
            embedded_trackers_mode="omitted",
        )
        before = _filesystem_digest(fixture.root)
        failed = fixture.initial_torrents[0]
        fixture.client.set_exact_trackers(failed.hash, OSError("removed and re-added"))
        current = list(fixture.initial_torrents)
        current[0] = replace(failed, name="same-hash-readded")
        fixture.client.set_torrent_snapshot(current)
        clear_cache()
        try:
            analyze_impact(fixture.client, _production_torrents(fixture), tracker_config(), ["unregistered"])
        except ImpactAnalysisError:
            record("same_hash_readd_fail_closed", fixture, before)
        else:
            raise GauntletSafetyError("same-hash re-add did not remain active and fail closed")

        for offset, (name, snapshot) in enumerate(
            (
                ("malformed_refresh_fail_closed", [SimpleNamespace(hash=""), SimpleNamespace(hash="valid")]),
                ("duplicate_refresh_fail_closed", [SimpleNamespace(hash="duplicate"), SimpleNamespace(hash="duplicate")]),
            )
        ):
            fixture = build_tracker_fixture(
                scenario_root / name,
                scenario_profile,
                30_020 + offset,
                embedded_trackers_mode="omitted",
            )
            before = _filesystem_digest(fixture.root)
            fixture.client.set_exact_trackers(fixture.initial_torrents[0].hash, OSError("unavailable"))
            fixture.client.set_torrent_snapshot(snapshot)
            clear_cache()
            try:
                analyze_impact(
                    fixture.client,
                    _production_torrents(fixture),
                    tracker_config(),
                    ["unregistered"],
                )
            except ImpactAnalysisError:
                record(name, fixture, before)
            else:
                raise GauntletSafetyError("malformed tracker refresh did not fail closed")

        for offset, (name, change) in enumerate(
            (
                ("delete_disappearance_preflight", "disappear"),
                ("delete_tag_change_preflight", "tag"),
            )
        ):
            fixture = build_tracker_fixture(scenario_root / name, scenario_profile, 30_030 + offset)
            before = _filesystem_digest(fixture.root)
            clear_cache()
            config = tracker_config()
            summary = analyze_impact(fixture.client, _production_torrents(fixture), config, ["unregistered"])
            plan = summary.unregistered_deletion_plan
            if plan is None:
                raise GauntletSafetyError("tracker preflight scenario lacks a deletion plan")
            current = list(fixture.initial_torrents)
            if change == "disappear":
                current = current[1:]
            else:
                current[0] = replace(current[0], tags="")
            fixture.client.set_torrent_snapshot(current)
            try:
                unregistered_checks(
                    fixture.client,
                    _production_torrents(fixture),
                    config,
                    True,
                    [DELETE_TAG],
                    {DELETE_TAG: False},
                    False,
                    deletion_plan=plan,
                )
            except SafetyCheckError:
                record(name, fixture, before, action_digest=_intended_action_digest(summary))
            else:
                raise GauntletSafetyError("tracker mutating preflight churn did not fail closed")

        name = "tracker_change_snapshot_bound"
        fixture = build_tracker_fixture(scenario_root / name, scenario_profile, 30_040)
        before = _filesystem_digest(fixture.root)
        clear_cache()
        config = tracker_config()
        summary = analyze_impact(fixture.client, _production_torrents(fixture), config, ["unregistered"])
        plan = summary.unregistered_deletion_plan
        if plan is None:
            raise GauntletSafetyError("tracker snapshot-binding scenario lacks a deletion plan")
        changed_hash = fixture.initial_torrents[0].hash
        fixture.client.set_exact_trackers(changed_hash, fixture.client.trackers_by_hash[fixture.initial_torrents[-1].hash])
        _paths, counts = unregistered_checks(
            fixture.client,
            _production_torrents(fixture),
            config,
            True,
            [DELETE_TAG],
            {DELETE_TAG: False},
            True,
            deletion_plan=plan,
        )
        if sum(counts.values()) != scenario_profile.default_tag_count + scenario_profile.cross_seed_tag_count:
            raise GauntletSafetyError("tracker dry-run was not bound to the preview snapshot")
        record(name, fixture, before, action_digest=_intended_action_digest(summary))
    clear_cache()
    return evidence


def evaluate_tracker_fixture(
    fixture: TrackerGauntletFixture,
    *,
    samples: int,
) -> TrackerEvaluationResult:
    """Evaluate the real unregistered preview/dry-run pipeline without setup time."""
    if samples != DEFAULT_SAMPLES:
        raise ValueError(f"comparable gauntlet runs require exactly {DEFAULT_SAMPLES} timed samples")
    if tracemalloc.is_tracing():
        raise GauntletSafetyError("tracemalloc must be disabled before gauntlet evaluation")
    scenarios = evaluate_tracker_scenarios(fixture)
    manifest_digest = tracker_fixture_manifest_digest(fixture)
    initial_filesystem_digest = _filesystem_digest(fixture.root)
    filesystem_audit = _FilesystemMutationAudit(fixture.root)
    with filesystem_audit:
        warmup = _checked_pipeline_pass(fixture, initial_filesystem_digest, filesystem_audit)
        timed_evidence: list[_TrackerPassEvidence] = []
        runtimes: list[float] = []
        for _sample_index in range(samples):
            evidence, runtime = _timed_pipeline_pass(fixture, initial_filesystem_digest, filesystem_audit)
            timed_evidence.append(evidence)
            runtimes.append(runtime)
        memory_evidence, peak_memory_bytes = _memory_pipeline_pass(
            fixture,
            initial_filesystem_digest,
            filesystem_audit,
        )
    all_evidence = [warmup, *timed_evidence, memory_evidence]
    if any(
        item.action_digest != warmup.action_digest
        or item.reconciliation != warmup.reconciliation
        or item.candidate_counts != warmup.candidate_counts
        for item in all_evidence[1:]
    ):
        raise GauntletSafetyError("tracker evaluator evidence changed between passes")

    median_runtime = statistics.median(runtimes)
    median_absolute_deviation = statistics.median(abs(runtime - median_runtime) for runtime in runtimes)
    return {
        "profile_kind": "tracker",
        "profile": fixture.profile.name,
        "tier": fixture.profile.tier,
        "seed": fixture.seed,
        "workload": {
            "torrents": fixture.profile.torrent_count,
            "tracker_records": fixture.profile.tracker_record_count,
            "save_path_groups": fixture.profile.save_path_group_count,
            "exact_message_targets": fixture.profile.exact_message_count,
            "prefix_message_targets": fixture.profile.prefix_message_count,
            "default_tag_targets": fixture.profile.default_tag_count,
            "cross_seed_tag_targets": fixture.profile.cross_seed_tag_count,
            "torrent_only_delete_targets": fixture.profile.delete_count,
            "timed_samples": samples,
            "warmup_passes": 1,
            "memory_passes": 1,
        },
        "fixture_manifest_digest": manifest_digest,
        "intended_action_digest": warmup.action_digest,
        "reconciliation": warmup.reconciliation,
        "scenarios": scenarios,
        "candidate_counts": warmup.candidate_counts,
        "endpoint_counters": dict(warmup.endpoint_counters),
        "timed_sample_endpoint_counters": [dict(item.endpoint_counters) for item in timed_evidence],
        "pass_endpoint_counters": {
            "warmup": dict(warmup.endpoint_counters),
            "memory": dict(memory_evidence.endpoint_counters),
        },
        "mutation_counters": {
            "filesystem": filesystem_audit.attempt_count,
            "qbittorrent": fixture.client.mutation_total,
            **dict(sorted(fixture.client.mutation_counts.items())),
        },
        "measurement_policy": _measurement_policy(),
        "sample_runtime_seconds": runtimes,
        "median_runtime_seconds": median_runtime,
        "minimum_runtime_seconds": min(runtimes),
        "maximum_runtime_seconds": max(runtimes),
        "median_absolute_deviation_seconds": median_absolute_deviation,
        "peak_memory_bytes": peak_memory_bytes,
    }


assert tuple(MUTATION_COUNTER_KEYS) == (
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
)


__all__ = [
    "TrackerEvaluationResult",
    "evaluate_tracker_fixture",
    "tracker_config",
    "tracker_fixture_manifest_digest",
    "validate_tracker_endpoint_counts",
]
