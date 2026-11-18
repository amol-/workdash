"""Dashboard feedback for real autodispatch sessions."""

import asyncio
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from gh_work_state_fake import HEAD_SHA
from workdash.tui import WorkdashApp

from .steps.autodispatch import dashboard_dispatch  # noqa: F401
from .steps.common import make_work_item


@pytest.mark.usefixtures("dashboard_dispatch")
@pytest.mark.parametrize("outcome", ["empty", "failure"])
def test_finished_before_start_returns_is_reported(scenario_state, monkeypatch, outcome):
    session = scenario_state["dashboard_session"]
    item = make_work_item(repo="other/repo", number=3)
    session.work_items = [item]
    if outcome == "empty":
        scenario_state["github"].issues["other/repo#3"]["state"] = "CLOSED"
    else:
        scenario_state["zellij_unavailable"] = True
    start = session.start_autodispatch_for_item

    def start_and_wait(item, *, agent):
        result = start(item, agent=agent)
        deadline = time.monotonic() + 2
        while session.autodispatch_sessions()[0]["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return result

    monkeypatch.setattr(session, "start_autodispatch_for_item", start_and_wait)
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test(size=(60, 24)) as pilot:
            await pilot.press("d", "1")
            footer = app.query_one("#status-footer", Static)
            for _ in range(100):
                await pilot.pause(0.02)
                if (
                    "no work available to dispatch" in footer.render().plain
                    or "failed:" in footer.render().plain
                ):
                    break
            status = footer.render().plain
            if outcome == "empty":
                assert "no work available to dispatch" in status
                assert session.autodispatch_sessions()[0]["status"] == "finished"
            else:
                assert "Dispatch failed:" in status
                assert "other/repo#3" in status
                assert "Cannot check for running" in status
                assert session.autodispatch_sessions()[0]["status"] == "failed"

    asyncio.run(exercise())


@pytest.mark.usefixtures("dashboard_dispatch")
def test_completed_work_is_not_reported_as_an_empty_start(scenario_state, monkeypatch):
    session = scenario_state["dashboard_session"]
    session.work_items = [make_work_item(repo="other/repo", number=3)]
    scenario_state["github"].pull_requests.clear()
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            await pilot.press("d", "1")
            for _ in range(100):
                await pilot.pause(0.02)
                if scenario_state.get("agent_launches"):
                    break
            assert scenario_state.get("agent_launches")
            scenario_state["github"].issues["other/repo#3"]["state"] = "CLOSED"
            scenario_state["panes"].clear()
            for _ in range(100):
                await pilot.pause(0.02)
                entries = session.autodispatch_sessions()
                if entries and entries[0]["status"] != "running":
                    break
            assert entries[0]["status"] == "finished", entries
            await pilot.pause(1.1)
            assert (
                "no work available to dispatch"
                not in app.query_one("#status-footer", Static).render().plain
            )

    asyncio.run(exercise())


@pytest.mark.usefixtures("dashboard_dispatch")
def test_running_ready_descendant_is_not_reported_as_missing(scenario_state, monkeypatch, tmp_path):
    session = scenario_state["dashboard_session"]
    session.work_items = [make_work_item(repo="other/repo", number=3)]
    github = scenario_state["github"]
    github.pull_requests.clear()
    github.add_issue("other/repo#4", parent="other/repo#3")
    worktree = tmp_path / "other_repo_4"
    worktree.mkdir()
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path",
        lambda workdir, item, **kwargs: str(worktree) if item.number == 4 else None,
    )
    scenario_state["panes"].append(
        {"id": 42, "title": "code_other_repo_4", "pane_cwd": str(worktree)}
    )
    assert [work.item.number for work in session._ready("other/repo#3", True, panes=[])] == [4]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("d", "1")
            footer = app.query_one("#status-footer", Static)
            for _ in range(100):
                await pilot.pause(0.02)
                if "no work available to dispatch" in footer.render().plain:
                    break
            assert "no work available to dispatch" in footer.render().plain
            assert session.autodispatch_sessions()[0]["status"] == "finished"
            assert not scenario_state.get("agent_launches")
            assert scenario_state["panes"][0]["id"] == 42

    asyncio.run(exercise())


@pytest.mark.usefixtures("dashboard_dispatch")
def test_dashboard_selects_agent_or_cancels(scenario_state, monkeypatch, tmp_path):
    from workdash.config import AgentConfig

    session = scenario_state["dashboard_session"]
    session.work_items = [make_work_item(repo="other/repo", number=3)]
    session.config.agents.clear()
    session.config.agents.update(
        {
            "first": AgentConfig(launch="first", analyze="first -p"),
            "code-only": AgentConfig(launch="code"),
            "analyze-only": AgentConfig(analyze="analyze -p"),
            "second": AgentConfig(launch="second", analyze="second -p"),
        }
    )
    scenario_state["github"].pull_requests.clear()
    session.backend.resolve_agent_command_tokens.side_effect = lambda agent: [agent]
    session.backend.resolve_analyze_command_tokens.side_effect = lambda agent: [agent, "-p"]
    monkeypatch.setattr(
        session.backend.github_client, "fetch_launch_context", lambda item: {"state": "OPEN"}
    )
    exclude = tmp_path / "exclude"
    session.git.get_git_path.return_value = exclude
    reviews = []

    def run_review(*, prompt, command_tokens, **kwargs):
        reviews.append(command_tokens)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Review outcome: success\nNo major findings"
        )
        return ""

    original_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[:3] == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(stdout=HEAD_SHA)
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("workdash.agent_execution.executor.run_in_pane", run_review)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            await pilot.press("d")
            lines = [widget.render().plain for widget in app.screen.query(".review-line")]
            assert "(1) first" in lines and "(2) second" in lines
            assert not any("code-only" in line or "analyze-only" in line for line in lines)
            assert not session.autodispatch_sessions()
            await pilot.press("escape")
            assert not session.autodispatch_sessions()
            await pilot.press("d", "2")
            for _ in range(100):
                await pilot.pause(0.02)
                if scenario_state.get("agent_launches"):
                    break
            assert scenario_state["agent_launches"][0][2] == ["second"]
            assert "AUTOSTART: IMPLEMENT" in scenario_state["agent_launches"][0][1]
            scenario_state["github"].add_pull_request(
                "owner/repo#9", closes=("other/repo#3",), mergeable="CONFLICTING"
            )
            scenario_state["panes"].clear()
            for _ in range(100):
                await pilot.pause(0.02)
                if len(scenario_state["agent_launches"]) >= 2:
                    break
            assert reviews == [["second", "-p"]]
            assert scenario_state["agent_launches"][1][2] == ["second"]
            assert "merge conflicts" in scenario_state["agent_launches"][1][1]

    asyncio.run(exercise())
