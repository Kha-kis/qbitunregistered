"""Run the gauntlet with first-party imports bound to one worktree."""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import runpy
import stat
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import CodeType, ModuleType
from types import MappingProxyType
from typing import Never, Protocol

PROTECTED_PACKAGE_NAMES = ("benchmarks", "qbitunregistered")
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})
DEPENDENCY_DIGEST_ARGUMENT = "--dependency-environment-digest"
EXPECTED_REPOSITORY_COMMIT_ARGUMENT = "--expected-repository-commit"
IMMUTABLE_TQDM_MANIFEST_ARGUMENT = "--immutable-tqdm-manifest"
PROTECTED_IMPORT_ERROR = "gauntlet protected imports could not be verified"
DEPENDENCY_ISOLATION_ERROR = "gauntlet dependency imports could not be isolated"
COORDINATOR_BOOTSTRAP_MODULE = "_qbitunregistered_gauntlet_coordinator_bootstrap"
_DIGEST_CHUNK_BYTES = 1024 * 1024
_GIT_TIMEOUT_SECONDS = 10
_REGULAR_BLOB_MODES = frozenset({b"100644", b"100755"})
_IMMUTABLE_TQDM_MANIFEST_SCHEMA_VERSION = 1
_MAX_IMMUTABLE_TQDM_MANIFEST_BYTES = 256 * 1024
_MAX_IMMUTABLE_TQDM_SOURCES = 256
_MAX_IMMUTABLE_TQDM_SOURCE_BYTES = 1024 * 1024
_MAX_IMMUTABLE_TQDM_TOTAL_BYTES = 8 * 1024 * 1024
_BYTECODE_SUFFIXES = (".pyc", ".pyo")
_NATIVE_EXTENSION_SUFFIXES = tuple(
    sorted({suffix.casefold() for suffix in importlib.machinery.EXTENSION_SUFFIXES} | {".dll", ".dylib", ".pyd", ".so"})
)
_IMMUTABLE_TQDM_ORIGIN = "<qbitunregistered-gauntlet-immutable-tqdm>"
_QBITTORRENTAPI_ROOT_NAME = "qbittorrentapi"
_QBITTORRENTAPI_EXCEPTIONS_NAME = "qbittorrentapi.exceptions"
_QBITTORRENTAPI_SHIM_ORIGIN = "<qbitunregistered-gauntlet-qbittorrentapi-shim>"


class DependencyEnvironmentError(RuntimeError):
    """Raised when installed dependency contents cannot be bound safely."""


class ProtectedPackageTreeError(RuntimeError):
    """Raised when protected source packages cannot be imported safely."""


class _QbittorrentApiShimLoader(importlib.abc.Loader):
    """Identify evaluator-owned modules that must never execute loader code."""

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> Never:
        del spec
        raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

    def exec_module(self, module: ModuleType) -> Never:
        del module
        raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)


class _QbittorrentApiShimState:
    """Retain and validate the exact fail-closed client import surface."""

    __slots__ = (
        "_api_connection_error",
        "_client",
        "_client_constructions",
        "_exception_instances",
        "_exceptions_loader",
        "_exceptions_module",
        "_exceptions_spec",
        "_installed",
        "_root_loader",
        "_root_module",
        "_root_spec",
    )

    def __init__(self) -> None:
        self._client_constructions = 0
        self._exception_instances = 0
        self._installed = False
        state = self

        class Client:
            __module__ = _QBITTORRENTAPI_ROOT_NAME

            def __new__(cls, *args: object, **kwargs: object) -> Never:
                del cls, args, kwargs
                state._client_constructions += 1
                raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

            def __init_subclass__(cls, **kwargs: object) -> Never:
                del cls, kwargs
                raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

        class APIConnectionError(Exception):
            __module__ = _QBITTORRENTAPI_EXCEPTIONS_NAME

            def __new__(cls, *args: object, **kwargs: object) -> Never:
                del cls, args, kwargs
                state._exception_instances += 1
                raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

            def __init_subclass__(cls, **kwargs: object) -> Never:
                del cls, kwargs
                raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

        Client.__qualname__ = "Client"
        APIConnectionError.__qualname__ = "APIConnectionError"
        self._client = Client
        self._api_connection_error = APIConnectionError
        self._root_loader = _QbittorrentApiShimLoader()
        self._exceptions_loader = _QbittorrentApiShimLoader()
        self._root_spec = importlib.machinery.ModuleSpec(
            _QBITTORRENTAPI_ROOT_NAME,
            self._root_loader,
            origin=_QBITTORRENTAPI_SHIM_ORIGIN,
            is_package=True,
        )
        self._exceptions_spec = importlib.machinery.ModuleSpec(
            _QBITTORRENTAPI_EXCEPTIONS_NAME,
            self._exceptions_loader,
            origin=_QBITTORRENTAPI_SHIM_ORIGIN,
            is_package=False,
        )
        self._root_module = ModuleType(_QBITTORRENTAPI_ROOT_NAME, "Protected evaluator qBittorrent API shim.")
        self._exceptions_module = ModuleType(
            _QBITTORRENTAPI_EXCEPTIONS_NAME,
            "Protected evaluator qBittorrent API exception shim.",
        )
        self._root_module.__package__ = _QBITTORRENTAPI_ROOT_NAME
        self._root_module.__loader__ = self._root_loader
        self._root_module.__spec__ = self._root_spec
        self._root_module.__dict__["__path__"] = self._root_spec.submodule_search_locations
        self._root_module.__dict__["Client"] = self._client
        self._root_module.__dict__["exceptions"] = self._exceptions_module
        self._exceptions_module.__package__ = _QBITTORRENTAPI_ROOT_NAME
        self._exceptions_module.__loader__ = self._exceptions_loader
        self._exceptions_module.__spec__ = self._exceptions_spec
        self._exceptions_module.__dict__["APIConnectionError"] = self._api_connection_error

    @property
    def client_constructions(self) -> int:
        """Return attempted constructions of the forbidden real-client boundary."""
        return self._client_constructions

    @property
    def exception_instances(self) -> int:
        """Return attempted instantiations of the sentinel connection error."""
        return self._exception_instances

    def install(self) -> None:
        """Install both protected modules without replacing any existing import."""
        if self._installed or any(
            name == _QBITTORRENTAPI_ROOT_NAME or name.startswith(f"{_QBITTORRENTAPI_ROOT_NAME}.") for name in sys.modules
        ):
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
        sys.modules[_QBITTORRENTAPI_ROOT_NAME] = self._root_module
        sys.modules[_QBITTORRENTAPI_EXCEPTIONS_NAME] = self._exceptions_module
        self._installed = True
        self.validate()

    def _spec_is_valid(
        self,
        module: ModuleType,
        spec: importlib.machinery.ModuleSpec,
        loader: _QbittorrentApiShimLoader,
        *,
        name: str,
        is_package: bool,
    ) -> bool:
        search_locations = spec.submodule_search_locations
        return (
            module.__name__ == name
            and module.__package__ == _QBITTORRENTAPI_ROOT_NAME
            and module.__loader__ is loader
            and module.__spec__ is spec
            and spec.name == name
            and spec.loader is loader
            and spec.origin == _QBITTORRENTAPI_SHIM_ORIGIN
            and spec.has_location is False
            and spec.cached is None
            and spec.loader_state is None
            and (search_locations == [] if is_package else search_locations is None)
            and (not is_package or module.__path__ is search_locations)
        )

    def validate(self) -> None:
        """Fail closed if the protected graph drifted or either sentinel was used."""
        protected_module_names = {
            name
            for name in sys.modules
            if name == _QBITTORRENTAPI_ROOT_NAME or name.startswith(f"{_QBITTORRENTAPI_ROOT_NAME}.")
        }
        if (
            not self._installed
            or protected_module_names != {_QBITTORRENTAPI_ROOT_NAME, _QBITTORRENTAPI_EXCEPTIONS_NAME}
            or sys.modules.get(_QBITTORRENTAPI_ROOT_NAME) is not self._root_module
            or sys.modules.get(_QBITTORRENTAPI_EXCEPTIONS_NAME) is not self._exceptions_module
            or set(vars(self._root_module))
            != {
                "__name__",
                "__doc__",
                "__package__",
                "__loader__",
                "__spec__",
                "__path__",
                "Client",
                "exceptions",
            }
            or set(vars(self._exceptions_module))
            != {
                "__name__",
                "__doc__",
                "__package__",
                "__loader__",
                "__spec__",
                "APIConnectionError",
            }
            or self._root_module.__doc__ != "Protected evaluator qBittorrent API shim."
            or self._exceptions_module.__doc__ != "Protected evaluator qBittorrent API exception shim."
            or self._root_module.Client is not self._client
            or self._root_module.exceptions is not self._exceptions_module
            or self._exceptions_module.APIConnectionError is not self._api_connection_error
            or type(self._client) is not type
            or self._client.__module__ != _QBITTORRENTAPI_ROOT_NAME
            or self._client.__qualname__ != "Client"
            or self._client.__bases__ != (object,)
            or type(self._api_connection_error) is not type
            or self._api_connection_error.__module__ != _QBITTORRENTAPI_EXCEPTIONS_NAME
            or self._api_connection_error.__qualname__ != "APIConnectionError"
            or self._api_connection_error.__bases__ != (Exception,)
            or not self._spec_is_valid(
                self._root_module,
                self._root_spec,
                self._root_loader,
                name=_QBITTORRENTAPI_ROOT_NAME,
                is_package=True,
            )
            or not self._spec_is_valid(
                self._exceptions_module,
                self._exceptions_spec,
                self._exceptions_loader,
                name=_QBITTORRENTAPI_EXCEPTIONS_NAME,
                is_package=False,
            )
            or self._client_constructions != 0
            or self._exception_instances != 0
        ):
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

    def _matches_unapproved_use(self, error: AttributeError | ImportError) -> bool:
        """Identify only missing names owned by the protected shim."""
        if isinstance(error, ImportError):
            missing_name = error.name
            return isinstance(missing_name, str) and (
                missing_name == _QBITTORRENTAPI_ROOT_NAME or missing_name.startswith(f"{_QBITTORRENTAPI_ROOT_NAME}.")
            )
        owner = getattr(error, "obj", None)
        return owner is self._root_module or owner is self._exceptions_module


