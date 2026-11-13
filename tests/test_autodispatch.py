import asyncio
import json
import queue
import re
import socket
import subprocess
import threading
import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from textual.widgets import DataTable, Static
from webob import Request

from gh_work_state_fake import HEAD_SHA, check_run, install_fake_github, todo_body
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashControlError, WorkdashSession, _make_turbogears_app
from workdash.github_client import GitHubClient
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import _MAX_PROGRESS_ERROR_WIDTH, WorkdashApp
from workdash.work_state import ReadyWork


def _issue_item(number: int) -> WorkItem:
    now = datetime.now(UTC)
    return WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=number,
        title="Fix the thing",
        created_at=now,
        updated_at=now,
        url=f"https://github.com/owner/repo/issues/{number}",
    )


def test_api_starts_reports_and_stops_session_without_closing_pane(monkeypatch):
    waits = queue.Queue()
    original_wait = threading.Event.wait

    def observe_wait(event, timeout=None):
        if threading.current_thread().name.startswith("autodispatch-"):
            waits.put(timeout)
        return original_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", observe_wait)
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(
        session,
        "dispatch_once",
        lambda **kwargs: {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "workdash",
        },
    )
    panes = [{"id": 1, "title": "code_test", "exited": False}]
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: panes)
    monkeypatch.setattr("workdash.control.launcher.zellij.load_zellij_panes", lambda _: panes)
    close_pane = MagicMock()
    monkeypatch.setattr("workdash.control.launcher.zellij.close_zellij_pane", close_pane)
    app = _make_turbogears_app(session)

    def request(endpoint, payload):
        req = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        )
        resp = req.get_response(app)
        return resp.status_int, json.loads(resp.text)

    status, error = request("autodispatch", {"scope": "owner/repo", "group": "  "})
    assert status == 400 and error["error"]["code"] == "bad_request"
    status, error = request("autodispatch", {"scope": "owner/repo", "group": None})
    assert status == 400 and error["error"]["code"] == "bad_request"
    status, started = request(
        "autodispatch", {"scope": "owner/repo", "group": "team", "agent": "pi"}
    )
    assert status == 200
    session_id = started["result"]["id"]
    assert waits.get(timeout=2) == 30
    status, info = request("info", {})
    assert status == 200, info
    entry = info["result"]["autodispatch_sessions"][0]
    assert entry["id"] == session_id
    assert (entry["scope"], entry["group"], entry["agent"], entry["current_item"]) == (
        "owner/repo",
        "team",
        "pi",
        "owner/repo#ISSUE-1",
    )
    status, stopped = request("autodispatch_stop", {"id": session_id})
    assert status == 200 and stopped["result"]["status"] == "stopped"
    thread = session._autodispatch[session_id].thread
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert panes == [{"id": 1, "title": "code_test", "exited": False}]
    close_pane.assert_not_called()


@pytest.mark.parametrize("group", [None, "team"])
def test_api_dispatches_real_ready_work_until_no_issues_remain(monkeypatch, tmp_path, group):
    github = install_fake_github(monkeypatch)
    issues = [github.add_issue(f"owner/repo#{n}") for n in (1, 2)]
    now = datetime.now(UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title=f"Issue {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in (1, 2)
    ]
    panes = []
    launches = []

    def worktree(_workdir, item, **_kwargs):
        return str(tmp_path / f"owner_repo_{item.number}")

    def launch(repo, prompt, **kwargs):
        repo = Path(repo)
        pane = {"id": len(launches) + 1, "title": f"code_{repo.name}", "pane_cwd": str(repo)}
        launches.append((repo, prompt, kwargs))
        panes.append(pane)
        return SimpleNamespace(
            session="workdash", pane_id=None, pane_title=pane["title"], cwd=str(repo)
        )

    monkeypatch.setattr("workdash.control.existing_worktree_path", worktree)
    monkeypatch.setattr("workdash.control.ensure_worktree", worktree)
    monkeypatch.setattr("workdash.control.get_merge_base", lambda _, **kwargs: None)
    from workdash.github_client import GitHubClient

    github_client = GitHubClient()
    original_run = subprocess.run
    launch_reads = []

    def run(command, *args, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            launch_reads.append(command)
            if len(launch_reads) == 1:
                raise subprocess.CalledProcessError(1, command, stderr="HTTP 503")
            return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    polls = queue.Queue()

    def load_panes(_):
        if threading.current_thread().name.startswith("autodispatch-") and panes:
            polls.put(None)
        return list(panes)

    monkeypatch.setattr("workdash.control.launcher.zellij.load_zellij_panes", load_panes)
    monkeypatch.setattr("workdash.agent_execution.executor.launch", launch)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            workdir=str(tmp_path),
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=github_client),
        work_items=items,
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(session, "review", MagicMock(return_value={"status": "nothing_to_review"}))
    app = _make_turbogears_app(session)
    request = Request.blank(
        "/api/v0/autodispatch",
        method="POST",
        content_type="application/json",
        body=json.dumps(
            {"scope": "owner/repo", "agent": "pi", **({"group": group} if group else {})}
        ).encode(),
    )
    response = request.get_response(app)
    assert response.status_int == 200
    session_id = json.loads(response.text)["result"]["id"]

    def wait_for(item=None, status="running"):
        deadline = time.monotonic() + 3
        while True:
            entry = session.autodispatch_sessions()[0]
            assert entry["status"] != "failed", entry["error"]
            if entry["status"] == status and entry["current_item"] == item:
                return entry
            assert time.monotonic() < deadline, entry
            time.sleep(0.01)

    try:
        entry = wait_for("owner/repo#ISSUE-1")
        assert entry["id"] == session_id and entry["group"] == group
        assert (
            entry["selected_group"],
            entry["stage"],
            entry["launch_reason"],
            entry["iteration"],
        ) == (group or "repo", "developing", "implement", 1)
        assert [work["issue_id"] for work in session.ready(scope="owner/repo")["items"]] == [
            "owner/repo#ISSUE-2"
        ]
        while not polls.empty():
            polls.get_nowait()
        polls.get(timeout=3)
        polls.get(timeout=3)
        assert len(launch_reads) == 2
        assert len(launches) == 1 and launches[0][2]["group"] == (group or "repo")
        panes.clear()
        deadline = time.monotonic() + 3
        while len(launches) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert launches[1][0].name == "owner_repo_1"
        while not polls.empty():
            polls.get_nowait()
        polls.get(timeout=3)
        polls.get(timeout=3)
        issues[0]["state"] = "CLOSED"
        panes.clear()
        entry = wait_for("owner/repo#ISSUE-2")
        assert entry["iteration"] == 3
        assert len(launches) == 3 and launches[2][2]["group"] == (group or "repo")
        assert [launch[0].name for launch in launches] == [
            "owner_repo_1",
            "owner_repo_1",
            "owner_repo_2",
        ]
        assert all("AUTOSTART: IMPLEMENT" in launch[1] for launch in launches)
        issues[1]["state"] = "CLOSED"
        panes.clear()
        wait_for(status="finished")
        assert len(launches) == 3
    finally:
        session.stop_autodispatch_sessions()


