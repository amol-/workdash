"""Autonomous review scenarios at external boundaries."""

import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import Static

import workdash.agent_execution as agent_execution
import workdash.control as control
import workdash.launcher as launcher
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import format_work_item_id
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import BusyScreen, CodeDialog, ReviewDialog

from .common import install_pane, run_app


@pytest.fixture
def review_state(monkeypatch, tmp_path, scenario_state):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    now = datetime.now(UTC)
    issue = WorkItem(
        WorkItemKind.TRACKED_ISSUE,
        WorkItemType.ISSUE,
        "owner/repo",
        1,
        "Issue",
        now,
        now,
        "https://github.com/owner/repo/issues/1",
    )
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
    backend.resolve_analyze_command_tokens.return_value = ["agent", "-p"]
    session = control.WorkdashSession(
        config=WorkdashConfig(
            github_username="me", agents={"agent": AgentConfig(analyze="agent -p")}
        ),
        backend=backend,
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session=None if scenario_state.get("no_zellij_session") else "workdash-main",
    )
    worktree = tmp_path / "owner_repo_2"
    worktree.mkdir()
    state = {"worktree": worktree}
    monkeypatch.setattr(control, "ensure_worktree", lambda *args, **kwargs: str(state["worktree"]))
    backend.github_client.fetch_launch_context.return_value = {
        "state": "OPEN",
        "author": {"login": "someone"},
    }
    launches = []

    def run_in_pane(*, item, prompt, command_tokens, cwd, work_action, zellij_session, group):
        launches.append(
            {
                "cwd": cwd,
                "work_action": work_action,
                "pane_name": launcher._zellij_pane_name_for_work_action(work_action, cwd),
                "zellij_session": zellij_session,
                "group": group,
            }
        )
        path = re.search(r"completion document at (\S+)", prompt).group(1)
        Path(path).write_text("Outcome: approved\nInline comments: none")
        return ""

    original_run = agent_execution.executor.run_in_pane
    monkeypatch.setattr(agent_execution.executor, "run_in_pane", run_in_pane)
    state.update(session=session, original_run=original_run, tmp_path=tmp_path, launches=launches)
    return state


@given("an open pull request is available for review")
def pr_available(review_state):
    pass


@when("the reviewer requests an autonomous review")
def review_pr(review_state):
    try:
        review_state["result"] = review_state["session"].review(
            target="owner/repo#PR-2", background=True
        )
    except control.WorkdashControlError as error:
        review_state["result"] = error


@then("the reviewer is told the review started and where its document will be written")
def review_started(review_state):
    assert review_state["result"]["status"] == "started"
    assert (
        Path(review_state["result"]["document_path"]).parent
        == review_state["worktree"] / ".workdash" / "reviews"
    )


@given(parsers.parse('the pull request\'s worktree directory is named "{name}"'))
def worktree_directory_name(review_state, name):
    worktree = review_state["tmp_path"] / name
    worktree.mkdir(exist_ok=True)
    review_state["worktree"] = worktree


@when(parsers.parse('the reviewer requests an autonomous review in group "{group}"'))
def review_pr_in_group(review_state, group):
    review_state["result"] = review_state["session"].review(
        target="owner/repo#PR-2", background=True, group=group
    )


