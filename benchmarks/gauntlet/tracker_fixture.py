"""Deterministic tracker-metadata fixtures for the repository gauntlet."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TypedDict, cast

from benchmarks.gauntlet.fixture_factory import MUTATING_ENDPOINTS

TRACKER_READ_ENDPOINTS = (
    "torrents.info",
    "torrents.info.include_trackers",
    "torrents_trackers",
)
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


class FakeTrackerBulkTorrent(dict[str, object]):
    """Mapping-shaped bulk torrent response with qBittorrent-style attributes."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        super().__init__(payload)
        self.hash = cast(str, payload["hash"])
        self.name = cast(str, payload["name"])
        self.save_path = cast(str, payload["save_path"])
        self.content_path = cast(str, payload["content_path"])
        self.category = cast(str, payload["category"])
        self.tags = cast(str, payload["tags"])

    @property
    def trackers(self) -> object:
        """Expose the embedded field without normalizing malformed metadata."""
        return self["trackers"]


EmbeddedTrackersMode = Literal["supported", "omitted", "rejected", "malformed"]


def _fresh_decoded_payload(value: object) -> object:
    return json.loads(json.dumps(value, separators=(",", ":")))


class _FakeTrackerTorrents:
    def __init__(self, client: FakeTrackerClient) -> None:
        self._client = client

    def info(self, **kwargs: Any) -> object:
        include_trackers = kwargs.get("include_trackers") is True
        endpoint = "torrents.info.include_trackers" if include_trackers else "torrents.info"
        self._client.read_counts[endpoint] += 1
        if include_trackers and self._client.embedded_trackers_mode == "rejected":
            raise TypeError("include_trackers is unsupported")

        snapshot = self._client.torrent_snapshot
        if isinstance(snapshot, BaseException):
            raise snapshot
        if not isinstance(snapshot, Sequence) or isinstance(snapshot, (str, bytes, bytearray)):
            return snapshot

        response: list[dict[str, object]] = []
        for index, torrent in enumerate(snapshot):
            if not isinstance(torrent, TrackerTorrent):
                return snapshot
            payload: dict[str, object] = {
                "hash": torrent.hash,
                "name": torrent.name,
                "save_path": torrent.save_path,
                "content_path": torrent.content_path,
                "category": torrent.category,
                "tags": torrent.tags,
            }
            if include_trackers and self._client.embedded_trackers_mode != "omitted":
                payload["trackers"] = (
                    {"malformed": True}
                    if self._client.embedded_trackers_mode == "malformed" and index == 0
                    else self._client.trackers_by_hash[torrent.hash]
                )
            response.append(payload)
        decoded = _fresh_decoded_payload(response)
        if not isinstance(decoded, list):
            raise TypeError("tracker snapshot did not decode to a list")
        return [FakeTrackerBulkTorrent(item) for item in decoded if isinstance(item, Mapping)]


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
        self.embedded_trackers_mode = embedded_trackers_mode
        self.read_counts: Counter[str] = Counter({endpoint: 0 for endpoint in TRACKER_READ_ENDPOINTS})
        self.mutation_counts: Counter[str] = Counter({endpoint: 0 for endpoint in MUTATING_ENDPOINTS})
        self.torrents = _FakeTrackerTorrents(self)
        self.application: Any = None
        self.torrent_categories: Any = None
        self.logout_count = 0

    def reset_read_counts(self) -> None:
        """Reset every tracker read endpoint to an explicit zero."""
        self.read_counts.clear()
        self.read_counts.update({endpoint: 0 for endpoint in TRACKER_READ_ENDPOINTS})

    def set_torrent_snapshot(self, snapshot: object) -> None:
        """Replace the current torrent snapshot for churn scenarios."""
        self.torrent_snapshot = snapshot

    def set_exact_trackers(self, torrent_hash: str, value: object) -> None:
        """Replace one exact tracker response for failure and churn scenarios."""
        self.trackers_by_hash[torrent_hash] = value

    def set_embedded_trackers_mode(self, mode: EmbeddedTrackersMode) -> None:
        """Replace the optional embedded transport behavior."""
        self.embedded_trackers_mode = mode

    def torrents_info(self, **kwargs: Any) -> list[Any]:
        """Expose the direct API shape required by the project protocol."""
        return cast(list[Any], self.torrents.info(**kwargs))

    def torrents_trackers(self, torrent_hash: str | None = None, **_kwargs: Any) -> list[dict[str, object]]:
        """Return qBittorrent pseudo records followed by fresh real trackers."""
        self.read_counts["torrents_trackers"] += 1
        trackers = self.trackers_by_hash.get(torrent_hash or "", [])
        if isinstance(trackers, BaseException):
            raise trackers
        if trackers is None:
            return cast(list[dict[str, object]], trackers)
        if not isinstance(trackers, Sequence) or isinstance(trackers, (str, bytes, bytearray)):
            return cast(list[dict[str, object]], _fresh_decoded_payload(trackers))
        payload = [*_PSEUDO_TRACKERS, *trackers]
        return cast(list[dict[str, object]], _fresh_decoded_payload(payload))

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

    def torrents_add_tags(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_add_tags")

    def torrents_delete(self, *_args: Any, **_kwargs: Any) -> None:
        self._record_mutation("torrents_delete")

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
    return [
        {
            "url": f"https://tracker-{index:05d}-{record_index}.invalid/announce",
            "status": status,
            "tier": record_index,
            "num_peers": 10 + record_index,
            "num_seeds": 20 + record_index,
            "num_leeches": record_index,
            "num_downloaded": 30 + record_index,
            "msg": message,
        }
        for record_index, (status, message) in enumerate(statuses)
    ]


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

    client = FakeTrackerClient(
        torrents,
        trackers_by_hash,
        embedded_trackers_mode=embedded_trackers_mode,
    )
    return TrackerGauntletFixture(
        root=root,
        profile=profile,
        seed=seed,
        initial_torrents=tuple(torrents),
        client=client,
    )


__all__ = [
    "EmbeddedTrackersMode",
    "TrackerActionRecord",
    "FakeTrackerBulkTorrent",
    "FakeTrackerClient",
    "TRACKER_FULL_PROFILE",
    "TRACKER_PROFILES",
    "TRACKER_QUICK_PROFILE",
    "TrackerGauntletFixture",
    "TrackerGauntletProfile",
    "TrackerTorrent",
    "build_tracker_fixture",
    "expected_tracker_action_digest",
    "expected_tracker_action_records",
]
