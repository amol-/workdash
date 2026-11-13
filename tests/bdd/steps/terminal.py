"""Step definitions for the open-terminal feature."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import Static

from workdash.agent_execution import executor
from workdash.control import WorkdashSession, format_work_item_id
from workdash.launcher import _zellij_pane_name_for_work_action, launch_terminal_context
from workdash.models import WorkItem

from .common import make_valid_config, make_work_item, modal_screen_names, run_app


@given("the next terminal launch will fail")
def _next_terminal_launch_will_fail(scenario_state: dict[str, Any]) -> None:
    scenario_state["terminal_launch_fails"] = True


@given(parsers.parse('the selected work item uses the worktree directory "{worktree_dir}"'))
def _selected_work_item_uses_worktree_directory(
    worktree_dir: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    worktree_path = tmp_path / worktree_dir
    worktree_path.mkdir()
    if not work_items:
        work_items.append(make_work_item())
    scenario_state["selected_item"] = work_items[0]
    scenario_state["pane_title_worktree_path"] = str(worktree_path)


@when(parsers.parse('the user launches the "{action}" terminal-backed work action'))
def _launch_named_terminal_backed_work_action(
    action: str,
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

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

    repo_path = scenario_state["pane_title_worktree_path"]
    if action == "code":
        executor.launch(repo_path, "work on this item", agent_command_tokens=["claude"])
    elif action == "terminal":
        launch_terminal_context(repo_path)
    else:
        raise AssertionError(f"Unsupported terminal-backed work action: {action}")
    scenario_state["zellij_pane_title_commands"] = commands


@then(parsers.parse('the new Zellij pane is named "{pane_name}"'))
def _new_zellij_pane_named(pane_name: str, scenario_state: dict[str, Any]) -> None:
    commands = scenario_state["zellij_pane_title_commands"]
    assert len(commands) == 1
    command = commands[0]
    assert Path(command[0]).name == "zellij"
    assert command[1:3] == ["action", "new-pane"]
    name_index = command.index("--name")
    assert command[name_index + 1] == pane_name


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
    if scenario_state.get("direct_mode"):
        assert scenario_state.get("outside_zellij") is True
    if not work_items:
        work_items.append(make_work_item())
    # A dashboard without a known Zellij session name relies on the ZELLIJ environment.
    run_terminal_scenario(scenario_state, work_items, monkeypatch, tmp_path, zellij_session=None)


@then("the work action opens in the current Zellij session")
def _work_action_opens_current_zellij_session(scenario_state: dict[str, Any]) -> None:
    commands = scenario_state["terminal_commands"]
    assert len(commands) == 1
    command = commands[0]
    assert Path(command[0]).name == "zellij"
    assert command[1:3] == ["action", "new-pane"]
    cwd_index = command.index("--cwd")
    assert command[cwd_index : cwd_index + 3] == ["--cwd", scenario_state["worktree_path"], "--"]


@then("the system does not target the shared `workdash` Zellij session")
def _does_not_target_shared_zellij_session(scenario_state: dict[str, Any]) -> None:
    command_text = " ".join(scenario_state["terminal_commands"][0])
    assert "--session workdash" not in command_text


@then("the system reports that terminal-backed work actions require an active Zellij session")
def _reports_terminal_backed_actions_require_zellij(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["terminal_commands"] == []
    assert (
        "terminal-backed work actions require an active Zellij session"
        in scenario_state["terminal_status"]
    )


def _scenario_panes(
    scenario_state: dict[str, Any], worktree_path: Path, tmp_path: Path
) -> list[dict[str, object]]:
    """Return the Zellij panes the scenario's fake ``list-panes`` reports."""

    terminal_title = _zellij_pane_name_for_work_action("terminal", str(worktree_path))
    if scenario_state.get("existing_terminal"):
        return [
            {"id": 4, "title": "code_wt", "pane_cwd": str(worktree_path)},
            {
                "id": 5,
                "title": "amol@host: cwd",
                "pane_command": "/bin/bash -i",
                "pane_cwd": str(worktree_path),
            },
        ]
    if scenario_state.get("running_terminal"):
        return [
            {
                "id": 5,
                "title": terminal_title,
                "pane_command": "sleep 300",
                "pane_cwd": str(worktree_path),
            }
        ]
    if scenario_state.get("terminal_left_worktree"):
        return [
            {
                "id": 5,
                "title": terminal_title,
                "pane_command": "/bin/bash -i",
                "pane_cwd": str(tmp_path.parent),
            }
        ]
    if scenario_state.get("terminal_in_same_named_worktree"):
        other_worktree = tmp_path / "other" / worktree_path.name
        other_worktree.mkdir(parents=True)
        return [
            {
                "id": 5,
                "title": _zellij_pane_name_for_work_action("terminal", str(other_worktree)),
                "pane_command": "/bin/bash -i",
                "pane_cwd": str(other_worktree),
            }
        ]
    if scenario_state.get("other_programs"):
        return [
            {
                "id": pane_id,
                "title": command,
                "pane_command": command,
                "pane_cwd": str(worktree_path),
            }
            for pane_id, command in [(6, "nvim"), (7, "pytest")]
        ]
    return []


