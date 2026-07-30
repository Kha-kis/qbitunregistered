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
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Protocol

PROTECTED_PACKAGE_NAMES = ("benchmarks", "qbitunregistered")
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})
DEPENDENCY_DIGEST_ARGUMENT = "--dependency-environment-digest"
_DIGEST_CHUNK_BYTES = 1024 * 1024


class DependencyEnvironmentError(RuntimeError):
    """Raised when installed dependency contents cannot be bound safely."""


class ProtectedPackageTreeError(RuntimeError):
    """Raised when protected source packages cannot be imported safely."""


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


class _WorktreePackageFinder(importlib.abc.MetaPathFinder):
    """Resolve protected top-level packages only from the selected worktree."""

    def __init__(self, package_roots: Mapping[str, Path]) -> None:
        self._package_roots = dict(package_roots)

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del target
        if path is not None:
            return None
        package_root = self._package_roots.get(fullname)
        if package_root is None:
            return None
        initializer = package_root / "__init__.py"
        return importlib.util.spec_from_file_location(
            fullname,
            initializer,
            submodule_search_locations=[str(package_root)],
        )


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
    package_roots = {name: repository_root / name for name in PROTECTED_PACKAGE_NAMES}
    if any(not (package_root / "__init__.py").is_file() for package_root in package_roots.values()):
        raise SystemExit("gauntlet worktree does not contain protected source packages")

    # The worktree root is deliberately absent: only its protected first-party
    # packages outrank ordinary installed dependencies.
    sys.path[:] = [*interpreter_paths, *dependency_paths]
    sys.meta_path.insert(0, _WorktreePackageFinder(package_roots))
    sys.argv[:] = ["benchmarks.gauntlet", *resolved_arguments]
    try:
        runpy.run_module("benchmarks.gauntlet", run_name="__main__", alter_sys=True)
    finally:
        _require_safe_package_trees(repository_root)
        if (
            expected_dependency_digest is not None
            and _current_dependency_digest(dependency_paths) != expected_dependency_digest
        ):
            raise SystemExit("gauntlet dependency environment changed during evaluation")


if __name__ == "__main__":
    main()
