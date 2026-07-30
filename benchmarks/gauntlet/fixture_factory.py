"""Deterministic filesystem and qBittorrent fixtures for the gauntlet."""

from __future__ import annotations

import hashlib
import json
import stat
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal, Protocol, cast

ROOT_NAMES = ("default", "movies", "series")
MUTATING_ENDPOINTS = (
    "torrents_add_tags",
    "torrents_delete",
    "torrents_pause",
    "torrents_remove_tags",
    "torrents_resume",
    "torrents_set_auto_management",
    "torrents_set_share_limits",
    "torrents_tags",
)
READ_ENDPOINTS = (
    "application.default_save_path",
    "torrent_categories.categories",
    "torrents.info",
    "torrents_files",
)
MUTATION_COUNTER_KEYS = ("filesystem", "qbittorrent", *MUTATING_ENDPOINTS)


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


def _fresh_decoded_payload(value: object) -> object:
    """Model a fresh HTTP JSON response rather than sharing fixture objects."""
    return json.loads(json.dumps(value, separators=(",", ":")))


@dataclass(frozen=True, slots=True)
class EndpointBudget:
    """Inclusive read-count limits for one qBittorrent API boundary."""

    minimum: int
    maximum: int


@dataclass(frozen=True, slots=True)
class ReconciliationOracle:
    """Sanitized operator-visible dry-run reconciliation evidence."""

    file_action_count: int
    empty_directory_count: int
    file_action_digest: str
    empty_directory_digest: str
    digest: str


@dataclass(frozen=True, slots=True)
class GauntletProfile:
    """One locked synthetic workload shape."""

    name: str
    torrent_count: int
    file_count: int
    orphan_count: int
    exact_metadata_torrent_count: int
    shard_count: int
    configured_root_count: int = len(ROOT_NAMES)
    tier: str = "candidate"

    def __post_init__(self) -> None:
        """Reject malformed or internally inconsistent workloads."""
        if not self.name or not self.name.replace("-", "_").isalnum():
            raise ValueError("profile name must contain only letters, numbers, hyphens, or underscores")
        if self.torrent_count < 1 or self.file_count <= self.orphan_count:
            raise ValueError("profile must contain owned files and at least one torrent")
        if self.orphan_count < 1:
            raise ValueError("profile must contain at least one known orphan")
        if not 1 <= self.exact_metadata_torrent_count <= self.torrent_count:
            raise ValueError("exact metadata torrent count must fit within the torrent count")
        if self.shard_count < 1:
            raise ValueError("profile shard count must be positive")
        if self.configured_root_count != len(ROOT_NAMES):
            raise ValueError(f"configured root count must remain locked to {len(ROOT_NAMES)}")
        if self.exact_owned_file_count < self.exact_metadata_torrent_count:
            raise ValueError("each exact metadata torrent must own at least one file")

    @property
    def bulk_path_torrent_count(self) -> int:
        """Return torrents whose validated single-file content path is sufficient."""
        return self.torrent_count - self.exact_metadata_torrent_count

    @property
    def owned_file_count(self) -> int:
        """Return files known to be owned by the synthetic torrent snapshot."""
        return self.file_count - self.orphan_count

    @property
    def exact_owned_file_count(self) -> int:
        """Return files collectively owned through exact metadata responses."""
        return self.owned_file_count - self.bulk_path_torrent_count


QUICK_PROFILE = GauntletProfile(
    name="quick",
    torrent_count=1_200,
    file_count=9_400,
    orphan_count=1,
    exact_metadata_torrent_count=674,
    shard_count=32,
    tier="round",
)
FULL_PROFILE = GauntletProfile(
    name="full",
    torrent_count=12_000,
    file_count=94_000,
    orphan_count=3,
    exact_metadata_torrent_count=6_739,
    shard_count=128,
    tier="candidate",
)
PROFILES: Mapping[str, GauntletProfile] = {
    QUICK_PROFILE.name: QUICK_PROFILE,
    FULL_PROFILE.name: FULL_PROFILE,
}


@dataclass(frozen=True, slots=True)
class LogicalFile:
    """One sanitized file record independent of its temporary absolute root."""

    root_name: str
    relative_path: str
    content: bytes
    owner_hash: str | None

    @property
    def fixture_path(self) -> str:
        """Return the stable path used in manifests and action digests."""
        return f"{self.root_name}/{self.relative_path}"


