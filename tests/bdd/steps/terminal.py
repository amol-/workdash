"""Step definitions for the open-terminal feature."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import then
from textual.widgets import Static

from workdash.models import WorkItem

from .common import run_app


def run_terminal_scenario(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Drive pressing 't' in the TUI and record worktree + terminal calls."""

    worktree_calls: list[WorkItem] = []
    terminal_calls: list[str] = []
    # The terminal flow must never invoke a coding-agent launch. We still
    # wire a launch_callback so that if the TUI mistakenly dispatched to
    # coding on 't', the appended entry would make the "no coding agent is
    # started" Then step fail loudly.
    launch_calls: list[tuple[WorkItem, str]] = []
    captured: dict[str, Any] = {}

    def worktree_callback(item: WorkItem) -> str:
        wt_path = tmp_path / "wt"
        wt_path.mkdir(exist_ok=True)
        worktree_calls.append(item)
        return str(wt_path)

    def terminal_callback(item: WorkItem) -> None:
        terminal_calls.append(item.repo)

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item, tool))

    async def interactions(app, pilot) -> None:
        await pilot.press("t")
        for _ in range(40):
            await pilot.pause()
            if terminal_calls:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain

    run_app(
        work_items=list(work_items),
        worktree_callback=worktree_callback,
        terminal_callback=terminal_callback,
        launch_callback=launch_callback,
        interactions=interactions,
    )
    scenario_state["worktree_calls"] = worktree_calls
    scenario_state["terminal_calls"] = terminal_calls
    scenario_state["launch_calls"] = launch_calls
    scenario_state["terminal_status"] = captured["status"]


@then("the system ensures the worktree for that work item exists")
def _worktree_ensured_for_terminal(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["worktree_calls"], "Expected worktree callback to run"


@then("a terminal is opened in that worktree")
def _terminal_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["terminal_calls"], "Expected terminal callback to run"


@then("no coding agent is started")
def _no_coding_agent_started(scenario_state: dict[str, Any]) -> None:
    # The terminal scenario wires a launch_callback that records any coding
    # agent activation. Assert it stayed untouched: pressing 't' must only
    # open a terminal and never branch into the coding launch path.
    assert scenario_state["launch_calls"] == [], scenario_state["launch_calls"]
    assert scenario_state["terminal_calls"]


@then("the TUI reports that a terminal was opened")
def _tui_reports_terminal(scenario_state: dict[str, Any]) -> None:
    assert "terminal" in scenario_state["terminal_status"].lower(), scenario_state
