"""Deterministic tracker-metadata fixtures for the repository gauntlet."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, UserList
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypedDict, cast

from benchmarks.gauntlet.fixture_factory import MUTATING_ENDPOINTS

TRACKER_READ_ENDPOINTS = (
    "torrents.info",
    "torrents.info.include_trackers",
    "torrents_trackers",
)
TRACKER_BULK_BATCH_SIZE = 100
EXACT_UNREGISTERED_MESSAGE = "torrent is not registered"
PREFIX_UNREGISTERED_MESSAGE = "tracker prefix unavailable: fixture"
DEFAULT_UNREGISTERED_TAG = "unregistered"
CROSS_SEED_UNREGISTERED_TAG = "unregistered:crossseeding"
DELETE_TAG = "tracker-delete"


class TrackerActionRecord(TypedDict):
    """One independently expected tag or torrent-only deletion action."""

    action: str
    tag: str
    torrent_hash: str


@dataclass(frozen=True, slots=True)
class TrackerGauntletProfile:
    """One locked tracker-metadata workload shape."""

    name: str
    torrent_count: int
    tracker_record_count: int
    save_path_group_count: int
    default_tag_count: int
    cross_seed_tag_count: int
    delete_count: int
    tier: str = "candidate"

    def __post_init__(self) -> None:
        """Reject malformed or internally inconsistent workloads."""
        if not self.name or not self.name.replace("-", "").replace("_", "").isalnum():
            raise ValueError("profile name must contain only letters, numbers, hyphens, or underscores")
        if self.torrent_count < 1 or self.tracker_record_count != self.torrent_count * 3:
            raise ValueError("tracker profiles require exactly three records per torrent")
        if self.save_path_group_count != self.torrent_count - self.cross_seed_tag_count:
            raise ValueError("save-path groups must pair each cross-seed target with one healthy torrent")
        if self.default_tag_count < 1 or self.cross_seed_tag_count < 1:
            raise ValueError("tracker profiles require default-tag and cross-seed targets")
        if self.default_tag_count + 2 * self.cross_seed_tag_count > self.torrent_count:
            raise ValueError("tracker target counts exceed the torrent workload")
        if (self.default_tag_count + self.cross_seed_tag_count) % 2:
            raise ValueError("tracker target count must divide evenly between exact and prefix messages")
        if not 1 <= self.delete_count <= self.default_tag_count:
            raise ValueError("torrent-only deletion count must fit within default-tag targets")

    @property
    def exact_message_count(self) -> int:
        """Return the locked number of exact-message targets."""
        return (self.default_tag_count + self.cross_seed_tag_count) // 2

    @property
    def prefix_message_count(self) -> int:
        """Return the locked number of prefix-message targets."""
        return self.exact_message_count


TRACKER_QUICK_PROFILE = TrackerGauntletProfile(
    name="tracker-quick",
    torrent_count=1_300,
    tracker_record_count=3_900,
    save_path_group_count=1_200,
    default_tag_count=200,
    cross_seed_tag_count=100,
    delete_count=13,
    tier="round",
)
TRACKER_FULL_PROFILE = TrackerGauntletProfile(
    name="tracker-full",
    torrent_count=13_000,
    tracker_record_count=39_000,
    save_path_group_count=12_000,
    default_tag_count=2_000,
    cross_seed_tag_count=1_000,
    delete_count=130,
    tier="candidate",
)
TRACKER_PROFILES: Mapping[str, TrackerGauntletProfile] = {
    TRACKER_QUICK_PROFILE.name: TRACKER_QUICK_PROFILE,
    TRACKER_FULL_PROFILE.name: TRACKER_FULL_PROFILE,
}


@dataclass(frozen=True, slots=True)
class _TrackerState:
    """Minimal complete paused state required by the torrent protocol."""

    is_complete: bool = True
    is_paused: bool = True


@dataclass(frozen=True, slots=True)
class TrackerTorrent:
    """Sanitized torrent metadata passed to production operation boundaries."""

    hash: str
    name: str
    save_path: str
    content_path: str
    category: str = ""
    tags: str = ""
    state_enum: _TrackerState = _TrackerState()
    added_on: int = 0
    completion_on: int = 0
    seeding_time: int = 0
    ratio: float = 0.0
    uploaded: int = 0
    downloaded: int = 0
    files: list[Any] | None = None


class FakeAttrDict(dict[str, object]):
    """Dependency-free mapping normalization used by installed API responses."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        super().__init__({key: FakeAttrDict(value) if isinstance(value, Mapping) else value for key, value in payload.items()})

    def __getattr__(self, name: str) -> object:
        """Expose valid response mapping fields through attribute access."""
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class FakeTrackerBulkTorrent(FakeAttrDict):
    """Dependency-free equivalent of qBittorrent's torrent dictionary wrapper."""

    def __init__(self, payload: Mapping[str, object], client: FakeTrackerClient | None) -> None:
        converted = dict(payload)
        converted["reannounce_in"] = converted.pop("reannounce")
        super().__init__(converted)
        self._client = client

    @property
    def trackers(self) -> object:
        """Fetch exact tracker metadata as the installed client property does."""
        if self._client is None:
            raise AttributeError("trackers")
        return self._client.torrents_trackers(torrent_hash=cast(str, self["hash"]))