@pytest.mark.parametrize("targeted_todo", [False, True])
def test_dashboard_refreshes_elapsed_time_and_shows_real_ci_dispatch(
    monkeypatch, tmp_path, targeted_todo
):
    github = install_fake_github(monkeypatch)
    issue = "testuser/todos#1" if targeted_todo else "owner/repo#1"
    github.add_issue(issue, body=todo_body("owner/repo") if targeted_todo else "")
    github.add_pull_request(
        "owner/repo#51",
        closes=(issue,),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    client = GitHubClient()
    monkeypatch.setattr(client, "fetch_launch_context", lambda _: {})
    monkeypatch.setattr(client, "fetch_worktree_head", lambda _: ("branch", "owner/repo"))
    worktree = str(tmp_path / "owner_repo_1")
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **k: worktree)
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *a, **k: worktree)
    monkeypatch.setattr("workdash.control.get_merge_base", lambda *a, **k: None)
    panes = []
    prompts = []

    def launch(repo, prompt, **kwargs):
        prompts.append(prompt)
        panes.append({"id": 1, "title": "code_owner_repo_1", "pane_cwd": str(repo)})
        return SimpleNamespace(
            session="workdash", pane_id=None, pane_title=panes[0]["title"], cwd=str(repo)
        )

    monkeypatch.setattr("workdash.launcher.zellij.load_zellij_panes", lambda _: list(panes))
    monkeypatch.setattr("workdash.agent_execution.executor.launch", launch)
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            todo_repository="testuser/todos",
            workdir=str(tmp_path),
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=client),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.git = MagicMock()
    scope = "https://github.com/testuser/todos/issues/1" if targeted_todo else "owner/repo"
    session.start_autodispatch(scope=scope)
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test(size=(120, 30)) as pilot:
            deadline = time.monotonic() + 5
            initial = None
            while time.monotonic() < deadline:
                rows = [strip.text for strip in app.screen._compositor.render_strips()]
                row = next((row for row in rows if "fix ci" in row), "")
                if targeted_todo and row:
                    assert "testuser/todos#1" in row
                    assert "owner/repo#ISSUE-WT1" not in row
                match = re.search(r"1/(\d+)s", row)
                if match:
                    elapsed = int(match.group(1))
                    if initial is None:
                        initial = elapsed
                    elif elapsed > initial:
                        return
                await pilot.pause(0.1)
            pytest.fail(f"Dashboard did not show advancing CI dispatch: {rows}")

    try:
        asyncio.run(exercise())
        assert len(prompts) == 1 and "tests" in prompts[0]
        entry = session.autodispatch_sessions()[0]
        assert (entry["current_item"], entry["launch_reason"]) == (
            "owner/repo#ISSUE-WT1" if targeted_todo else "owner/repo#ISSUE-1",
            "fix_ci",
        )
    finally:
        session.stop_autodispatch_sessions()
        next(iter(session._autodispatch.values())).thread.join(timeout=3)


