"""Review journeys through the CLI and dashboard."""

import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, then, when
from textual.widgets import Static

import workdash.agent_execution as agent_execution
import workdash.control as control
import workdash.workdash as cli
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import format_work_item_id
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import BusyScreen, CodeDialog, ReviewDialog

from .common import run_app


@pytest.fixture
def review_state(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    now = datetime.now(UTC)
    pr = WorkItem(
        WorkItemKind.TRACKED_PR,
        WorkItemType.PR,
        "owner/repo",
        2,
        "PR",
        now,
        now,
        "https://github.com/owner/repo/pull/2",
    )
    backend = MagicMock()
    backend.github_client.work_pull_request.return_value.state = "OPEN"
    backend.github_client.work_pull_request.return_value.head_sha = head
    backend.github_client.fetch_launch_context.return_value = {
        "state": "OPEN",
        "author": {"login": "someone"},
    }
    backend.resolve_analyze_command_tokens.return_value = ["agent", "-p"]
    session = control.WorkdashSession(
        config=WorkdashConfig(
            github_username="me",
            agents={
                "claude": AgentConfig(analyze="claude -p"),
                "agent": AgentConfig(analyze="agent -p"),
            },
        ),
        backend=backend,
        work_items=[pr],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    state = {"session": session, "pane_ready": threading.Event()}
    monkeypatch.setattr(control, "ensure_worktree", lambda *args, **kwargs: str(tmp_path))

    def run_in_pane(*, prompt, **kwargs):
        path = re.search(r"completion document at (\S+)", prompt).group(1)
        Path(path).write_text("Review outcome: success\nOutcome: approved\nInline comments: none")
        state["document"] = path
        state["pane_ready"].set()
        return ""

    monkeypatch.setattr(agent_execution.executor, "run_in_pane", run_in_pane)
    return state


@given("an open pull request is available for review")
def pr_available(review_state):
    pass


@when("the user runs `workdash review owner/repo#PR-2`")
def start_review(review_state, monkeypatch, capsys):
    def request(client, endpoint, payload):
        assert endpoint == "review"
        return review_state["session"].review(**payload, background=True)

    monkeypatch.setattr(control.WorkdashControlClient, "request", request)
    assert cli.main(["review", "owner/repo#PR-2"]) == 0
    review_state["output"] = capsys.readouterr().out
    assert review_state["pane_ready"].wait(5), "review pane did not open"


@then("the command reports that the review started and where to find its document")
def review_started(review_state):
    assert "Review started: owner/repo#PR-2" in review_state["output"]
    assert f"Document (on server): {review_state['document']}" in review_state["output"]


@when("the user presses `c`, `r`, then `2` to choose a review agent")
def review_from_code_menu(review_state):
    session = review_state["session"]
    release = threading.Event()

    def review_callback(item, tool):
        assert release.wait(5)
        return session.review(target=format_work_item_id(item), agent=tool)

    async def interactions(app, pilot):
        await pilot.press("c")
        assert isinstance(app.screen, CodeDialog)
        await pilot.press("r")
        assert isinstance(app.screen, ReviewDialog)
        review_state["review_dialog_lines"] = [
            widget.render().plain for widget in app.screen.query(".review-line")
        ]
        try:
            await pilot.press("2")
            assert isinstance(app.screen, BusyScreen)
            review_state["busy_screen_message"] = (
                app.screen.query_one("#busy-message", Static).render().plain
            )
        finally:
            release.set()
        footer = app.query_one("#status-footer", Static)
        for _ in range(40):
            review_state["review_status"] = footer.render().plain
            if review_state["review_status"].startswith("Reviewed"):
                break
            await pilot.pause()
        assert len(app.screen_stack) == 1

    run_app(
        work_items=session.work_items,
        session=session,
        config=session.config,
        review_callback=review_callback,
        interactions=interactions,
    )


@then("the code menu offers a choice of analysis agent")
def review_agent_choice(review_state):
    assert review_state["review_dialog_lines"] == [
        "Review with:",
        "(1) claude",
        "(2) agent",
        "(Esc) Cancel",
    ]


@then("the dashboard shows a progress dialog until the review finishes")
def review_shows_progress(review_state):
    assert review_state["busy_screen_message"] == "Reviewing with agent..."


@then("the dashboard reports the reviewed pull request with the agent that reviewed it")
def review_reports_completion(review_state):
    assert review_state["review_status"] == "Reviewed pr owner/repo#2 with agent."