class FakeTorrentInfoList(UserList[FakeTrackerBulkTorrent]):
    """Dependency-free equivalent of the installed TorrentInfoList."""

    def __init__(
        self,
        payloads: Sequence[Mapping[str, object]],
        client: FakeTrackerClient | None = None,
    ) -> None:
        super().__init__(
            [
                payload if isinstance(payload, FakeTrackerBulkTorrent) else FakeTrackerBulkTorrent(payload, client)
                for payload in payloads
            ]
        )


class FakeTracker(FakeAttrDict):
    """Dependency-free equivalent of one installed exact Tracker response."""


class FakeTrackersList(UserList[FakeTracker]):
    """Dependency-free equivalent of the installed TrackersList."""

    def __init__(self, payloads: Sequence[Mapping[str, object]]) -> None:
        super().__init__([payload if isinstance(payload, FakeTracker) else FakeTracker(payload) for payload in payloads])


EmbeddedTrackersMode = Literal["supported", "omitted", "rejected", "malformed"]


def _encode_wire_payload(value: object) -> bytes:
    """Serialize one fake server response before client measurement starts."""
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _receive_and_decode_wire_payload(wire_payload: bytes) -> object:
    """Allocate a fresh received buffer and decode it as the client would."""
    received_payload = memoryview(wire_payload).tobytes()
    return json.loads(received_payload)


def torrent_info_payload(torrent: TrackerTorrent, index: int) -> dict[str, object]:
    """Return a deterministic sanitized Web API 2.15.1 torrent-info mapping."""
    total_size = 1_073_741_824 + index * 4_096
    downloaded = total_size + index * 1_024
    uploaded = downloaded * 2
    save_path = Path(torrent.save_path)
    return {
        "added_on": 1_700_000_000 + index,
        "amount_left": 0,
        "auto_tmm": False,
        "availability": 1.0,
        "category": torrent.category,
        "comment": (
            "Sanitized deterministic gauntlet fixture metadata for offline evaluator fidelity; "
            "contains no operator data, credentials, routable hosts, or live tracker addresses."
        ),
        "completed": total_size,
        "completion_on": 1_700_003_600 + index,
        "connections_count": 4 + index % 5,
        "connections_limit": 100,
        "content_path": torrent.content_path,
        "created_by": "qbitunregistered-gauntlet/fixture",
        "creation_date": 1_699_900_000 + index,
        "dl_limit": 0,
        "dlspeed": 0,
        "download_path": str(save_path / ".unfinished"),
        "downloaded": downloaded,
        "downloaded_session": index * 128,
        "eta": 8_640_000,
        "f_l_piece_prio": False,
        "force_start": False,
        "has_metadata": True,
        "hash": torrent.hash,
        "inactive_seeding_time_limit": -2,
        "infohash_v1": torrent.hash,
        "infohash_v2": hashlib.sha256(f"gauntlet:tracker:v2:{torrent.hash}".encode("ascii")).hexdigest(),
        "last_activity": 1_700_100_000 + index,
        "magnet_uri": f"magnet:?xt=urn:btih:{torrent.hash}&dn={torrent.name}&tr=https%3A%2F%2Ftracker.invalid%2Fannounce",
        "max_inactive_seeding_time": -1,
        "max_ratio": -1.0,
        "max_seeding_time": -1,
        "name": torrent.name,
        "num_complete": 20 + index % 7,
        "num_incomplete": index % 3,
        "num_leechs": index % 4,
        "num_seeds": 5 + index % 6,
        "piece_size": 4_194_304,
        "pieces_have": 256 + index % 8,
        "pieces_num": 256 + index % 8,
        "popularity": 1.0 + (index % 10) / 100,
        "priority": 0,
        "private": False,
        "progress": 1.0,
        "ratio": 2.0,
        "ratio_limit": -2.0,
        "reannounce": 1_800 + index % 300,
        "root_path": torrent.content_path,
        "save_path": torrent.save_path,
        "seeding_time": 96_400 + index,
        "seeding_time_limit": -2,
        "seen_complete": 1_700_090_000 + index,
        "seq_dl": False,
        "share_limit_action": "Stop",
        "share_limits_mode": "Default",
        "size": total_size,
        "state": "stoppedUP",
        "super_seeding": False,
        "tags": torrent.tags,
        "time_active": 100_000 + index,
        "total_size": total_size,
        "total_wasted": index * 64,
        "tracker": "https://tracker.invalid/announce",
        "trackers_count": 3,
        "up_limit": 0,
        "uploaded": uploaded,
        "uploaded_session": index * 256,
        "upspeed": 0,
    }


