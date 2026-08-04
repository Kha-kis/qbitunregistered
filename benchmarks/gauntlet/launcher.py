"""Start the paired gauntlet before repository bytecode can be imported."""

from __future__ import annotations

import argparse
import json
import os
import site
import stat
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

ISOLATED_PARENT_CACHE_ENV = "QBITUNREGISTERED_GAUNTLET_PARENT_PYCACHE"
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})
BOOTSTRAP_RELATIVE_PATH = "benchmarks/gauntlet/import_bootstrap.py"
BOOTSTRAP_VERIFICATION_ERROR = "gauntlet import bootstrap could not be verified"
REPOSITORY_METADATA_ERROR = "gauntlet repository metadata could not be resolved safely"
DEPENDENCY_PATH_ERROR = "gauntlet dependency import paths could not be resolved safely"
STARTUP_ERROR = "gauntlet launcher must be started with python -I -S -B"
_GIT_TIMEOUT_SECONDS = 10
_MAX_BOOTSTRAP_BYTES = 1024 * 1024
_MAX_PYVENV_CONFIG_BYTES = 64 * 1024
_REGULAR_BLOB_MODES = frozenset({b"100644", b"100755"})


def _require_isolated_startup() -> None:
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.safe_path and sys.flags.dont_write_bytecode):
        raise SystemExit(STARTUP_ERROR)


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
        if not key.upper().startswith(("PYTHON", "GIT_")) and key.upper() != ISOLATED_PARENT_CACHE_ENV
    }
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(cache_root)
    environment[ISOLATED_PARENT_CACHE_ENV] = str(cache_root)
    return environment


def _pyvenv_config_candidates(executable: Path) -> tuple[Path, Path]:
    """Return CPython's lexical virtual-environment configuration locations."""
    executable_directory = executable.parent
    return (
        executable_directory / "pyvenv.cfg",
        executable_directory.parent / "pyvenv.cfg",
    )


def _find_pyvenv_config() -> Path | None:
    """Find ``pyvenv.cfg`` beneath one canonical environment parent."""
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise SystemExit(DEPENDENCY_PATH_ERROR)
    for candidate in _pyvenv_config_candidates(executable):
        try:
            candidate_before = os.lstat(candidate)
        except FileNotFoundError:
            continue
        except OSError as error:
            raise SystemExit(DEPENDENCY_PATH_ERROR) from error
        try:
            canonical_parent = candidate.parent.resolve(strict=True)
            candidate_after = os.lstat(candidate)
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(DEPENDENCY_PATH_ERROR) from error
        if _stable_file_identity(candidate_after) != _stable_file_identity(candidate_before):
            raise SystemExit(DEPENDENCY_PATH_ERROR)
        return canonical_parent / candidate.name
    return None


def _parse_include_system_site_packages(payload: bytes) -> bool:
    """Parse only the bounded boolean needed from ``pyvenv.cfg``."""
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as error:
        raise SystemExit(DEPENDENCY_PATH_ERROR) from error
    values: list[str] = []
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "include-system-site-packages":
            values.append(value.strip().casefold())
    if not values:
        return False
    if len(values) != 1 or values[0] not in {"true", "false"}:
        raise SystemExit(DEPENDENCY_PATH_ERROR)
    return values[0] == "true"


def _venv_site_paths(prefix: Path) -> tuple[str, ...]:
    """Construct the active platform's venv package directories explicitly."""
    try:
        paths = sysconfig.get_paths(
            scheme="venv",
            vars={"base": str(prefix), "platbase": str(prefix)},
        )
        return tuple(paths[key] for key in ("purelib", "platlib"))
    except (KeyError, OSError, TypeError, ValueError) as error:
        raise SystemExit(DEPENDENCY_PATH_ERROR) from error


def _system_site_paths(prefixes: Sequence[str]) -> tuple[str, ...]:
    """Construct system package directories without processing site hooks."""
    try:
        return tuple(site.getsitepackages(list(prefixes)))
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise SystemExit(DEPENDENCY_PATH_ERROR) from error


def _canonical_package_directories(candidates: Sequence[str]) -> tuple[str, ...]:
    dependency_paths: list[str] = []
    try:
        for value in candidates:
            path = Path(value)
            if not path.is_absolute():
                raise ValueError
            try:
                resolved_path = path.resolve(strict=True)
            except FileNotFoundError:
                continue
            if not resolved_path.is_dir():
                raise ValueError
            if not SITE_DIRECTORY_NAMES.intersection(part.casefold() for part in resolved_path.parts):
                continue
            resolved_value = str(resolved_path)
            if resolved_value not in dependency_paths:
                dependency_paths.append(resolved_value)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SystemExit(DEPENDENCY_PATH_ERROR) from error
    if not dependency_paths:
        raise SystemExit(DEPENDENCY_PATH_ERROR)
    return tuple(dependency_paths)