def _wait_for_launch(review_state, timeout=5):
    """Wait for the background review to start its pane."""
    deadline = time.monotonic() + timeout
    while not review_state["launches"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert review_state["launches"], "no review pane was launched"


@then(parsers.parse('the review agent runs in a Zellij pane named "{pane_name}"'))
def review_pane_name(review_state, pane_name):
    _wait_for_launch(review_state)
    (launch,) = review_state["launches"]
    assert launch["pane_name"] == pane_name


@then(parsers.parse('the review pane opens in the Zellij tab named "{tab_name}"'))
def review_pane_tab(review_state, tab_name):
    _wait_for_launch(review_state)
    assert [launch["group"] for launch in review_state["launches"]] == [tab_name]


@then("the reviewer is told that review requires a Zellij session")
def review_needs_zellij(review_state):
    error = review_state["result"]
    assert isinstance(error, control.WorkdashControlError), error
    assert (error.code, error.status) == ("zellij_session_required", 409)
    assert review_state["launches"] == []


@given("an issue has no linked open pull request")
def no_pr(review_state):
    review_state["session"].backend.github_client.linked_open_pull_request.return_value = None


@when("the reviewer requests an autonomous review of the issue")
def review_issue(review_state):
    review_state["result"] = review_state["session"].review(target="owner/repo#1")


@then("the reviewer is told there is nothing to review")
def nothing(review_state):
    assert review_state["result"]["status"] == "nothing_to_review"


@when("the user chooses auto review from the code menu and picks an analysis agent")
def review_from_code_menu(review_state):
    session = review_state["session"]
    pull_request = next(item for item in session.work_items if item.item_type == WorkItemType.PR)
    session.work_items = [pull_request]
    release = threading.Event()

    def review_callback(item, tool):
        # Hold the review open so the pilot can observe the progress dialog.
        release.wait(5)
        return session.review(target=format_work_item_id(item), agent=tool, background=False)

    async def interactions(app, pilot) -> None:
        await pilot.press("c")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, CodeDialog):
                break
        await pilot.press("r")
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, ReviewDialog):
                break
        review_state["review_dialog_lines"] = [
            widget.render().plain for widget in app.screen.query(".review-line")
        ]
        await pilot.press("1")
        for _ in range(40):
            await pilot.pause()
            if isinstance(app.screen, BusyScreen):
                review_state["busy_screen_message"] = (
                    app.screen.query_one("#busy-message", Static).render().plain
                )
                break
        release.set()
        for _ in range(40):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if status.startswith("Reviewed"):
                break
        review_state["review_status"] = app.query_one("#status-footer", Static).render().plain

    run_app(
        work_items=[pull_request],
        session=session,
        config=session.config,
        review_callback=review_callback,
        interactions=interactions,
    )


@then("the code menu asks which analysis agent reviews the pull request")
def review_agent_choice(review_state):
    assert review_state["review_dialog_lines"] == ["Review with:", "(1) agent", "(Esc) Cancel"]


@then("the dashboard shows a progress dialog until the review finishes")
def review_shows_progress(review_state):
    assert review_state["busy_screen_message"] == "Reviewing with agent..."


@then("the dashboard reports the reviewed pull request with the agent that reviewed it")
def review_reports_completion(review_state):
    assert review_state["review_status"] == "Reviewed pr owner/repo#2 with agent."


@given("an open pull request review is in progress")
def running_review(review_state, scenario_state, monkeypatch):
    import socket
    import sys

    tmp_path = review_state["tmp_path"]
    started, release, posted = (tmp_path / name for name in ("started", "release", "posted"))
    script = (
        "import pathlib, re, sys, time\n"
        "started, release, posted = map(pathlib.Path, sys.argv[1:4])\n"
        "started.touch()\n"
        "while not release.exists(): time.sleep(0.01)\n"
        "posted.touch()\n"
        "path = re.search(r'completion document at (\\S+)', sys.argv[4]).group(1)\n"
        "pathlib.Path(path).write_text('Outcome: approved\\nInline comments: none')\n"
    )
    review_state["session"].backend.resolve_analyze_command_tokens.return_value = [
        sys.executable,
        "-c",
        script,
        str(started),
        str(release),
        str(posted),
    ]
    monkeypatch.setattr(agent_execution.executor, "run_in_pane", review_state["original_run"])
    install_pane(scenario_state, monkeypatch)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    server = control.WorkdashControlServer(review_state["session"])
    server.start()
    review_state["server"] = server
    result = []

    def request_review():
        try:
            result.append(
                control.WorkdashControlClient().request("review", {"target": "owner/repo#PR-2"})
            )
        except control.WorkdashControlError as error:
            result.append(error)

    thread = threading.Thread(target=request_review)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        thread.join(2)
        assert not thread.is_alive()
        review_state.update(release=release, posted=posted, result=result)
        yield
    finally:
        release.touch()
        thread.join(10)
        server.stop()
    assert not thread.is_alive()
    assert result[0]["status"] == "started"


@when("the reviewer lists work items")
def list_during_review(review_state):
    import json
    from urllib.request import Request, urlopen

    request = Request(
        f"http://{control.server_address()[0]}:{control.server_address()[1]}/api/v0/list",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        review_state["list"] = json.load(response)["result"]


@then("the list is returned before the review finishes")
def list_returned(review_state):
    assert {item["id"] for item in review_state["list"]["items"]} == {
        "owner/repo#ISSUE-1",
        "owner/repo#CHECK-2",
    }


@when("the reviewer requests the same review again")
def repeat_review(review_state):
    review_state["repeat"] = control.WorkdashControlClient().request(
        "review", {"target": "owner/repo#PR-2"}
    )


@then("the reviewer receives the same started information")
def same_review(review_state):
    assert review_state["repeat"] == review_state["result"][0]
