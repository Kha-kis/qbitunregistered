"""Run the deterministic unregistered tracker preview and dry-run evaluator."""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import stat
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator, Literal, Protocol, TypedDict, cast
from unittest.mock import patch

_COORDINATOR_BOOTSTRAP_MODULE = "_qbitunregistered_gauntlet_coordinator_bootstrap"
_COORDINATOR_BOOTSTRAP_STATE = sys.modules.get(_COORDINATOR_BOOTSTRAP_MODULE)

from benchmarks.gauntlet.baseline import (
    derive_tracker_artifact_role,
    load_quality_bar,
    tracker_scenarios_match_role_contracts,
)
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
    TrackerActionRecord,
    TrackerGauntletFixture,
    TrackerGauntletProfile,
    TrackerTorrent,
    build_tracker_fixture,
    effective_torrent_info_payload,
    expected_tracker_action_digest,
    expected_tracker_action_records,
)
from qbitunregistered.cache import clear_cache
from qbitunregistered.impact import ImpactSummary, analyze_impact
from qbitunregistered.operations.unregistered_checks import (
    DeletionAction,
    UnregisteredDeletionPlan,
    unregistered_checks,
)
from qbitunregistered.types import QBittorrentClient, TorrentInfo


def _validate_imported_application_boundary() -> None:
    """Revalidate protected dependency state before tracker evaluation."""
    if _COORDINATOR_BOOTSTRAP_STATE is not None:
        _COORDINATOR_BOOTSTRAP_STATE.validate_after_imports()


_validate_imported_application_boundary()

DEFAULT_TAG = "unregistered"
CROSS_SEED_TAG = "unregistered:crossseeding"
DELETE_TAG = "tracker-delete"
_QBITTORRENTAPI_SHIM_ORIGIN = "<qbitunregistered-gauntlet-qbittorrentapi-shim>"


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
    execution_action_digest: str
    reconciliation: TrackerReconciliationEvidence
    scenarios: dict[str, TrackerScenarioEvidence]
    candidate_counts: dict[str, int]
    endpoint_counters: dict[str, int]
    timed_sample_endpoint_counters: list[dict[str, int]]
    pass_endpoint_counters: PassEndpointCounters
    mutation_counters: dict[str, int]
    isolation_counters: dict[str, int]
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
    exit_code: int
    terminal_phase: str
    observation_order: list[str]
    mutation_counters: dict[str, int]
    isolation_counters: dict[str, int]


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


@dataclass(frozen=True, slots=True)
class _ScenarioCliResult:
    """Structured observations from one real semantic-scenario CLI run."""

    exit_code: int
    observation_order: list[str]
    summary: ImpactSummary | None
    execution_result: tuple[dict[str, list[str]], dict[str, int]] | None


@dataclass(slots=True)
class _PassMeasurement:
    """Measure only initial response materialization through execution return."""

    kind: Literal["checked", "timed", "memory"]
    started: bool = False
    stopped: bool = False
    started_at: float | None = None
    elapsed_seconds: float | None = None
    peak_memory_bytes: int | None = None

    def start(self) -> None:
        """Arm the selected measurement immediately before response allocation."""
        if self.started or self.stopped:
            raise GauntletSafetyError("tracker CLI observation started measurement more than once")
        self.started = True
        if self.kind == "timed":
            self.started_at = time.perf_counter()
        elif self.kind == "memory":
            if tracemalloc.is_tracing():
                raise GauntletSafetyError("tracemalloc was active before tracker response materialization")
            tracemalloc.start()

    def stop(self) -> None:
        """Stop the selected measurement immediately after execution returns."""
        if not self.started or self.stopped:
            raise GauntletSafetyError("tracker CLI observation stopped measurement outside execution order")
        if self.kind == "timed":
            if self.started_at is None:
                raise GauntletSafetyError("tracker timed measurement did not retain its start")
            self.elapsed_seconds = time.perf_counter() - self.started_at
        elif self.kind == "memory":
            _current_bytes, self.peak_memory_bytes = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        self.stopped = True

    def validate(self) -> None:
        """Require both boundaries and the selected measurement output."""
        if not self.started or not self.stopped:
            raise GauntletSafetyError("tracker CLI observation did not bracket the measured execution")
        if self.kind == "timed" and self.elapsed_seconds is None:
            raise GauntletSafetyError("tracker timed measurement is missing")
        if self.kind == "memory" and self.peak_memory_bytes is None:
            raise GauntletSafetyError("tracker memory measurement is missing")

    def cleanup(self) -> None:
        """Stop tracing after a failed memory pass without claiming evidence."""
        if self.kind == "memory" and tracemalloc.is_tracing():
            tracemalloc.stop()


class _DiscardCliOutput:
    """Discard the sanitized human preview while preserving the real formatter."""

    def write(self, value: str) -> int:
        return len(value)

    def flush(self) -> None:
        return None


_ACTIVE_PRODUCTION_AUDITS: list[_ProductionBoundaryAudit] = []
_PRODUCTION_AUDIT_HOOK_INSTALLED = False
_WRITE_OPEN_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
_FILESYSTEM_MUTATION_EVENTS = {
    "os.chmod",
    "os.chown",
    "os.link",
    "os.mkdir",
    "os.mknod",
    "os.remove",
    "os.removexattr",
    "os.rename",
    "os.rmdir",
    "os.setxattr",
    "os.symlink",
    "os.truncate",
    "os.utime",
}
_NETWORK_CONNECT_EVENTS = {"socket.connect", "socket.connect_ex"}
_NETWORK_OUTBOUND_EVENTS = {"socket.sendmsg", "socket.sendto"}
_NETWORK_DNS_EVENTS = {
    "socket.getaddrinfo",
    "socket.gethostbyaddr",
    "socket.gethostbyname",
    "socket.gethostbyname_ex",
    "socket.getnameinfo",
}
_ISOLATION_COUNTER_KEYS = (
    "filesystem_write_attempts",
    "network_connect_attempts",
    "network_dns_attempts",
    "network_outbound_attempts",
)
_WINDOWS_CRT_DESCRIPTOR_LIMIT = 8192