def _dependency_import_paths() -> tuple[str, ...]:
    """Return installed-package paths without importing site hooks."""
    config_path = _find_pyvenv_config()
    if config_path is None:
        candidates = _system_site_paths((sys.prefix, sys.exec_prefix))
        return _canonical_package_directories(candidates)

    config_payload = _read_stable_regular_file(
        config_path,
        maximum_bytes=_MAX_PYVENV_CONFIG_BYTES,
        error_message=DEPENDENCY_PATH_ERROR,
    )
    include_system = _parse_include_system_site_packages(config_payload)
    environment_prefix = config_path.parent
    venv_candidates = list(_venv_site_paths(environment_prefix))
    if include_system:
        venv_candidates.extend(_system_site_paths((sys.base_prefix, sys.base_exec_prefix)))
    return _canonical_package_directories(venv_candidates)


def _git_output(repository_root: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "--no-replace-objects", *arguments],
            cwd=repository_root,
            check=False,
            env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR) from error
    if completed.returncode != 0:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    return completed.stdout


def _repository_git_directories(repository_root: Path) -> tuple[Path, ...]:
    """Return canonical worktree-specific and shared Git directories."""
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "rev-parse",
                "--path-format=absolute",
                "--git-dir",
                "--git-common-dir",
            ],
            cwd=repository_root,
            check=False,
            env={key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise SystemExit(REPOSITORY_METADATA_ERROR) from error
    output_lines = completed.stdout.splitlines()
    if (
        completed.returncode != 0
        or completed.stderr
        or len(output_lines) != 2
        or any(not line or b"\0" in line for line in output_lines)
    ):
        raise SystemExit(REPOSITORY_METADATA_ERROR)
    try:
        git_directories = tuple(Path(os.fsdecode(line)) for line in output_lines)
        if any(not path.is_absolute() for path in git_directories):
            raise ValueError
        resolved_directories = tuple(path.resolve(strict=True) for path in git_directories)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise SystemExit(REPOSITORY_METADATA_ERROR) from error
    if any(not path.is_dir() for path in resolved_directories):
        raise SystemExit(REPOSITORY_METADATA_ERROR)
    return tuple(dict.fromkeys(resolved_directories))


def _repository_protected_roots(repository_roots: Sequence[Path]) -> tuple[Path, ...]:
    """Return canonical worktree and Git metadata roots without duplicates."""
    protected_roots: list[Path] = []
    try:
        for repository_root in repository_roots:
            resolved_root = repository_root.resolve(strict=True)
            if not resolved_root.is_dir():
                raise ValueError
            protected_roots.extend((resolved_root, *_repository_git_directories(resolved_root)))
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(REPOSITORY_METADATA_ERROR) from error
    return tuple(dict.fromkeys(protected_roots))


def _split_git_record(output: bytes) -> tuple[bytes, bytes]:
    if not output.endswith(b"\0") or output.count(b"\0") != 1:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    try:
        metadata, path = output[:-1].split(b"\t", 1)
    except ValueError as error:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR) from error
    return metadata, path


def _bootstrap_blob_identity(repository_root: Path) -> bytes:
    encoded_path = BOOTSTRAP_RELATIVE_PATH.encode("ascii")
    index_metadata, index_path = _split_git_record(
        _git_output(
            repository_root,
            (
                "ls-files",
                "--cached",
                "-v",
                "--stage",
                "-z",
                "--",
                BOOTSTRAP_RELATIVE_PATH,
            ),
        ),
    )
    index_fields = index_metadata.split(b" ")
    if len(index_fields) != 4 or any(not field for field in index_fields):
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    index_status, index_mode, index_oid, index_stage = index_fields
    if (
        index_path != encoded_path
        or index_status != b"H"
        or index_mode not in _REGULAR_BLOB_MODES
        or index_stage != b"0"
        or len(index_oid) not in {40, 64}
        or any(character not in b"0123456789abcdef" for character in index_oid)
    ):
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)

    head_metadata, head_path = _split_git_record(
        _git_output(
            repository_root,
            ("ls-tree", "--full-tree", "-z", "HEAD", "--", BOOTSTRAP_RELATIVE_PATH),
        )
    )
    head_fields = head_metadata.split(b" ")
    if len(head_fields) != 3 or any(not field for field in head_fields):
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    head_mode, head_type, head_oid = head_fields
    if head_path != encoded_path or head_type != b"blob" or head_mode != index_mode or head_oid != index_oid:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    return index_oid


def _entry_is_redirecting(file_stat: os.stat_result) -> bool:
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(reparse_point and file_attributes & reparse_point)


def _stable_file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        getattr(file_stat, "st_file_attributes", 0),
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