@dataclass(frozen=True, slots=True)
class LogicalTorrent:
    """One torrent and the logical files it owns."""

    torrent_hash: str
    name: str
    root_name: str
    content_directory: str | None
    files: tuple[LogicalFile, ...]


@dataclass(frozen=True, slots=True)
class FixtureBlueprint:
    """Pure deterministic manifest that can be checked without filesystem setup."""

    profile: GauntletProfile
    seed: int
    torrents: tuple[LogicalTorrent, ...]
    orphans: tuple[LogicalFile, ...]
    manifest_digest: str
    intended_action_digest: str
    reconciliation: ReconciliationOracle

    def iter_files(self) -> Iterator[LogicalFile]:
        """Yield every owned file followed by every known orphan."""
        for torrent in self.torrents:
            yield from torrent.files
        yield from self.orphans


@dataclass(frozen=True, slots=True)
class FakeTorrent:
    """Small torrent response with only evaluator-relevant attributes."""

    hash: str
    name: str
    save_path: str
    content_path: str
    category: str = ""
    tags: str = ""


class FakeBulkTorrent(dict[str, object]):
    """Mapping-shaped qBittorrent torrent response with attribute access."""

    def __init__(self, payload: Mapping[str, object]) -> None:
        super().__init__(payload)
        self.hash = cast(str, payload["hash"])
        self.name = cast(str, payload["name"])
        self.save_path = cast(str, payload["save_path"])
        self.content_path = cast(str, payload["content_path"])
        self.category = cast(str, payload["category"])
        self.tags = cast(str, payload["tags"])


class _FakeApplication:
    def __init__(self, client: FakeQBittorrentClient, default_save_path: str) -> None:
        self._client = client
        self._default_save_path = default_save_path

    @property
    def default_save_path(self) -> str:
        self._client.record_read("application.default_save_path")
        return self._default_save_path


class _FakeCategories:
    def __init__(self, client: FakeQBittorrentClient, categories: Mapping[str, Mapping[str, str]]) -> None:
        self._client = client
        self._categories = {name: dict(value) for name, value in categories.items()}

    @property
    def categories(self) -> dict[str, dict[str, str]]:
        self._client.record_read("torrent_categories.categories")
        return {name: dict(value) for name, value in self._categories.items()}


class _FakeTorrents:
    def __init__(self, client: FakeQBittorrentClient, snapshot: object) -> None:
        self._client = client
        self.snapshot = snapshot

    def info(self, **kwargs: Any) -> object:
        self._client.record_read("torrents.info")
        if isinstance(self.snapshot, BaseException):
            raise self.snapshot
        if kwargs.get("include_files") is True:
            return self._client.bulk_torrent_snapshot(self.snapshot)
        return self.snapshot


BulkFilesMode = Literal["supported", "legacy_missing", "unsupported", "malformed"]