@pytest.mark.parametrize("group", [None, "team"])
def test_checking_progress_before_first_launch(monkeypatch, group):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    entered, release = threading.Event(), threading.Event()

    def dispatch(**kwargs):
        entered.set()
        assert release.wait(3)
        return {"status": "no_ready_task"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    session.start_autodispatch(scope="owner/repo", group=group)
    try:
        assert entered.wait(3)
        entry = session.autodispatch_sessions()[0]
        assert (entry["selected_group"], entry["stage"], entry["iteration"]) == (
            group,
            "checking",
            1,
        )
    finally:
        release.set()
        next(iter(session._autodispatch.values())).thread.join(timeout=3)


@pytest.mark.parametrize("group", [None, "team"])
def test_checking_next_task_retains_previous_group(monkeypatch, group):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def dispatch(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "group": "previous-task",
                "pane_id": "terminal_1",
                "session": "workdash",
            }
        entered.set()
        assert release.wait(3)
        return {"status": "no_ready_task"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr(session, "review", MagicMock(return_value={"status": "nothing_to_review"}))
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo", group=group)
    thread = next(iter(session._autodispatch.values())).thread
    try:
        assert entered.wait(3)
        entry = session.autodispatch_sessions()[0]
        assert (
            entry["stage"],
            entry["iteration"],
            entry["current_item"],
            entry["selected_group"],
        ) == (
            "checking",
            2,
            None,
            "previous-task",
        )
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_failed_pane_lookup_fails_session_without_dispatching_again(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock(
        return_value={
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "workdash",
        }
    )
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "workdash.autodispatch.launcher.zellij.load_zellij_panes",
        MagicMock(side_effect=RuntimeError("unavailable")),
    )
    session.start_autodispatch(scope="owner/repo", group="team")
    deadline = time.monotonic() + 2
    while session.autodispatch_sessions()[0]["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    entry = session.autodispatch_sessions()[0]
    assert entry["status"] == "failed" and "unavailable" in entry["error"]
    assert dispatch.call_count == 1


def test_one_empty_pane_listing_does_not_end_supervision(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(
        session,
        "dispatch_once",
        MagicMock(
            side_effect=[
                {
                    "status": "launched",
                    "item_id": "owner/repo#ISSUE-1",
                    "pane_id": "terminal_1",
                    "session": "workdash",
                },
                {"status": "no_ready_task"},
            ]
        ),
    )
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    listings = [[], [{"id": 1}], [{"id": 1}], [], []]
    polls = 0

    def load_panes(_session):
        nonlocal polls
        polls += 1
        return listings.pop(0) if listings else []

    review = MagicMock(return_value={"status": "started"})
    monkeypatch.setattr(session, "review", review)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", load_panes)
    session.start_autodispatch(scope="owner/repo")
    deadline = time.monotonic() + 3
    while session.autodispatch_sessions()[0]["status"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert session.autodispatch_sessions()[0]["status"] == "finished"
    # The pane counts as gone only after two consecutive listings miss it, so the empty
    # listing while the agent was still working did not end supervision.
    assert polls == 5
    review.assert_called_once()


def test_dispatch_reports_a_failed_linked_issue_lookup_as_a_github_error():
    now = datetime.now(UTC)
    pull_request = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=9,
        title="Authored pull request",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/9",
    )
    github_client = MagicMock()
    github_client.fetch_linked_issues_for_pull_request.side_effect = RuntimeError(
        "Could not resolve to a Repository with the name 'owner/repo'."
    )
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(github_client=github_client),
        work_items=[pull_request],
        suggestion_markers={},
        zellij_session="workdash",
    )

    with pytest.raises(WorkdashControlError) as raised:
        session.start_autodispatch_for_item(pull_request)

    assert (raised.value.code, raised.value.status) == ("github_error", HTTPStatus.BAD_GATEWAY)
    assert str(raised.value) == (
        "GitHub lookup for 'owner/repo#PR-9' failed: "
        "Could not resolve to a Repository with the name 'owner/repo'."
    )
    assert session.autodispatch_sessions() == []


def _tui_session(*items: WorkItem) -> MagicMock:
    """Return a fake session whose dashboard shows the given work items."""

    session = MagicMock()
    session.work_items = list(items)
    session.suggestion_markers = {}
    return session


def _visible_task() -> WorkItem:
    now = datetime.now(UTC)
    return WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=42,
        title="Visible task",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/42",
    )


@pytest.mark.parametrize(
    "scope",
    [
        "https://github.com/owner/a/issues/1",
        "http://github.com/owner/a/issues/1/comments?view=all#note",
        "https://github.com/owner/a/issues/1?x=1#note",
        "owner/a#001",
        "owner/a#ISSUE-001",
    ],
)
def test_tui_shows_running_sessions_and_remains_interactive(scope):
    session = _tui_session(_visible_task())
    session.autodispatch_sessions.return_value = [
        {
            "status": "running",
            "scope": scope,
            "group": "first",
            "current_item": "owner/a#ISSUE-1",
            "elapsed_seconds": 5,
            "selected_group": "first",
            "stage": "developing",
            "launch_reason": "fix_ci",
            "iteration": 1,
        },
        {
            "status": "running",
            "scope": "owner/b",
            "group": None,
            "current_item": "owner/b#ISSUE-2",
            "elapsed_seconds": 10,
            "selected_group": None,
            "stage": "checking",
            "launch_reason": None,
            "iteration": 2,
        },
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            progress = app.query_one("#autodispatch-progress", Static)
            lines = progress.render().plain.splitlines()
            first = next(line for line in lines if "first" in line)
            assert first.count("owner/a#1") == 1 and "1/5s" in first and "fix ci" in first
            assert "scope:" not in first and "item:" not in first
            assert any(
                "automatic" in line
                and "scope: owner/b" in line
                and "item: owner/b#2" in line
                and "2/10s" in line
                and "checking" in line
                for line in lines
            )
            table = app.query_one("#work-items", DataTable)
            assert str(table.get_row_at(0)[2]) == "Visible task"
            assert progress.region.y < table.region.y
            await pilot.press("/")
            assert len(app.screen_stack) > 1

    asyncio.run(exercise())


def test_tui_reports_the_failure_of_the_session_the_user_started():
    session = _tui_session(_visible_task())
    session.start_autodispatch_for_item.return_value = {
        "id": "session-1",
        "scope": "owner/repo#ISSUE-42",
    }
    running = {
        "id": "session-1",
        "status": "running",
        "scope": "owner/repo#ISSUE-42",
        "group": None,
        "current_item": None,
        "elapsed_seconds": 1,
        "selected_group": None,
        "stage": "checking",
        "launch_reason": None,
        "iteration": 1,
    }
    session.autodispatch_sessions.return_value = [running]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)
            await pilot.press("d")
            await pilot.pause()
            assert footer.render().plain == (
                "Started autodispatch for owner/repo#ISSUE-42: session-1."
            )
            # Another action replaces the footer before the session fails.
            await pilot.press("r")
            await pilot.pause()
            assert footer.render().plain == "Refresh skipped."
            session.autodispatch_sessions.return_value = [
                {**running, "status": "failed", "error": "GitHub unavailable"}
            ]
            for _ in range(100):
                await pilot.pause(0.02)
                if "GitHub unavailable" in footer.render().plain:
                    break
            assert footer.render().plain == "Autodispatch session-1 failed: GitHub unavailable"

    asyncio.run(exercise())


def test_tui_reports_a_dispatch_that_cannot_start():
    session = _tui_session(_visible_task())
    session.autodispatch_sessions.return_value = []
    session.start_autodispatch_for_item.side_effect = RuntimeError("linked issue lookup denied")
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)
            await pilot.press("d")
            for _ in range(100):
                await pilot.pause(0.02)
                if "Dispatch failed" in footer.render().plain:
                    break
            assert footer.render().plain == "Dispatch failed: linked issue lookup denied"

    asyncio.run(exercise())


def test_tui_limits_old_failures_without_hiding_running_sessions():
    session = _tui_session()
    session.autodispatch_sessions.return_value = [
        {
            "id": str(i),
            "status": "failed",
            "scope": f"owner/failure-{i}",
            "group": None,
            "current_item": None,
            "elapsed_seconds": 1,
            "error": "dispatch failed",
        }
        for i in range(10)
    ] + [
        {
            "id": "active",
            "status": "running",
            "scope": "owner/active",
            "group": None,
            "current_item": "owner/active#ISSUE-1",
            "elapsed_seconds": 2,
            "selected_group": None,
            "stage": "checking",
            "launch_reason": None,
            "iteration": 1,
        }
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test():
            lines = app.query_one("#autodispatch-progress", Static).render().plain.splitlines()
            assert any("owner/active" in line for line in lines)
            for i in range(7, 10):
                assert any(
                    f"owner/failure-{i}" in line and "dispatch failed" in line for line in lines
                )
            assert not any("owner/failure-6" in line for line in lines)
            assert app.query_one("#work-items", DataTable).region.height > 0

    asyncio.run(exercise())


def test_tui_long_failures_leave_room_for_work_items():
    session = _tui_session(_visible_task())
    error = "dispatch failed " + "long error " * 80 + "\nmore details"
    session.autodispatch_sessions.return_value = [
        {
            "id": str(i),
            "status": "failed",
            "scope": f"owner/failure-{i}",
            "group": None,
            "current_item": None,
            "elapsed_seconds": 1,
            "error": error,
        }
        for i in range(3)
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test(size=(80, 24)):
            table = app.query_one("#work-items", DataTable)
            assert str(table.get_row_at(0)[2]) == "Visible task"
            assert table.region.height > 0
            assert app.query_one("#autodispatch-progress", Static).region.height == 3

    asyncio.run(exercise())


def test_tui_progress_keeps_a_failed_error_on_one_plain_line():
    session = _tui_session()
    error = "first line\n\x1b[31msecond line\x1b[0m\n" + "x" * 200
    session.autodispatch_sessions.return_value = [
        {
            "id": "session-1",
            "status": "failed",
            "scope": "owner/repo",
            "group": None,
            "current_item": "owner/repo#ISSUE-1",
            "elapsed_seconds": 2,
            "selected_group": None,
            "stage": "checking",
            "launch_reason": None,
            "iteration": 1,
            "error": error,
        }
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        # Wide enough that the row is not clipped by the screen, so the assertion below
        # proves the truncation rather than the terminal width.
        async with app.run_test(size=(200, 24)):
            rows = [strip.text for strip in app.screen._compositor.render_strips()]
            [progress] = [row for row in rows if "Failed owner/repo:" in row]
            assert ("first line second line " + "x" * 200)[:_MAX_PROGRESS_ERROR_WIDTH] in progress
            assert "\x1b" not in progress
            assert sum("second line" in row for row in rows) == 1

    asyncio.run(exercise())


def test_cli_starts_and_stops_through_server(monkeypatch, capsys):
    from workdash.workdash import main

    client = MagicMock()
    client.request.side_effect = [
        {"id": "abc", "status": "running", "scope": "owner/repo", "group": "team"},
        {"id": "abc", "status": "stopped", "scope": "owner/repo", "group": "team"},
    ]
    monkeypatch.setattr("workdash.workdash.WorkdashControlClient", lambda *args, **kwargs: client)
    assert main(["autodispatch", "owner/repo", "--group", "team", "--agent", "pi", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "abc"
    client.request.assert_called_with(
        "autodispatch", {"scope": "owner/repo", "group": "team", "agent": "pi"}
    )
    assert main(["autodispatch-stop", "abc"]) == 0
    client.request.assert_called_with("autodispatch_stop", {"id": "abc"})
    assert "stopped" in capsys.readouterr().out


def test_cli_omits_group_and_reports_automatic_group(monkeypatch, capsys):
    from workdash.workdash import main

    client = MagicMock()
    client.request.return_value = {
        "id": "abc",
        "status": "running",
        "scope": "owner/repo",
        "group": None,
    }
    monkeypatch.setattr("workdash.workdash.WorkdashControlClient", lambda *args, **kwargs: client)
    assert main(["autodispatch", "owner/repo"]) == 0
    client.request.assert_called_with("autodispatch", {"scope": "owner/repo", "agent": None})
    assert "None" not in capsys.readouterr().out


def test_info_reports_automatic_group_without_none(capsys):
    from workdash.workdash import _print_pane_info

    _print_pane_info(
        {
            "session": "workdash",
            "panes": [],
            "autodispatch_sessions": [
                {
                    "id": "abc",
                    "status": "running",
                    "scope": "owner/repo",
                    "group": None,
                    "current_item": None,
                    "elapsed_seconds": 1,
                }
            ],
        }
    )
    output = capsys.readouterr().out
    assert "group=automatic" in output and "None" not in output


@pytest.mark.parametrize(
    "items, expected_launches, expected_status",
    [
        (["1"] * 10, 9, "failed"),
        (["1"] * 9 + ["2"] + ["1"] * 9, 19, "finished"),
    ],
)
def test_autodispatch_reviews_before_reselecting_and_limits_consecutive_issues(
    monkeypatch, items, expected_launches, expected_status
):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    remaining = iter(items)
    events = []

    def dispatch(**kwargs):
        number = next(remaining, None)
        if number is None:
            return {"status": "no_ready_task"}
        item = f"owner/repo#ISSUE-{number}"
        kwargs["before_launch"](item)
        events.append(("dispatch", item, kwargs["agent"]))
        return {
            "status": "launched",
            "item_id": item,
            "pane_id": "terminal_1",
            "session": "workdash",
        }

    def review(**kwargs):
        events.append(("review", kwargs["target"], kwargs["agent"]))
        return {"status": "nothing_to_review"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr(session, "review", review)
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    assert entry.status == expected_status
    assert len(events) == 2 * expected_launches
    for dispatch_event, review_event in zip(events[::2], events[1::2], strict=True):
        assert dispatch_event[0] == "dispatch"
        assert review_event == ("review", *dispatch_event[1:])


def test_autodispatch_fails_on_review_error(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock(
        return_value={
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "workdash",
        }
    )
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr(session, "review", MagicMock(side_effect=RuntimeError("review failed")))
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert entry.status == "failed" and "review failed" in entry.error
    assert dispatch.call_count == 1


def test_autodispatch_rejects_agent_without_analysis(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock()
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    session.start_autodispatch(scope="owner/repo")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert entry.status == "failed" and "analysis command" in entry.error
    dispatch.assert_not_called()


def test_autodispatch_rejects_selected_agent_without_launch(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(
            agents={
                "coder": AgentConfig(launch="coder", analyze="coder -p"),
                "analyst": AgentConfig(analyze="analyst -p"),
            }
        ),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock()
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    session.start_autodispatch(scope="owner/repo", agent="analyst")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert entry.status == "failed" and "launch command" in entry.error
    dispatch.assert_not_called()


def test_repeated_actual_selection_stops_before_tenth_launch(monkeypatch):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )

    def launch(**kwargs):
        kwargs["before_launch"]()
        return {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "workdash",
        }

    code = MagicMock(side_effect=launch)
    monkeypatch.setattr(session, "code", code)
    monkeypatch.setattr(session, "review", MagicMock(return_value={"status": "nothing_to_review"}))
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    assert entry.status == "failed" and "10 consecutive" in entry.error
    assert code.call_count == 10


@pytest.mark.parametrize("selected_open", [True, False])
def test_autodispatch_waits_for_selected_pull_request_review(monkeypatch, tmp_path, selected_open):
    now = datetime.now(UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE if n == 1 else WorkItemKind.AUTHORED_PR,
            item_type=WorkItemType.ISSUE if n == 1 else WorkItemType.PR,
            repo="owner/repo",
            number=n,
            title=str(n),
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/{'issues' if n == 1 else 'pull'}/{n}",
        )
        for n in (1, 51, 52)
    ]
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=items,
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock(
        side_effect=[
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "pr_id": "owner/repo#PR-52",
                "pane_id": "terminal_1",
                "session": "workdash",
            },
            {"status": "no_ready_task"},
        ]
    )
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    entered, release = threading.Event(), threading.Event()
    reviewed = []

    def reviewer(*, item, prompt, command_tokens, cwd):
        reviewed.append(item.number)
        entered.set()
        assert release.wait(3)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    session.backend.github_client.linked_open_pull_request.return_value = ("owner/repo", 51)
    session.backend.github_client.fetch_launch_context.side_effect = lambda item: {
        "state": "OPEN" if item.number == 51 or selected_open else "CLOSED"
    }
    session.backend.resolve_analyze_command_tokens.return_value = ["pi", "-p"]
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *args, **kwargs: str(tmp_path))
    monkeypatch.setattr("workdash.agent_execution.executor", MagicMock(analyze=reviewer))
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo")
    entry = next(iter(session._autodispatch.values()))
    try:
        assert entered.wait(3)
        assert reviewed == [52 if selected_open else 51]
        assert dispatch.call_count == 1
    finally:
        release.set()
        entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    assert entry.status == "finished"
    assert dispatch.call_count == 2


@pytest.mark.parametrize("has_pr", [False, True])
def test_autodispatch_real_review_handles_no_pr_and_reviewer_failure(monkeypatch, tmp_path, has_pr):
    now = datetime.now(UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE if n == 1 else WorkItemKind.AUTHORED_PR,
            item_type=WorkItemType.ISSUE if n == 1 else WorkItemType.PR,
            repo="owner/repo",
            number=n,
            title=str(n),
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/pull/{n}",
        )
        for n in (1, 51)
    ]
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=items,
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock(
        side_effect=[
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "pane_id": "terminal_1",
                "session": "workdash",
            },
            {"status": "no_ready_task"},
        ]
    )
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    session.backend.github_client.linked_open_pull_request.return_value = (
        ("owner/repo", 51) if has_pr else None
    )
    session.backend.github_client.fetch_launch_context.return_value = {"state": "OPEN"}
    session.backend.resolve_analyze_command_tokens.return_value = ["pi", "-p"]
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *args, **kwargs: str(tmp_path))
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    reviewer = MagicMock(side_effect=RuntimeError("review failed"))
    monkeypatch.setattr("workdash.agent_execution.executor", MagicMock(analyze=reviewer))
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    if has_pr:
        assert entry.status == "failed" and "review failed" in entry.error
        assert dispatch.call_count == 1
        reviewer.assert_called_once()
    else:
        assert entry.status == "finished" and dispatch.call_count == 2
        reviewer.assert_not_called()


def test_autodispatch_review_feedback_cycle(monkeypatch, tmp_path):
    from workdash.work_state import PullRequestWorkState, ReadyWork, ReviewFeedback

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("Unexpected process"))
    monkeypatch.setattr(socket, "socket", lambda *a, **k: pytest.fail("Unexpected network"))
    now = datetime.now(UTC)
    issue = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="Issue",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
    )
    pr = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=51,
        title="PR",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/51",
        closing_issue_numbers=(1,),
    )
    feedback = ReviewFeedback("thread", "reviewer", "https://github.com/comment/1", now)

    class FakeGitHub:
        opened = False
        finding = False
        resolved = False
        stale_ready = False

        def find_ready_work(self, **kwargs):
            if self.stale_ready and not self.resolved:
                return [ReadyWork(issue)]
            state = self.work_pull_request(issue)
            if state is None:
                return [ReadyWork(issue)]
            return [ReadyWork(issue, state)] if state.ready else []

        def work_pull_request(self, item, **kwargs):
            if not self.opened:
                return None
            return PullRequestWorkState(
                item=pr,
                state="OPEN",
                author="testuser",
                linked_issues=(issue,),
                head_sha="fixed-head" if self.resolved else "original-head",
                head_writable=True,
                base_ref="main",
                base_head_sha="base",
                mergeability="clean",
                review_decision=None,
                ci_status="passing",
                failing_checks=(),
                feedback=(feedback,) if self.finding and not self.resolved else (),
            )

        def linked_open_pull_request(self, item):
            return (pr.repo, pr.number) if self.opened else None

        def fetch_item(self, repo, number, **kwargs):
            return {1: issue, 51: pr}.get(number)

        def fetch_launch_context(self, item):
            return {"state": "OPEN"}

        def fetch_worktree_head(self, item):
            return "branch", "owner/repo"

    class FakeGit:
        def worktree_proves_item(self, path, item):
            return True

        def pull_ff_only(self, path):
            pass

        def repository_matches(self, path, repo):
            return True

        def branch_tracks_repository(self, path, branch, repo):
            return True

        def merge_base_with_origin_default(self, path):
            return None

        def get_git_path(self, name, cwd):
            return cwd / ".git" / name

    class FakeZellij:
        def __init__(self):
            self.panes = []
            self.polled = threading.Event()

        def load_zellij_panes(self, session):
            if threading.current_thread().name.startswith("autodispatch-") and self.panes:
                self.polled.set()
            return list(self.panes)

    class FakeAgent:
        def __init__(self):
            self.launches = queue.Queue(maxsize=2)
            self.reviews = queue.Queue(maxsize=2)
            self.review_prompts = queue.Queue(maxsize=2)
            self.release_review = threading.Event()

        def launch(self, repo, prompt, **kwargs):
            pane = {"id": 1, "title": f"code_{Path(repo).name}", "pane_cwd": str(repo)}
            zellij.panes.append(pane)
            self.launches.put(prompt)
            return SimpleNamespace(
                session="workdash", pane_id=None, pane_title=pane["title"], cwd=str(repo)
            )

        def finish_implementation(self):
            if not github.opened:
                github.opened = True
            else:
                github.resolved = True
            zellij.panes.clear()

        def analyze(self, *, item, prompt, command_tokens, cwd):
            if not github.finding:
                github.finding = True
                outcome = "Major finding posted inline"
            else:
                outcome = "No major findings; no inline comments"
            Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(outcome)
            self.review_prompts.put(prompt)
            self.reviews.put((item.url, outcome))
            assert self.release_review.wait(3)
            self.release_review.clear()
            return ""

    github = FakeGitHub()
    zellij = FakeZellij()
    agent = FakeAgent()
    worktree = tmp_path / "owner_repo_1"
    (worktree / ".git/info").mkdir(parents=True)
    monkeypatch.setattr("workdash.launcher.zellij", zellij)
    monkeypatch.setattr("workdash.agent_execution.executor", agent)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    backend = MagicMock(github_client=github)
    backend.resolve_analyze_command_tokens.return_value = ["fake", "-p"]
    backend.analysis_cache.load_analysis.return_value = None
    backend.analysis_cache.load_analysis_date.return_value = None
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            workdir=str(tmp_path),
            agents={"fake": AgentConfig(launch="fake", analyze="fake -p")},
        ),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.git = FakeGit()
    try:
        assert session.ready(scope="owner/repo")["items"][0]["action"] == "implement"
        session.start_autodispatch(scope="owner/repo", agent="fake")
        prompt = agent.launches.get(timeout=3)
        assert "https://github.com/owner/repo/issues/1" in prompt
        assert "AUTOSTART: IMPLEMENT" in prompt
        assert "start implementing without asking" in prompt
        assert zellij.polled.wait(3)
        entry = session.autodispatch_sessions()[0]
        assert (entry["stage"], entry["launch_reason"], entry["iteration"]) == (
            "developing",
            "implement",
            1,
        )
        agent.finish_implementation()
        assert agent.reviews.get(timeout=3) == (
            "https://github.com/owner/repo/pull/51",
            "Major finding posted inline",
        )
        entry = session.autodispatch_sessions()[0]
        assert entry["stage"] == "reviewing" and entry["launch_reason"] is None
        review_prompt = agent.review_prompts.get(timeout=3)
        assert (
            "Report only major findings whose value outweighs the author's effort" in review_prompt
        )
        assert "Post findings as inline review comments on the pull request" in review_prompt
        assert github.work_pull_request(issue).linked_issues == (issue,)
        ready = session.ready(scope="owner/repo")["items"]
        assert len(ready) == 1 and ready[0]["issue_id"] == "owner/repo#ISSUE-1"
        assert ready[0]["action"] == "address_review"
        assert github.finding and not github.resolved
        github.stale_ready = True
        zellij.polled.clear()
        agent.release_review.set()
        prompt = agent.launches.get(timeout=3)
        assert "https://github.com/owner/repo/pull/51" in prompt
        assert feedback.url in prompt
        assert "review feedback" in prompt and "resolve every inline review thread" in prompt
        assert zellij.polled.wait(3)
        entry = session.autodispatch_sessions()[0]
        assert (entry["stage"], entry["launch_reason"], entry["iteration"]) == (
            "developing",
            "address_review",
            2,
        )
        agent.finish_implementation()
        assert agent.reviews.get(timeout=3) == (
            "https://github.com/owner/repo/pull/51",
            "No major findings; no inline comments",
        )
        assert github.work_pull_request(issue).head_sha == "fixed-head" and github.resolved
        assert session.ready(scope="owner/repo")["items"] == []
        agent.release_review.set()
        entry = next(iter(session._autodispatch.values()))
        entry.thread.join(timeout=3)
        assert not entry.thread.is_alive()
        assert entry.status == "finished", entry.error
        assert agent.launches.empty() and agent.reviews.empty()
    finally:
        agent.release_review.set()
        session.stop_autodispatch_sessions()
        if session._autodispatch:
            next(iter(session._autodispatch.values())).thread.join(timeout=3)


