"""Tests for suite-owned pytest logging isolation."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from tests import windows_pytest_plugin


class _PluginManager:
    def __init__(self, plugin: object | None) -> None:
        self._plugin = plugin

    def get_plugin(self, name: str) -> object | None:
        assert name == "logging-plugin"
        return self._plugin


class _Config:
    def __init__(self, plugin: object | None, *, option: str | None = None, ini: str = "") -> None:
        self.pluginmanager = _PluginManager(plugin)
        self._option = option
        self._ini = ini

    def getoption(self, name: str) -> str | None:
        assert name == "log_file"
        return self._option

    def getini(self, name: str) -> str:
        assert name == "log_file"
        return self._ini


def _logging_plugin(handler: logging.FileHandler) -> SimpleNamespace:
    return SimpleNamespace(
        log_file_handler=handler,
        caplog_handler=object(),
        report_handler=object(),
    )


def test_close_unused_pytest_devnull_handler_preserves_capture_handlers() -> None:
    """Close only pytest's unused default file stream without touching capture."""
    helper = getattr(windows_pytest_plugin, "_close_unused_pytest_devnull_handler", None)
    assert callable(helper), "pytest devnull logging isolation is missing"
    handler = logging.FileHandler(os.devnull, mode="a", encoding="UTF-8")
    plugin = _logging_plugin(handler)
    caplog_handler = plugin.caplog_handler
    report_handler = plugin.report_handler

    try:
        helper(cast(pytest.Config, _Config(plugin)))

        assert handler.stream is None
        handler.emit(logging.LogRecord("test", logging.INFO, __file__, 1, "ignored", (), None))
        assert handler.stream is None
        assert plugin.caplog_handler is caplog_handler
        assert plugin.report_handler is report_handler
    finally:
        handler.close()


@pytest.mark.parametrize("configured_by", ["option", "ini", "handler"])
def test_close_unused_pytest_devnull_handler_preserves_configured_file_logging(
    tmp_path: Path,
    configured_by: str,
) -> None:
    """Keep every explicit or non-devnull pytest file logger open."""
    helper = getattr(windows_pytest_plugin, "_close_unused_pytest_devnull_handler", None)
    assert callable(helper), "pytest devnull logging isolation is missing"
    log_path = tmp_path / "pytest.log"
    handler_path = log_path if configured_by == "handler" else Path(os.devnull)
    handler = logging.FileHandler(handler_path, mode="w", encoding="UTF-8")
    plugin = _logging_plugin(handler)
    config = _Config(
        plugin,
        option=str(log_path) if configured_by == "option" else None,
        ini=str(log_path) if configured_by == "ini" else "",
    )

    try:
        helper(cast(pytest.Config, config))

        assert handler.stream is not None
    finally:
        handler.close()