class FakeQBittorrentClient:
    """Auditable qBittorrent boundary used by evaluator runs and safety tests."""

    def __init__(
        self,
        default_save_path: Path,
        categories: Mapping[str, Mapping[str, str]],
        torrents: Sequence[FakeTorrent],
        files_by_hash: Mapping[str, object],
        *,
        bulk_files_mode: BulkFilesMode = "supported",
    ) -> None:
        self.read_counts: Counter[str] = Counter()
        self.mutation_counts: Counter[str] = Counter({name: 0 for name in MUTATING_ENDPOINTS})
        self.application = _FakeApplication(self, str(default_save_path))
        self.torrent_categories = _FakeCategories(self, categories)
        self.torrents = _FakeTorrents(self, list(torrents))
        self._files_by_hash = dict(files_by_hash)
        self._bulk_files_mode = bulk_files_mode
        self.logout_count = 0

    def record_read(self, name: str) -> None:
        """Record one read at an evaluator-relevant API boundary."""
        self.read_counts[name] += 1

    def set_torrent_snapshot(self, snapshot: object) -> None:
        """Replace the next/current torrent snapshot for churn tests."""
        self.torrents.snapshot = snapshot

    def set_torrent_files(self, torrent_hash: str, value: object) -> None:
        """Replace exact file metadata for one torrent in safety tests."""
        self._files_by_hash[torrent_hash] = value

    def set_bulk_files_mode(self, mode: BulkFilesMode) -> None:
        """Select supported, legacy, unsupported, or malformed bulk metadata."""
        self._bulk_files_mode = mode

    def bulk_torrent_snapshot(self, snapshot: object) -> object:
        """Return the qBittorrent 5.2 mapping response for ``include_files``."""
        if self._bulk_files_mode == "unsupported":
            raise TypeError("include_files is unsupported")
        if not isinstance(snapshot, Sequence) or isinstance(snapshot, (str, bytes, bytearray)):
            return snapshot
        response_payload: list[object] = []
        malformed_emitted = False
        for torrent in snapshot:
            if not isinstance(torrent, FakeTorrent):
                response_payload.append(torrent)
                continue
            raw_files = self._files_by_hash.get(torrent.hash, [])
            torrent_payload: dict[str, object] = {
                "hash": torrent.hash,
                "name": torrent.name,
                "save_path": torrent.save_path,
                "content_path": torrent.content_path,
                "category": torrent.category,
                "tags": torrent.tags,
            }
            if self._bulk_files_mode != "legacy_missing":
                torrent_payload["files"] = raw_files
            if self._bulk_files_mode == "malformed" and raw_files and not malformed_emitted:
                torrent_payload["files"] = {"malformed": True}
                malformed_emitted = True
            response_payload.append(torrent_payload)
        decoded_payload = _fresh_decoded_payload(response_payload)
        if not isinstance(decoded_payload, list):
            raise TypeError("bulk torrent payload did not decode to a list")
        return [FakeBulkTorrent(item) if isinstance(item, Mapping) else item for item in decoded_payload]

    def reset_read_counts(self) -> None:
        """Clear reads while retaining mutation evidence."""
        self.read_counts.clear()

    def torrents_info(self, **kwargs: Any) -> list[Any]:
        """Expose the direct API shape required by the project protocol."""
        return cast(list[Any], self.torrents.info(**kwargs))

    def torrents_trackers(self, torrent_hash: str | None = None, **_kwargs: Any) -> list[Any]:
        """Return no trackers; orphan evaluation never relies on them."""
        del torrent_hash
        return []

    def torrents_files(self, torrent_hash: str | None = None, **_kwargs: Any) -> list[Any]:
        """Return exact torrent file metadata while counting the request."""
        self.record_read("torrents_files")
        value = self._files_by_hash.get(torrent_hash or "", [])
        if isinstance(value, BaseException):
            raise value
        return cast(list[Any], _fresh_decoded_payload(value))

    def app_default_save_path(self) -> str:
        """Expose the direct API shape required by the project protocol."""
        return self.application.default_save_path

    def torrents_categories(self) -> dict[str, dict[str, str]]:
        """Expose the direct API shape required by the project protocol."""
        return self.torrent_categories.categories

    def auth_log_out(self) -> None:
        """Record the non-mutating CLI logout boundary."""
        self.logout_count += 1

    def _record_mutation(self, name: str) -> None:
        self.mutation_counts[name] += 1

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
class GauntletFixture:
    """Materialized workload whose setup is excluded from measurements."""

    root: Path
    scan_roots: tuple[Path, ...]
    profile: GauntletProfile
    seed: int
    files: tuple[Path, ...]
    owned_files: tuple[Path, ...]
    orphan_files: tuple[Path, ...]
    initial_torrents: tuple[FakeTorrent, ...]
    client: FakeQBittorrentClient
    manifest_digest: str
    expected_manifest_digest: str
    intended_action_digest: str
    reconciliation: ReconciliationOracle


def _torrent_hash(seed: int, index: int) -> str:
    return hashlib.sha256(f"gauntlet:torrent:{seed}:{index}".encode("ascii")).hexdigest()


def _file_content(seed: int, fixture_path: str) -> bytes:
    return hashlib.sha256(f"gauntlet:file:{seed}:{fixture_path}".encode("ascii")).digest()


def _logical_file(
    *,
    seed: int,
    root_name: str,
    relative_path: str,
    owner_hash: str | None,
) -> LogicalFile:
    fixture_path = f"{root_name}/{relative_path}"
    return LogicalFile(
        root_name=root_name,
        relative_path=relative_path,
        content=_file_content(seed, fixture_path),
        owner_hash=owner_hash,
    )


