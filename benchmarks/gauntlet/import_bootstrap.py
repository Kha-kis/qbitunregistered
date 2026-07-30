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
from typing import Protocol

PROTECTED_PACKAGE_NAMES = ("benchmarks", "qbitunregistered")
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})
DEPENDENCY_DIGEST_ARGUMENT = "--dependency-environment-digest"
PROTECTED_IMPORT_ERROR = "gauntlet protected imports could not be verified"
_DIGEST_CHUNK_BYTES = 1024 * 1024
_GIT_TIMEOUT_SECONDS = 10
_REGULAR_BLOB_MODES = frozenset({b"100644", b"100755"})


class DependencyEnvironmentError(RuntimeError):
    """Raised when installed dependency contents cannot be bound safely."""


class ProtectedPackageTreeError(RuntimeError):
    """Raised when protected source packages cannot be imported safely."""


@dataclass(frozen=True, slots=True)
class _ProtectedSource:
    """One Git-tracked source bound to its canonical import name."""

    fullname: str
    path: Path
    is_package: bool
    mode: str
    oid: str
    source_bytes: bytes


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


def _update_regular_file_digest(
    path: Path,
    expected_stat: os.stat_result,
    digest: _Digest,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise DependencyEnvironmentError("could not open an installed dependency safely") from error
    try:
        before = os.fstat(descriptor)
        if (
            _entry_is_redirecting(before)
            or not stat.S_ISREG(before.st_mode)
            or _entry_identity(before) != _entry_identity(expected_stat)
        ):
            raise DependencyEnvironmentError("installed dependency entry changed during validation")
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


def _tracked_protected_sources(
    repository_root: Path,
    *,
    capture_source_bytes: bool = True,
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
    if capture_source_bytes:
        sources = {
            fullname: replace(
                source,
                source_bytes=_read_git_blob(repository_root, source.oid),
            )
            for fullname, source in sources.items()
        }
    return sources


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
    ) -> None:
        self._repository_root = repository_root
        self._sources = dict(sources)
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
        )
        if _source_index_identity(current_sources) != _source_index_identity(self._sources):
            raise ProtectedPackageTreeError(PROTECTED_IMPORT_ERROR)


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
    if resolved_arguments[:1] == [DEPENDENCY_DIGEST_ARGUMENT]:
        resolved_arguments.pop(0)
        if not resolved_arguments:
            raise SystemExit("gauntlet dependency environment digest is missing")
        expected_dependency_digest = _validated_dependency_digest(resolved_arguments.pop(0))
        if _current_dependency_digest(dependency_paths) != expected_dependency_digest:
            raise SystemExit("gauntlet dependency environment changed before evaluation")
    interpreter_paths = _validate_interpreter_paths(repository_root)
    _require_safe_package_trees(repository_root)
    try:
        protected_sources = _tracked_protected_sources(repository_root)
    except ProtectedPackageTreeError:
        raise SystemExit(PROTECTED_IMPORT_ERROR) from None
    protected_finder = _WorktreePackageFinder(repository_root, protected_sources)

    # The worktree root is deliberately absent: only its protected first-party
    # packages outrank ordinary installed dependencies.
    sys.path[:] = [*interpreter_paths, *dependency_paths]
    sys.meta_path.insert(0, protected_finder)
    sys.argv[:] = ["benchmarks.gauntlet", *resolved_arguments]
    try:
        try:
            runpy.run_module("benchmarks.gauntlet", run_name="__main__", alter_sys=True)
        except ProtectedPackageTreeError:
            raise SystemExit(PROTECTED_IMPORT_ERROR) from None
    finally:
        _require_safe_package_trees(repository_root)
        try:
            protected_finder.validate_sources()
        except ProtectedPackageTreeError:
            raise SystemExit(PROTECTED_IMPORT_ERROR) from None
        if (
            expected_dependency_digest is not None
            and _current_dependency_digest(dependency_paths) != expected_dependency_digest
        ):
            raise SystemExit("gauntlet dependency environment changed during evaluation")


if __name__ == "__main__":
    main()
