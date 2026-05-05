"""Step definitions for the browse (open-in-browser) feature."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest
from pytest_bdd import then
from textual.widgets import Static

from workdash.launcher import open_in_browser
from workdash.models import WorkItem

from .common import run_app


def run_open_scenario(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive pressing 'o' in the TUI and capture the xdg-open invocation."""

    invocations: list[list[str]] = []

    def open_callback(item: WorkItem) -> None:
        # Use the real launcher under a patched subprocess.run so we verify
        # the open_in_browser code path executes end-to-end.
        def fake_run(command, **kwargs):
            invocations.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        open_in_browser(item.url)

    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await pilot.press("o")
        for _ in range(20):
            await pilot.pause()
            if invocations:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain

    run_app(
        work_items=list(work_items),
        open_callback=open_callback,
        interactions=interactions,
    )
    scenario_state["open_invocations"] = invocations
    scenario_state["open_status"] = captured["status"]
    scenario_state["selected_item"] = work_items[0]


@then("the selected work item's GitHub URL is opened in the user's default browser")
def _url_opened(scenario_state: dict[str, Any]) -> None:
    invocations = scenario_state["open_invocations"]
    assert invocations, "Expected xdg-open to be invoked"
    assert invocations[0][0] == "xdg-open"
    assert invocations[0][1] == scenario_state["selected_item"].url


@then("the TUI reports that the item was opened")
def _tui_reports_opened(scenario_state: dict[str, Any]) -> None:
    assert "Opened" in scenario_state["open_status"], scenario_state["open_status"]