@dataclass(frozen=True, slots=True)
class _ProtectedSource:
    """One Git-tracked source bound to its canonical import name."""

    fullname: str
    path: Path
    is_package: bool
    mode: str
    oid: str
    source_bytes: bytes


@dataclass(frozen=True, slots=True)
class _ImmutableDependencySource:
    """One bounded installed source record without a retained root path."""

    fullname: str
    root_index: int
    relative_path: PurePosixPath
    is_package: bool
    size: int
    sha256: str
    source_bytes: bytes = b""


@dataclass(frozen=True, slots=True)
class _ImmutableDependencySourceCandidate:
    """Transient filesystem identity used while capturing one source."""

    source: _ImmutableDependencySource
    path: Path
    expected_stat: os.stat_result


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object: ...


def _entry_is_redirecting(file_stat: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(reparse_point and file_attributes & reparse_point)


def _entry_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        getattr(file_stat, "st_file_attributes", 0),
    )


def _stable_entry_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        *_entry_identity(file_stat),
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _path_descriptor_entry_identity(file_stat: os.stat_result) -> tuple[int, ...]:
    """Return metadata comparable across path and descriptor stat APIs."""
    stable_identity = _stable_entry_identity(file_stat)
    if os.name == "nt":
        # Windows path stat preserves creation time in deprecated st_ctime,
        # while descriptor stat can expose metadata-change time instead.
        return stable_identity[:-1]
    return stable_identity