def _open_descriptor_numbers() -> Iterator[int]:
    """Yield every Python-visible descriptor on a supported platform."""
    if sys.platform.startswith("linux"):
        try:
            entries = os.listdir("/proc/self/fd")
        except OSError:
            raise GauntletSafetyError("tracker descriptor inventory unavailable") from None
        for entry in entries:
            try:
                yield int(entry)
            except ValueError:
                continue
        return
    if sys.platform == "win32":
        yield from range(3, _WINDOWS_CRT_DESCRIPTOR_LIMIT)
        return
    raise GauntletSafetyError("tracker descriptor inventory unsupported on this platform")


def _descriptor_stat(descriptor: int) -> os.stat_result | None:
    """Return one descriptor stat, tolerating only a concurrently closed fd."""
    try:
        return os.fstat(descriptor)
    except OSError as error:
        if error.errno == errno.EBADF:
            return None
        raise GauntletSafetyError("tracker descriptor inventory failed") from None


def _assert_safe_preexisting_descriptors() -> None:
    """Reject non-stdio regular files held at production-boundary entry."""
    stdio_stats = tuple(
        descriptor_stat for descriptor in (1, 2) if (descriptor_stat := _descriptor_stat(descriptor)) is not None
    )
    for descriptor in _open_descriptor_numbers():
        if descriptor <= 2:
            continue
        descriptor_stat = _descriptor_stat(descriptor)
        if descriptor_stat is None or not stat.S_ISREG(descriptor_stat.st_mode):
            continue
        if any(os.path.samestat(descriptor_stat, stdio_stat) for stdio_stat in stdio_stats):
            continue
        stdin_stat = _descriptor_stat(0)
        stdio_aliases = "0" if stdin_stat is not None and os.path.samestat(descriptor_stat, stdin_stat) else "none"
        try:
            inheritable = str(os.get_inheritable(descriptor)).lower()
        except OSError:
            inheritable = "unknown"
        raise GauntletSafetyError(
            "tracker production boundary found unsafe regular file descriptor "
            f"(descriptor={descriptor}; stdio_aliases={stdio_aliases}; inheritable={inheritable})"
        )


@dataclass(slots=True)
class _ProductionBoundaryAudit:
    """Deny and count global filesystem-write and network audit events."""

    counters: dict[str, int] = field(default_factory=lambda: {key: 0 for key in _ISOLATION_COUNTER_KEYS})
    _activation_totals: list[int] = field(default_factory=list)
    _last_attempt_class: str | None = None

    @property
    def filesystem_attempt_count(self) -> int:
        """Return the number of denied filesystem write or mutation attempts."""
        return self.counters["filesystem_write_attempts"]

    @property
    def total_attempt_count(self) -> int:
        """Return the total number of denied isolation-boundary attempts."""
        return sum(self.counters.values())

    def __enter__(self) -> _ProductionBoundaryAudit:
        _install_production_audit_hook()
        self._activation_totals.append(self.total_attempt_count)
        _ACTIVE_PRODUCTION_AUDITS.append(self)
        try:
            _assert_safe_preexisting_descriptors()
        except BaseException:
            self._deactivate()
            raise
        return self

    def __exit__(self, _error_type: object, _error: object, _traceback: object) -> None:
        starting_total = self._deactivate()
        if self.total_attempt_count != starting_total:
            raise GauntletSafetyError(f"tracker production boundary denied {self._last_attempt_class}")

    def _deactivate(self) -> int:
        """Pop one exact LIFO activation and return its starting count."""
        if not _ACTIVE_PRODUCTION_AUDITS or _ACTIVE_PRODUCTION_AUDITS[-1] is not self:
            raise GauntletSafetyError("tracker production boundary activation order is invalid")
        _ACTIVE_PRODUCTION_AUDITS.pop()
        if not self._activation_totals:
            raise GauntletSafetyError("tracker production boundary activation state is missing")
        return self._activation_totals.pop()

    def observe(self, event: str, arguments: tuple[object, ...]) -> None:
        """Reject one audited attempt without retaining its arguments."""
        attempt_class: str | None = None
        if event == "open" and len(arguments) >= 3 and _open_requests_write(arguments[1], arguments[2]):
            attempt_class = "filesystem write"
            counter = "filesystem_write_attempts"
        elif event in _FILESYSTEM_MUTATION_EVENTS:
            attempt_class = "filesystem write"
            counter = "filesystem_write_attempts"
        elif event in _NETWORK_CONNECT_EVENTS:
            attempt_class = "network connect"
            counter = "network_connect_attempts"
        elif event in _NETWORK_DNS_EVENTS:
            attempt_class = "network dns"
            counter = "network_dns_attempts"
        elif event in _NETWORK_OUTBOUND_EVENTS:
            attempt_class = "network outbound"
            counter = "network_outbound_attempts"
        else:
            return
        self.counters[counter] += 1
        self._last_attempt_class = attempt_class
        raise GauntletSafetyError(f"tracker production boundary denied {attempt_class}")


def _open_requests_write(raw_mode: object, raw_flags: object) -> bool:
    if isinstance(raw_mode, str) and any(marker in raw_mode for marker in "wax+"):
        return True
    return isinstance(raw_flags, int) and bool(raw_flags & _WRITE_OPEN_FLAGS)


def _production_audit_hook(event: str, arguments: tuple[object, ...]) -> None:
    for audit in tuple(_ACTIVE_PRODUCTION_AUDITS):
        audit.observe(event, arguments)


def _install_production_audit_hook() -> None:
    global _PRODUCTION_AUDIT_HOOK_INSTALLED
    if not _PRODUCTION_AUDIT_HOOK_INSTALLED:
        sys.addaudithook(_production_audit_hook)
        _PRODUCTION_AUDIT_HOOK_INSTALLED = True


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