def test_explicit_issue_does_not_dispatch_if_pane_close_is_unconfirmed(monkeypatch, tmp_path):
    item = _issue_item(1)
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(session, "_ready", MagicMock(return_value=[ReadyWork(item=item)]))
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **kw: str(tmp_path))
    pane = {"id": 7, "title": f"code_{tmp_path.name}", "exited": False}
    monkeypatch.setattr(session, "_live_panes", lambda: [pane])
    monkeypatch.setattr("workdash.control._LAUNCH_VISIBILITY_CHECKS", 1)
    monkeypatch.setattr("workdash.control._LAUNCH_VISIBILITY_INTERVAL_SECONDS", 0)
    close = MagicMock()
    monkeypatch.setattr("workdash.launcher.zellij.close_zellij_pane", close)
    dispatch = MagicMock()
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    started = session.start_autodispatch(scope="owner/repo#ISSUE-1")
    session._autodispatch[started["id"]].thread.join(2)
    assert session.autodispatch_sessions()[0]["status"] == "failed"
    close.assert_called_once()
    dispatch.assert_not_called()


def test_prepare_autodispatch_closes_agent_pane_in_a_pull_request_worktree(monkeypatch, tmp_path):
    from workdash.work_state import PullRequestWorkState

    item = _issue_item(1)
    now = datetime.now(UTC)
    pull_request = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=51,
        title="Fix the thing",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/51",
        closing_issue_numbers=(1,),
    )
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[item, pull_request],
        suggestion_markers={},
        zellij_session="workdash",
    )
    state = PullRequestWorkState(
        item=pull_request,
        state="OPEN",
        author="testuser",
        linked_issues=(item,),
        head_sha="head",
        head_writable=True,
        base_ref="main",
        base_head_sha="base",
        mergeability="clean",
        review_decision=None,
        ci_status="passing",
        failing_checks=(),
        feedback=(),
    )
    monkeypatch.setattr(
        session, "_ready", MagicMock(return_value=[ReadyWork(item=item, pull_request=state)])
    )
    # Both the issue and the pull request have a worktree, and the agent works in the
    # pull request one.
    issue_worktree = tmp_path / "owner_repo_1"
    pull_request_worktree = tmp_path / "owner_repo_51"
    issue_worktree.mkdir()
    pull_request_worktree.mkdir()
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path",
        lambda _workdir, claim, **kwargs: (
            issue_worktree if claim.number == 1 else pull_request_worktree
        ),
    )
    panes = [{"id": 7, "title": "code_owner_repo_51", "pane_cwd": str(pull_request_worktree)}]
    monkeypatch.setattr(session, "_live_panes", lambda: list(panes))
    close = MagicMock(side_effect=lambda _session, _pane_id: panes.clear())
    monkeypatch.setattr("workdash.launcher.zellij.close_zellij_pane", close)
    session.prepare_autodispatch("owner/repo#ISSUE-1")
    close.assert_called_once_with("workdash", "terminal_7")
    assert panes == []