def _torrent_state(torrent: TrackerTorrent) -> str:
    """Return the qBittorrent state corresponding to the current fake snapshot."""
    if torrent.state_enum.is_complete:
        return "stoppedUP" if torrent.state_enum.is_paused else "uploading"
    return "stoppedDL" if torrent.state_enum.is_paused else "downloading"


def effective_torrent_info_payload(
    stored_payload: Mapping[str, object],
    torrent: TrackerTorrent,
) -> dict[str, object]:
    """Return the authoritative post-overlay torrent-info base mapping."""
    save_path = Path(torrent.save_path)
    payload = dict(stored_payload)
    payload.update(
        {
            "added_on": torrent.added_on,
            "category": torrent.category,
            "completion_on": torrent.completion_on,
            "content_path": torrent.content_path,
            "downloaded": torrent.downloaded,
            "download_path": str(save_path / ".unfinished"),
            "hash": torrent.hash,
            "magnet_uri": (
                f"magnet:?xt=urn:btih:{torrent.hash}&dn={torrent.name}" "&tr=https%3A%2F%2Ftracker.invalid%2Fannounce"
            ),
            "name": torrent.name,
            "ratio": torrent.ratio,
            "root_path": torrent.content_path,
            "save_path": torrent.save_path,
            "seeding_time": torrent.seeding_time,
            "state": _torrent_state(torrent),
            "tags": torrent.tags,
            "uploaded": torrent.uploaded,
        }
    )
    return payload


class _FakeTrackerTorrents:
    def __init__(self, client: FakeTrackerClient) -> None:
        self._client = client

    def info(self, **kwargs: Any) -> object:
        include_trackers = kwargs.get("include_trackers") is True
        endpoint = "torrents.info.include_trackers" if include_trackers else "torrents.info"
        self._client.read_counts[endpoint] += 1
        wire_payload: bytes | None
        if include_trackers:
            requested_hashes = self._client.accept_bulk_tracker_request(kwargs.get("torrent_hashes"))
            if self._client.embedded_trackers_mode == "rejected":
                raise TypeError("include_trackers is unsupported")
            wire_payload = self._client._bulk_tracker_wire_by_hashes[requested_hashes]
        else:
            if kwargs.get("torrent_hashes") is not None:
                raise TypeError("ordinary tracker snapshots must be unfiltered")
            snapshot = self._client.torrent_snapshot
            if isinstance(snapshot, BaseException):
                raise snapshot
            wire_payload = self._client._ordinary_torrent_wire_payload
            if wire_payload is None:
                return snapshot
            self._client.mark_ordinary_snapshot_served()
        self._client.begin_info_materialization()
        decoded = _receive_and_decode_wire_payload(wire_payload)
        if not isinstance(decoded, list):
            raise TypeError("tracker snapshot did not decode to a list")
        return FakeTorrentInfoList(
            [item for item in decoded if isinstance(item, Mapping)],
            self._client,
        )


