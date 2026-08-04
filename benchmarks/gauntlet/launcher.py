"""Start the paired gauntlet before repository bytecode can be imported."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

ISOLATED_PARENT_CACHE_ENV = "QBITUNREGISTERED_GAUNTLET_PARENT_PYCACHE"
SITE_DIRECTORY_NAMES = frozenset({"site-packages", "dist-packages"})
BOOTSTRAP_RELATIVE_PATH = "benchmarks/gauntlet/import_bootstrap.py"
BOOTSTRAP_VERIFICATION_ERROR = "gauntlet import bootstrap could not be verified"
_GIT_TIMEOUT_SECONDS = 10
_MAX_BOOTSTRAP_BYTES = 1024 * 1024
_REGULAR_BLOB_MODES = frozenset({b"100644", b"100755"})


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
        if not key.upper().startswith(("PYTHON", "GIT_")) and key.upper() != ISOLATED_PARENT_CACHE_ENV
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


def _read_worktree_bootstrap(path: Path) -> bytes:  # noqa: C901
    try:
        path_before = os.lstat(path)
        if (
            _entry_is_redirecting(path_before)
            or not stat.S_ISREG(path_before.st_mode)
            or path_before.st_size > _MAX_BOOTSTRAP_BYTES
        ):
            raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except SystemExit:
        raise
    except OSError as error:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR) from error
    try:
        descriptor_before = os.fstat(descriptor)
        if (
            _entry_is_redirecting(descriptor_before)
            or not stat.S_ISREG(descriptor_before.st_mode)
            or _stable_file_identity(descriptor_before) != _stable_file_identity(path_before)
        ):
            raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
        payload = bytearray()
        while chunk := os.read(descriptor, min(64 * 1024, _MAX_BOOTSTRAP_BYTES + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > _MAX_BOOTSTRAP_BYTES:
                raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
        descriptor_after = os.fstat(descriptor)
        path_after = os.lstat(path)
    except SystemExit:
        raise
    except OSError as error:
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR) from error
    finally:
        try:
            os.close(descriptor)
        except OSError as error:
            raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR) from error
    expected_identity = _stable_file_identity(path_before)
    if (
        _stable_file_identity(descriptor_after) != expected_identity
        or _stable_file_identity(path_after) != expected_identity
        or len(payload) != path_before.st_size
    ):
        raise SystemExit(BOOTSTRAP_VERIFICATION_ERROR)
    return bytes(payload)


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
