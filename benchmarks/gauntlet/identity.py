"""Sanitized repository identity capture for evaluator comparability."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None:
        """Add bytes to a one-way digest."""


@dataclass(frozen=True, slots=True)
class RepositoryIdentity:
    """Commit and declared worktree state without raw paths or diff content."""

    commit: str
    clean: bool | None
    diff_sha256: str

    @property
    def known(self) -> bool:
        """Return whether Git supplied a complete comparable identity."""
        return self.commit != "unknown" and self.clean is not None and self.diff_sha256 != "unknown"


class RepositoryIdentityError(RuntimeError):
    """Raised when repository state changes during evaluator execution."""


def _git_output(repository_root: Path, arguments: list[str]) -> bytes | None:
    """Return one Git command's stdout, or ``None`` when Git is unavailable."""
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _stream_regular_file(digest: _Digest, path: Path) -> bool:
    """Hash an untracked regular file without loading it all into memory."""
    try:
        with path.open("rb") as file_handle:
            while chunk := file_handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError:
        return False
    return True


def _hash_untracked_candidate(
    digest: _Digest,
    repository_root: Path,
    encoded_relative_path: bytes,
) -> bool:
    """Add one untracked path and payload to a private candidate digest."""
    relative_path = Path(os.fsdecode(encoded_relative_path))
    untracked_path = repository_root / relative_path
    try:
        path_stat = untracked_path.lstat()
    except OSError:
        return False
    digest.update(b"\0untracked\0")
    digest.update(encoded_relative_path)
    digest.update(b"\0mode\0")
    digest.update(str(path_stat.st_mode).encode("ascii"))
    digest.update(b"\0")
    if stat.S_ISREG(path_stat.st_mode):
        return _stream_regular_file(digest, untracked_path)
    if stat.S_ISLNK(path_stat.st_mode):
        try:
            digest.update(os.fsencode(os.readlink(untracked_path)))
        except OSError:
            return False
        return True
    return True


def capture_repository_identity(repository_root: Path) -> RepositoryIdentity:
    """Capture commit plus staged, unstaged, and untracked candidate content."""
    resolved_root = repository_root.resolve()
    commit_output = _git_output(resolved_root, ["rev-parse", "HEAD"])
    status = _git_output(resolved_root, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    staged_diff = _git_output(resolved_root, ["diff", "--cached", "--binary", "--no-ext-diff", "--"])
    unstaged_diff = _git_output(resolved_root, ["diff", "--binary", "--no-ext-diff", "--"])
    untracked = _git_output(resolved_root, ["ls-files", "--others", "--exclude-standard", "-z"])
    if any(value is None for value in (commit_output, status, staged_diff, unstaged_diff, untracked)):
        return RepositoryIdentity(commit="unknown", clean=None, diff_sha256="unknown")

    assert commit_output is not None
    assert status is not None
    assert staged_diff is not None
    assert unstaged_diff is not None
    assert untracked is not None
    commit = commit_output.decode("ascii", errors="ignore").strip()
    if len(commit) != 40:
        return RepositoryIdentity(commit="unknown", clean=None, diff_sha256="unknown")

    digest = hashlib.sha256()
    digest.update(b"staged-diff\0")
    digest.update(staged_diff)
    digest.update(b"\0unstaged-diff\0")
    digest.update(unstaged_diff)
    for encoded_relative_path in sorted(filter(None, untracked.split(b"\0"))):
        if not _hash_untracked_candidate(digest, resolved_root, encoded_relative_path):
            return RepositoryIdentity(commit="unknown", clean=None, diff_sha256="unknown")
    return RepositoryIdentity(
        commit=commit,
        clean=not bool(status),
        diff_sha256=digest.hexdigest(),
    )


def require_same_identity(expected: RepositoryIdentity, actual: RepositoryIdentity) -> None:
    """Fail when commit or declared candidate content changes during a run."""
    if actual != expected:
        raise RepositoryIdentityError("repository identity changed during gauntlet evaluation")
