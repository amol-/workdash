"""Shared fixtures for the whole test suite."""

from __future__ import annotations

from wsgiref.simple_server import WSGIServer

import pytest


@pytest.fixture(autouse=True)
def _fast_http_server_shutdown(monkeypatch):
    """Avoid waiting up to half a second for idle test servers to stop."""
    serve_forever = WSGIServer.serve_forever

    def serve_with_short_poll(server, poll_interval=0.01):
        return serve_forever(server, poll_interval=poll_interval)

    monkeypatch.setattr(WSGIServer, "serve_forever", serve_with_short_poll)


@pytest.fixture(autouse=True)
def _server_log_in_tmp_path(tmp_path, monkeypatch):
    """Keep server starts in tests away from the developer's own diagnostic log."""

    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", tmp_path / "workdash.log")


@pytest.fixture(autouse=True)
def _not_inside_zellij(monkeypatch):
    """Keep the suite independent of the Zellij pane pytest itself runs in."""

    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _dashboard_records_in_tmp_path(tmp_path, monkeypatch):
    """Keep dashboard session records away from the developer's own config directory."""

    monkeypatch.setattr("workdash.dashboard_record.SESSIONS_DIR", tmp_path / "sessions")


@pytest.fixture(autouse=True)
def _pane_launch_state_in_tmp_path(tmp_path, monkeypatch):
    """Keep restart receipts out of the developer's state directory."""

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def _no_answering_dashboard(monkeypatch):
    """Keep dashboard startup from probing a dashboard that may run on this machine."""

    monkeypatch.setattr("workdash.control.probe_dashboard_session", lambda address, **_kwargs: None)
