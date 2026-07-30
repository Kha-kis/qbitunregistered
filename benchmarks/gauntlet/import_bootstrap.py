"""Run the gauntlet with first-party imports bound to one worktree."""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import json
import runpy
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

PROTECTED_PACKAGE_NAMES = ("benchmarks", "qbitunregistered")
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})


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
    interpreter_paths = _validate_interpreter_paths(repository_root)
    package_roots = {name: repository_root / name for name in PROTECTED_PACKAGE_NAMES}
    if any(not (package_root / "__init__.py").is_file() for package_root in package_roots.values()):
        raise SystemExit("gauntlet worktree does not contain protected source packages")

    # The worktree root is deliberately absent: only its protected first-party
    # packages outrank ordinary installed dependencies.
    sys.path[:] = [*interpreter_paths, *dependency_paths]
    sys.meta_path.insert(0, _WorktreePackageFinder(package_roots))
    sys.argv[:] = ["benchmarks.gauntlet", *resolved_arguments]
    runpy.run_module("benchmarks.gauntlet", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