class FakeTrackerClient:
    """Auditable fake client with independent exact and embedded tracker reads."""

    def __init__(
        self,
        torrents: Sequence[TrackerTorrent],
        trackers_by_hash: Mapping[str, object],
        *,
        embedded_trackers_mode: EmbeddedTrackersMode,
    ) -> None:
        self.initial_torrents = tuple(torrents)
        self.torrent_snapshot: object = self.initial_torrents
        self.trackers_by_hash: dict[str, object] = {
            torrent_hash: list(trackers) if isinstance(trackers, Sequence) else trackers
            for torrent_hash, trackers in trackers_by_hash.items()
        }
        self.torrent_info_by_hash = {
            torrent.hash: torrent_info_payload(torrent, index) for index, torrent in enumerate(self.initial_torrents)
        }
        self._exact_tracker_wire_by_hash: dict[str, bytes] = {}
        self._default_exact_tracker_wire = _encode_wire_payload(_PSEUDO_TRACKERS)
        self._ordinary_torrent_wire_payload: bytes | None = None
        self._bulk_tracker_wire_by_hashes: dict[tuple[str, ...], bytes] = {}
        self._expected_bulk_hash_batches = tuple(
            tuple(torrent.hash for torrent in self.initial_torrents[offset : offset + TRACKER_BULK_BATCH_SIZE])
            for offset in range(0, len(self.initial_torrents), TRACKER_BULK_BATCH_SIZE)
        )
        self._observed_bulk_hash_batches: list[tuple[str, ...]] = []
        self._ordinary_snapshot_served = False
        self._measurement_in_progress = False
        self.embedded_trackers_mode: EmbeddedTrackersMode = embedded_trackers_mode
        self.prepare_exact_tracker_wire_payloads()
        self.prepare_torrent_wire_payloads()
        self.read_counts: Counter[str] = Counter({endpoint: 0 for endpoint in TRACKER_READ_ENDPOINTS})
        self.mutation_counts: Counter[str] = Counter({endpoint: 0 for endpoint in MUTATING_ENDPOINTS})
        self.execution_action_records: list[TrackerActionRecord] = []
        self.torrents = _FakeTrackerTorrents(self)
        self.application: Any = None
        self.torrent_categories: Any = None
        self.logout_count = 0
        self._before_info_materialization: Callable[[], None] | None = None
        self._after_execution_return: Callable[[], None] | None = None

    def set_measurement_callbacks(
        self,
        before_info_materialization: Callable[[], None],
        after_execution_return: Callable[[], None],
    ) -> None:
        """Set one-shot callbacks around the measured production response lifetime."""
        if self._before_info_materialization is not None or self._after_execution_return is not None:
            raise RuntimeError("tracker measurement callbacks are already configured")
        self._before_info_materialization = before_info_materialization
        self._after_execution_return = after_execution_return

    def begin_info_materialization(self) -> None:
        """Arm measurement immediately before the first response is materialized."""
        callback = self._before_info_materialization
        if callback is None:
            return
        self._before_info_materialization = None
        self._measurement_in_progress = True
        try:
            callback()
        except BaseException:
            self._measurement_in_progress = False
            raise

    def finish_execution_measurement(self) -> None:
        """Stop measurement immediately after the observed execution returns."""
        callback = self._after_execution_return
        if callback is None:
            raise RuntimeError("tracker execution measurement callback is unavailable")
        self._after_execution_return = None
        try:
            callback()
        finally:
            self._measurement_in_progress = False

    @property
    def measurement_callbacks_configured(self) -> bool:
        """Return whether either one-shot measurement callback remains pending."""
        return self._before_info_materialization is not None or self._after_execution_return is not None

    def reset_read_counts(self) -> None:
        """Reset every tracker read endpoint to an explicit zero."""
        self.read_counts.clear()
        self.read_counts.update({endpoint: 0 for endpoint in TRACKER_READ_ENDPOINTS})
        self._observed_bulk_hash_batches.clear()
        self._ordinary_snapshot_served = False

    def mark_ordinary_snapshot_served(self) -> None:
        """Record the required unfiltered snapshot before tracker batches."""
        self._ordinary_snapshot_served = True

    @staticmethod
    def _requested_hash_tuple(value: object) -> tuple[str, ...]:
        """Normalize the public qbittorrent-api ``torrent_hashes`` argument."""
        if isinstance(value, str):
            requested = tuple(value.split("|"))
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            requested = tuple(value)
        else:
            raise TypeError("bulk tracker requests require one canonical torrent_hashes batch")
        if not requested or any(not isinstance(item, str) or not item for item in requested):
            raise TypeError("bulk tracker requests require non-empty hash strings")
        return cast(tuple[str, ...], requested)

    def accept_bulk_tracker_request(self, value: object) -> tuple[str, ...]:
        """Validate and record the next exact ordered tracker batch."""
        if not self._ordinary_snapshot_served:
            raise ValueError("bulk tracker batches require the ordinary snapshot first")
        requested = self._requested_hash_tuple(value)
        if len(set(requested)) != len(requested):
            raise ValueError("bulk tracker batch contains duplicate hashes")
        known_hashes = {torrent.hash for torrent in self.initial_torrents}
        if any(torrent_hash not in known_hashes for torrent_hash in requested):
            raise ValueError("bulk tracker batch contains an unknown hash")
        next_index = len(self._observed_bulk_hash_batches)
        if next_index >= len(self._expected_bulk_hash_batches) or requested != self._expected_bulk_hash_batches[next_index]:
            raise ValueError("bulk tracker request does not match the next canonical hash batch")
        self._observed_bulk_hash_batches.append(requested)
        return requested

    def validate_bulk_tracker_requests(self) -> None:
        """Require an attempted bulk transport to cover the snapshot exactly once."""
        observed = tuple(self._observed_bulk_hash_batches)
        expected = (
            self._expected_bulk_hash_batches[:1]
            if self.embedded_trackers_mode == "rejected"
            else self._expected_bulk_hash_batches
        )
        if observed and observed != expected:
            raise ValueError("bulk tracker requests did not complete the canonical hash batches")

    def _ordinary_wire_payload(self, snapshot: object) -> bytes | None:
        """Encode one valid ordinary fake-server response, if representable."""
        if not isinstance(snapshot, Sequence) or isinstance(snapshot, (str, bytes, bytearray)):
            return None
        response: list[dict[str, object]] = []
        for torrent in snapshot:
            if not isinstance(torrent, TrackerTorrent):
                return None
            response.append(effective_torrent_info_payload(self.torrent_info_by_hash[torrent.hash], torrent))
        return _encode_wire_payload(response)

    def _bulk_wire_payloads(
        self,
        trackers_by_hash: Mapping[str, object],
        mode: EmbeddedTrackersMode,
    ) -> dict[tuple[str, ...], bytes]:
        """Pre-encode every canonical filtered fake-server response."""
        prepared: dict[tuple[str, ...], bytes] = {}
        torrent_by_hash = {torrent.hash: torrent for torrent in self.initial_torrents}
        for batch_index, requested_hashes in enumerate(self._expected_bulk_hash_batches):
            response: list[dict[str, object]] = []
            for item_index, torrent_hash in enumerate(requested_hashes):
                torrent = torrent_by_hash[torrent_hash]
                payload = effective_torrent_info_payload(self.torrent_info_by_hash[torrent_hash], torrent)
                if mode != "omitted":
                    payload["trackers"] = (
                        {"malformed": True}
                        if mode == "malformed" and batch_index == 0 and item_index == 0
                        else trackers_by_hash[torrent_hash]
                    )
                response.append(payload)
            prepared[requested_hashes] = _encode_wire_payload(response)
        return prepared

    def prepare_torrent_wire_payloads(self) -> None:
        """Refresh ordinary and filtered response bytes before measurement."""
        if self._measurement_in_progress:
            raise RuntimeError("torrent wire payloads cannot be prepared during measurement")
        ordinary = self._ordinary_wire_payload(self.torrent_snapshot)
        bulk = self._bulk_wire_payloads(self.trackers_by_hash, self.embedded_trackers_mode)
        self._ordinary_torrent_wire_payload = ordinary
        self._bulk_tracker_wire_by_hashes = bulk

    def set_torrent_snapshot(self, snapshot: object) -> None:
        """Replace the current torrent snapshot for churn scenarios."""
        if self._measurement_in_progress:
            raise RuntimeError("torrent wire payload cannot be prepared during measurement")
        ordinary = self._ordinary_wire_payload(snapshot)
        self.torrent_snapshot = snapshot
        self._ordinary_torrent_wire_payload = ordinary

    def set_exact_trackers(self, torrent_hash: str, value: object) -> None:
        """Replace one exact tracker response for failure and churn scenarios."""
        if self._measurement_in_progress:
            raise RuntimeError("exact tracker wire payload cannot be prepared during measurement")
        wire_payload = self._encode_exact_tracker_wire_payload(value)
        prospective_trackers = dict(self.trackers_by_hash)
        prospective_trackers[torrent_hash] = value
        self.trackers_by_hash = prospective_trackers
        if wire_payload is None:
            self._exact_tracker_wire_by_hash.pop(torrent_hash, None)
        else:
            self._exact_tracker_wire_by_hash[torrent_hash] = wire_payload

    def set_embedded_trackers_mode(self, mode: EmbeddedTrackersMode) -> None:
        """Replace the optional embedded transport behavior."""
        if self._measurement_in_progress:
            raise RuntimeError("torrent wire payloads cannot be prepared during measurement")
        bulk_payloads = self._bulk_wire_payloads(self.trackers_by_hash, mode)
        self.embedded_trackers_mode = mode
        self._bulk_tracker_wire_by_hashes = bulk_payloads

    def torrents_info(self, **kwargs: Any) -> list[Any]:
        """Expose the direct API shape required by the project protocol."""
        return cast(list[Any], self.torrents.info(**kwargs))

    @staticmethod
    def _encode_exact_tracker_wire_payload(trackers: object) -> bytes | None:
        """Return canonical wire bytes for one exact fake-server response."""
        if isinstance(trackers, BaseException) or trackers is None:
            return None
        if isinstance(trackers, Sequence) and not isinstance(trackers, (str, bytes, bytearray)):
            response: object = [*_PSEUDO_TRACKERS, *trackers]
        else:
            response = trackers
        return _encode_wire_payload(response)

    def prepare_exact_tracker_wire_payloads(self) -> None:
        """Refresh canonical exact-response bytes before measurement starts."""
        if self._measurement_in_progress:
            raise RuntimeError("exact tracker wire payloads cannot be prepared during measurement")
        prepared_payloads: dict[str, bytes] = {}
        for torrent_hash, trackers in self.trackers_by_hash.items():
            wire_payload = self._encode_exact_tracker_wire_payload(trackers)
            if wire_payload is not None:
                prepared_payloads[torrent_hash] = wire_payload
        self._exact_tracker_wire_by_hash = prepared_payloads

    def torrents_trackers(self, torrent_hash: str | None = None, **_kwargs: Any) -> list[Any]:
        """Return qBittorrent pseudo records followed by fresh real trackers."""
        self.read_counts["torrents_trackers"] += 1
        resolved_hash = torrent_hash or ""
        trackers = self.trackers_by_hash.get(resolved_hash, [])
        if isinstance(trackers, BaseException):
            raise trackers
        if trackers is None:
            return cast(list[Any], trackers)
        if resolved_hash in self.trackers_by_hash:
            wire_payload = self._exact_tracker_wire_by_hash.get(resolved_hash)
            if wire_payload is None:
                raise RuntimeError("exact tracker wire payload was not prepared")
        else:
            wire_payload = self._default_exact_tracker_wire
        decoded = _receive_and_decode_wire_payload(wire_payload)
        if not isinstance(trackers, Sequence) or isinstance(trackers, (str, bytes, bytearray)):
            return cast(list[Any], decoded)
        if not isinstance(decoded, list) or any(not isinstance(item, Mapping) for item in decoded):
            raise TypeError("exact tracker response did not decode to mappings")
        return cast(list[Any], FakeTrackersList(cast(list[Mapping[str, object]], decoded)))

    def torrents_files(self, torrent_hash: str | None = None, **_kwargs: Any) -> list[object]:
        """Return no files because tracker deletion candidates are torrent-only."""
        del torrent_hash
        return []

    def app_default_save_path(self) -> str:
        """Return an unused deterministic default path for protocol completeness."""
        return ""

    def torrents_categories(self) -> dict[str, object]:
        """Return no categories for the tracker-only evaluator workload."""
        return {}

    def auth_log_out(self) -> None:
        """Record the non-mutating CLI logout boundary."""
        self.logout_count += 1

    def _record_mutation(self, endpoint: str) -> None:
        self.mutation_counts[endpoint] += 1

    @staticmethod
    def _normalized_strings(value: object, description: str) -> list[str]:
        if isinstance(value, str):
            return [value]
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            raise TypeError(f"{description} must be a string or sequence of strings")
        resolved = list(value)
        if any(not isinstance(item, str) or not item for item in resolved):
            raise TypeError(f"{description} must contain non-empty strings")
        return cast(list[str], resolved)

    def torrents_add_tags(self, torrent_hashes: object, tags: object, **_kwargs: Any) -> None:
        """Record the complete normalized tag endpoint arguments in memory."""
        self._record_mutation("torrents_add_tags")
        for tag in self._normalized_strings(tags, "tags"):
            for torrent_hash in self._normalized_strings(torrent_hashes, "torrent_hashes"):
                self.execution_action_records.append(
                    {
                        "action": "add_tag",
                        "tag": tag,
                        "torrent_hash": torrent_hash,
                    }
                )

    def torrents_delete(self, delete_files: bool, torrent_hashes: object, **_kwargs: Any) -> None:
        """Record the complete normalized delete endpoint arguments in memory."""
        self._record_mutation("torrents_delete")
        action = "delete_torrent_with_files" if delete_files else "delete_torrent_only"
        for torrent_hash in self._normalized_strings(torrent_hashes, "torrent_hashes"):
            self.execution_action_records.append(
                {
                    "action": action,
                    "tag": DELETE_TAG,
                    "torrent_hash": torrent_hash,
                }
            )

    def torrents_pause(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_pause")

    def torrents_remove_tags(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_remove_tags")

    def torrents_resume(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_resume")

    def torrents_set_auto_management(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_set_auto_management")

    def torrents_set_share_limits(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_set_share_limits")

    def torrents_tags(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_tags")

    @property
    def mutation_total(self) -> int:
        """Return the number of qBittorrent mutation attempts."""
        return sum(self.mutation_counts.values())


@dataclass(frozen=True, slots=True)
class TrackerGauntletFixture:
    """One materialized tracker workload with no filesystem candidates."""

    root: Path
    profile: TrackerGauntletProfile
    seed: int
    initial_torrents: tuple[TrackerTorrent, ...]
    client: FakeTrackerClient


_PSEUDO_TRACKERS: tuple[dict[str, object], ...] = tuple(
    {
        "url": url,
        "status": 0,
        "tier": -1,
        "num_peers": peers,
        "num_seeds": -1,
        "num_leeches": -1,
        "num_downloaded": -1,
        "msg": "",
    }
    for url, peers in (("** [DHT] **", 3), ("** [PeX] **", 2), ("** [LSD] **", 1))
)


def _torrent_hash(seed: int, index: int) -> str:
    return hashlib.sha256(f"gauntlet:tracker:torrent:{seed}:{index}".encode("ascii")).hexdigest()


def expected_tracker_action_records(
    profile: TrackerGauntletProfile,
    seed: int,
) -> tuple[TrackerActionRecord, ...]:
    """Derive the complete expected action set from fixture roles alone."""
    records: list[TrackerActionRecord] = []
    for index in range(profile.default_tag_count):
        records.append(
            {
                "action": "add_tag",
                "tag": DEFAULT_UNREGISTERED_TAG,
                "torrent_hash": _torrent_hash(seed, index),
            }
        )
    for index in range(
        profile.default_tag_count,
        profile.default_tag_count + profile.cross_seed_tag_count,
    ):
        records.append(
            {
                "action": "add_tag",
                "tag": CROSS_SEED_UNREGISTERED_TAG,
                "torrent_hash": _torrent_hash(seed, index),
            }
        )
    for index in range(profile.delete_count):
        records.append(
            {
                "action": "delete_torrent_only",
                "tag": DELETE_TAG,
                "torrent_hash": _torrent_hash(seed, index),
            }
        )
    return tuple(sorted(records, key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))))


def expected_tracker_action_digest(profile: TrackerGauntletProfile, seed: int) -> str:
    """Hash independently derived fixture actions without production output."""
    digest = hashlib.sha256()
    for record in expected_tracker_action_records(profile, seed):
        digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _real_trackers(index: int, role: Literal["exact", "prefix", "healthy"]) -> list[dict[str, object]]:
    primary_status = {"exact": 4, "prefix": 5, "healthy": 2}[role]
    primary_message = {
        "exact": EXACT_UNREGISTERED_MESSAGE,
        "prefix": PREFIX_UNREGISTERED_MESSAGE,
        "healthy": "",
    }[role]
    statuses = ((primary_status, primary_message), (2, ""), (0, ""))
    trackers: list[dict[str, object]] = []
    for record_index, (status, message) in enumerate(statuses):
        next_announce = 120 + (index % 300) + record_index
        min_announce = 30 + record_index
        endpoints = [
            {
                "name": f"endpoint-{address_family}",
                "updating": False,
                "status": status,
                "msg": message,
                "bt_version": 2,
                "num_peers": 10 + record_index + endpoint_index,
                "num_seeds": 20 + record_index + endpoint_index,
                "num_leeches": record_index + endpoint_index,
                "num_downloaded": 30 + record_index + endpoint_index,
                "next_announce": next_announce + endpoint_index,
                "min_announce": min_announce,
            }
            for endpoint_index, address_family in enumerate(("ipv4", "ipv6"))
        ]
        trackers.append(
            {
                "url": f"https://tracker-{index:05d}-{record_index}.invalid/announce",
                "status": status,
                "tier": record_index,
                "num_peers": 10 + record_index,
                "num_seeds": 20 + record_index,
                "num_leeches": record_index,
                "num_downloaded": 30 + record_index,
                "msg": message,
                "next_announce": next_announce,
                "min_announce": min_announce,
                "endpoints": endpoints,
            }
        )
    return trackers


def _interleaved_response_order(
    torrents: Sequence[TrackerTorrent],
    *,
    seed: int,
    action_hashes: set[str],
) -> list[TrackerTorrent]:
    """Return stable hash-shuffled roles with one action target at the tail."""
    ordered = sorted(
        torrents,
        key=lambda torrent: hashlib.sha256(f"gauntlet:tracker:response-order:{seed}:{torrent.hash}".encode("ascii")).digest(),
    )
    tail_index = max(index for index, torrent in enumerate(ordered) if torrent.hash in action_hashes)
    ordered.append(ordered.pop(tail_index))
    return ordered


def build_tracker_fixture(
    root: Path,
    profile: TrackerGauntletProfile,
    seed: int,
    *,
    embedded_trackers_mode: EmbeddedTrackersMode = "supported",
) -> TrackerGauntletFixture:
    """Build a sanitized tracker fixture without filesystem or network access."""
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    unregistered_count = profile.default_tag_count + profile.cross_seed_tag_count
    cross_group_start = profile.default_tag_count
    healthy_pair_start = unregistered_count
    torrents: list[TrackerTorrent] = []
    trackers_by_hash: dict[str, list[dict[str, object]]] = {}
    for index in range(profile.torrent_count):
        torrent_hash = _torrent_hash(seed, index)
        if index < profile.exact_message_count:
            role: Literal["exact", "prefix", "healthy"] = "exact"
        elif index < unregistered_count:
            role = "prefix"
        else:
            role = "healthy"

        if index < profile.default_tag_count:
            group_index = index
        elif index < unregistered_count:
            group_index = index
        elif index < healthy_pair_start + profile.cross_seed_tag_count:
            group_index = cross_group_start + (index - healthy_pair_start)
        else:
            group_index = index - profile.cross_seed_tag_count
        save_path = str(root / f"group-{group_index:05d}")
        torrents.append(
            TrackerTorrent(
                hash=torrent_hash,
                name=f"tracker-fixture-{index:05d}",
                save_path=save_path,
                content_path=str(root / f"content-{index:05d}"),
                tags="tracker-delete" if index < profile.delete_count else "",
            )
        )
        trackers_by_hash[torrent_hash] = _real_trackers(index, role)

    action_hashes = {_torrent_hash(seed, index) for index in range(profile.default_tag_count + profile.cross_seed_tag_count)}
    interleaved_torrents = _interleaved_response_order(torrents, seed=seed, action_hashes=action_hashes)
    client = FakeTrackerClient(
        interleaved_torrents,
        trackers_by_hash,
        embedded_trackers_mode=embedded_trackers_mode,
    )
    return TrackerGauntletFixture(
        root=root,
        profile=profile,
        seed=seed,
        initial_torrents=tuple(interleaved_torrents),
        client=client,
    )


__all__ = [
    "EmbeddedTrackersMode",
    "FakeAttrDict",
    "TrackerActionRecord",
    "FakeTrackerBulkTorrent",
    "FakeTrackerClient",
    "FakeTorrentInfoList",
    "FakeTracker",
    "FakeTrackersList",
    "TRACKER_FULL_PROFILE",
    "TRACKER_BULK_BATCH_SIZE",
    "TRACKER_PROFILES",
    "TRACKER_QUICK_PROFILE",
    "TrackerGauntletFixture",
    "TrackerGauntletProfile",
    "TrackerTorrent",
    "build_tracker_fixture",
    "expected_tracker_action_digest",
    "expected_tracker_action_records",
    "effective_torrent_info_payload",
    "torrent_info_payload",
]