def _digest_record(digest: _Digest, record: object) -> None:
    digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def _manifest_header(profile: GauntletProfile, seed: int) -> dict[str, object]:
    return {
        "configured_root_count": profile.configured_root_count,
        "exact_metadata_torrent_count": profile.exact_metadata_torrent_count,
        "file_count": profile.file_count,
        "name": profile.name,
        "orphan_count": profile.orphan_count,
        "seed": seed,
        "shard_count": profile.shard_count,
        "tier": profile.tier,
        "torrent_count": profile.torrent_count,
    }


def _root_record() -> dict[str, object]:
    return {
        "categories": {"movies": "movies", "series": "series"},
        "default_root": "default",
    }


def _reconciliation_oracle(orphans: Sequence[LogicalFile]) -> ReconciliationOracle:
    file_digest = hashlib.sha256()
    directory_digest = hashlib.sha256()
    combined_digest = hashlib.sha256()
    directories = sorted({f"{file.root_name}/{Path(file.relative_path).parent.as_posix()}" for file in orphans})
    for file in sorted(orphans, key=lambda item: item.fixture_path):
        record = {"action": "would_delete_file", "path": file.fixture_path}
        _digest_record(file_digest, record)
        _digest_record(combined_digest, record)
    for directory in directories:
        record = {"action": "would_remove_empty_directory", "path": directory}
        _digest_record(directory_digest, record)
        _digest_record(combined_digest, record)
    return ReconciliationOracle(
        file_action_count=len(orphans),
        empty_directory_count=len(directories),
        file_action_digest=file_digest.hexdigest(),
        empty_directory_digest=directory_digest.hexdigest(),
        digest=combined_digest.hexdigest(),
    )


def build_blueprint(profile: GauntletProfile, seed: int) -> FixtureBlueprint:
    """Build a pure logical fixture and its locked manifest/action digests."""
    exact_base, exact_remainder = divmod(
        profile.exact_owned_file_count,
        profile.exact_metadata_torrent_count,
    )
    torrents: list[LogicalTorrent] = []
    for torrent_index in range(profile.torrent_count):
        torrent_hash = _torrent_hash(seed, torrent_index)
        root_name = ROOT_NAMES[torrent_index % profile.configured_root_count]
        if torrent_index < profile.exact_metadata_torrent_count:
            file_count = exact_base + (torrent_index < exact_remainder)
            content_directory = f"multi/torrent-{torrent_index:06d}"
            files = tuple(
                _logical_file(
                    seed=seed,
                    root_name=root_name,
                    relative_path=f"{content_directory}/file-{file_index:03d}.bin",
                    owner_hash=torrent_hash,
                )
                for file_index in range(file_count)
            )
        else:
            content_directory = None
            files = (
                _logical_file(
                    seed=seed,
                    root_name=root_name,
                    relative_path=(f"single/shard-{torrent_index % profile.shard_count:03d}/torrent-{torrent_index:06d}.bin"),
                    owner_hash=torrent_hash,
                ),
            )
        torrents.append(
            LogicalTorrent(
                torrent_hash=torrent_hash,
                name=f"fixture-torrent-{torrent_index:06d}",
                root_name=root_name,
                content_directory=content_directory,
                files=files,
            )
        )

    seed_token = hashlib.sha256(f"gauntlet:orphan:{seed}".encode("ascii")).hexdigest()[:12]
    orphans = tuple(
        _logical_file(
            seed=seed,
            root_name=ROOT_NAMES[index % profile.configured_root_count],
            relative_path=f"orphan/orphan-{seed_token}-{index:02d}.bin",
            owner_hash=None,
        )
        for index in range(profile.orphan_count)
    )

    manifest_digest = hashlib.sha256()
    _digest_record(manifest_digest, _manifest_header(profile, seed))
    _digest_record(manifest_digest, _root_record())
    for torrent in torrents:
        _digest_record(
            manifest_digest,
            {
                "category": "" if torrent.root_name == ROOT_NAMES[0] else torrent.root_name,
                "content_directory": torrent.content_directory,
                "hash": torrent.torrent_hash,
                "name": torrent.name,
                "root": torrent.root_name,
            },
        )
        for file in torrent.files:
            _digest_record(
                manifest_digest,
                {
                    "content_sha256": hashlib.sha256(file.content).hexdigest(),
                    "owner": file.owner_hash,
                    "path": file.fixture_path,
                    "size": len(file.content),
                },
            )
    for file in orphans:
        _digest_record(
            manifest_digest,
            {
                "content_sha256": hashlib.sha256(file.content).hexdigest(),
                "owner": None,
                "path": file.fixture_path,
                "size": len(file.content),
            },
        )

    intended_action_digest = hashlib.sha256()
    for file in sorted(orphans, key=lambda item: item.fixture_path):
        _digest_record(
            intended_action_digest,
            {
                "action": "would_delete_file",
                "path": file.fixture_path,
                "size_bytes": len(file.content),
            },
        )
    return FixtureBlueprint(
        profile=profile,
        seed=seed,
        torrents=tuple(torrents),
        orphans=orphans,
        manifest_digest=manifest_digest.hexdigest(),
        intended_action_digest=intended_action_digest.hexdigest(),
        reconciliation=_reconciliation_oracle(orphans),
    )