def _tracker_cli_config_path(fixture: TrackerGauntletFixture, *, dry_run: bool = True) -> Path:
    """Materialize one sanitized CLI configuration outside production calls."""
    config_path = fixture.root / "gauntlet-config.json"
    config = {
        **tracker_config(),
        "host": "http://qbitunregistered-gauntlet.invalid",
        "api_key": "synthetic-gauntlet-placeholder",
        "dry_run": dry_run,
        "log_level": "ERROR",
    }
    config_path.write_text(json.dumps(config, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    return config_path


def _validate_scenario_notification_boundary(config_path: Path) -> None:
    """Require notifications to stay outside the protected fake-client run."""
    from qbitunregistered import notifications

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise GauntletSafetyError("tracker scenario notification boundary could not be verified") from error
    if not isinstance(config, dict) or any(config.get(name) for name in ("apprise_url", "notifiarr_key", "notifiarr_channel")):
        raise GauntletSafetyError("tracker scenario notification boundary is not disabled")
    qbittorrentapi = sys.modules.get("qbittorrentapi")
    module_spec = getattr(qbittorrentapi, "__spec__", None)
    if getattr(module_spec, "origin", None) == _QBITTORRENTAPI_SHIM_ORIGIN and notifications.APPRISE_AVAILABLE is not False:
        raise GauntletSafetyError("tracker scenario notification boundary is not disabled")


def _production_torrents(fixture: TrackerGauntletFixture) -> Sequence[TorrentInfo]:
    """Expose frozen evaluator torrents through the production read protocol."""
    return cast(Sequence[TorrentInfo], fixture.initial_torrents)


def tracker_fixture_manifest_digest(fixture: TrackerGauntletFixture) -> str:  # noqa: C901
    """Hash the validated materialized tracker fixture without host paths."""
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
    snapshot = fixture.client.torrent_snapshot
    if not isinstance(snapshot, Sequence) or isinstance(snapshot, (str, bytes, bytearray)):
        raise GauntletSafetyError("tracker fixture snapshot is not a torrent sequence")
    torrents = list(snapshot)
    if any(not isinstance(torrent, TrackerTorrent) for torrent in torrents):
        raise GauntletSafetyError("tracker fixture snapshot contains malformed torrents")
    typed_torrents = cast(list[TrackerTorrent], torrents)
    snapshot_hashes = [torrent.hash for torrent in typed_torrents]
    if len(set(snapshot_hashes)) != len(snapshot_hashes):
        raise GauntletSafetyError("tracker fixture contains a duplicate torrent hash")
    payload_hashes = set(fixture.client.torrent_info_by_hash)
    if payload_hashes != set(snapshot_hashes):
        raise GauntletSafetyError("tracker fixture torrent-info payload ownership does not match the snapshot")

    tracker_count = 0
    for torrent in typed_torrents:
        save_path_group = Path(torrent.save_path).name
        if not re.fullmatch(r"group-[0-9]{5}", save_path_group):
            raise GauntletSafetyError("tracker fixture contains an unsafe save-path group")
        stored_info = fixture.client.torrent_info_by_hash[torrent.hash]
        if not isinstance(stored_info, Mapping) or stored_info.get("hash") != torrent.hash:
            raise GauntletSafetyError("tracker fixture contains a mismatched torrent-info payload hash")
        try:
            effective_info = effective_torrent_info_payload(stored_info, torrent)
            normalized_info = json.loads(json.dumps(effective_info, separators=(",", ":")))
        except (TypeError, ValueError) as error:
            raise GauntletSafetyError("tracker fixture contains malformed torrent-info payload") from error
        if not isinstance(normalized_info, dict):
            raise GauntletSafetyError("tracker fixture contains malformed torrent-info payload")
        for path_field in ("save_path", "download_path", "content_path", "root_path"):
            path_value = normalized_info.get(path_field)
            if not isinstance(path_value, str):
                raise GauntletSafetyError("tracker fixture contains a malformed torrent-info path")
            try:
                normalized_info[path_field] = Path(path_value).relative_to(fixture.root).as_posix()
            except ValueError as error:
                raise GauntletSafetyError("tracker fixture contains an unsafe torrent-info path") from error
        _digest_record(
            digest,
            {
                "torrent_info": normalized_info,
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
    if len(snapshot_hashes) != profile.torrent_count or tracker_count != profile.tracker_record_count:
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


def _validated_scenario_action_digest(
    summary: ImpactSummary,
    profile: TrackerGauntletProfile,
    seed: int,
) -> str:
    """Validate exact scenario actions against fixture roles and return their digest."""
    action_records = _action_records(summary)
    expected_records = list(expected_tracker_action_records(profile, seed))
    if action_records != expected_records:
        raise GauntletSafetyError("tracker scenario action records did not match the independent fixture oracle")
    action_digest = _intended_action_digest(summary)
    if action_digest != expected_tracker_action_digest(profile, seed):
        raise GauntletSafetyError("tracker scenario action digest did not match the independent fixture oracle")
    return action_digest


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
    """Accept only one complete ordinary/exact or one embedded snapshot."""
    if derive_tracker_artifact_role(endpoint_counts, profile.torrent_count) is None:
        raise GauntletSafetyError("tracker API evidence is partial, redundant, or outside the locked budget")


def _prepare_pass(fixture: TrackerGauntletFixture) -> None:
    clear_cache()
    fixture.client.reset_read_counts()


@contextmanager
def _fresh_pass_fixture(source: TrackerGauntletFixture) -> Iterator[TrackerGauntletFixture]:
    """Yield one response-owning fixture that exists for exactly one primary pass."""
    with tempfile.TemporaryDirectory(prefix="qbitunregistered-tracker-pass-") as temporary_root:
        fixture = build_tracker_fixture(
            Path(temporary_root),
            source.profile,
            source.seed,
            embedded_trackers_mode=source.client.embedded_trackers_mode,
        )
        _tracker_cli_config_path(fixture)
        yield fixture


def _execute_pipeline(fixture: TrackerGauntletFixture) -> _TrackerPipelineResult:  # noqa: C901
    """Invoke the real CLI and transparently retain its structured evidence."""
    from qbitunregistered import cli as cli_module
    from qbitunregistered import impact as impact_module

    _validate_imported_application_boundary()

    config_path = fixture.root / "gauntlet-config.json"
    if not config_path.is_file():
        config_path = _tracker_cli_config_path(fixture)
    local_measurement: _PassMeasurement | None = None
    if not fixture.client.measurement_callbacks_configured:
        local_measurement = _PassMeasurement("checked")
        fixture.client.set_measurement_callbacks(local_measurement.start, local_measurement.stop)

    expected_hashes = tuple(torrent.hash for torrent in fixture.initial_torrents)
    observation_order: list[str] = []
    preview_calls: list[tuple[tuple[str, ...], ImpactSummary]] = []
    execution_calls: list[tuple[tuple[str, ...], object, bool]] = []
    execution_results: list[tuple[dict[str, list[str]], dict[str, int]]] = []
    client_call_count = 0
    capture = _TrackerReconciliationCapture()
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    previous_handlers = list(root_logger.handlers)

    def observed_create_client(_config: dict[str, object]) -> object:
        nonlocal client_call_count
        client_call_count += 1
        return fixture.client

    selected_analyze_impact = analyze_impact

    def observed_analyze_impact(
        client: QBittorrentClient,
        torrents: Sequence[TorrentInfo],
        config: dict[str, object],
        operations: Sequence[str],
    ) -> ImpactSummary:
        observation_order.append("preview")
        summary = selected_analyze_impact(client, torrents, config, operations)
        preview_calls.append((tuple(torrent.hash for torrent in torrents), summary))
        return summary

    selected_unregistered_checks = unregistered_checks

    def observed_unregistered_checks(
        client: QBittorrentClient,
        torrents: Sequence[TorrentInfo],
        config: dict[str, object],
        use_delete_tags: bool,
        delete_tags: list[str],
        delete_files: dict[str, bool],
        dry_run: bool,
        recycle_bin: str | None = None,
        *,
        deletion_plan: UnregisteredDeletionPlan | None = None,
    ) -> tuple[dict[str, list[str]], dict[str, int]]:
        observation_order.append("execution")
        execution_calls.append((tuple(torrent.hash for torrent in torrents), deletion_plan, dry_run))
        execution_handlers = list(root_logger.handlers)
        for handler in execution_handlers:
            root_logger.removeHandler(handler)
        root_logger.addHandler(capture)
        root_logger.setLevel(logging.INFO)
        try:
            result = selected_unregistered_checks(
                client,
                torrents,
                config,
                use_delete_tags,
                delete_tags,
                delete_files,
                dry_run,
                recycle_bin,
                deletion_plan=deletion_plan,
            )
            fixture.client.finish_execution_measurement()
        finally:
            root_logger.removeHandler(capture)
            for handler in execution_handlers:
                root_logger.addHandler(handler)
            root_logger.setLevel(logging.ERROR)
        execution_results.append(result)
        return result

    for handler in previous_handlers:
        root_logger.removeHandler(handler)
    try:
        with (
            patch.object(cli_module, "create_client", observed_create_client),
            patch.object(impact_module, "analyze_impact", observed_analyze_impact),
            patch.object(cli_module, "unregistered_checks", observed_unregistered_checks),
            redirect_stdout(_DiscardCliOutput()),
            redirect_stderr(_DiscardCliOutput()),
        ):
            exit_code = cli_module.main(["--config", str(config_path), "--unregistered", "--dry-run"])
    finally:
        for handler in list(root_logger.handlers):
            if handler not in previous_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        for handler in previous_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)
        root_logger.setLevel(previous_level)
    if exit_code != 0:
        raise GauntletSafetyError("tracker CLI did not return success")
    if client_call_count != 1:
        raise GauntletSafetyError("tracker CLI client observation did not occur exactly once")
    if observation_order != ["preview", "execution"] or len(preview_calls) != 1 or len(execution_calls) != 1:
        raise GauntletSafetyError("tracker CLI preview and execution observations were missing, duplicated, or reordered")
    preview_hashes, summary = preview_calls[0]
    execution_hashes, execution_plan, execution_dry_run = execution_calls[0]
    if preview_hashes != expected_hashes or execution_hashes != expected_hashes:
        raise GauntletSafetyError("tracker CLI snapshot order did not match the initial response")
    plan = summary.unregistered_deletion_plan
    if plan is None:
        raise GauntletSafetyError("tracker preview did not produce a deletion plan")
    if execution_plan is not plan:
        raise GauntletSafetyError("tracker CLI execution did not reuse the preview deletion plan")
    if execution_dry_run is not True or fixture.client.mutation_total:
        raise GauntletSafetyError("tracker CLI execution was not a mutation-free dry-run")
    if len(execution_results) != 1:
        raise GauntletSafetyError("tracker CLI execution result observation is missing")
    if fixture.client.measurement_callbacks_configured:
        raise GauntletSafetyError("tracker CLI observation did not consume both measurement boundaries")
    if local_measurement is not None:
        local_measurement.validate()
    torrent_file_paths, unregistered_counts = execution_results[0]
    return _TrackerPipelineResult(
        summary=summary,
        torrent_file_paths=torrent_file_paths,
        unregistered_counts=unregistered_counts,
        operator_counts=capture.counts(),
    )


def _validate_unchanged_state(
    fixture: TrackerGauntletFixture,
    initial_filesystem_digest: str,
    production_audit: _ProductionBoundaryAudit,
) -> None:
    if production_audit.filesystem_attempt_count:
        raise GauntletSafetyError("tracker production boundary denied filesystem write")
    if production_audit.counters["network_connect_attempts"]:
        raise GauntletSafetyError("tracker production boundary denied network connect")
    if production_audit.counters["network_dns_attempts"]:
        raise GauntletSafetyError("tracker production boundary denied network dns")
    if production_audit.counters["network_outbound_attempts"]:
        raise GauntletSafetyError("tracker production boundary denied network outbound")
    if fixture.client.mutation_total:
        raise GauntletSafetyError("tracker dry-run attempted a qBittorrent mutation")
    if _filesystem_digest(fixture.root) != initial_filesystem_digest:
        raise GauntletSafetyError("tracker dry-run changed the fixture filesystem")


def _validate_pass(
    fixture: TrackerGauntletFixture,
    pipeline: _TrackerPipelineResult,
    initial_filesystem_digest: str,
    production_audit: _ProductionBoundaryAudit,
) -> _TrackerPassEvidence:
    _validate_unchanged_state(fixture, initial_filesystem_digest, production_audit)
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
    _initial_filesystem_digest: str,
    production_audit: _ProductionBoundaryAudit,
) -> _TrackerPassEvidence:
    with _fresh_pass_fixture(fixture) as pass_fixture:
        _prepare_pass(pass_fixture)
        initial_filesystem_digest = _filesystem_digest(pass_fixture.root)
        measurement = _PassMeasurement("checked")
        pass_fixture.client.set_measurement_callbacks(measurement.start, measurement.stop)
        try:
            with production_audit:
                pipeline = _execute_pipeline(pass_fixture)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _validate_unchanged_state(pass_fixture, initial_filesystem_digest, production_audit)
            raise
        finally:
            measurement.cleanup()
            clear_cache()
        measurement.validate()
        return _validate_pass(pass_fixture, pipeline, initial_filesystem_digest, production_audit)


def _timed_pipeline_pass(
    fixture: TrackerGauntletFixture,
    _initial_filesystem_digest: str,
    production_audit: _ProductionBoundaryAudit,
) -> tuple[_TrackerPassEvidence, float]:
    with _fresh_pass_fixture(fixture) as pass_fixture:
        _prepare_pass(pass_fixture)
        initial_filesystem_digest = _filesystem_digest(pass_fixture.root)
        measurement = _PassMeasurement("timed")
        pass_fixture.client.set_measurement_callbacks(measurement.start, measurement.stop)
        try:
            with production_audit:
                pipeline = _execute_pipeline(pass_fixture)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _validate_unchanged_state(pass_fixture, initial_filesystem_digest, production_audit)
            raise
        finally:
            measurement.cleanup()
            clear_cache()
        measurement.validate()
        if measurement.elapsed_seconds is None:
            raise GauntletSafetyError("tracker timed measurement is missing")
        return (
            _validate_pass(pass_fixture, pipeline, initial_filesystem_digest, production_audit),
            measurement.elapsed_seconds,
        )


def _memory_pipeline_pass(
    fixture: TrackerGauntletFixture,
    _initial_filesystem_digest: str,
    production_audit: _ProductionBoundaryAudit,
) -> tuple[_TrackerPassEvidence, int]:
    with _fresh_pass_fixture(fixture) as pass_fixture:
        _prepare_pass(pass_fixture)
        initial_filesystem_digest = _filesystem_digest(pass_fixture.root)
        measurement = _PassMeasurement("memory")
        pass_fixture.client.set_measurement_callbacks(measurement.start, measurement.stop)
        try:
            with production_audit:
                pipeline = _execute_pipeline(pass_fixture)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            _validate_unchanged_state(pass_fixture, initial_filesystem_digest, production_audit)
            raise
        finally:
            measurement.cleanup()
            clear_cache()
        measurement.validate()
        if measurement.peak_memory_bytes is None:
            raise GauntletSafetyError("tracker memory measurement is missing")
        return (
            _validate_pass(pass_fixture, pipeline, initial_filesystem_digest, production_audit),
            measurement.peak_memory_bytes,
        )


def _action_record_digest(records: Sequence[TrackerActionRecord]) -> str:
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))):
        _digest_record(digest, record)
    return digest.hexdigest()