def _open_stable_regular_file(path: Path, expected_stat: os.stat_result) -> tuple[int, os.stat_result]:
    """Open one expected regular file without following its final component."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DependencyEnvironmentError("could not open an installed dependency safely") from error
    try:
        opened_stat = os.fstat(descriptor)
        if (
            _entry_is_redirecting(opened_stat)
            or not stat.S_ISREG(opened_stat.st_mode)
            or _entry_identity(opened_stat) != _entry_identity(expected_stat)
        ):
            raise DependencyEnvironmentError("installed dependency entry changed during validation")
        if not getattr(os, "O_NOFOLLOW", 0):
            path_stat = os.lstat(path)
            if _entry_is_redirecting(path_stat) or _entry_identity(path_stat) != _entry_identity(opened_stat):
                raise DependencyEnvironmentError("installed dependency entry changed during validation")
    except DependencyEnvironmentError:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    except OSError as error:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise DependencyEnvironmentError("could not inspect an installed dependency safely") from error
    return descriptor, opened_stat


def _update_regular_file_digest(
    path: Path,
    expected_stat: os.stat_result,
    digest: _Digest,
) -> None:
    descriptor, before = _open_stable_regular_file(path, expected_stat)
    try:
        digest.update(str(stat.S_IMODE(before.st_mode)).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(before.st_size).encode("ascii"))
        digest.update(b"\0")
        while chunk := os.read(descriptor, _DIGEST_CHUNK_BYTES):
            digest.update(chunk)
        after = os.fstat(descriptor)
    except OSError as error:
        raise DependencyEnvironmentError("could not read an installed dependency safely") from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise DependencyEnvironmentError("could not close an installed dependency safely") from error
    if _stable_entry_identity(before) != _stable_entry_identity(after):
        raise DependencyEnvironmentError("installed dependency entry changed during validation")


def _read_bounded_regular_file(
    path: Path,
    expected_stat: os.stat_result,
    *,
    maximum_bytes: int,
) -> bytes:
    """Read exactly one stable regular file within an explicit byte limit."""
    descriptor, before = _open_stable_regular_file(path, expected_stat)
    try:
        if before.st_size > maximum_bytes:
            raise DependencyEnvironmentError("installed dependency source exceeds its byte limit")
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(remaining, _DIGEST_CHUNK_BYTES))
            if not chunk:
                raise DependencyEnvironmentError("installed dependency source changed during capture")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise DependencyEnvironmentError("installed dependency source changed during capture")
        after = os.fstat(descriptor)
    except OSError as error:
        raise DependencyEnvironmentError("could not read an installed dependency safely") from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise DependencyEnvironmentError("could not close an installed dependency safely") from error
    if _stable_entry_identity(before) != _stable_entry_identity(after):
        raise DependencyEnvironmentError("installed dependency entry changed during validation")
    if not getattr(os, "O_NOFOLLOW", 0):
        try:
            path_stat = os.lstat(path)
        except OSError as error:
            raise DependencyEnvironmentError("could not revalidate an installed dependency safely") from error
        if _entry_is_redirecting(path_stat) or _path_descriptor_entry_identity(path_stat) != _path_descriptor_entry_identity(
            after
        ):
            raise DependencyEnvironmentError("installed dependency entry changed during validation")
    return b"".join(chunks)


def _tqdm_module_identity(relative_path: PurePosixPath) -> tuple[str, bool]:
    relative_value = relative_path.as_posix()
    if (
        relative_path.is_absolute()
        or "\\" in relative_value
        or len(relative_path.parts) < 2
        or relative_path.parts[0] != "tqdm"
        or any(part in {"", ".", ".."} for part in relative_path.parts)
        or relative_path.suffix != ".py"
    ):
        raise DependencyEnvironmentError("installed tqdm source path is unsafe")
    module_parts = list(relative_path.with_suffix("").parts)
    is_package = module_parts[-1] == "__init__"
    if is_package:
        module_parts.pop()
    if not module_parts or any(not part.isidentifier() for part in module_parts):
        raise DependencyEnvironmentError("installed tqdm source path is unsafe")
    return ".".join(module_parts), is_package


def _is_top_level_tqdm_import_artifact(name: str) -> bool:
    folded_name = name.casefold()
    if folded_name == "tqdm.py" or folded_name in {f"tqdm{suffix}" for suffix in _BYTECODE_SUFFIXES}:
        return True
    return folded_name.startswith("tqdm.") and folded_name.endswith(_NATIVE_EXTENSION_SUFFIXES)


def _immutable_tqdm_package_root(dependency_paths: Sequence[str]) -> tuple[int, Path]:
    if not dependency_paths:
        raise DependencyEnvironmentError("installed dependency environment is empty")
    candidates: list[tuple[int, Path]] = []
    for root_index, value in enumerate(dependency_paths):
        dependency_root = Path(value)
        try:
            resolved_root = dependency_root.resolve(strict=True)
            root_stat = os.lstat(dependency_root)
        except (OSError, RuntimeError, ValueError) as error:
            raise DependencyEnvironmentError("could not inspect the installed dependency environment") from error
        if (
            not dependency_root.is_absolute()
            or dependency_root != resolved_root
            or _entry_is_redirecting(root_stat)
            or not stat.S_ISDIR(root_stat.st_mode)
        ):
            raise DependencyEnvironmentError("installed dependency path is unsafe")
        try:
            with os.scandir(dependency_root) as iterator:
                entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        except OSError as error:
            raise DependencyEnvironmentError("could not inspect the installed tqdm package") from error
        package_root: Path | None = None
        for entry in entries:
            if _is_top_level_tqdm_import_artifact(entry.name):
                raise DependencyEnvironmentError("installed tqdm package root is ambiguous")
            if entry.name.casefold() != "tqdm":
                continue
            if entry.name != "tqdm" or package_root is not None:
                raise DependencyEnvironmentError("installed tqdm package root is ambiguous")
            entry_path = dependency_root / entry.name
            try:
                package_stat = os.lstat(entry_path)
            except OSError as error:
                raise DependencyEnvironmentError("could not inspect the installed tqdm package") from error
            if _entry_is_redirecting(package_stat) or not stat.S_ISDIR(package_stat.st_mode):
                raise DependencyEnvironmentError("installed tqdm package is redirecting")
            package_root = entry_path
        try:
            current_root_stat = os.lstat(dependency_root)
        except OSError as error:
            raise DependencyEnvironmentError("could not inspect the installed tqdm package") from error
        if _entry_is_redirecting(current_root_stat) or _stable_entry_identity(root_stat) != _stable_entry_identity(
            current_root_stat
        ):
            raise DependencyEnvironmentError("installed dependency environment changed during validation")
        if package_root is not None:
            candidates.append((root_index, package_root))
    if len(candidates) != 1:
        raise DependencyEnvironmentError("installed dependency environment must contain exactly one tqdm package")
    return candidates[0]


def _discover_immutable_tqdm_candidates(
    dependency_paths: Sequence[str],
) -> tuple[_ImmutableDependencySourceCandidate, ...]:
    root_index, package_root = _immutable_tqdm_package_root(dependency_paths)
    candidates: list[_ImmutableDependencySourceCandidate] = []
    bytecode_paths: list[PurePosixPath] = []
    total_size = 0

    def visit(directory: Path, relative_directory: PurePosixPath) -> None:
        nonlocal total_size
        try:
            before = os.lstat(directory)
        except OSError as error:
            raise DependencyEnvironmentError("could not inspect the installed tqdm package") from error
        if _entry_is_redirecting(before) or not stat.S_ISDIR(before.st_mode):
            raise DependencyEnvironmentError("installed tqdm package contains a redirecting directory")
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
        except OSError as error:
            raise DependencyEnvironmentError("could not inspect the installed tqdm package") from error
        for entry in entries:
            entry_path = directory / entry.name
            relative_path = relative_directory / entry.name
            try:
                entry_stat = os.lstat(entry_path)
            except OSError as error:
                raise DependencyEnvironmentError("could not inspect an installed tqdm entry") from error
            if _entry_is_redirecting(entry_stat):
                raise DependencyEnvironmentError("installed tqdm package contains a redirecting entry")
            if stat.S_ISDIR(entry_stat.st_mode):
                visit(entry_path, relative_path)
                continue
            if not stat.S_ISREG(entry_stat.st_mode):
                raise DependencyEnvironmentError("installed tqdm package contains a special entry")
            folded_name = entry.name.casefold()
            if folded_name.endswith(_BYTECODE_SUFFIXES):
                bytecode_paths.append(relative_path)
                continue
            if folded_name.endswith(_NATIVE_EXTENSION_SUFFIXES):
                raise DependencyEnvironmentError("installed tqdm package contains an unsupported import artifact")
            if relative_path.suffix != ".py":
                continue
            fullname, is_package = _tqdm_module_identity(relative_path)
            if entry_stat.st_size > _MAX_IMMUTABLE_TQDM_SOURCE_BYTES:
                raise DependencyEnvironmentError("installed tqdm source exceeds its byte limit")
            total_size += entry_stat.st_size
            if total_size > _MAX_IMMUTABLE_TQDM_TOTAL_BYTES:
                raise DependencyEnvironmentError("installed tqdm sources exceed their total byte limit")
            if len(candidates) >= _MAX_IMMUTABLE_TQDM_SOURCES:
                raise DependencyEnvironmentError("installed tqdm package contains too many sources")
            candidates.append(
                _ImmutableDependencySourceCandidate(
                    source=_ImmutableDependencySource(
                        fullname=fullname,
                        root_index=root_index,
                        relative_path=relative_path,
                        is_package=is_package,
                        size=entry_stat.st_size,
                        sha256="",
                    ),
                    path=entry_path,
                    expected_stat=entry_stat,
                )
            )
        try:
            after = os.lstat(directory)
        except OSError as error:
            raise DependencyEnvironmentError("could not revalidate the installed tqdm package") from error
        if _entry_is_redirecting(after) or _stable_entry_identity(before) != _stable_entry_identity(after):
            raise DependencyEnvironmentError("installed tqdm package changed during validation")

    visit(package_root, PurePosixPath("tqdm"))
    source_paths = {candidate.source.relative_path for candidate in candidates}
    for bytecode_path in bytecode_paths:
        if bytecode_path.parent.name == "__pycache__":
            source_stem = bytecode_path.stem.partition(".")[0]
            if not source_stem:
                raise DependencyEnvironmentError("installed tqdm package contains bytecode without source")
            source_path = bytecode_path.parent.parent / f"{source_stem}.py"
        else:
            source_path = bytecode_path.with_suffix(".py")
        if source_path not in source_paths:
            raise DependencyEnvironmentError("installed tqdm package contains bytecode without source")
    candidates.sort(key=lambda candidate: candidate.source.fullname)
    sources_by_name: dict[str, _ImmutableDependencySource] = {}
    casefold_names: set[str] = set()
    for candidate in candidates:
        source = candidate.source
        folded_name = source.fullname.casefold()
        if source.fullname in sources_by_name or folded_name in casefold_names:
            raise DependencyEnvironmentError("installed tqdm package contains a module-name collision")
        sources_by_name[source.fullname] = source
        casefold_names.add(folded_name)
    root_source = sources_by_name.get("tqdm")
    if root_source is None or not root_source.is_package:
        raise DependencyEnvironmentError("installed tqdm package has no canonical package source")
    for source in sources_by_name.values():
        parent_name = source.fullname.rpartition(".")[0]
        while parent_name:
            parent = sources_by_name.get(parent_name)
            if parent is None or not parent.is_package:
                raise DependencyEnvironmentError("installed tqdm source has no canonical parent package")
            parent_name = parent_name.rpartition(".")[0]
    return tuple(candidates)


def _candidate_identities(
    candidates: Sequence[_ImmutableDependencySourceCandidate],
) -> tuple[tuple[_ImmutableDependencySource, Path, tuple[int, int, int, int, int, int, int]], ...]:
    return tuple(
        (candidate.source, candidate.path, _stable_entry_identity(candidate.expected_stat)) for candidate in candidates
    )


def _immutable_tqdm_sources(
    dependency_paths: Sequence[str],
    *,
    capture_source_bytes: bool,
) -> tuple[_ImmutableDependencySource, ...]:
    candidates = _discover_immutable_tqdm_candidates(dependency_paths)
    captured: list[_ImmutableDependencySource] = []
    for candidate in candidates:
        source_bytes = _read_bounded_regular_file(
            candidate.path,
            candidate.expected_stat,
            maximum_bytes=_MAX_IMMUTABLE_TQDM_SOURCE_BYTES,
        )
        captured.append(
            replace(
                candidate.source,
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                source_bytes=source_bytes if capture_source_bytes else b"",
            )
        )
    current_candidates = _discover_immutable_tqdm_candidates(dependency_paths)
    if _candidate_identities(current_candidates) != _candidate_identities(candidates):
        raise DependencyEnvironmentError("installed tqdm package changed during capture")
    return tuple(captured)


def _immutable_tqdm_manifest_payload(
    sources: Sequence[_ImmutableDependencySource],
) -> dict[str, object]:
    return {
        "schema_version": _IMMUTABLE_TQDM_MANIFEST_SCHEMA_VERSION,
        "namespace": "tqdm",
        "sources": [
            {
                "fullname": source.fullname,
                "root_index": source.root_index,
                "relative_path": source.relative_path.as_posix(),
                "is_package": source.is_package,
                "size": source.size,
                "sha256": source.sha256,
            }
            for source in sources
        ],
    }


def _canonical_manifest_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def immutable_tqdm_manifest(dependency_paths: Sequence[str]) -> str:
    """Return canonical hash-only metadata for one installed tqdm source tree."""
    sources = _immutable_tqdm_sources(dependency_paths, capture_source_bytes=False)
    manifest = _canonical_manifest_json(_immutable_tqdm_manifest_payload(sources))
    if len(manifest.encode("utf-8")) > _MAX_IMMUTABLE_TQDM_MANIFEST_BYTES:
        raise DependencyEnvironmentError("installed tqdm source manifest exceeds its byte limit")
    return manifest


def _parsed_immutable_tqdm_manifest(
    raw_manifest: str,
    *,
    dependency_root_count: int,
) -> tuple[_ImmutableDependencySource, ...]:
    if type(raw_manifest) is not str:
        raise DependencyEnvironmentError("immutable tqdm source manifest is malformed")
    if len(raw_manifest) > _MAX_IMMUTABLE_TQDM_MANIFEST_BYTES:
        raise DependencyEnvironmentError("immutable tqdm source manifest exceeds its byte limit")
    try:
        encoded_manifest = raw_manifest.encode("utf-8")
    except UnicodeError as error:
        raise DependencyEnvironmentError("immutable tqdm source manifest is malformed") from error
    if len(encoded_manifest) > _MAX_IMMUTABLE_TQDM_MANIFEST_BYTES:
        raise DependencyEnvironmentError("immutable tqdm source manifest exceeds its byte limit")
    try:
        payload = json.loads(raw_manifest)
    except (ValueError, RecursionError, TypeError) as error:
        raise DependencyEnvironmentError("immutable tqdm source manifest is malformed") from error
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "namespace", "sources"}:
        raise DependencyEnvironmentError("immutable tqdm source manifest is malformed")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != _IMMUTABLE_TQDM_MANIFEST_SCHEMA_VERSION:
        raise DependencyEnvironmentError("immutable tqdm source manifest has an unsupported schema")
    if type(payload["namespace"]) is not str or payload["namespace"] != "tqdm":
        raise DependencyEnvironmentError("immutable tqdm source manifest has an unsupported namespace")
    source_values = payload["sources"]
    if type(source_values) is not list or not source_values or len(source_values) > _MAX_IMMUTABLE_TQDM_SOURCES:
        raise DependencyEnvironmentError("immutable tqdm source manifest is malformed")

    parsed_sources: list[_ImmutableDependencySource] = []
    source_names: set[str] = set()
    casefold_names: set[str] = set()
    total_size = 0
    expected_source_keys = {"fullname", "root_index", "relative_path", "is_package", "size", "sha256"}
    for source_value in source_values:
        if type(source_value) is not dict or set(source_value) != expected_source_keys:
            raise DependencyEnvironmentError("immutable tqdm source manifest is malformed")
        fullname = source_value["fullname"]
        root_index = source_value["root_index"]
        relative_value = source_value["relative_path"]
        is_package = source_value["is_package"]
        size = source_value["size"]
        sha256 = source_value["sha256"]
        if (
            type(fullname) is not str
            or type(root_index) is not int
            or type(relative_value) is not str
            or type(is_package) is not bool
            or type(size) is not int
            or type(sha256) is not str
        ):
            raise DependencyEnvironmentError("immutable tqdm source manifest is malformed")
        relative_path = PurePosixPath(relative_value)
        if relative_path.as_posix() != relative_value:
            raise DependencyEnvironmentError("immutable tqdm source manifest contains an unsafe path")
        expected_fullname, expected_is_package = _tqdm_module_identity(relative_path)
        if fullname != expected_fullname or is_package is not expected_is_package:
            raise DependencyEnvironmentError("immutable tqdm source manifest contains inconsistent metadata")
        folded_name = fullname.casefold()
        if fullname in source_names or folded_name in casefold_names:
            raise DependencyEnvironmentError("immutable tqdm source manifest contains duplicate records")
        if root_index < 0 or root_index >= dependency_root_count or size < 0 or size > _MAX_IMMUTABLE_TQDM_SOURCE_BYTES:
            raise DependencyEnvironmentError("immutable tqdm source manifest contains invalid bounds")
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise DependencyEnvironmentError("immutable tqdm source manifest contains an invalid hash")
        total_size += size
        if total_size > _MAX_IMMUTABLE_TQDM_TOTAL_BYTES:
            raise DependencyEnvironmentError("immutable tqdm source manifest exceeds its total byte limit")
        parsed_sources.append(
            _ImmutableDependencySource(
                fullname=fullname,
                root_index=root_index,
                relative_path=relative_path,
                is_package=is_package,
                size=size,
                sha256=sha256,
            )
        )
        source_names.add(fullname)
        casefold_names.add(folded_name)
    if tuple(source.fullname for source in parsed_sources) != tuple(sorted(source.fullname for source in parsed_sources)):
        raise DependencyEnvironmentError("immutable tqdm source manifest is not canonically ordered")
    if _canonical_manifest_json(payload) != raw_manifest:
        raise DependencyEnvironmentError("immutable tqdm source manifest is not canonical JSON")
    return tuple(parsed_sources)


def _capture_immutable_tqdm_sources(
    dependency_paths: Sequence[str],
    raw_manifest: str,
) -> tuple[_ImmutableDependencySource, ...]:
    """Capture installed tqdm bytes only when they exactly match a manifest."""
    manifest_sources = _parsed_immutable_tqdm_manifest(
        raw_manifest,
        dependency_root_count=len(dependency_paths),
    )
    candidates = _discover_immutable_tqdm_candidates(dependency_paths)
    manifest_metadata = tuple(replace(source, sha256="") for source in manifest_sources)
    if tuple(candidate.source for candidate in candidates) != manifest_metadata:
        raise DependencyEnvironmentError("installed tqdm sources do not match the immutable manifest")
    captured_sources: list[_ImmutableDependencySource] = []
    for candidate, manifest_source in zip(candidates, manifest_sources, strict=True):
        source_bytes = _read_bounded_regular_file(
            candidate.path,
            candidate.expected_stat,
            maximum_bytes=_MAX_IMMUTABLE_TQDM_SOURCE_BYTES,
        )
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        if source_hash != manifest_source.sha256:
            raise DependencyEnvironmentError("installed tqdm source hash does not match the immutable manifest")
        captured_sources.append(
            replace(
                candidate.source,
                sha256=source_hash,
                source_bytes=source_bytes,
            )
        )
    current_candidates = _discover_immutable_tqdm_candidates(dependency_paths)
    if _candidate_identities(current_candidates) != _candidate_identities(candidates):
        raise DependencyEnvironmentError("installed tqdm package changed during capture")
    return tuple(captured_sources)


def _immutable_source_identity(source: _ImmutableDependencySource) -> tuple[object, ...]:
    """Return every retained field that must stay immutable during evaluation."""
    return (
        source.fullname,
        source.root_index,
        source.relative_path,
        source.is_package,
        source.size,
        source.sha256,
        source.source_bytes,
    )


class _ImmutableDependencySourceLoader(importlib.abc.SourceLoader):
    """Compile one installed dependency module only from captured source bytes."""

    def __init__(self, source: _ImmutableDependencySource) -> None:
        self._source = source
        self._expected_identity = _immutable_source_identity(source)

    def _validate_source(self) -> None:
        if _immutable_source_identity(self._source) != self._expected_identity:
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)

    def get_filename(self, fullname: str) -> str:
        if fullname != self._source.fullname:
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
        self._validate_source()
        return _IMMUTABLE_TQDM_ORIGIN

    def get_data(self, path: str) -> bytes:
        del path
        raise OSError(DEPENDENCY_ISOLATION_ERROR)

    def get_resource_reader(self, fullname: str) -> None:
        self.get_filename(fullname)
        return None

    def get_code(self, fullname: str) -> CodeType:
        filename = self.get_filename(fullname)
        return self.source_to_code(self._source.source_bytes, filename)

    def is_package(self, fullname: str) -> bool:
        self.get_filename(fullname)
        return self._source.is_package


class _ImmutableDependencyFinder(importlib.abc.MetaPathFinder):
    """Resolve only exact captured tqdm modules without installed import roots."""

    def __init__(self, sources: Sequence[_ImmutableDependencySource]) -> None:
        source_map = {source.fullname: source for source in sources}
        if len(source_map) != len(sources) or "tqdm" not in source_map:
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
        self._sources = MappingProxyType(source_map)
        self._expected_source_identity = tuple(
            (fullname, _immutable_source_identity(source)) for fullname, source in self._sources.items()
        )
        self._loaders = {fullname: _ImmutableDependencySourceLoader(source) for fullname, source in self._sources.items()}
        self._specs = {
            fullname: importlib.machinery.ModuleSpec(
                fullname,
                self._loaders[fullname],
                origin=_IMMUTABLE_TQDM_ORIGIN,
                is_package=source.is_package,
            )
            for fullname, source in self._sources.items()
        }
        self._expected_loaders = tuple(self._loaders.items())
        self._expected_specs = tuple(self._specs.items())

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path
        del target
        if fullname != "tqdm" and not fullname.startswith("tqdm."):
            return None
        self.validate_sources()
        spec = self._specs.get(fullname)
        if spec is None:
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
        return spec

    def validate_sources(self) -> None:
        """Require retained sources and every loaded tqdm module to stay bound."""
        current_identity = tuple((fullname, _immutable_source_identity(source)) for fullname, source in self._sources.items())
        if current_identity != self._expected_source_identity:
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
        if (
            tuple(self._loaders) != tuple(name for name, _loader in self._expected_loaders)
            or tuple(self._specs) != tuple(name for name, _spec in self._expected_specs)
            or any(self._loaders[name] is not loader for name, loader in self._expected_loaders)
            or any(self._specs[name] is not spec for name, spec in self._expected_specs)
        ):
            raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
        for expected_loader in self._loaders.values():
            expected_loader._validate_source()
        for fullname, module in tuple(sys.modules.items()):
            if fullname != "tqdm" and not fullname.startswith("tqdm."):
                continue
            source = self._sources.get(fullname)
            module_loader = self._loaders.get(fullname)
            spec = self._specs.get(fullname)
            if source is None or module_loader is None or spec is None or not isinstance(module, ModuleType):
                raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
            module_spec = getattr(module, "__spec__", None)
            loaded_by = getattr(module, "__loader__", None)
            if (
                not isinstance(module_spec, importlib.machinery.ModuleSpec)
                or loaded_by is not module_loader
                or module_spec is not spec
                or module_spec.loader is not module_loader
                or module_spec.origin != _IMMUTABLE_TQDM_ORIGIN
                or (module_spec.submodule_search_locations is not None) is not source.is_package
            ):
                raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)


def _update_dependency_tree_digest(
    directory: Path,
    relative_directory: str,
    digest: _Digest,
) -> None:
    try:
        before = os.lstat(directory)
    except OSError as error:
        raise DependencyEnvironmentError("could not inspect the installed dependency environment") from error
    if _entry_is_redirecting(before) or not stat.S_ISDIR(before.st_mode):
        raise DependencyEnvironmentError("installed dependency environment contains a redirecting entry")
    relative_value = relative_directory or "."
    digest.update(b"D\0")
    digest.update(os.fsencode(relative_value))
    digest.update(b"\0")
    digest.update(str(stat.S_IMODE(before.st_mode)).encode("ascii"))
    digest.update(b"\n")
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: os.fsencode(entry.name))
    except OSError as error:
        raise DependencyEnvironmentError("could not inspect the installed dependency environment") from error
    for entry in entries:
        relative_path = f"{relative_directory}/{entry.name}" if relative_directory else entry.name
        entry_path = directory / entry.name
        try:
            entry_stat = os.lstat(entry_path)
        except OSError as error:
            raise DependencyEnvironmentError("could not inspect an installed dependency entry") from error
        if _entry_is_redirecting(entry_stat):
            raise DependencyEnvironmentError("installed dependency environment contains a redirecting entry")
        if stat.S_ISDIR(entry_stat.st_mode):
            _update_dependency_tree_digest(entry_path, relative_path, digest)
        elif stat.S_ISREG(entry_stat.st_mode):
            digest.update(b"F\0")
            digest.update(os.fsencode(relative_path))
            digest.update(b"\0")
            _update_regular_file_digest(entry_path, entry_stat, digest)
            digest.update(b"\n")
        else:
            raise DependencyEnvironmentError("installed dependency environment contains a special file")
    try:
        after = os.lstat(directory)
    except OSError as error:
        raise DependencyEnvironmentError("could not revalidate the installed dependency environment") from error
    if _entry_is_redirecting(after) or _stable_entry_identity(before) != _stable_entry_identity(after):
        raise DependencyEnvironmentError("installed dependency environment changed during validation")


def dependency_environment_digest(dependency_paths: Sequence[str]) -> str:
    """Hash dependency paths and contents without following redirecting entries."""
    if not dependency_paths:
        raise DependencyEnvironmentError("installed dependency environment is empty")
    digest = hashlib.sha256()
    for index, value in enumerate(dependency_paths):
        path = Path(value)
        if not path.is_absolute():
            raise DependencyEnvironmentError("installed dependency path is not absolute")
        digest.update(b"R\0")
        digest.update(str(index).encode("ascii"))
        digest.update(b"\n")
        _update_dependency_tree_digest(path, "", digest)
    return digest.hexdigest()


def _validated_dependency_digest(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SystemExit("gauntlet dependency environment digest is malformed")
    return value


def _current_dependency_digest(dependency_paths: Sequence[str]) -> str:
    try:
        return dependency_environment_digest(dependency_paths)
    except DependencyEnvironmentError as error:
        raise SystemExit("gauntlet dependency environment could not be verified") from error


def _module_import_locations(module: ModuleType) -> list[object]:
    locations: list[object] = [getattr(module, "__file__", None)]
    spec = getattr(module, "__spec__", None)
    if spec is None:
        return locations
    locations.append(spec.origin)
    if spec.submodule_search_locations is not None:
        try:
            locations.extend(spec.submodule_search_locations)
        except TypeError as error:
            raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from error
    return locations


def _resolved_module_location(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    try:
        path = Path(value)
        return path.resolve() if path.is_absolute() else None
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from error


def _reject_preloaded_dependency_modules(dependency_paths: Sequence[str]) -> None:
    """Reject modules already loaded from a dependency directory."""
    dependency_roots = tuple(Path(value) for value in dependency_paths)
    for module in sys.modules.values():
        if not isinstance(module, ModuleType):
            continue
        for value in _module_import_locations(module):
            resolved_path = _resolved_module_location(value)
            if resolved_path is not None and any(
                resolved_path == root or resolved_path.is_relative_to(root) for root in dependency_roots
            ):
                raise SystemExit(DEPENDENCY_ISOLATION_ERROR)


def _validate_protected_package_trees(repository_root: Path) -> None:
    """Reject redirects in protected source packages without following them."""

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for package_name in PROTECTED_PACKAGE_NAMES:
            package_root = repository_root / package_name
            package_stat = os.lstat(package_root)
            if _entry_is_redirecting(package_stat) or not stat.S_ISDIR(package_stat.st_mode):
                raise ProtectedPackageTreeError("gauntlet protected package tree contains a redirecting entry")
            for current_root, directory_names, file_names in os.walk(
                package_root,
                topdown=True,
                onerror=raise_walk_error,
                followlinks=False,
            ):
                for name in (*directory_names, *file_names):
                    if _entry_is_redirecting(os.lstat(Path(current_root) / name)):
                        raise ProtectedPackageTreeError("gauntlet protected package tree contains a redirecting entry")
    except OSError as error:
        raise ProtectedPackageTreeError("gauntlet protected package trees could not be verified") from error


def _require_safe_package_trees(repository_root: Path) -> None:
    try:
        _validate_protected_package_trees(repository_root)
    except ProtectedPackageTreeError as error:
        raise SystemExit(str(error)) from error


def _validate_protected_source(
    repository_root: Path,
    source: _ProtectedSource,
) -> None:
    """Require every source component to remain local and non-redirecting."""
    try:
        relative_source = source.path.relative_to(repository_root)
    except ValueError as error:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
    current = repository_root
    try:
        for component in relative_source.parts[:-1]:
            current /= component
            component_stat = os.lstat(current)
            if _entry_is_redirecting(component_stat) or not stat.S_ISDIR(component_stat.st_mode):
                raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        source_stat = os.lstat(source.path)
    except OSError as error:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
    if _entry_is_redirecting(source_stat) or not stat.S_ISREG(source_stat.st_mode):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)


def _read_git_blob(repository_root: Path, oid: str) -> bytes:
    """Read one immutable Git blob with a timeout and path-free failures."""
    try:
        completed = subprocess.run(
            ["git", "--no-replace-objects", "cat-file", "blob", oid],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
    if completed.returncode != 0:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    return completed.stdout


def _source_index_identity(
    sources: Mapping[str, _ProtectedSource],
) -> dict[str, tuple[Path, bool, str, str]]:
    """Return source metadata that must remain stable across revalidation."""
    return {fullname: (source.path, source.is_package, source.mode, source.oid) for fullname, source in sources.items()}


def _revision_protected_source_identities(
    repository_root: Path,
    revision: str,
) -> dict[str, tuple[str, str]]:
    """Return canonical protected Python blob identities from one revision."""
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "ls-tree",
                "--full-tree",
                "-r",
                "-z",
                revision,
                "--",
                *PROTECTED_PACKAGE_NAMES,
            ],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
    if completed.returncode != 0 or not completed.stdout.endswith(b"\0"):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    encoded_records = completed.stdout[:-1].split(b"\0")
    if not encoded_records or any(not record for record in encoded_records):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)

    identities: dict[str, tuple[str, str]] = {}
    casefold_paths: set[str] = set()
    for encoded_record in encoded_records:
        try:
            encoded_metadata, encoded_path = encoded_record.split(b"\t", 1)
        except ValueError as error:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
        metadata_fields = encoded_metadata.split(b" ")
        if len(metadata_fields) != 3 or any(not field for field in metadata_fields):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        encoded_mode, object_type, encoded_oid = metadata_fields
        relative_value = os.fsdecode(encoded_path)
        relative_path = PurePosixPath(relative_value)
        if (
            relative_path.is_absolute()
            or "\\" in relative_value
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative_path.parts[0] not in PROTECTED_PACKAGE_NAMES
        ):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        if relative_path.suffix != ".py":
            continue
        if (
            encoded_mode not in _REGULAR_BLOB_MODES
            or object_type != b"blob"
            or len(encoded_oid) not in {40, 64}
            or any(character not in b"0123456789abcdef" for character in encoded_oid)
        ):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        folded_path = relative_value.casefold()
        if relative_value in identities or folded_path in casefold_paths:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        try:
            identities[relative_value] = (
                encoded_mode.decode("ascii"),
                encoded_oid.decode("ascii"),
            )
        except UnicodeDecodeError as error:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
        casefold_paths.add(folded_path)
    return identities


def _tracked_protected_sources(
    repository_root: Path,
    *,
    capture_source_bytes: bool = True,
    expected_revision: str = "HEAD",
) -> dict[str, _ProtectedSource]:
    """Build the canonical protected-source map from Git's staged index."""
    try:
        completed = subprocess.run(
            [
                "git",
                "ls-files",
                "--cached",
                "-v",
                "--stage",
                "-z",
                "--",
                *PROTECTED_PACKAGE_NAMES,
            ],
            cwd=repository_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
    if completed.returncode != 0:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)

    if not completed.stdout.endswith(b"\0"):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    encoded_records = completed.stdout[:-1].split(b"\0")
    if not encoded_records or any(not record for record in encoded_records):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)

    sources: dict[str, _ProtectedSource] = {}
    casefold_names: dict[str, str] = {}
    for encoded_record in encoded_records:
        try:
            encoded_metadata, encoded_path = encoded_record.split(b"\t", 1)
        except ValueError as error:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
        metadata_fields = encoded_metadata.split(b" ")
        if len(metadata_fields) != 4 or any(not field for field in metadata_fields):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        index_status, encoded_mode, encoded_oid, encoded_stage = metadata_fields
        relative_value = os.fsdecode(encoded_path)
        relative_path = PurePosixPath(relative_value)
        if (
            relative_path.is_absolute()
            or "\\" in relative_value
            or not relative_path.parts
            or any(part in {"", ".", ".."} for part in relative_path.parts)
            or relative_path.parts[0] not in PROTECTED_PACKAGE_NAMES
        ):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        if relative_path.suffix != ".py":
            continue
        if (
            index_status != b"H"
            or encoded_mode not in _REGULAR_BLOB_MODES
            or encoded_stage != b"0"
            or len(encoded_oid) not in {40, 64}
            or any(character not in b"0123456789abcdefABCDEF" for character in encoded_oid)
        ):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        try:
            mode = encoded_mode.decode("ascii")
            oid = encoded_oid.decode("ascii")
        except UnicodeDecodeError as error:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR) from error
        module_parts = list(relative_path.with_suffix("").parts)
        is_package = module_parts[-1] == "__init__"
        if is_package:
            module_parts.pop()
        if not module_parts or any(not part.isidentifier() for part in module_parts):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        fullname = ".".join(module_parts)
        folded_name = fullname.casefold()
        if fullname in sources or folded_name in casefold_names:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        source = _ProtectedSource(
            fullname=fullname,
            path=repository_root.joinpath(*relative_path.parts),
            is_package=is_package,
            mode=mode,
            oid=oid,
            source_bytes=b"",
        )
        _validate_protected_source(repository_root, source)
        sources[fullname] = source
        casefold_names[folded_name] = fullname

    for package_name in PROTECTED_PACKAGE_NAMES:
        package = sources.get(package_name)
        if package is None or not package.is_package:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    for source in sources.values():
        parent_name = source.fullname.rpartition(".")[0]
        while parent_name:
            parent = sources.get(parent_name)
            if parent is None or not parent.is_package:
                raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
            parent_name = parent_name.rpartition(".")[0]
    index_identities = {
        source.path.relative_to(repository_root).as_posix(): (source.mode, source.oid) for source in sources.values()
    }
    if index_identities != _revision_protected_source_identities(repository_root, expected_revision):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    if capture_source_bytes:
        sources = {
            fullname: replace(
                source,
                source_bytes=_read_git_blob(repository_root, source.oid),
            )
            for fullname, source in sources.items()
        }
    return sources