def expected_endpoint_budgets(profile: GauntletProfile) -> dict[str, EndpointBudget]:
    """Return inclusive qBittorrent read budgets for each evaluator pass."""
    return {
        "application.default_save_path": EndpointBudget(1, 1),
        "torrent_categories.categories": EndpointBudget(1, 1),
        "torrents.info": EndpointBudget(1, 1),
        "torrents_files": EndpointBudget(0, profile.exact_metadata_torrent_count),
    }


def expected_endpoint_counters(profile: GauntletProfile) -> dict[str, int]:
    """Return the representative maximum read count retained in result summaries."""
    return {name: budget.maximum for name, budget in expected_endpoint_budgets(profile).items()}


def _root_name(path: Path, root_paths: Mapping[str, Path]) -> str:
    matches = [name for name, root_path in root_paths.items() if path.resolve() == root_path.resolve()]
    if len(matches) != 1:
        raise ValueError("materialized qBittorrent path does not identify one configured fixture root")
    return matches[0]


def materialized_fixture_digest(fixture: GauntletFixture) -> str:
    """Derive the manifest from completed filesystem and fake API state."""
    digest = hashlib.sha256()
    _digest_record(digest, _manifest_header(fixture.profile, fixture.seed))
    root_paths: dict[str, Path] = dict(zip(ROOT_NAMES, fixture.scan_roots, strict=True))

    default_root = _root_name(Path(fixture.client.application.default_save_path), root_paths)
    categories = fixture.client.torrent_categories.categories
    category_roots: dict[str, str] = {}
    for category, value in sorted(categories.items()):
        category_save_path = value.get("savePath")
        if not isinstance(category_save_path, str):
            raise ValueError("materialized category metadata is malformed")
        category_roots[category] = _root_name(Path(category_save_path), root_paths)
    _digest_record(
        digest,
        {"categories": category_roots, "default_root": default_root},
    )

    snapshot = fixture.client.torrents.info()
    if not isinstance(snapshot, Sequence) or isinstance(snapshot, (str, bytes, bytearray)):
        raise ValueError("materialized torrent snapshot is not a sequence")
    torrents: list[FakeTorrent] = []
    seen_hashes: set[str] = set()
    for torrent in snapshot:
        if (
            not isinstance(torrent, FakeTorrent)
            or not isinstance(torrent.hash, str)
            or len(torrent.hash) != 64
            or any(character not in "0123456789abcdef" for character in torrent.hash)
            or not isinstance(torrent.name, str)
            or not torrent.name
            or not isinstance(torrent.save_path, str)
            or not torrent.save_path
            or not isinstance(torrent.content_path, str)
            or not torrent.content_path
            or not isinstance(torrent.category, str)
            or torrent.hash in seen_hashes
        ):
            raise ValueError("materialized torrent snapshot contains malformed or duplicate metadata")
        seen_hashes.add(torrent.hash)
        torrents.append(torrent)

    owned_paths: set[Path] = set()
    for torrent in torrents:
        torrent_save_path = Path(torrent.save_path).resolve()
        root_name = _root_name(torrent_save_path, root_paths)
        content_path = Path(torrent.content_path).resolve()
        if content_path.is_dir():
            content_directory: str | None = content_path.relative_to(torrent_save_path).as_posix()
            metadata = fixture.client.torrents_files(torrent_hash=torrent.hash)
            materialized_files: list[Path] = []
            for item in metadata:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    raise ValueError("materialized exact file metadata is malformed")
                materialized_files.append((torrent_save_path / item["name"]).resolve())
        else:
            content_directory = None
            materialized_files = [content_path]
        _digest_record(
            digest,
            {
                "category": torrent.category,
                "content_directory": content_directory,
                "hash": torrent.hash,
                "name": torrent.name,
                "root": root_name,
            },
        )
        for path in materialized_files:
            try:
                relative_path = path.relative_to(fixture.root).as_posix()
            except ValueError as error:
                raise ValueError("materialized torrent file escaped the fixture root") from error
            path_stat = path.lstat()
            if not stat.S_ISREG(path_stat.st_mode):
                raise ValueError("materialized torrent file is missing or not regular")
            owned_paths.add(path)
            _digest_record(
                digest,
                {
                    "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "owner": torrent.hash,
                    "path": relative_path,
                    "size": path_stat.st_size,
                },
            )

    orphan_paths: list[Path] = []
    for path in sorted(fixture.root.rglob("*")):
        path_stat = path.lstat()
        if stat.S_ISLNK(path_stat.st_mode):
            raise ValueError("materialized fixture contains a symbolic link")
        if stat.S_ISREG(path_stat.st_mode) and path.resolve() not in owned_paths:
            orphan_paths.append(path.resolve())
    for path in orphan_paths:
        path_stat = path.lstat()
        _digest_record(
            digest,
            {
                "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "owner": None,
                "path": path.relative_to(fixture.root).as_posix(),
                "size": path_stat.st_size,
            },
        )
    return digest.hexdigest()


