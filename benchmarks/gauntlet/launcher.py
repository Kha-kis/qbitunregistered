"""Start the paired gauntlet before repository bytecode can be imported."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

ISOLATED_PARENT_CACHE_ENV = "QBITUNREGISTERED_GAUNTLET_PARENT_PYCACHE"
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})


def _require_isolated_startup() -> None:
    if not sys.flags.isolated:
        raise SystemExit("gauntlet launcher must be started with python -I")


def _paired_repository_roots(arguments: Sequence[str]) -> tuple[Path, ...]:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--paired-control", type=Path)
    parser.add_argument("--paired-candidate", type=Path)
    parsed_arguments, _unknown = parser.parse_known_args(arguments)
    try:
        return tuple(
            path.expanduser().resolve()
            for path in (
                parsed_arguments.paired_control,
                parsed_arguments.paired_candidate,
            )
            if path is not None
        )
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("paired gauntlet worktree paths could not be resolved") from error


def _isolated_environment(
    inherited_environment: Mapping[str, str],
    cache_root: Path,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in inherited_environment.items()
        if not key.upper().startswith("PYTHON") and key.upper() != ISOLATED_PARENT_CACHE_ENV
    }
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(cache_root)
    environment[ISOLATED_PARENT_CACHE_ENV] = str(cache_root)
    return environment


def _dependency_import_paths() -> tuple[str, ...]:
    """Return ordinary installed-package paths without editable source roots."""
    dependency_paths: list[str] = []
    for value in sys.path:
        if not value:
            continue
        try:
            path = Path(value)
            resolved_path = path.resolve()
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit("gauntlet dependency import path could not be resolved") from error
        if (
            path.is_absolute()
            and resolved_path.is_dir()
            and SITE_DIRECTORY_NAMES.intersection(part.casefold() for part in resolved_path.parts)
        ):
            resolved_value = str(resolved_path)
            if resolved_value not in dependency_paths:
                dependency_paths.append(resolved_value)
    return tuple(dependency_paths)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the gauntlet module with a fresh parent-process bytecode cache."""
    _require_isolated_startup()
    resolved_arguments = list(sys.argv[1:] if arguments is None else arguments)
    repository_root = Path(__file__).resolve().parents[2]
    protected_roots = (
        repository_root,
        *_paired_repository_roots(resolved_arguments),
    )
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve()
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("gauntlet bytecode cache directory could not be resolved") from error
    if any(temporary_root.is_relative_to(root) for root in protected_roots):
        raise SystemExit("gauntlet bytecode cache directory must be outside evaluated repositories")

    with tempfile.TemporaryDirectory(
        prefix="qbitunregistered-gauntlet-parent-pycache-",
        dir=temporary_root,
    ) as cache_name:
        cache_root = Path(cache_name).resolve()
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-s",
                    "-S",
                    "-P",
                    str(repository_root / "benchmarks" / "gauntlet" / "import_bootstrap.py"),
                    str(repository_root),
                    json.dumps(_dependency_import_paths()),
                    *resolved_arguments,
                ],
                check=False,
                env=_isolated_environment(os.environ, cache_root),
            )
        except OSError as error:
            raise SystemExit("isolated gauntlet coordinator could not start") from error
    if completed.returncode < 0:
        return 128 - completed.returncode
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