def _validated_repository_commit(value: str) -> str:
    if len(value) not in {40, 64} or any(character not in "0123456789abcdef" for character in value):
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    return value


def verified_import_bootstrap_source(
    repository_root: Path,
    expected_commit: str,
) -> bytes:
    """Return bootstrap bytes bound to one exact repository commit and index."""
    revision = _validated_repository_commit(expected_commit)
    sources = _tracked_protected_sources(
        repository_root,
        capture_source_bytes=False,
        expected_revision=revision,
    )
    source = sources.get("benchmarks.gauntlet.import_bootstrap")
    expected_path = repository_root / "benchmarks" / "gauntlet" / "import_bootstrap.py"
    if source is None or source.path != expected_path or source.is_package:
        raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
    return _read_git_blob(repository_root, source.oid)


class _ProtectedSourceLoader(importlib.abc.SourceLoader):
    """Compile protected modules only from immutable captured index bytes."""

    def __init__(self, source: _ProtectedSource) -> None:
        self._source = source

    def get_filename(self, fullname: str) -> str:
        if fullname != self._source.fullname:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        return str(self._source.path)

    def get_data(self, path: str) -> bytes:
        if Path(path) != self._source.path:
            raise OSError(PROTECTED_IMPORT_ERROR)
        return self._source.source_bytes

    def get_code(self, fullname: str) -> CodeType:
        filename = self.get_filename(fullname)
        return self.source_to_code(self._source.source_bytes, filename)

    def is_package(self, fullname: str) -> bool:
        self.get_filename(fullname)
        return self._source.is_package