def _shadow_execution_action_digest(
    profile: TrackerGauntletProfile,
    seed: int,
    production_audit: _ProductionBoundaryAudit,
) -> str:
    """Verify exact mutating endpoint arguments against a fresh fixture oracle."""
    with tempfile.TemporaryDirectory(prefix="qbitunregistered-tracker-shadow-") as temporary_root:
        fixture = build_tracker_fixture(Path(temporary_root), profile, seed)
        before = _filesystem_digest(fixture.root)
        config = tracker_config()
        clear_cache()
        fixture.client.reset_read_counts()
        try:
            with production_audit:
                summary = analyze_impact(
                    fixture.client,
                    _production_torrents(fixture),
                    config,
                    ["unregistered"],
                )
                plan = summary.unregistered_deletion_plan
                if plan is None:
                    raise GauntletSafetyError("tracker shadow preview did not produce a deletion plan")
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
        finally:
            clear_cache()
        if _filesystem_digest(fixture.root) != before:
            raise GauntletSafetyError("tracker shadow execution changed the filesystem")
        if fixture.client.mutation_counts["torrents_add_tags"] != 2:
            raise GauntletSafetyError("tracker shadow execution used an unexpected tag mutation shape")
        if fixture.client.mutation_counts["torrents_delete"] != 1:
            raise GauntletSafetyError("tracker shadow execution used an unexpected delete mutation shape")
        unexpected_mutations = {
            name: count
            for name, count in fixture.client.mutation_counts.items()
            if name not in {"torrents_add_tags", "torrents_delete"} and count
        }
        if unexpected_mutations:
            raise GauntletSafetyError("tracker shadow execution used an unexpected mutation endpoint")
        actual_records = sorted(
            fixture.client.execution_action_records,
            key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
        )
        expected_records = list(expected_tracker_action_records(profile, seed))
        if actual_records != expected_records:
            raise GauntletSafetyError("tracker execution action records did not match the independent fixture oracle")
        action_digest = _action_record_digest(actual_records)
        if action_digest != expected_tracker_action_digest(profile, seed):
            raise GauntletSafetyError("tracker execution action digest did not match the independent fixture oracle")
        return action_digest


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