@pytest.mark.parametrize("scope", ["owner/repo", "any", "owner/repo#PR-1"])
def test_prepare_autodispatch_ignores_scopes_that_are_not_one_issue(monkeypatch, tmp_path, scope):
    now = datetime.now(UTC)
    pull_request = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=1,
        title="Fix the thing",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/1",
    )
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[pull_request],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr(
        session, "_live_panes", lambda: [{"id": 7, "title": f"code_{tmp_path.name}"}]
    )
    close = MagicMock()
    monkeypatch.setattr("workdash.launcher.zellij.close_zellij_pane", close)
    session.prepare_autodispatch(scope)
    close.assert_not_called()


def test_rate_limited_launch_read_does_not_hold_the_session_lock(monkeypatch, tmp_path):
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            workdir=str(tmp_path),
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[_issue_item(1)],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *a, **kw: str(tmp_path))
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **kw: None)
    monkeypatch.setattr("workdash.control.get_merge_base", lambda *a, **kw: None)
    monkeypatch.setattr(
        "workdash.control.autostart_briefing", lambda item, **kw: (item, "briefing", None)
    )
    monkeypatch.setattr("workdash.control.launcher.zellij.load_zellij_panes", lambda _: [])
    monkeypatch.setattr("workdash.github_client._TRANSIENT_RETRY_DELAY_SECONDS", 0.01)
    reading = threading.Event()

    def run(command, *args, **kwargs):
        reading.set()
        raise subprocess.CalledProcessError(1, command, stderr="API rate limit exceeded")

    monkeypatch.setattr(subprocess, "run", run)
    failures = []

    def launch():
        try:
            session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True, autoclose=True)
        except RuntimeError as error:
            failures.append(str(error))

    worker = threading.Thread(target=launch)
    worker.start()
    assert reading.wait(2)
    probe = threading.Thread(target=session.list_items)
    probe.start()
    probe.join(5)
    assert not probe.is_alive(), "the session lock was held while waiting out a rate limit"
    worker.join(10)
    assert failures and "rate limit" in failures[0]