class _WorktreePackageFinder(importlib.abc.MetaPathFinder):
    """Resolve every protected import only from tracked worktree sources."""

    def __init__(
        self,
        repository_root: Path,
        sources: Mapping[str, _ProtectedSource],
        expected_revision: str = "HEAD",
    ) -> None:
        self._repository_root = repository_root
        self._sources = dict(sources)
        self._expected_revision = expected_revision
        self._protected_names = frozenset(name.casefold() for name in PROTECTED_PACKAGE_NAMES)

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del target
        del path
        if fullname.partition(".")[0].casefold() not in self._protected_names:
            return None
        self.validate_sources()
        source = self._sources.get(fullname)
        if source is None:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        _validate_protected_source(self._repository_root, source)
        search_locations = [str(source.path.parent)] if source.is_package else None
        loader = _ProtectedSourceLoader(source)
        spec = importlib.util.spec_from_file_location(
            fullname,
            source.path,
            loader=loader,
            submodule_search_locations=search_locations,
        )
        if spec is None or spec.loader is None:
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)
        return spec

    def validate_sources(self) -> None:
        """Revalidate every tracked protected source after evaluation."""
        current_sources = _tracked_protected_sources(
            self._repository_root,
            capture_source_bytes=False,
            expected_revision=self._expected_revision,
        )
        if _source_index_identity(current_sources) != _source_index_identity(self._sources):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)