def verify_materialized_fixture(fixture: GauntletFixture) -> str:
    """Fail closed unless actual filesystem/API state matches the logical oracle."""
    try:
        actual_digest = materialized_fixture_digest(fixture)
    finally:
        fixture.client.reset_read_counts()
    if actual_digest != fixture.expected_manifest_digest:
        raise ValueError("materialized fixture does not match the locked logical blueprint")
    return actual_digest


def build_fixture(root: Path, profile: GauntletProfile, seed: int) -> GauntletFixture:
    """Materialize a stable, sanitized qBittorrent and filesystem workload."""
    blueprint = build_blueprint(profile, seed)
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    root_paths = {root_name: root / root_name for root_name in ROOT_NAMES}
    for root_path in root_paths.values():
        root_path.mkdir()

    all_files: list[Path] = []
    owned_files: list[Path] = []
    orphan_files: list[Path] = []
    fake_torrents: list[FakeTorrent] = []
    files_by_hash: dict[str, list[dict[str, str]]] = {}
    for torrent in blueprint.torrents:
        save_path = root_paths[torrent.root_name]
        materialized_files: list[Path] = []
        for logical_file in torrent.files:
            path = save_path / logical_file.relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(logical_file.content)
            materialized_files.append(path)
        all_files.extend(materialized_files)
        owned_files.extend(materialized_files)
        if torrent.content_directory is None:
            content_path = str(materialized_files[0])
        else:
            content_path = str(save_path / torrent.content_directory)
            files_by_hash[torrent.torrent_hash] = [{"name": logical_file.relative_path} for logical_file in torrent.files]
        fake_torrents.append(
            FakeTorrent(
                hash=torrent.torrent_hash,
                name=torrent.name,
                save_path=str(save_path),
                content_path=content_path,
                category="" if torrent.root_name == ROOT_NAMES[0] else torrent.root_name,
            )
        )

    for logical_file in blueprint.orphans:
        path = root_paths[logical_file.root_name] / logical_file.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(logical_file.content)
        all_files.append(path)
        orphan_files.append(path)

    categories = {root_name: {"savePath": str(root_paths[root_name])} for root_name in ROOT_NAMES[1:]}
    client = FakeQBittorrentClient(
        root_paths[ROOT_NAMES[0]],
        categories,
        fake_torrents,
        files_by_hash,
    )
    provisional = GauntletFixture(
        root=root.resolve(),
        scan_roots=tuple(root_paths[name].resolve() for name in ROOT_NAMES),
        profile=profile,
        seed=seed,
        files=tuple(all_files),
        owned_files=tuple(owned_files),
        orphan_files=tuple(orphan_files),
        initial_torrents=tuple(fake_torrents),
        client=client,
        manifest_digest="",
        expected_manifest_digest=blueprint.manifest_digest,
        intended_action_digest=blueprint.intended_action_digest,
        reconciliation=blueprint.reconciliation,
    )
    actual_digest = verify_materialized_fixture(provisional)
    return replace(provisional, manifest_digest=actual_digest)
