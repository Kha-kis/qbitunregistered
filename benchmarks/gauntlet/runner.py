"""Run the locked orphan discovery and dry-run reconciliation evaluator."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import secrets
import stat
import statistics
import tempfile
import time
import tracemalloc
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, NotRequired, Protocol, TypedDict, cast

from benchmarks.gauntlet.fixture_factory import (
    PROFILES,
    EndpointBudget,
    GauntletFixture,
    GauntletProfile,
    ReconciliationOracle,
    build_fixture,
    expected_endpoint_budgets,
    verify_materialized_fixture,
)
from benchmarks.gauntlet.identity import (
    RepositoryIdentity,
    capture_repository_identity,
    require_same_identity,
)
from qbitunregistered.cache import clear_cache
from qbitunregistered.operations.orphaned import (
    OrphanFilePlan,
    build_orphan_file_plan,
    check_files_on_disk,
    delete_orphaned_files,
)

SCHEMA_NAME = "qbitunregistered.gauntlet.result"
SCHEMA_VERSION = 3
EVALUATOR_VERSION = "1.3.0"
DEFAULT_SEED = 20_260_729
DEFAULT_SAMPLES = 5
_RESULT_STAGING_PREFIX = ".qbit-gauntlet-"


class GauntletSafetyError(RuntimeError):
    """Raised when an evaluator run observes mutation or unstable evidence."""


@dataclass(frozen=True, slots=True)
class BoundOutputDirectory:
    """An identity-checked directory descriptor for safe result publication."""

    path: Path
    descriptor: int
    protected_roots: tuple[Path, ...]


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


class _AffinityGetter(Protocol):
    def __call__(self, process_id: int, /) -> set[int]: ...


class _StatvfsResult(Protocol):
    f_bsize: int


class _StatvfsGetter(Protocol):
    def __call__(self, path: Path, /) -> _StatvfsResult: ...


class WorkloadResult(TypedDict):
    """Every locked profile and execution dimension emitted in results."""

    torrents: int
    filesystem_files: int
    owned_files: int
    orphan_files: int
    exact_metadata_torrents: int
    bulk_path_torrents: int
    configured_roots: int
    shards: int
    timed_samples: int
    warmup_passes: int
    memory_passes: int


class MeasurementPolicy(TypedDict):
    """Fixed pass order and cache policy for comparable measurements."""

    sequence: str
    timed_samples_traced: bool
    memory_pass_timed: bool
    application_cache: str
    fixture_metadata: str
    os_page_cache: str
    sample_rejection: str


class PassEndpointCounters(TypedDict):
    """Read counters for non-timed passes."""

    warmup: dict[str, int]
    memory: dict[str, int]


class EvaluationResult(TypedDict):
    """Measurements and safety evidence from one materialized fixture."""

    profile: str
    tier: str
    seed: int
    workload: WorkloadResult
    fixture_manifest_digest: str
    intended_action_digest: str
    reconciliation: ReconciliationEvidence
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


class CandidateState(TypedDict):
    """Sanitized Git worktree identity without paths or raw diff content."""

    clean: bool | None
    diff_sha256: str


class GauntletResult(EvaluationResult):
    """Complete versioned JSON evaluator result."""

    schema: str
    schema_version: int
    evaluator_version: str
    commit: str
    candidate_state: CandidateState
    identity_verified: bool
    environment: dict[str, str]
    scope: str
    comparison: NotRequired[object]


@dataclass(frozen=True, slots=True)
class _PassEvidence:
    action_digest: str
    candidate_count: int
    endpoint_counters: dict[str, int]
    reconciliation: ReconciliationOracle


class ReconciliationEvidence(TypedDict):
    """Sanitized evidence captured from real dry-run operator logs."""

    file_action_count: int
    empty_directory_count: int
    file_action_digest: str
    empty_directory_digest: str
    digest: str


@dataclass(frozen=True, slots=True)
class _PipelineResult:
    plan: OrphanFilePlan
    reconciliation: ReconciliationOracle


class _ReconciliationCapture(logging.Handler):
    """Capture only dry-run action messages and sanitize them to fixture paths."""

    def __init__(self, root: Path) -> None:
        super().__init__(level=logging.INFO)
        self.root = root
        self.file_paths: list[str] = []
        self.directory_paths: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        for prefix, target in (
            ("Would delete orphaned file: ", self.file_paths),
            ("Would remove empty directory: ", self.directory_paths),
        ):
            if message.startswith(prefix):
                try:
                    relative = Path(message.removeprefix(prefix)).resolve().relative_to(self.root).as_posix()
                except ValueError as error:
                    raise GauntletSafetyError("dry-run reconciliation path escaped the fixture root") from error
                target.append(relative)
                return

    def evidence(self) -> ReconciliationOracle:
        """Return stable digests over exact operator-visible actions."""
        file_digest = hashlib.sha256()
        directory_digest = hashlib.sha256()
        combined_digest = hashlib.sha256()
        for path in sorted(self.file_paths):
            record = {"action": "would_delete_file", "path": path}
            _digest_record(file_digest, record)
            _digest_record(combined_digest, record)
        for path in sorted(self.directory_paths):
            record = {"action": "would_remove_empty_directory", "path": path}
            _digest_record(directory_digest, record)
            _digest_record(combined_digest, record)
        return ReconciliationOracle(
            file_action_count=len(self.file_paths),
            empty_directory_count=len(self.directory_paths),
            file_action_digest=file_digest.hexdigest(),
            empty_directory_digest=directory_digest.hexdigest(),
            digest=combined_digest.hexdigest(),
        )


def _digest_record(digest: _Digest, record: object) -> None:
    digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def _filesystem_digest(root: Path) -> str:
    """Hash path identities and regular-file contents outside measurements."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        path_stat = path.lstat()
        relative_path = path.relative_to(root).as_posix()
        record = (
            relative_path,
            path_stat.st_dev,
            path_stat.st_ino,
            path_stat.st_mode,
            path_stat.st_size,
            path_stat.st_mtime_ns,
            path_stat.st_ctime_ns,
        )
        digest.update(json.dumps(record, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\0")
        if stat.S_ISREG(path_stat.st_mode):
            with path.open("rb") as file_handle:
                while chunk := file_handle.read(1024 * 1024):
                    digest.update(chunk)
        elif stat.S_ISLNK(path_stat.st_mode):
            digest.update(os.fsencode(os.readlink(path)))
        digest.update(b"\n")
    return digest.hexdigest()


def _intended_action_digest(plan: OrphanFilePlan, root: Path) -> str:
    """Hash sanitized, ordered file actions without persisting raw candidates."""
    digest = hashlib.sha256()
    for identity in plan.files:
        try:
            relative_path = identity.path.relative_to(root).as_posix()
        except ValueError as error:
            raise GauntletSafetyError("evaluator candidate escaped the fixture root") from error
        action = {
            "action": "would_delete_file",
            "path": relative_path,
            "size_bytes": identity.size,
        }
        digest.update(json.dumps(action, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _prepare_pass(fixture: GauntletFixture) -> None:
    """Reset evaluator-visible application state before one pipeline pass."""
    clear_cache()
    fixture.client.reset_read_counts()


def _execute_pipeline(fixture: GauntletFixture) -> _PipelineResult:
    """Execute the real discovery, immutable planning, and dry-run pipeline."""
    orphaned_files = check_files_on_disk(
        fixture.client,
        list(fixture.initial_torrents),
        orphan_scan_roots=(),
    )
    plan = build_orphan_file_plan(orphaned_files)
    capture = _ReconciliationCapture(fixture.root)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(capture)
    root_logger.setLevel(logging.INFO)
    try:
        delete_orphaned_files(
            list(orphaned_files),
            True,
            fixture.client,
            torrents=list(fixture.initial_torrents),
            plan=plan,
            orphan_scan_roots=(),
        )
    finally:
        root_logger.removeHandler(capture)
        root_logger.setLevel(previous_level)
    return _PipelineResult(plan=plan, reconciliation=capture.evidence())


def _validate_unchanged_state(fixture: GauntletFixture, initial_filesystem_digest: str) -> None:
    """Reject qBittorrent or filesystem mutation after every evaluator pass."""
    if fixture.client.mutation_total:
        raise GauntletSafetyError("dry-run attempted a qBittorrent mutation")
    if _filesystem_digest(fixture.root) != initial_filesystem_digest:
        raise GauntletSafetyError("dry-run changed the fixture filesystem")


def _validate_pass(
    fixture: GauntletFixture,
    pipeline: _PipelineResult,
    initial_filesystem_digest: str,
) -> _PassEvidence:
    """Validate intent, reads, and zero mutation after one completed pass."""
    action_digest = _intended_action_digest(pipeline.plan, fixture.root)
    candidate_count = len(pipeline.plan.files)
    endpoint_counts = dict(sorted(fixture.client.read_counts.items()))
    _validate_unchanged_state(fixture, initial_filesystem_digest)
    if action_digest != fixture.intended_action_digest:
        raise GauntletSafetyError("intended actions did not match the fixture oracle")
    if candidate_count != fixture.profile.orphan_count:
        raise GauntletSafetyError("candidate count did not match the fixture oracle")
    _validate_endpoint_budget(endpoint_counts, expected_endpoint_budgets(fixture.profile))
    if pipeline.reconciliation != fixture.reconciliation:
        raise GauntletSafetyError("operator-visible reconciliation did not match the fixture oracle")
    return _PassEvidence(
        action_digest=action_digest,
        candidate_count=candidate_count,
        endpoint_counters=endpoint_counts,
        reconciliation=pipeline.reconciliation,
    )


def _validate_endpoint_budget(
    endpoint_counts: Mapping[str, int],
    budgets: Mapping[str, EndpointBudget],
) -> None:
    if set(endpoint_counts) != set(budgets):
        raise GauntletSafetyError("API read endpoints did not match the locked budget schema")
    for endpoint, count in endpoint_counts.items():
        budget = budgets[endpoint]
        if isinstance(count, bool) or count < budget.minimum or count > budget.maximum:
            raise GauntletSafetyError(f"API reads exceeded the locked budget for {endpoint}")


def _checked_pipeline_pass(
    fixture: GauntletFixture,
    initial_filesystem_digest: str,
) -> _PassEvidence:
    """Run and validate one untimed, untraced pipeline pass."""
    _prepare_pass(fixture)
    try:
        pipeline = _execute_pipeline(fixture)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        _validate_unchanged_state(fixture, initial_filesystem_digest)
        raise
    finally:
        clear_cache()
    return _validate_pass(fixture, pipeline, initial_filesystem_digest)


def _timed_pipeline_pass(
    fixture: GauntletFixture,
    initial_filesystem_digest: str,
) -> tuple[_PassEvidence, float]:
    """Time only the production pipeline, then validate outside the timer."""
    _prepare_pass(fixture)
    started_at = time.perf_counter()
    try:
        pipeline = _execute_pipeline(fixture)
    except (KeyboardInterrupt, SystemExit):
        clear_cache()
        raise
    except Exception:
        _validate_unchanged_state(fixture, initial_filesystem_digest)
        clear_cache()
        raise
    elapsed_seconds = time.perf_counter() - started_at
    clear_cache()
    return _validate_pass(fixture, pipeline, initial_filesystem_digest), elapsed_seconds


def _memory_pipeline_pass(
    fixture: GauntletFixture,
    initial_filesystem_digest: str,
) -> tuple[_PassEvidence, int]:
    """Trace one untimed pipeline pass independently from runtime samples."""
    _prepare_pass(fixture)
    tracemalloc.start()
    try:
        pipeline = _execute_pipeline(fixture)
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        _validate_unchanged_state(fixture, initial_filesystem_digest)
        raise
    finally:
        tracemalloc.stop()
        clear_cache()
    return _validate_pass(fixture, pipeline, initial_filesystem_digest), peak_bytes


def _measurement_policy() -> MeasurementPolicy:
    """Return the fixed always-warm measurement and cache policy."""
    return {
        "sequence": "one warmup, five untraced timed samples, one traced untimed memory pass",
        "timed_samples_traced": False,
        "memory_pass_timed": False,
        "application_cache": "cleared before every pass",
        "fixture_metadata": "materialized once and reused after the explicit warmup",
        "os_page_cache": "not flushed; timed and memory passes are warm",
        "sample_rejection": "none; all five timed samples are retained",
    }


def evaluate_fixture(fixture: GauntletFixture, *, samples: int) -> EvaluationResult:
    """Evaluate an already-built fixture, excluding setup and safety checks."""
    if samples != DEFAULT_SAMPLES:
        raise ValueError(f"comparable gauntlet runs require exactly {DEFAULT_SAMPLES} timed samples")
    if tracemalloc.is_tracing():
        raise GauntletSafetyError("tracemalloc must be disabled before gauntlet evaluation")

    try:
        verify_materialized_fixture(fixture)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as error:
        raise GauntletSafetyError("materialized fixture verification failed") from error
    initial_filesystem_digest = _filesystem_digest(fixture.root)
    warmup = _checked_pipeline_pass(fixture, initial_filesystem_digest)
    timed_evidence: list[_PassEvidence] = []
    runtimes: list[float] = []
    for _sample_index in range(samples):
        evidence, runtime = _timed_pipeline_pass(fixture, initial_filesystem_digest)
        timed_evidence.append(evidence)
        runtimes.append(runtime)
    memory_evidence, peak_memory_bytes = _memory_pipeline_pass(fixture, initial_filesystem_digest)

    median_runtime = statistics.median(runtimes)
    median_absolute_deviation = statistics.median(abs(runtime - median_runtime) for runtime in runtimes)
    return {
        "profile": fixture.profile.name,
        "tier": fixture.profile.tier,
        "seed": fixture.seed,
        "workload": {
            "torrents": fixture.profile.torrent_count,
            "filesystem_files": fixture.profile.file_count,
            "owned_files": fixture.profile.owned_file_count,
            "orphan_files": fixture.profile.orphan_count,
            "exact_metadata_torrents": fixture.profile.exact_metadata_torrent_count,
            "bulk_path_torrents": fixture.profile.bulk_path_torrent_count,
            "configured_roots": fixture.profile.configured_root_count,
            "shards": fixture.profile.shard_count,
            "timed_samples": samples,
            "warmup_passes": 1,
            "memory_passes": 1,
        },
        "fixture_manifest_digest": fixture.manifest_digest,
        "intended_action_digest": warmup.action_digest,
        "reconciliation": {
            "file_action_count": warmup.reconciliation.file_action_count,
            "empty_directory_count": warmup.reconciliation.empty_directory_count,
            "file_action_digest": warmup.reconciliation.file_action_digest,
            "empty_directory_digest": warmup.reconciliation.empty_directory_digest,
            "digest": warmup.reconciliation.digest,
        },
        "candidate_counts": {"orphan_files": warmup.candidate_count},
        "endpoint_counters": dict(warmup.endpoint_counters),
        "timed_sample_endpoint_counters": [dict(evidence.endpoint_counters) for evidence in timed_evidence],
        "pass_endpoint_counters": {
            "warmup": dict(warmup.endpoint_counters),
            "memory": dict(memory_evidence.endpoint_counters),
        },
        "mutation_counters": {
            "filesystem": 0,
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


def _sanitized_processor() -> str:
    """Return bounded processor metadata without path-like punctuation."""
    value = platform.processor().strip()
    if not value:
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                key, separator, item = line.partition(":")
                if separator and key.strip() in {"model name", "Hardware"}:
                    value = item.strip()
                    break
        except OSError:
            pass
    sanitized = "".join(character if character.isalnum() or character in " ._-" else "_" for character in value)
    return sanitized[:120] or "unknown"


def _filesystem_type(path: Path) -> str:
    """Return the Linux mount filesystem type without exposing mount paths."""
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return "unknown"
    resolved = path.resolve()
    best: tuple[int, str] | None = None
    for line in lines:
        left, separator, right = line.partition(" - ")
        fields = left.split()
        right_fields = right.split()
        if not separator or len(fields) < 5 or not right_fields:
            continue
        mount_point = Path(fields[4].replace("\\040", " "))
        try:
            resolved.relative_to(mount_point)
        except ValueError:
            continue
        candidate = (len(mount_point.parts), right_fields[0])
        if best is None or candidate[0] > best[0]:
            best = candidate
    return best[1] if best is not None else "unknown"


def _environment(fixture_root: Path) -> dict[str, str]:
    """Return useful environment metadata without hostnames or private paths."""
    logical_cpu_count = os.cpu_count()
    affinity_getter = cast("_AffinityGetter | None", getattr(os, "sched_getaffinity", None))
    try:
        affinity = sorted(affinity_getter(0)) if callable(affinity_getter) else []
    except OSError:
        affinity = []
    affinity_digest = hashlib.sha256(",".join(str(cpu) for cpu in affinity).encode("ascii")).hexdigest()
    filesystem_block_size = "unknown"
    filesystem_id = "unknown"
    statvfs_getter = cast("_StatvfsGetter | None", getattr(os, "statvfs", None))
    if callable(statvfs_getter):
        try:
            filesystem_stat = fixture_root.stat()
            filesystem_info = statvfs_getter(fixture_root)
        except OSError:
            pass
        else:
            filesystem_block_size = str(filesystem_info.f_bsize)
            filesystem_id = hashlib.sha256(
                f"{filesystem_stat.st_dev}:{getattr(filesystem_info, 'f_fsid', 0)}".encode("ascii")
            ).hexdigest()
    return {
        "cpu_affinity_digest": affinity_digest if affinity else "unknown",
        "effective_cpu_count": str(len(affinity)) if affinity else "unknown",
        "filesystem_block_size": filesystem_block_size,
        "filesystem_id": filesystem_id,
        "filesystem_type": _filesystem_type(fixture_root),
        "implementation": platform.python_implementation(),
        "kernel_release": platform.release() or "unknown",
        "logical_cpu_count": str(logical_cpu_count) if logical_cpu_count else "unknown",
        "machine": platform.machine() or "unknown",
        "operating_system": platform.system() or "unknown",
        "processor": _sanitized_processor(),
        "python": platform.python_version(),
    }


def run_gauntlet(
    profile: str | GauntletProfile = "quick",
    *,
    seed: int = DEFAULT_SEED,
    samples: int = DEFAULT_SAMPLES,
    repository_root: Path | None = None,
    expected_identity: RepositoryIdentity | None = None,
) -> GauntletResult:
    """Build and evaluate one profile while requiring stable repository state."""
    resolved_repository_root = (
        repository_root.resolve() if repository_root is not None else Path(__file__).resolve().parents[2]
    )
    identity_before = capture_repository_identity(resolved_repository_root)
    if expected_identity is not None:
        require_same_identity(expected_identity, identity_before)
    if isinstance(profile, str):
        try:
            resolved_profile = PROFILES[profile]
        except KeyError as error:
            raise ValueError(f"unknown gauntlet profile: {profile}") from error
    else:
        resolved_profile = profile
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed must be an integer")

    with tempfile.TemporaryDirectory(prefix="qbitunregistered-gauntlet-fixture-") as temporary_root:
        fixture = build_fixture(Path(temporary_root), resolved_profile, seed)
        measurements = evaluate_fixture(fixture, samples=samples)
        environment = _environment(fixture.root)
    result: GauntletResult = {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "commit": identity_before.commit,
        "candidate_state": {
            "clean": identity_before.clean,
            "diff_sha256": identity_before.diff_sha256,
        },
        "identity_verified": True,
        "environment": environment,
        "scope": "orphan_discovery_plan_and_dry_run_reconciliation",
        **measurements,
    }
    require_same_identity(
        identity_before,
        capture_repository_identity(resolved_repository_root),
    )
    return result


def serialize_result(result: Mapping[str, object]) -> str:
    """Return stable, human-readable JSON ready for output."""
    return json.dumps(result, indent=2, sort_keys=True) + "\n"


_BOUND_PUBLICATION_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and bool(getattr(os, "O_NOFOLLOW", 0))
    and os.open in os.supports_dir_fd
    and os.rename in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.unlink in os.supports_dir_fd
    and Path("/proc/self/fd").is_dir()
)


def _supports_bound_publication() -> bool:
    return _BOUND_PUBLICATION_SUPPORTED


def _directory_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _bound_descriptor_path(bound_directory: BoundOutputDirectory) -> Path | None:
    try:
        return (Path("/proc/self/fd") / str(bound_directory.descriptor)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None


def _bound_directory_is_safe(bound_directory: BoundOutputDirectory) -> bool:
    try:
        descriptor_stat = os.fstat(bound_directory.descriptor)
        path_stat = os.stat(bound_directory.path, follow_symlinks=False)
    except OSError:
        return False
    descriptor_path = _bound_descriptor_path(bound_directory)
    if descriptor_path is None or descriptor_path != bound_directory.path:
        return False
    return (
        stat.S_ISDIR(descriptor_stat.st_mode)
        and stat.S_ISDIR(path_stat.st_mode)
        and _directory_identity(descriptor_stat) == _directory_identity(path_stat)
        and all(not descriptor_path.is_relative_to(root) for root in bound_directory.protected_roots)
    )


@contextmanager
def bind_output_directory(
    directory: Path,
    *,
    protected_roots: tuple[Path, ...] = (),
) -> Iterator[BoundOutputDirectory]:
    """Bind result publication to one identity-checked directory descriptor."""
    if not _supports_bound_publication():
        raise GauntletSafetyError("safe descriptor-relative result publication is unavailable")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise GauntletSafetyError("could not bind the validated result directory") from error
    bound_directory = BoundOutputDirectory(directory, descriptor, protected_roots)
    try:
        if not _bound_directory_is_safe(bound_directory):
            raise GauntletSafetyError("validated result directory changed before publication")
        yield bound_directory
    finally:
        os.close(descriptor)


def _open_unique_bound_file(
    bound_directory: BoundOutputDirectory,
    *,
    prefix: str,
    suffix: str,
) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _attempt in range(100):
        name = f"{prefix}{secrets.token_hex(16)}{suffix}"
        try:
            descriptor = os.open(name, flags, 0o600, dir_fd=bound_directory.descriptor)
        except FileExistsError:
            continue
        except OSError as error:
            raise GauntletSafetyError("could not create a bound result file") from error
        return descriptor, name
    raise GauntletSafetyError("could not allocate a unique bound result file")


def validate_bound_output_leaf(
    bound_directory: BoundOutputDirectory,
    output_name: str,
) -> None:
    """Require a safely replaceable output leaf without following symlinks."""
    if not output_name or output_name != Path(output_name).name:
        raise GauntletSafetyError("bound result output name is invalid")
    if not _bound_directory_is_safe(bound_directory):
        raise GauntletSafetyError("validated result directory changed before evaluation")
    try:
        output_stat = os.stat(
            output_name,
            dir_fd=bound_directory.descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise GauntletSafetyError("could not validate the result output safely") from error
    if not (stat.S_ISREG(output_stat.st_mode) or stat.S_ISLNK(output_stat.st_mode)):
        raise GauntletSafetyError("result output must be missing, a regular file, or a symbolic link")


def _unlink_bound_file(bound_directory: BoundOutputDirectory, name: str) -> None:
    try:
        os.unlink(name, dir_fd=bound_directory.descriptor)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise GauntletSafetyError("could not clean up a bound result file") from error


def _write_bound_result(
    serialized_result: str,
    bound_directory: BoundOutputDirectory,
    output_name: str | None,
) -> Path:
    if not _bound_directory_is_safe(bound_directory):
        raise GauntletSafetyError("validated result directory changed before publication")
    remove_output_on_failure = output_name is None
    temporary_name: str | None = None
    published = False
    try:
        if output_name is None:
            output_descriptor, output_name = _open_unique_bound_file(
                bound_directory,
                prefix="qbitunregistered-gauntlet-",
                suffix=".json",
            )
            try:
                os.close(output_descriptor)
            except BaseException:
                _unlink_bound_file(bound_directory, output_name)
                raise
        temporary_descriptor, temporary_name = _open_unique_bound_file(
            bound_directory,
            prefix=_RESULT_STAGING_PREFIX,
            suffix=".tmp",
        )
        with os.fdopen(temporary_descriptor, mode="w", encoding="utf-8") as temporary_file:
            temporary_file.write(serialized_result)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if not _bound_directory_is_safe(bound_directory):
            raise GauntletSafetyError("validated result directory changed during publication")
        try:
            os.rename(
                temporary_name,
                output_name,
                src_dir_fd=bound_directory.descriptor,
                dst_dir_fd=bound_directory.descriptor,
            )
        except OSError as error:
            raise GauntletSafetyError("could not publish the bound result safely") from error
        published = True
    finally:
        try:
            if temporary_name is not None:
                _unlink_bound_file(bound_directory, temporary_name)
        finally:
            if remove_output_on_failure and not published and output_name is not None:
                _unlink_bound_file(bound_directory, output_name)
    assert output_name is not None
    return bound_directory.path / output_name


def write_serialized_result(
    serialized_result: str,
    output: Path | None = None,
    *,
    default_directory: Path | None = None,
    bound_directory: BoundOutputDirectory | None = None,
) -> Path:
    """Write JSON to an explicit path or a unique file in the temporary directory."""
    if bound_directory is not None:
        if output is not None:
            if output.parent != bound_directory.path or output.name != Path(output.name).name:
                raise GauntletSafetyError("bound result path does not match its validated directory")
            output_name: str | None = output.name
        else:
            if default_directory != bound_directory.path:
                raise GauntletSafetyError("bound default result directory does not match validation")
            output_name = None
        return _write_bound_result(serialized_result, bound_directory, output_name)

    remove_output_on_failure = output is None
    if output is None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="qbitunregistered-gauntlet-",
            suffix=".json",
            dir=default_directory,
        )
        output_path = Path(temporary_name)
        try:
            os.close(descriptor)
        except BaseException:
            output_path.unlink(missing_ok=True)
            raise
    else:
        expanded_output = output.expanduser()
        output_path = expanded_output.parent.resolve() / expanded_output.name
        output_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=_RESULT_STAGING_PREFIX,
            suffix=".tmp",
            dir=output_path.parent,
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(serialized_result)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, output_path)
        published = True
    finally:
        try:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        finally:
            if remove_output_on_failure and not published:
                output_path.unlink(missing_ok=True)
    return output_path


def write_result(result: Mapping[str, object], output: Path | None = None) -> Path:
    """Serialize and write one evaluator result."""
    return write_serialized_result(serialize_result(result), output)


__all__ = [
    "BoundOutputDirectory",
    "DEFAULT_SAMPLES",
    "DEFAULT_SEED",
    "EVALUATOR_VERSION",
    "GauntletSafetyError",
    "bind_output_directory",
    "evaluate_fixture",
    "run_gauntlet",
    "serialize_result",
    "write_result",
    "write_serialized_result",
]