class _CoordinatorBootstrapState(ModuleType):
    """One-use proof that the coordinator was loaded by this bootstrap."""

    def __init__(
        self,
        repository_root: Path,
        protected_finder: _WorktreePackageFinder,
        import_paths: Sequence[str],
        qbittorrentapi_shim: _QbittorrentApiShimState | None,
    ) -> None:
        super().__init__(COORDINATOR_BOOTSTRAP_MODULE)
        self._expected_main = repository_root / "benchmarks" / "gauntlet" / "__main__.py"
        self._protected_finder = protected_finder
        self._import_paths = tuple(import_paths)
        self._qbittorrentapi_shim = qbittorrentapi_shim
        self._accepted = False

    def accept(self, source_file: str) -> bool:
        """Consume the bootstrap proof only in its bound coordinator."""
        if (
            self._accepted
            or sys.modules.get(COORDINATOR_BOOTSTRAP_MODULE) is not self
            or not sys.meta_path
            or sys.meta_path[0] is not self._protected_finder
            or tuple(sys.path) != self._import_paths
            or Path(source_file) != self._expected_main
        ):
            return False
        for module_name in ("benchmarks", "benchmarks.gauntlet", "__main__"):
            module = sys.modules.get(module_name)
            if module is None or not isinstance(module.__loader__, _ProtectedSourceLoader):
                return False
        if self._qbittorrentapi_shim is not None:
            self._qbittorrentapi_shim.validate()
        self._accepted = True
        return True

    def validate_after_imports(self) -> None:
        """Revalidate the protected dependency shim after application imports."""
        if self._qbittorrentapi_shim is not None:
            self._qbittorrentapi_shim.validate()