def _scenario_mutation_counters(
    fixture: TrackerGauntletFixture,
    production_audit: _ProductionBoundaryAudit,
) -> dict[str, int]:
    counters = {
        "filesystem": production_audit.filesystem_attempt_count,
        "qbittorrent": fixture.client.mutation_total,
        **dict(sorted(fixture.client.mutation_counts.items())),
    }
    if set(counters) != set(MUTATION_COUNTER_KEYS):
        raise GauntletSafetyError("tracker scenario mutation evidence is malformed")
    return counters


def _execute_scenario_cli(  # noqa: C901
    fixture: TrackerGauntletFixture,
    *,
    before_preview: Callable[[TrackerGauntletFixture], None] | None,
    before_execution: Callable[[TrackerGauntletFixture, ImpactSummary], None] | None,
    dry_run: bool,
    production_audit: _ProductionBoundaryAudit,
) -> _ScenarioCliResult:
    """Run one semantic scenario through the real CLI with transparent phase hooks."""
    from qbitunregistered import cli as cli_module
    from qbitunregistered import impact as impact_module

    _validate_imported_application_boundary()

    config_path = fixture.root / "gauntlet-config.json"
    if not config_path.is_file():
        config_path = _tracker_cli_config_path(fixture, dry_run=dry_run)
    _validate_scenario_notification_boundary(config_path)
    observation_order: list[str] = []
    summaries: list[ImpactSummary] = []
    execution_results: list[tuple[dict[str, list[str]], dict[str, int]]] = []
    client_call_count = 0
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    previous_handlers = list(root_logger.handlers)

    def observed_create_client(_config: dict[str, object]) -> object:
        nonlocal client_call_count
        client_call_count += 1
        return fixture.client

    selected_analyze_impact = analyze_impact

    def observed_analyze_impact(
        client: QBittorrentClient,
        torrents: Sequence[TorrentInfo],
        config: dict[str, object],
        operations: Sequence[str],
    ) -> ImpactSummary:
        observation_order.append("preview")
        if before_preview is not None:
            before_preview(fixture)
        summary = selected_analyze_impact(client, torrents, config, operations)
        summaries.append(summary)
        return summary

    selected_unregistered_checks = unregistered_checks

    def observed_unregistered_checks(
        client: QBittorrentClient,
        torrents: Sequence[TorrentInfo],
        config: dict[str, object],
        use_delete_tags: bool,
        delete_tags: list[str],
        delete_files: dict[str, bool],
        dry_run: bool,
        recycle_bin: str | None = None,
        *,
        deletion_plan: UnregisteredDeletionPlan | None = None,
    ) -> tuple[dict[str, list[str]], dict[str, int]]:
        observation_order.append("execution")
        if len(summaries) != 1:
            raise GauntletSafetyError("tracker scenario execution did not follow one successful preview")
        if before_execution is not None:
            before_execution(fixture, summaries[0])
        result = selected_unregistered_checks(
            client,
            torrents,
            config,
            use_delete_tags,
            delete_tags,
            delete_files,
            dry_run,
            recycle_bin,
            deletion_plan=deletion_plan,
        )
        execution_results.append(result)
        return result

    arguments = ["--config", str(config_path), "--unregistered"]
    if dry_run:
        arguments.append("--dry-run")
    for handler in previous_handlers:
        root_logger.removeHandler(handler)
    try:
        with (
            patch.object(cli_module, "create_client", observed_create_client),
            patch.object(impact_module, "analyze_impact", observed_analyze_impact),
            patch.object(cli_module, "unregistered_checks", observed_unregistered_checks),
            patch("builtins.input", return_value="yes"),
            redirect_stdout(_DiscardCliOutput()),
            redirect_stderr(_DiscardCliOutput()),
            production_audit,
        ):
            exit_code = cli_module.main(arguments)
    finally:
        for handler in list(root_logger.handlers):
            if handler not in previous_handlers:
                root_logger.removeHandler(handler)
                handler.close()
        for handler in previous_handlers:
            if handler not in root_logger.handlers:
                root_logger.addHandler(handler)
        root_logger.setLevel(previous_level)
    if client_call_count != 1:
        raise GauntletSafetyError("tracker scenario CLI client observation did not occur exactly once")
    if fixture.client.logout_count != 1:
        raise GauntletSafetyError("tracker scenario CLI did not log out exactly once")
    if observation_order not in (["preview"], ["preview", "execution"]):
        raise GauntletSafetyError("tracker scenario CLI phase observations were missing, duplicated, or reordered")
    if len(summaries) > 1 or len(execution_results) > 1:
        raise GauntletSafetyError("tracker scenario CLI phase observations were duplicated")
    return _ScenarioCliResult(
        exit_code=exit_code,
        observation_order=observation_order,
        summary=summaries[0] if summaries else None,
        execution_result=execution_results[0] if execution_results else None,
    )


