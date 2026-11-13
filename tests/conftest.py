"""Shared fixtures for the whole test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _server_log_in_tmp_path(tmp_path, monkeypatch):
    """Keep server starts in tests away from the developer's own diagnostic log."""

    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", tmp_path / "workdash.log")


@pytest.fixture(autouse=True)
def _not_inside_zellij(monkeypatch):
    """Keep the suite independent of the Zellij pane pytest itself runs in."""

    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        monkeypatch.delenv(key, raising=False)
