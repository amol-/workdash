"""Step definitions for the open-terminal feature."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when
from textual.widgets import Static

from workdash.launcher import launch_terminal_context
from workdash.models import WorkItem

from .common import make_work_item, run_app


@given("the dashboard was started with `--direct`")
def _dashboard_started_direct(scenario_state: dict[str, Any]) -> None:
    scenario_state["direct_mode"] = True


@when("the user launches a terminal-backed work action")
def _launch_terminal_backed_work_action(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    terminal_calls: list[WorkItem] = []
    captured: dict[str, str] = {}

    def fake_which(name: str) -> str | None:
        if name == "zellij":
            return f"/usr/bin/{name}"
        return None

    def fake_run(*args, **kwargs):
        commands.append(args[0])
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(subprocess, "run", fake_run)
    if scenario_state.get("direct_mode"):
        assert scenario_state.get("outside_zellij") is True

    if not work_items:
        work_items.append(make_work_item())

    def worktree_callback(item: WorkItem) -> str:
        repo_path = tmp_path / "repo"
        repo_path.mkdir(exist_ok=True)
        captured["repo_path"] = str(repo_path)
        return str(repo_path)

    def terminal_callback(item: WorkItem) -> None:
        terminal_calls.append(item)
        launch_terminal_context(captured["repo_path"])

    async def interactions(app, pilot) -> None:
        await pilot.press("t")
        for _ in range(40):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if commands or "Terminal failed:" in status:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain

    run_app(
        work_items=list(work_items),
        worktree_callback=worktree_callback,
        terminal_callback=terminal_callback,
        interactions=interactions,
    )

    scenario_state["zellij_routing_commands"] = commands
    scenario_state["zellij_routing_repo_path"] = captured["repo_path"]
    scenario_state["zellij_routing_terminal_calls"] = terminal_calls
    scenario_state["zellij_routing_status"] = captured["status"]


@then("the work action opens in the current Zellij session")
def _work_action_opens_current_zellij_session(scenario_state: dict[str, Any]) -> None:
    commands = scenario_state["zellij_routing_commands"]
    repo_path = scenario_state["zellij_routing_repo_path"]
    assert scenario_state["zellij_routing_terminal_calls"]
    assert len(commands) == 1
    assert Path(commands[0][0]).name == "zellij"
    assert commands[0][1:6] == ["action", "new-pane", "--cwd", repo_path, "--"]


@then("the system does not target the shared `workdash` Zellij session")
def _does_not_target_shared_zellij_session(scenario_state: dict[str, Any]) -> None:
    command_text = " ".join(scenario_state["zellij_routing_commands"][0])
    assert "--session workdash" not in command_text


@then("the system reports that terminal-backed work actions require an active Zellij session")
def _reports_terminal_backed_actions_require_zellij(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["zellij_routing_commands"] == []
    assert (
        "terminal-backed work actions require an active Zellij session"
        in scenario_state["zellij_routing_status"]
    )


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
