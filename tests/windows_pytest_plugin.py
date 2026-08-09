"""Windows-only pytest isolation for the tracker production boundary."""

from __future__ import annotations

import logging
import os
import sys

import pytest


def _close_unused_pytest_devnull_handler(config: pytest.Config) -> None:
    """Close pytest's unused default file logger without disabling capture."""
    if config.getoption("log_file") or config.getini("log_file"):
        return
    logging_plugin = config.pluginmanager.get_plugin("logging-plugin")
    file_handler = getattr(logging_plugin, "log_file_handler", None)
    if not isinstance(file_handler, logging.FileHandler):
        return
    handler_path = os.path.normcase(os.path.abspath(file_handler.baseFilename))
    devnull_path = os.path.normcase(os.path.abspath(os.devnull))
    if handler_path != devnull_path:
        return
    file_handler.close()
    # A closed append-mode FileHandler can reopen itself on the next record.
    file_handler.mode = "w"


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    """Remove pytest's unused regular-looking Windows null descriptor."""
    if sys.platform == "win32":
        _close_unused_pytest_devnull_handler(session.config)
