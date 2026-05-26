"""Step definitions for the browse (open-in-browser) feature."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

import pytest
from pytest_bdd import given, then
from textual.widgets import Static

from workdash.launcher import open_in_browser
from workdash.models import WorkItem

from .common import modal_screen_names, run_app


@given("the next browser open will fail")
def _next_browser_open_will_fail(scenario_state: dict[str, Any]) -> None:
    scenario_state["browser_open_fails"] = True


@given("the next browser open will not respond")
def _next_browser_open_will_not_respond(scenario_state: dict[str, Any]) -> None:
    scenario_state["browser_open_hangs"] = True


def run_open_scenario(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive pressing 'o' in the TUI and capture the browser-open invocation."""

    invocations: list[list[str]] = []

    def open_callback(item: WorkItem) -> None:
        # Use the real launcher under a patched subprocess.run so we verify
        # the open_in_browser code path executes end-to-end.
        def fake_run(command, **kwargs):
            invocations.append(list(command))
            if scenario_state.get("browser_open_fails"):
                raise subprocess.CalledProcessError(
                    returncode=1,
                    cmd=command,
                    stderr="cannot open display",
                )
            if scenario_state.get("browser_open_hangs"):
                assert kwargs.get("timeout") == 4
                raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None,
        )
        open_in_browser(item.url)

    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await pilot.press("o")
        for _ in range(20):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if invocations and (
                not scenario_state.get("browser_open_fails")
                and not scenario_state.get("browser_open_hangs")
                or "failed" in status
            ):
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain
        captured["modal_screen_names"] = modal_screen_names(app)

    run_app(
        work_items=list(work_items),
        open_callback=open_callback,
        interactions=interactions,
    )
    scenario_state["open_invocations"] = invocations
    scenario_state["open_status"] = captured["status"]
    scenario_state["selected_item"] = work_items[0]
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


@then("the selected work item's GitHub URL is opened in the user's default browser")
def _url_opened(scenario_state: dict[str, Any]) -> None:
    invocations = scenario_state["open_invocations"]
    assert invocations, "Expected a browser-open command to be invoked"
    assert invocations[0][0] in {"xdg-open", "open"}
    assert invocations[0][1] == scenario_state["selected_item"].url


@then("the TUI reports that the item was opened")
def _tui_reports_opened(scenario_state: dict[str, Any]) -> None:
    assert "Opened" in scenario_state["open_status"], scenario_state["open_status"]


@then("the system reports the browser error details to the user")
def _reports_browser_error_details(scenario_state: dict[str, Any]) -> None:
    assert (
        "Open failed: Failed to open URL via xdg-open: cannot open display"
        in scenario_state["open_status"]
    )


@then("the system reports that browser opening may not be supported from this session")
def _reports_browser_open_may_not_be_supported(scenario_state: dict[str, Any]) -> None:
    status = scenario_state["open_status"]
    assert "Open failed: Failed to open URL via xdg-open" in status
    assert "command did not finish within 4 seconds" in status
    assert "Opening a browser may not be supported from this session" in status
