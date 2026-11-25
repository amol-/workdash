"""Drive the dashboard terminal menu and the open shell."""

import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, then, when
from textual.widgets import Static

from workdash.control import WorkdashSession, format_work_item_id
from workdash.launcher import find_zellij_pane, is_live_zellij_pane, launch_terminal_context, zellij
from zellij_pane_launch import mark_pane_started, pane_launch

from .common import make_valid_config, run_app


@given("the selected item does not have a worktree yet")
def _no_worktree(tmp_path, scenario_state):
    worktree = tmp_path / "owner_repo_42"
    assert not worktree.exists()
    scenario_state["terminal_worktree"] = worktree


@given("a plain shell is already open in that item's worktree")
def _existing_shell(tmp_path, scenario_state):
    worktree = tmp_path / "owner_repo_42"
    worktree.mkdir()
    scenario_state["terminal_worktree"] = worktree
    scenario_state["existing_shell"] = {
        "id": 5,
        "title": "amol@host: cwd",
        "pane_command": "/bin/sh -i",
        "pane_cwd": str(worktree),
    }


@when('the user presses "c" then "t" to open a terminal')
def _choose_terminal(scenario_state, work_items, tmp_path, monkeypatch):
    launches = []
    item = scenario_state["selected_item"]
    worktree = scenario_state["terminal_worktree"]
    existing_shell = scenario_state.get("existing_shell")
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=work_items,
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    session.git = MagicMock()
    session.git.worktree_proves_item.side_effect = lambda path, candidate: (
        path == worktree and candidate == item
    )
    session.git.find_worktree_for_branch.return_value = None
    session.git.create_worktree.side_effect = lambda _main, path, *_: path.mkdir()

    def launch_pane(command, **_kwargs):
        args, program = pane_launch(command)
        cwd = Path(args[args.index("--cwd") + 1])
        assert cwd.is_dir(), "the worktree must exist when the shell opens"
        launches.append((cwd, program))
        mark_pane_started(command, "22")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zellij" if name == "zellij" else None
    )
    monkeypatch.setattr(subprocess, "run", launch_pane)
    monkeypatch.setattr(
        zellij, "load_zellij_panes", lambda _: [existing_shell] if existing_shell else []
    )
    focus = MagicMock()
    monkeypatch.setattr(zellij, "focus_zellij_pane", focus)
    agent_launch = MagicMock()
    expected_status = (
        "Focused pane terminal_5."
        if existing_shell
        else f"Opened terminal for {item.item_type.value} {item.repo}#{item.number}."
    )

    async def interactions(app, pilot):
        footer = app.query_one("#status-footer", Static)
        await pilot.press("c", "t")
        for _ in range(20):
            if footer.render().plain == expected_status:
                break
            await pilot.pause()
        scenario_state["terminal_status"] = footer.render().plain

    run_app(
        work_items=work_items,
        session=session,
        terminal_callback=lambda item: session.terminal(target=format_work_item_id(item)),
        launch_callback=agent_launch,
        focus_callback=session.focus_pane,
        interactions=interactions,
    )
    scenario_state["terminal_launches"] = launches
    scenario_state["agent_launch"] = agent_launch
    scenario_state["terminal_focus"] = focus
    scenario_state["terminal_git"] = session.git


@then("a plain shell opens in the selected item's new worktree")
def _plain_shell_opens(scenario_state):
    assert scenario_state["terminal_launches"] == [
        (scenario_state["terminal_worktree"], ["/bin/sh", "-i"])
    ]
    scenario_state["agent_launch"].assert_not_called()


@then("the TUI reports that a terminal was opened")
def _opening_reported(scenario_state):
    assert scenario_state["terminal_status"] == "Opened terminal for issue owner/repo#42."


@then("the existing terminal is focused without opening another")
def _existing_terminal_focused(scenario_state):
    scenario_state["terminal_focus"].assert_called_once_with("workdash-main", "terminal_5")
    assert scenario_state["terminal_status"] == "Focused pane terminal_5."
    assert scenario_state["terminal_launches"] == []
    scenario_state["terminal_git"].pull_ff_only.assert_not_called()
    scenario_state["terminal_git"].create_worktree.assert_not_called()


@given("a Workdash terminal is open in a worktree", target_fixture="terminal_pane")
def _open_terminal_in_worktree(tmp_path, monkeypatch):
    from test_zellij_integration import live_zellij_session

    with live_zellij_session(monkeypatch) as session:
        launch = launch_terminal_context(str(tmp_path), zellij_session=session)
        pane = find_zellij_pane(zellij.load_zellij_panes(session), launch.pane_id)
        assert pane is not None and is_live_zellij_pane(pane)
        yield session, launch.pane_id


@when("the user exits the shell")
def _exit_terminal_shell(terminal_pane):
    session, pane_id = terminal_pane
    zellij.send_zellij_pane_input(session, pane_id, "exit 0")


@then("that terminal pane closes")
def _terminal_closes(terminal_pane):
    session, pane_id = terminal_pane
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if find_zellij_pane(zellij.load_zellij_panes(session), pane_id) is None:
            return
        time.sleep(0.1)
    pytest.fail(f"terminal pane {pane_id} stayed open after exit")


@then("the system reports that terminal-backed work actions require an active Zellij session")
def _reports_terminal_backed_actions_require_zellij(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["terminal_commands"] == []
    assert (
        "terminal-backed work actions require an active Zellij session"
        in scenario_state["terminal_status"]
    )