@pytest.mark.parametrize(
    ("first_scope", "second_scope"),
    [
        ("owner/repo#42", "https://github.com/OWNER/repo/issues/42"),
        ("owner/repo#PR-7", "https://github.com/OWNER/repo/pull/7"),
        ("target/repo#ISSUE-WT42", "https://github.com/owner/todos/issues/42"),
    ],
)
def test_restart_equivalent_scope_replaces_failure(monkeypatch, first_scope, second_scope):
    session = WorkdashSession(
        config=WorkdashConfig(
            todo_repository="owner/todos", agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}
        ),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(session, "prepare_autodispatch", lambda _: None)
    monkeypatch.setattr(session, "dispatch_once", MagicMock(side_effect=RuntimeError("failed")))
    first = session.start_autodispatch(scope=first_scope)
    other = session.start_autodispatch(scope="owner/other#7")
    for entry_id in (first["id"], other["id"]):
        session._autodispatch[entry_id].thread.join(2)
    monkeypatch.setattr(
        session, "dispatch_once", MagicMock(return_value={"status": "no_ready_task"})
    )
    second = session.start_autodispatch(scope=second_scope)
    session._autodispatch[second["id"]].thread.join(2)
    # Only the failure of the restarted target is replaced.
    assert [entry["id"] for entry in session.autodispatch_sessions()] == [other["id"], second["id"]]


