"""Focused checks for the temporary Windows descriptor-leak diagnostic."""

from __future__ import annotations

import errno
import os
import stat
from typing import cast

import pytest

from tests import conftest as suite_config


def _fake_stat(mode: int, inode: int) -> os.stat_result:
    return os.stat_result((mode, inode, 7, 1, 0, 0, 0, 0, 0, 0))


def test_windows_descriptor_inventory_excludes_only_stdout_and_stderr_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the diagnostic allowing stdin aliases or reporting output aliases."""
    stdin = _fake_stat(stat.S_IFREG, 40)
    stdout = _fake_stat(stat.S_IFREG, 41)
    stderr = _fake_stat(stat.S_IFREG, 42)
    regular = _fake_stat(stat.S_IFREG, 43)
    stats = {0: stdin, 1: stdout, 2: stderr, 3: regular, 4: stdout, 5: stderr, 6: stdin}

    def fake_fstat(descriptor: int) -> os.stat_result:
        try:
            return stats[descriptor]
        except KeyError:
            raise OSError(errno.EBADF, "closed") from None

    monkeypatch.setattr(suite_config.os, "fstat", fake_fstat)

    assert suite_config._collect_windows_unsafe_regular_descriptors() == {3: regular, 6: stdin}


def test_windows_descriptor_delta_detects_replacement_and_new_number() -> None:
    """Catch descriptor-number reuse hiding a different leaked regular file."""
    unchanged = _fake_stat(stat.S_IFREG, 50)
    replaced_before = _fake_stat(stat.S_IFREG, 51)
    replaced_after = _fake_stat(stat.S_IFREG, 52)
    newly_opened = _fake_stat(stat.S_IFREG, 53)

    assert suite_config._new_windows_regular_descriptors(
        {3: unchanged, 4: replaced_before},
        {3: unchanged, 4: replaced_after, 5: newly_opened},
    ) == {4: replaced_after, 5: newly_opened}


@pytest.mark.parametrize(
    ("stdin_alias", "inheritable", "expected"),
    [
        (
            True,
            True,
            "descriptor=9; stdio_aliases=0; inheritable=true; stat_fingerprint=(32768, 7, 60, 0)",
        ),
        (
            False,
            OSError(errno.EBADF, "closed"),
            "descriptor=9; stdio_aliases=none; inheritable=unknown; stat_fingerprint=(32768, 7, 60, 0)",
        ),
    ],
)
def test_windows_descriptor_metadata_is_sanitized_and_race_safe(
    monkeypatch: pytest.MonkeyPatch,
    stdin_alias: bool,
    inheritable: bool | OSError,
    expected: str,
) -> None:
    """Catch path disclosure or a raced inheritance lookup hiding a leak."""
    regular = _fake_stat(stat.S_IFREG, 60)
    stdin = regular if stdin_alias else _fake_stat(stat.S_IFIFO, 61)
    monkeypatch.setattr(suite_config.os, "fstat", lambda descriptor: stdin if descriptor == 0 else regular)

    def fake_get_inheritable(_descriptor: int) -> bool:
        if isinstance(inheritable, OSError):
            raise inheritable
        return inheritable

    monkeypatch.setattr(suite_config.os, "get_inheritable", fake_get_inheritable)

    assert suite_config._format_windows_descriptor_metadata(9, regular) == expected


def test_windows_descriptor_failure_reports_only_node_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch the temporary diagnostic disclosing descriptor-backed paths."""
    regular = _fake_stat(stat.S_IFREG, 70)
    pipe = _fake_stat(stat.S_IFIFO, 71)
    monkeypatch.setattr(suite_config.os, "fstat", lambda _descriptor: pipe)
    monkeypatch.setattr(suite_config.os, "get_inheritable", lambda _descriptor: False)

    assert suite_config._format_windows_descriptor_failure(
        "tests/test_example.py::test_leak",
        3,
        regular,
    ) == (
        "nodeid=tests/test_example.py::test_leak; phase=teardown; descriptor=3; "
        "stdio_aliases=none; inheritable=false; stat_fingerprint=(32768, 7, 70, 0)"
    )


def test_windows_descriptor_teardown_uses_exception_safe_wrapper() -> None:
    """Catch an intended leak failure becoming a Pluggy teardown warning."""
    hook_options = cast(
        "dict[str, object]",
        getattr(suite_config.pytest_runtest_teardown, "pytest_impl"),
    )

    assert hook_options["wrapper"] is True
    assert hook_options["hookwrapper"] is False