def _resolved_dependency_paths(raw_value: str, repository_root: Path) -> list[str]:
    try:
        values = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise SystemExit("gauntlet dependency import paths are malformed") from error
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise SystemExit("gauntlet dependency import paths are malformed")

    resolved_paths: list[str] = []
    for value in values:
        try:
            path = Path(value)
            resolved_path = path.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit("gauntlet dependency import path could not be resolved") from error
        if (
            not path.is_absolute()
            or not resolved_path.is_dir()
            or not SITE_DIRECTORY_NAMES.intersection(part.casefold() for part in resolved_path.parts)
        ):
            raise SystemExit("gauntlet dependency import path is unsafe")
        resolved_value = str(resolved_path)
        if resolved_value not in resolved_paths:
            resolved_paths.append(resolved_value)
    return resolved_paths


def _validate_interpreter_paths(repository_root: Path) -> list[str]:
    interpreter_paths: list[str] = []
    for value in sys.path:
        try:
            path = Path(value)
            resolved_path = path.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit("gauntlet interpreter import path could not be resolved") from error
        if (
            not value
            or not path.is_absolute()
            or SITE_DIRECTORY_NAMES.intersection(part.casefold() for part in resolved_path.parts)
            or resolved_path.is_relative_to(repository_root)
        ):
            raise SystemExit("gauntlet interpreter import path is unsafe")
        resolved_value = str(resolved_path)
        if resolved_value not in interpreter_paths:
            interpreter_paths.append(resolved_value)
    return interpreter_paths