def _validate_scenario_unchanged(fixture: TrackerGauntletFixture, before: str) -> None:
    if fixture.client.mutation_total:
        raise GauntletSafetyError("tracker safety scenario attempted a qBittorrent mutation")
    if _filesystem_digest(fixture.root) != before:
        raise GauntletSafetyError("tracker safety scenario changed the fixture filesystem")


def _scenario_terminal_phase(result: _ScenarioCliResult) -> str:
    if result.exit_code == 0 and result.summary is not None and result.execution_result is not None:
        return "execution_complete"
    if result.exit_code == 1 and result.summary is None and result.observation_order == ["preview"]:
        return "preview_fail_closed"
    if (
        result.exit_code == 1
        and result.summary is not None
        and result.execution_result is None
        and result.observation_order == ["preview", "execution"]
    ):
        return "execution_fail_closed"
    raise GauntletSafetyError("tracker scenario CLI result did not terminate at a locked phase")


def evaluate_tracker_scenarios(  # noqa: C901
    _fixture: TrackerGauntletFixture,
    *,
    production_audit: _ProductionBoundaryAudit | None = None,
) -> dict[str, TrackerScenarioEvidence]:
    """Run every transport-aware semantic scenario through the real CLI."""
    if production_audit is None:
        production_audit = _ProductionBoundaryAudit()
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
    scenario_contracts = load_quality_bar(Path(__file__).with_name("quality-bar.toml")).tracker_scenario_contracts
    evidence: dict[str, TrackerScenarioEvidence] = {}

    def record(
        name: str,
        fixture: TrackerGauntletFixture,
        before: str,
        result: _ScenarioCliResult,
        *,
        action_digest: str | None = None,
    ) -> None:
        _validate_scenario_unchanged(fixture, before)
        counters = _scenario_endpoint_counters(fixture)
        terminal_phase = _scenario_terminal_phase(result)
        shape = tuple(counters[endpoint] for endpoint in TRACKER_READ_ENDPOINTS)
        if name == "malformed_embedded_transport_aware" and shape == (0, 1, 0) and result.exit_code == 0:
            raise GauntletSafetyError("malformed embedded metadata did not fail closed")
        scenario_evidence: TrackerScenarioEvidence = {
            "outcome": "pass",
            "action_digest": action_digest or _scenario_digest(name, "fail_closed"),
            "endpoint_counters": counters,
            "exit_code": result.exit_code,
            "terminal_phase": terminal_phase,
            "observation_order": list(result.observation_order),
            "mutation_counters": _scenario_mutation_counters(fixture, production_audit),
            "isolation_counters": dict(production_audit.counters),
        }
        evidence[name] = scenario_evidence

    with tempfile.TemporaryDirectory(prefix="qbitunregistered-tracker-scenarios-") as temporary_root:
        scenario_root = Path(temporary_root)
        compatibility_modes: tuple[tuple[str, EmbeddedTrackersMode], ...] = (
            ("complete_embedded", "supported"),
            ("omitted_embedded_fallback", "omitted"),
            ("rejected_embedded_fallback", "rejected"),
            ("malformed_embedded_transport_aware", "malformed"),
        )
        for offset, (name, mode) in enumerate(compatibility_modes):
            scenario_seed = 30_000 + offset
            fixture = build_tracker_fixture(
                scenario_root / name,
                scenario_profile,
                scenario_seed,
                embedded_trackers_mode=mode,
            )
            _tracker_cli_config_path(fixture)
            before = _filesystem_digest(fixture.root)
            clear_cache()
            result = _execute_scenario_cli(
                fixture,
                before_preview=None,
                before_execution=None,
                dry_run=True,
                production_audit=production_audit,
            )
            if result.summary is not None:
                action_digest = _validated_scenario_action_digest(result.summary, scenario_profile, scenario_seed)
            elif mode == "malformed":
                action_digest = _scenario_digest(name, "transport_safe")
            else:
                raise GauntletSafetyError("tracker compatibility scenario failed before producing canonical actions")
            if mode == "malformed":
                action_digest = _scenario_digest(name, "transport_safe")
            record(name, fixture, before, result, action_digest=action_digest)

        fixture = build_tracker_fixture(
            scenario_root / "malformed-exact",
            scenario_profile,
            30_010,
            embedded_trackers_mode="omitted",
        )
        _tracker_cli_config_path(fixture)
        before = _filesystem_digest(fixture.root)
        clear_cache()

        def malformed_exact(current_fixture: TrackerGauntletFixture) -> None:
            current_fixture.client.set_exact_trackers(current_fixture.initial_torrents[0].hash, None)

        result = _execute_scenario_cli(
            fixture,
            before_preview=malformed_exact,
            before_execution=None,
            dry_run=True,
            production_audit=production_audit,
        )
        record("malformed_exact_fail_closed", fixture, before, result)

        fixture = build_tracker_fixture(
            scenario_root / "disappearance",
            scenario_profile,
            30_011,
            embedded_trackers_mode="omitted",
        )
        _tracker_cli_config_path(fixture)
        before = _filesystem_digest(fixture.root)
        clear_cache()
        failed = fixture.initial_torrents[0]

        def disappear(current_fixture: TrackerGauntletFixture) -> None:
            current_fixture.client.set_exact_trackers(failed.hash, OSError("removed"))
            current_fixture.client.set_torrent_snapshot(current_fixture.initial_torrents[1:])

        result = _execute_scenario_cli(
            fixture,
            before_preview=disappear,
            before_execution=None,
            dry_run=True,
            production_audit=production_audit,
        )
        if result.summary is None:
            raise GauntletSafetyError("tracker disappearance scenario did not produce a preview")
        plan = result.summary.unregistered_deletion_plan
        if plan is None or plan.confirmed_absent_hashes != (failed.hash,):
            raise GauntletSafetyError("tracker disappearance scenario lacked an exact fresh absence proof")
        record(
            "proven_disappearance",
            fixture,
            before,
            result,
            action_digest=_intended_action_digest(result.summary),
        )

        fixture = build_tracker_fixture(
            scenario_root / "readd",
            scenario_profile,
            30_012,
            embedded_trackers_mode="omitted",
        )
        _tracker_cli_config_path(fixture)
        before = _filesystem_digest(fixture.root)
        clear_cache()
        failed = fixture.initial_torrents[0]

        def readd(current_fixture: TrackerGauntletFixture) -> None:
            current_fixture.client.set_exact_trackers(failed.hash, OSError("removed and re-added"))
            current = list(current_fixture.initial_torrents)
            current[0] = replace(failed, name="same-hash-readded")
            current_fixture.client.set_torrent_snapshot(current)

        result = _execute_scenario_cli(
            fixture,
            before_preview=readd,
            before_execution=None,
            dry_run=True,
            production_audit=production_audit,
        )
        record("same_hash_readd_fail_closed", fixture, before, result)

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
            _tracker_cli_config_path(fixture)
            before = _filesystem_digest(fixture.root)
            clear_cache()

            def corrupt_refresh(
                current_fixture: TrackerGauntletFixture,
                replacement: list[SimpleNamespace] = snapshot,
            ) -> None:
                current_fixture.client.set_exact_trackers(
                    current_fixture.initial_torrents[0].hash,
                    OSError("unavailable"),
                )
                current_fixture.client.set_torrent_snapshot(replacement)

            result = _execute_scenario_cli(
                fixture,
                before_preview=corrupt_refresh,
                before_execution=None,
                dry_run=True,
                production_audit=production_audit,
            )
            record(name, fixture, before, result)

        for offset, (name, change) in enumerate(
            (
                ("delete_disappearance_preflight", "disappear"),
                ("delete_tag_change_preflight", "tag"),
            )
        ):
            fixture = build_tracker_fixture(scenario_root / name, scenario_profile, 30_030 + offset)
            _tracker_cli_config_path(fixture, dry_run=False)
            before = _filesystem_digest(fixture.root)
            clear_cache()

            def change_preflight(
                current_fixture: TrackerGauntletFixture,
                _summary: ImpactSummary,
                selected_change: str = change,
            ) -> None:
                current = list(current_fixture.initial_torrents)
                delete_index = next(
                    index
                    for index, torrent in enumerate(current)
                    if DELETE_TAG in {tag.strip() for tag in torrent.tags.split(",") if tag.strip()}
                )
                if selected_change == "disappear":
                    current.pop(delete_index)
                else:
                    current[delete_index] = replace(current[delete_index], tags="")
                current_fixture.client.set_torrent_snapshot(current)

            result = _execute_scenario_cli(
                fixture,
                before_preview=None,
                before_execution=change_preflight,
                dry_run=False,
                production_audit=production_audit,
            )
            if result.summary is None:
                raise GauntletSafetyError("tracker preflight scenario lacks a preview")
            record(name, fixture, before, result, action_digest=_intended_action_digest(result.summary))

        name = "tracker_change_snapshot_bound"
        fixture = build_tracker_fixture(scenario_root / name, scenario_profile, 30_040)
        _tracker_cli_config_path(fixture)
        before = _filesystem_digest(fixture.root)
        clear_cache()

        def change_trackers(current_fixture: TrackerGauntletFixture, _summary: ImpactSummary) -> None:
            changed_hash = current_fixture.initial_torrents[0].hash
            current_fixture.client.set_exact_trackers(
                changed_hash,
                current_fixture.client.trackers_by_hash[current_fixture.initial_torrents[-1].hash],
            )

        result = _execute_scenario_cli(
            fixture,
            before_preview=None,
            before_execution=change_trackers,
            dry_run=True,
            production_audit=production_audit,
        )
        if result.summary is None or result.execution_result is None:
            raise GauntletSafetyError("tracker snapshot-binding scenario did not complete")
        _paths, counts = result.execution_result
        if sum(counts.values()) != scenario_profile.default_tag_count + scenario_profile.cross_seed_tag_count:
            raise GauntletSafetyError("tracker dry-run was not bound to the preview snapshot")
        record(name, fixture, before, result, action_digest=_intended_action_digest(result.summary))
    clear_cache()
    if not any(
        tracker_scenarios_match_role_contracts(evidence, scenario_contracts, role) for role in ("control", "candidate")
    ):
        raise GauntletSafetyError("tracker scenarios do not share one locked transport role")
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
    production_audit = _ProductionBoundaryAudit()
    scenarios = evaluate_tracker_scenarios(fixture, production_audit=production_audit)
    manifest_digest = tracker_fixture_manifest_digest(fixture)
    initial_filesystem_digest = _filesystem_digest(fixture.root)
    warmup = _checked_pipeline_pass(fixture, initial_filesystem_digest, production_audit)
    timed_evidence: list[_TrackerPassEvidence] = []
    runtimes: list[float] = []
    for _sample_index in range(samples):
        evidence, runtime = _timed_pipeline_pass(fixture, initial_filesystem_digest, production_audit)
        timed_evidence.append(evidence)
        runtimes.append(runtime)
    memory_evidence, peak_memory_bytes = _memory_pipeline_pass(
        fixture,
        initial_filesystem_digest,
        production_audit,
    )
    all_evidence = [warmup, *timed_evidence, memory_evidence]
    if any(
        item.action_digest != warmup.action_digest
        or item.reconciliation != warmup.reconciliation
        or item.candidate_counts != warmup.candidate_counts
        for item in all_evidence[1:]
    ):
        raise GauntletSafetyError("tracker evaluator evidence changed between passes")
    artifact_role = derive_tracker_artifact_role(warmup.endpoint_counters, fixture.profile.torrent_count)
    if artifact_role is None or any(
        derive_tracker_artifact_role(item.endpoint_counters, fixture.profile.torrent_count) != artifact_role
        for item in all_evidence[1:]
    ):
        raise GauntletSafetyError("tracker primary passes do not share one transport role")
    scenario_contracts = load_quality_bar(Path(__file__).with_name("quality-bar.toml")).tracker_scenario_contracts
    if not tracker_scenarios_match_role_contracts(scenarios, scenario_contracts, artifact_role):
        raise GauntletSafetyError("tracker scenarios do not match the primary transport role")
    execution_action_digest = _shadow_execution_action_digest(
        fixture.profile,
        fixture.seed,
        production_audit,
    )

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
        "execution_action_digest": execution_action_digest,
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
            "filesystem": production_audit.filesystem_attempt_count,
            "qbittorrent": fixture.client.mutation_total,
            **dict(sorted(fixture.client.mutation_counts.items())),
        },
        "isolation_counters": dict(production_audit.counters),
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