def run_terminal_scenario(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    zellij_session: str | None = "workdash-main",
) -> None:
    """Drive pressing 't' in the TUI and record worktree + terminal calls.

    The TUI is wired to a real ``WorkdashSession`` exactly as ``workdash.workdash``
    wires it, so the shared ``terminal()`` path prepares the worktree and launches
    the terminal. Only worktree preparation and the ``zellij`` process are faked.
    """

    import workdash.control as control_module

    worktree_calls: list[WorkItem] = []
    terminal_commands: list[list[str]] = []
    # The terminal flow must never invoke a coding-agent launch. We still
    # wire a launch_callback so that if the TUI mistakenly dispatched to
    # coding on 't', the appended entry would make the "no coding agent is
    # started" Then step fail loudly.
    launch_calls: list[tuple[WorkItem, str]] = []
    captured: dict[str, Any] = {}
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir(exist_ok=True)
    panes = _scenario_panes(scenario_state, worktree_path, tmp_path)
    # A transient failure: only the advisory check for an existing terminal fails.
    lookup_failures = 1 if scenario_state.get("terminal_lookup_fails") else 0

    def ensure_worktree(_workdir: str, item: WorkItem, **_kwargs) -> str:
        if scenario_state.get("worktree_fails"):
            raise RuntimeError("worktree failed")
        worktree_calls.append(item)
        return str(worktree_path)

    def fake_run(command, **kwargs):
        nonlocal lookup_failures
        if "list-panes" in command:
            if lookup_failures:
                lookup_failures -= 1
                raise subprocess.CalledProcessError(
                    1, command, stderr="Failed to list Zellij panes: session not found"
                )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(panes), stderr="")
        if scenario_state.get("terminal_launch_fails"):
            raise subprocess.CalledProcessError(1, command, stderr="terminal launch failed")
        terminal_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(control_module, "ensure_worktree", ensure_worktree)
    monkeypatch.setattr(
        control_module, "existing_worktree_path", lambda *args, **kwargs: worktree_path
    )
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zellij" if name == "zellij" else None
    )
    monkeypatch.setattr(subprocess, "run", fake_run)
    # Worktrees live under the configured workdir, so the scenario's worktree and
    # its same-named sibling live there too.
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(analysis_cache=None, github_client=None),  # type: ignore[arg-type]
        work_items=list(work_items),
        suggestion_markers={},
        zellij_session=zellij_session,
    )

    focused_panes = []

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item, tool))

    async def interactions(app, pilot) -> None:
        await pilot.press("c", "t")
        for _ in range(40):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if "Opened terminal" in status or "Terminal failed" in status or "Focused" in status:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain
        captured["modal_screen_names"] = modal_screen_names(app)

    run_app(
        work_items=list(work_items),
        terminal_callback=lambda item: session.terminal(target=format_work_item_id(item)),
        launch_callback=launch_callback,
        session=session,
        focus_callback=focused_panes.append,
        interactions=interactions,
    )
    scenario_state["focused_panes"] = focused_panes
    scenario_state["worktree_calls"] = worktree_calls
    scenario_state["worktree_path"] = str(worktree_path)
    scenario_state["terminal_commands"] = terminal_commands
    scenario_state["terminal_calls"] = [
        command[command.index("--cwd") + 1] for command in terminal_commands
    ]
    scenario_state["launch_calls"] = launch_calls
    scenario_state["terminal_status"] = captured["status"]
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


@then("the system ensures the worktree for that work item exists")
def _worktree_ensured_for_terminal(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["worktree_calls"], "Expected worktree callback to run"


@then("a terminal is opened in that worktree")
def _terminal_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["terminal_calls"] == [scenario_state["worktree_path"]]


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


@then("the system reports the terminal launch error details to the user")
def _reports_terminal_launch_error_details(scenario_state: dict[str, Any]) -> None:
    status = scenario_state["terminal_status"]
    assert status.startswith("Terminal failed:")
    assert "terminal launch failed" in status


@then("no terminal is opened")
def _no_terminal_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["terminal_calls"] == []


@given("a plain shell is already open in that worktree alongside a coding agent")
def _existing_terminal(scenario_state):
    scenario_state["existing_terminal"] = True


@given("a Workdash terminal is running a program in that worktree")
def _running_terminal(scenario_state):
    scenario_state["running_terminal"] = True


@given("a Workdash terminal in that worktree changed to another directory")
def _terminal_left_worktree(scenario_state):
    scenario_state["terminal_left_worktree"] = True


@then("the existing terminal is focused without opening another")
def _focused_existing_terminal(scenario_state):
    assert scenario_state["focused_panes"] == ["terminal_5"]
    assert scenario_state["terminal_commands"] == []
    assert scenario_state["worktree_calls"] == []


@given("an editor and a test runner are open in that worktree")
def _other_programs(scenario_state):
    scenario_state["other_programs"] = True


@given("the next check for an existing terminal will fail")
def _terminal_lookup_fails(scenario_state):
    scenario_state["terminal_lookup_fails"] = True


@given(
    "a terminal is open in another worktree under the working directory "
    "whose directory has the same name"
)
def _terminal_in_same_named_worktree(scenario_state):
    scenario_state["terminal_in_same_named_worktree"] = True