def main(arguments: Sequence[str] | None = None) -> None:
    """Run the selected worktree after stdlib and dependency path isolation."""
    if not sys.flags.no_site or not sys.flags.safe_path:
        raise SystemExit("gauntlet import bootstrap requires Python -S -P")
    resolved_arguments = list(sys.argv[1:] if arguments is None else arguments)
    if len(resolved_arguments) < 2:
        raise SystemExit("gauntlet import bootstrap arguments are incomplete")
    try:
        repository_root = Path(resolved_arguments.pop(0)).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("gauntlet repository root could not be resolved") from error
    dependency_paths = _resolved_dependency_paths(
        resolved_arguments.pop(0),
        repository_root,
    )
    expected_dependency_digest: str | None = None
    immutable_dependency_sources: tuple[_ImmutableDependencySource, ...] = ()
    expected_revision = "HEAD"
    if resolved_arguments[:1] == [EXPECTED_REPOSITORY_COMMIT_ARGUMENT]:
        resolved_arguments.pop(0)
        if not resolved_arguments:
            raise SystemExit(PROTECTED_IMPORT_ERROR)
        try:
            expected_revision = _validated_repository_commit(resolved_arguments.pop(0))
        except ProtectedPackageTreeError:
            raise SystemExit(PROTECTED_IMPORT_ERROR) from None
    if resolved_arguments[:1] == [DEPENDENCY_DIGEST_ARGUMENT]:
        resolved_arguments.pop(0)
        if not resolved_arguments:
            raise SystemExit("gauntlet dependency environment digest is missing")
        expected_dependency_digest = _validated_dependency_digest(resolved_arguments.pop(0))
        if _current_dependency_digest(dependency_paths) != expected_dependency_digest:
            raise SystemExit("gauntlet dependency environment changed before evaluation")
        _reject_preloaded_dependency_modules(dependency_paths)
        if resolved_arguments[:1] != [IMMUTABLE_TQDM_MANIFEST_ARGUMENT]:
            raise SystemExit(DEPENDENCY_ISOLATION_ERROR)
        resolved_arguments.pop(0)
        if not resolved_arguments:
            raise SystemExit(DEPENDENCY_ISOLATION_ERROR)
        try:
            immutable_dependency_sources = _capture_immutable_tqdm_sources(
                dependency_paths,
                resolved_arguments.pop(0),
            )
        except DependencyEnvironmentError:
            raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from None
    interpreter_paths = _validate_interpreter_paths(repository_root)
    _require_safe_package_trees(repository_root)
    try:
        protected_sources = _tracked_protected_sources(
            repository_root,
            expected_revision=expected_revision,
        )
    except ProtectedPackageTreeError:
        raise SystemExit(PROTECTED_IMPORT_ERROR) from None
    protected_finder = _WorktreePackageFinder(
        repository_root,
        protected_sources,
        expected_revision,
    )
    immutable_dependency_finder = (
        _ImmutableDependencyFinder(immutable_dependency_sources) if expected_dependency_digest is not None else None
    )
    qbittorrentapi_shim = _QbittorrentApiShimState() if expected_dependency_digest is not None else None

    # The worktree root is deliberately absent. Digest-bound measured children
    # use no installed import roots; ordinary mode keeps them behind stdlib.
    sys.path[:] = interpreter_paths if expected_dependency_digest is not None else [*interpreter_paths, *dependency_paths]
    sys.meta_path.insert(0, protected_finder)
    if immutable_dependency_finder is not None:
        sys.meta_path.insert(1, immutable_dependency_finder)
    if qbittorrentapi_shim is not None:
        try:
            qbittorrentapi_shim.install()
        except DependencyEnvironmentError:
            raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from None
    if COORDINATOR_BOOTSTRAP_MODULE in sys.modules:
        raise SystemExit(PROTECTED_IMPORT_ERROR)
    bootstrap_state = _CoordinatorBootstrapState(
        repository_root,
        protected_finder,
        sys.path,
        qbittorrentapi_shim,
    )
    sys.modules[COORDINATOR_BOOTSTRAP_MODULE] = bootstrap_state
    sys.argv[:] = ["benchmarks.gauntlet", *resolved_arguments]
    try:
        try:
            try:
                runpy.run_module("benchmarks.gauntlet", run_name="__main__", alter_sys=True)
                if qbittorrentapi_shim is not None:
                    qbittorrentapi_shim.validate()
            except (AttributeError, ImportError) as error:
                if qbittorrentapi_shim is None or not qbittorrentapi_shim._matches_unapproved_use(error):
                    raise
                raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from None
            except DependencyEnvironmentError:
                raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from None
        except ProtectedPackageTreeError:
            raise SystemExit(PROTECTED_IMPORT_ERROR) from None
    finally:
        try:
            if qbittorrentapi_shim is not None:
                try:
                    qbittorrentapi_shim.validate()
                except DependencyEnvironmentError:
                    raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from None
            _require_safe_package_trees(repository_root)
            try:
                protected_finder.validate_sources()
            except ProtectedPackageTreeError:
                raise SystemExit(PROTECTED_IMPORT_ERROR) from None
            if immutable_dependency_finder is not None:
                try:
                    if (
                        len(sys.meta_path) < 2
                        or sys.meta_path[0] is not protected_finder
                        or sys.meta_path[1] is not immutable_dependency_finder
                    ):
                        raise DependencyEnvironmentError(DEPENDENCY_ISOLATION_ERROR)
                    immutable_dependency_finder.validate_sources()
                except DependencyEnvironmentError:
                    raise SystemExit(DEPENDENCY_ISOLATION_ERROR) from None
            if (
                expected_dependency_digest is not None
                and _current_dependency_digest(dependency_paths) != expected_dependency_digest
            ):
                raise SystemExit("gauntlet dependency environment changed during evaluation")
        finally:
            if sys.modules.get(COORDINATOR_BOOTSTRAP_MODULE) is bootstrap_state:
                del sys.modules[COORDINATOR_BOOTSTRAP_MODULE]


if __name__ == "__main__":
    main()