def test_starting_a_running_scope_replaces_it(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(session, "prepare_autodispatch", lambda _: None)
    dispatching = threading.Event()
    release = threading.Event()

    def dispatch(**_):
        dispatching.set()
        release.wait(2)
        return {"status": "no_ready_task"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    try:
        first = session.start_autodispatch(scope="owner/repo#42")
        assert dispatching.wait(2)
        first_entry = session._autodispatch[first["id"]]
        second = session.start_autodispatch(scope="owner/repo#42")
        assert [entry["id"] for entry in session.autodispatch_sessions()] == [second["id"]]
        assert first_entry.status == "stopped"
        release.set()
        for entry in (first_entry, session._autodispatch[second["id"]]):
            entry.thread.join(2)
            assert not entry.thread.is_alive()
    finally:
        release.set()
        session.stop_autodispatch_sessions()
        for entry in session._autodispatch.values():
            entry.thread.join(2)


def test_server_start_reports_an_unusable_log_path(monkeypatch, tmp_path):
    from workdash.control import WorkdashControlServer

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", blocker / "workdash.log")
    monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", 0))
    with pytest.raises(RuntimeError, match="workdash.log"):
        WorkdashControlServer(MagicMock()).start()


def test_started_server_logs_to_the_server_log_path_until_it_stops(monkeypatch, tmp_path):
    import logging

    from workdash.control import WorkdashControlServer

    log = tmp_path / "workdash.log"
    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", log)
    monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", 0))
    server = WorkdashControlServer(MagicMock())
    server.start()
    handler = server._log_handler
    logging.getLogger("workdash.autodispatch").info("started")
    server.stop()
    assert "started" in log.read_text()
    assert all(
        handler not in logging.getLogger(name).handlers
        for name in ("workdash.autodispatch", "workdash.control", "workdash.autoclose")
    )


def test_failed_second_server_preserves_running_server_log(monkeypatch, tmp_path):
    import logging

    from workdash.control import WorkdashControlServer

    log = tmp_path / "workdash.log"
    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", log)
    monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", 0))
    first = WorkdashControlServer(MagicMock())
    first.start()
    try:
        port = first._httpds[0].server_address[1]
        monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", port))
        logging.getLogger("workdash.autodispatch").info("first server event")
        with pytest.raises(RuntimeError, match="Cannot bind"):
            WorkdashControlServer(MagicMock()).start()
        assert "first server event" in log.read_text()
        logging.getLogger("workdash.autodispatch").info("later server event")
        assert "later server event" in log.read_text()
    finally:
        first.stop()


def test_server_log_records_request_failures(monkeypatch, tmp_path):
    import logging

    from workdash.control import WorkdashControlServer

    log = tmp_path / "workdash.log"
    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", log)
    monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", 0))
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    autodispatch_level = logging.getLogger("workdash.autodispatch").level
    server = WorkdashControlServer(session)
    server.start()
    try:
        app = _make_turbogears_app(session)
        response = Request.blank(
            "/api/v0/info",
            method="POST",
            content_type="application/json",
            body=b'{"secret token": }',
        ).get_response(app)
        assert response.status_int == 400
        session.info = MagicMock(
            side_effect=WorkdashControlError("github_error", "GitHub is unavailable", status=502)
        )
        response = Request.blank(
            "/api/v0/info", method="POST", content_type="application/json", body=b"{}"
        ).get_response(app)
        assert response.status_int == 502
        session.info = MagicMock(side_effect=ValueError("unexpected failure"))
        with pytest.raises(ValueError, match="unexpected failure"):
            Request.blank(
                "/api/v0/info", method="POST", content_type="application/json", body=b"{}"
            ).get_response(app)
        text = log.read_text()
        assert "JSONDecodeError" in text and "Expecting value" in text
        assert (
            "Control request failed (github_error): WorkdashControlError: GitHub is unavailable"
            in text
        )
        assert "Unexpected control request failure: ValueError: unexpected failure" in text
        assert "secret token" not in text
    finally:
        server.stop()
    assert logging.getLogger("workdash.autodispatch").level == autodispatch_level