def _cross_interface_file_identity(file_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return identity fields comparable between path and descriptor stats.

    Modern Windows path stats expose creation time as ``st_ctime_ns``, while
    descriptor stats expose metadata change time. Callers compare ctime only
    between repeated observations from the same stat interface.
    """
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        file_stat.st_mode,
        file_stat.st_size,
        getattr(file_stat, "st_file_attributes", 0),
        file_stat.st_mtime_ns,
    )


def _read_stable_regular_file(  # noqa: C901
    path: Path,
    *,
    maximum_bytes: int,
    error_message: str,
) -> bytes:
    """Read bounded bytes from a stable, regular, nonredirecting file."""
    try:
        path_before = os.lstat(path)
        if _entry_is_redirecting(path_before) or not stat.S_ISREG(path_before.st_mode) or path_before.st_size > maximum_bytes:
            raise SystemExit(error_message)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except SystemExit:
        raise
    except OSError as error:
        raise SystemExit(error_message) from error
    try:
        descriptor_before = os.fstat(descriptor)
        if (
            _entry_is_redirecting(descriptor_before)
            or not stat.S_ISREG(descriptor_before.st_mode)
            or _cross_interface_file_identity(descriptor_before) != _cross_interface_file_identity(path_before)
        ):
            raise SystemExit(error_message)
        payload = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, maximum_bytes + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > maximum_bytes:
                raise SystemExit(error_message)
        descriptor_after = os.fstat(descriptor)
        path_after = os.lstat(path)
    except SystemExit:
        raise
    except OSError as error:
        raise SystemExit(error_message) from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise SystemExit(error_message) from error
    expected_path_identity = _stable_file_identity(path_before)
    expected_descriptor_identity = _stable_file_identity(descriptor_before)
    if (
        _stable_file_identity(descriptor_after) != expected_descriptor_identity
        or _stable_file_identity(path_after) != expected_path_identity
        or len(payload) != path_before.st_size
    ):
        raise SystemExit(error_message)
    return bytes(payload)


def _read_worktree_bootstrap(path: Path) -> bytes:
    return _read_stable_regular_file(
        path,
        maximum_bytes=_MAX_BOOTSTRAP_BYTES,
        error_message=BOOTSTRAP_VERIFICATION_ERROR,
    )


def _bootstrap_checkout_matches(checkout_source: bytes, trusted_source: bytes) -> bool:
    """Accept exact bytes or Git's whole-file LF-to-CRLF checkout form."""
    if checkout_source == trusted_source:
        return True
    if b"\r" in trusted_source:
        return False
    return checkout_source == trusted_source.replace(b"\n", b"\r\n")


def _trusted_bootstrap_source(repository_root: Path) -> bytes:
    """Bind the downstream bootstrap to clean HEAD and stage-0 blob bytes."""
    encoded_oid = _bootstrap_blob_identity(repository_root)
    try:
        oid = encoded_oid.decode("ascii")
    except UnicodeDecodeError as error:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR) from error
    encoded_size = _git_output(repository_root, ("cat-file", "-s", oid)).strip()
    if not encoded_size.isdigit() or len(encoded_size) > len(str(_MAX_BOOTSTRAP_BYTES)):
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    source_size = int(encoded_size)
    if source_size > _MAX_BOOTSTRAP_BYTES:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    source_bytes = _git_output(repository_root, ("cat-file", "blob", oid))
    if len(source_bytes) != source_size:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    worktree_path = repository_root.joinpath(*BOOTSTRAP_RELATIVE_PATH.split("/"))
    worktree_source = _read_worktree_bootstrap(worktree_path)
    if not _bootstrap_checkout_matches(worktree_source, source_bytes):
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    return source_bytes


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the gauntlet module with a fresh parent-process bytecode cache."""
    _require_isolated_startup()
    resolved_arguments = list(sys.argv[1:] if arguments is None else arguments)
    repository_root = Path(__file__).resolve().parents[2]
    repository_roots = (
        repository_root,
        *_paired_repository_roots(resolved_arguments),
    )
    protected_roots = _repository_protected_roots(repository_roots)
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit("gauntlet bytecode cache directory could not be resolved") from error
    if any(temporary_root.is_relative_to(root) for root in protected_roots):
        raise SystemExit("gauntlet bytecode cache directory must be outside evaluated repositories")
    bootstrap_source = _trusted_bootstrap_source(repository_root)

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
                    "-",
                    str(repository_root),
                    json.dumps(_dependency_import_paths()),
                    *resolved_arguments,
                ],
                check=False,
                env=_isolated_environment(os.environ, cache_root),
                input=bootstrap_source,
            )
        except OSError as error:
            raise SystemExit("isolated gauntlet coordinator could not start") from error
    if completed.returncode < 0:
        return 128 - completed.returncode
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
