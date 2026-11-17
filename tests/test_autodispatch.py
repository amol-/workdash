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

from gh_work_state_fake import HEAD_SHA, check_run, comment, install_fake_github, thread, todo_body
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


def test_autodispatch_supervises_reminders_blocking_and_recovery(monkeypatch, tmp_path):
    from bdd.steps.autoclose import _install_supervision
    from bdd.steps.common import ensure_api_session

    real_monotonic = time.monotonic
    state = {}
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#42")
    session = ensure_api_session(state, [], tmp_path)
    _install_supervision(state, monkeypatch, tmp_path)
    started = session.start_autodispatch(scope="owner/repo", agent="codex")
    entry = session._autodispatch[started["id"]]
    try:
        deadline = real_monotonic() + 3
        while session.autodispatch_sessions()[0]["stage"] != "developing":
            assert entry.status == "running", entry.error
            assert real_monotonic() < deadline
            time.sleep(0.01)
        pane_id = f"terminal_{state['panes'][0]['id']}"
        prompt = state["agent_launches"][0][1]
        assert prompt.startswith("PROMPT\n\n")
        instruction = prompt.removeprefix("PROMPT\n\n")
        assert instruction
        assert session.autodispatch_sessions()[0]["current_item"] == "owner/repo#ISSUE-42"
        for _ in range(40):
            state["clock"]["now"] += 30
            session.autoclose.poll_once()
        assert state["sent"] == [(pane_id, instruction)] * 3
        assert session.info()["autodispatch_sessions"][0]["status"] == "blocked"
        for _ in range(10):
            state["clock"]["now"] += 30
            session.autoclose.poll_once()
        assert len(state["sent"]) == 3
        state["screen"] = "agent resumed work"
        session.autoclose.poll_once()
        assert session.info()["autodispatch_sessions"][0]["status"] == "running"
        for _ in range(10):
            state["clock"]["now"] += 30
            session.autoclose.poll_once()
        assert state["sent"] == [(pane_id, instruction)] * 4
    finally:
        session.stop_autodispatch(started["id"])
        entry.thread.join(timeout=3)
        assert not entry.thread.is_alive()


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
            "tab_name": "team",
            "launch_reason": "fix_ci",
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
    status, error = request("autodispatch", {"scope": "owner/repo", "automerge": "yes"})
    assert status == 400 and error["error"]["code"] == "bad_request"
    status, started = request(
        "autodispatch", {"scope": "owner/repo", "group": "team", "agent": "pi", "automerge": True}
    )
    assert status == 200
    session_id = started["result"]["id"]
    assert waits.get(timeout=2) == 30
    status, info = request("info", {})
    assert status == 200, info
    entry = info["result"]["autodispatch_sessions"][0]
    assert entry["id"] == session_id
    assert entry["automerge"] is True
    assert (entry["scope"], entry["group"], entry["agent"], entry["current_item"]) == (
        "owner/repo",
        "team",
        "pi",
        "owner/repo#ISSUE-1",
    )
    assert entry["selected_group"] == "team"
    assert entry["stage"] == "developing"
    assert entry["iteration"] == 1
    assert entry["launch_reason"] == "fix_ci"
    status, stopped = request("autodispatch_stop", {"id": session_id})
    assert status == 200 and stopped["result"]["status"] == "stopped"
    thread = session._autodispatch[session_id].thread
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert session.take_completed_autodispatch_items() == {"owner/repo#ISSUE-1"}
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


def test_blocked_session_does_not_poll_for_pane_disappearance(monkeypatch):
    polled = threading.Event()
    original_wait = threading.Event.wait
    waits = 0

    def controlled_wait(event, timeout=None):
        nonlocal waits
        if threading.current_thread().name.startswith("autodispatch-"):
            waits += 1
            if waits == 1:
                return False
            polled.set()
        return original_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", controlled_wait)
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
            return_value={
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "pane_id": "terminal_1",
                "session": "workdash",
            }
        ),
    )
    monkeypatch.setattr(session.autoclose, "blocked", lambda *_: True)
    load_panes = MagicMock(side_effect=RuntimeError("unavailable"))
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", load_panes)
    started = session.start_autodispatch(scope="owner/repo")
    entry = session._autodispatch[started["id"]]
    try:
        assert polled.wait(2), entry.error
        assert session.autodispatch_sessions()[0]["status"] == "blocked"
        load_panes.assert_not_called()
    finally:
        session.stop_autodispatch_sessions()
        entry.thread.join(timeout=3)
        assert not entry.thread.is_alive()


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


async def _choose_open(pilot, opened: list) -> None:
    """Pick the view menu's open choice and wait for the open callback to run."""
    count = len(opened)
    await pilot.press("v")
    await pilot.pause()
    await pilot.press("o")
    for _ in range(20):
        await pilot.pause()
        if len(opened) > count:
            return


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


@pytest.mark.parametrize("width", [40, 80])
def test_tui_short_dashboard_scrolls_progress_and_keeps_table_visible(width):
    session = _tui_session(_visible_task())
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
        for i in range(3)
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test(size=(width, 6)) as pilot:
            await pilot.pause()
            assert app.query_one("#work-items", DataTable).region.height >= 2
            assert any("IS 42" in strip.text for strip in app.screen._compositor.render_strips())
            assert "owner/failure-0" in app.export_screenshot()
            assert "owner/failure-2" not in app.export_screenshot()
            await pilot.press("shift+tab", "end")
            await pilot.pause()
            assert "owner/failure-2" in app.export_screenshot()
            assert any("IS 42" in strip.text for strip in app.screen._compositor.render_strips())

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
    assert (
        main(
            [
                "autodispatch",
                "owner/repo",
                "--group",
                "team",
                "--agent",
                "pi",
                "--automerge",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["id"] == "abc"
    client.request.assert_called_with(
        "autodispatch", {"scope": "owner/repo", "group": "team", "agent": "pi", "automerge": True}
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
                    "selected_group": None,
                    "current_item": None,
                    "stage": "checking",
                    "launch_reason": None,
                    "iteration": 1,
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


def test_autodispatch_reviews_in_its_group(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = MagicMock(
        side_effect=[
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "pr_id": "owner/repo#PR-2",
                "pane_id": "terminal_1",
                "session": "workdash",
            },
            {"status": "no_ready_task"},
        ]
    )
    reviews = []

    def review(**kwargs):
        reviews.append((kwargs["target"], kwargs["group"]))
        return {"status": "nothing_to_review"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr(session, "review", review)
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo", group="epic")
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    assert entry.status == "finished"
    # The pull request review and the retry on the issue both stay in the group.
    assert reviews == [("owner/repo#PR-2", "epic"), ("owner/repo#ISSUE-1", "epic")]


def test_stopping_during_dispatch_queues_launched_item_for_refresh(monkeypatch):
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
        return {"status": "launched", "item_id": "owner/repo#ISSUE-1", "pane_id": "terminal_1"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    started = session.start_autodispatch(scope="owner/repo")
    entry = session._autodispatch[started["id"]]
    try:
        assert entered.wait(3)
        session.stop_autodispatch(started["id"])
        assert session.take_completed_autodispatch_items() == set()
    finally:
        release.set()
        entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    assert entry.status == "stopped"
    assert session.take_completed_autodispatch_items() == {"owner/repo#ISSUE-1"}


@pytest.mark.parametrize("fails", [False, True])
def test_stopping_during_review_queues_refresh_after_review_ends(monkeypatch, fails):
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
    entered, release = threading.Event(), threading.Event()

    def review(**kwargs):
        entered.set()
        assert release.wait(3)
        if fails:
            raise RuntimeError("review failed")
        return {"status": "started"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr(session, "review", review)
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    started = session.start_autodispatch(scope="owner/repo")
    entry = session._autodispatch[started["id"]]
    try:
        assert entered.wait(3)
        session.stop_autodispatch(started["id"])
        assert session.take_completed_autodispatch_items() == {"owner/repo#ISSUE-1"}
    finally:
        release.set()
        entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    assert session.take_completed_autodispatch_items() == {"owner/repo#ISSUE-1"}
    assert entry.status == "stopped"
    assert dispatch.call_count == 1


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
    session.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN", head_sha=head
    )
    entered, release = threading.Event(), threading.Event()
    reviewed = []

    def reviewer(*, item, prompt, command_tokens, cwd, work_action, zellij_session, group):
        reviewed.append(item.number)
        entered.set()
        assert release.wait(3)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Review outcome: success\nOutcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    session.backend.github_client.linked_open_pull_request.return_value = ("owner/repo", 51)
    session.backend.github_client.fetch_launch_context.side_effect = lambda item: {
        "state": "OPEN" if item.number == 51 or selected_open else "CLOSED"
    }
    session.backend.resolve_analyze_command_tokens.return_value = ["pi", "-p"]
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *args, **kwargs: str(tmp_path))
    monkeypatch.setattr("workdash.agent_execution.executor", MagicMock(run_in_pane=reviewer))
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


@pytest.mark.parametrize(
    "outcome", ["no_pr", "success", "empty", "failed", "failure", "unconfirmed"]
)
def test_autodispatch_real_review_controls_merge(monkeypatch, tmp_path, outcome):
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
        None if outcome == "no_pr" else ("owner/repo", 51)
    )
    session.backend.github_client.fetch_launch_context.return_value = {"state": "OPEN"}
    session.backend.resolve_analyze_command_tokens.return_value = ["pi", "-p"]
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *args, **kwargs: str(tmp_path))
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
            "head",
        ],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()
    session.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha=head,
        reasons=(),
        changes_requested=False,
        review_threads_complete=True,
        mergeability="mergeable",
        ci_status="passing",
    )

    def run_review(**kwargs):
        if outcome == "failed":
            raise RuntimeError("review failed")
        if outcome in {"success", "failure", "unconfirmed"}:
            marker = {
                "success": "Review outcome: success",
                "failure": "Review outcome: failure",
                "unconfirmed": "Outcome: approved",
            }[outcome]
            Path(re.search(r"completion document at (\S+)", kwargs["prompt"]).group(1)).write_text(
                f"{marker}\nInline comments: none"
            )
        return ""

    reviewer = MagicMock(side_effect=run_review)
    monkeypatch.setattr("workdash.agent_execution.executor", MagicMock(run_in_pane=reviewer))
    monkeypatch.setattr("workdash.launcher.zellij", MagicMock(load_zellij_panes=lambda _: []))
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    session.start_autodispatch(scope="owner/repo", automerge=True)
    entry = next(iter(session._autodispatch.values()))
    entry.thread.join(timeout=3)
    assert not entry.thread.is_alive()
    merge = session.backend.github_client.merge_pull_request
    if outcome == "success":
        assert entry.status == "finished"
        merge.assert_called_once_with("owner/repo", 51, head)
    else:
        merge.assert_not_called()
        if outcome == "no_pr":
            assert entry.status == "finished" and dispatch.call_count == 2
            reviewer.assert_not_called()
        else:
            assert entry.status == "failed" and dispatch.call_count == 1
            assert reviewer.called


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

        def list_workdash_panes(self, session):
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

        def run_in_pane(
            self, *, item, prompt, command_tokens, cwd, work_action, zellij_session, group
        ):
            if not github.finding:
                github.finding = True
                outcome = "Major finding posted inline"
            else:
                outcome = "No major findings; no inline comments"
            Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
                "Review outcome: success\n" + outcome
            )
            self.review_prompts.put(prompt)
            self.reviews.put((item.url, outcome))
            assert self.release_review.wait(3)
            self.release_review.clear()
            return ""

    github = FakeGitHub()
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: SimpleNamespace(
            stdout=("fixed-head" if github.resolved else "original-head")
            if cmd[1] == "rev-parse"
            else ""
        ),
    )
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


def test_server_log_keeps_earlier_runs(monkeypatch, tmp_path):
    import logging

    from workdash.control import WorkdashControlServer

    log = tmp_path / "workdash.log"
    log.write_text("earlier run\n")
    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", log)
    monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", 0))
    server = WorkdashControlServer(MagicMock())
    server.start()
    try:
        logging.getLogger("workdash.autodispatch").info("current run")
    finally:
        server.stop()
    text = log.read_text()
    assert "earlier run" in text and "current run" in text


def test_server_log_records_autoclose_decisions(monkeypatch, tmp_path):
    import logging

    from workdash.control import WorkdashControlServer

    log = tmp_path / "workdash.log"
    monkeypatch.setattr("workdash.control.SERVER_LOG_PATH", log)
    monkeypatch.setattr("workdash.control.server_address", lambda: ("127.0.0.1", 0))
    autoclose = logging.getLogger("workdash.autoclose")
    level = autoclose.level
    server = WorkdashControlServer(MagicMock())
    server.start()
    try:
        assert autoclose.level == logging.INFO
        autoclose.info("Closed the autoclosed pane terminal_1")
    finally:
        server.stop()
    assert "Closed the autoclosed pane terminal_1" in log.read_text()
    assert autoclose.level == level


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


@pytest.mark.parametrize(
    "scope,item,same,stage,expected,absent",
    [
        (
            "https://github.com/owner/repo/issues/0042",
            "owner/repo#ISSUE-42",
            True,
            "developing",
            ["scope=owner/repo#42", "fix ci"],
            ["item="],
        ),
        (
            "owner/repo",
            "owner/other#ISSUE-7",
            False,
            "reviewing",
            ["scope=owner/repo", "item=owner/other#7"],
            ["fix ci"],
        ),
        ("owner/repo", None, False, "checking", ["group=automatic"], ["item=", "fix ci"]),
        (
            "https://github.com/owner/todos/issues/42",
            "owner/target#ISSUE-WT42",
            True,
            "developing",
            ["scope=owner/todos#42"],
            ["item="],
        ),
    ],
)
def test_info_progress_and_json(monkeypatch, capsys, scope, item, same, stage, expected, absent):
    from workdash.workdash import main

    entry = {
        "id": "abc",
        "status": "failed",
        "error": "failure details\nsecond line",
        "scope": scope,
        "group": None,
        "selected_group": None if stage == "checking" else "project",
        "current_item": item,
        "scope_is_current_item": same,
        "stage": stage,
        "launch_reason": "fix_ci",
        "iteration": 3,
        "elapsed_seconds": 15,
    }
    info = {"session": "workdash", "panes": [], "autodispatch_sessions": [entry]}
    client = MagicMock()
    client.request.return_value = info
    monkeypatch.setattr("workdash.workdash.WorkdashControlClient", lambda *a, **kw: client)
    assert main(["info"]) == 0
    output = capsys.readouterr().out
    for value in [
        "failed",
        "failure details\nsecond line",
        f"stage={stage}",
        "iteration=3",
        "elapsed=15s",
        *expected,
    ]:
        assert value in output
    for value in absent:
        assert value not in output
    assert main(["info", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["autodispatch_sessions"][0] == entry


def test_snapshot_identifies_targeted_todo_as_scope():
    from workdash.autodispatch import _AutodispatchSession

    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="owner/todos"),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    dispatch = _AutodispatchSession(session, "https://github.com/owner/todos/issues/42", None, None)
    dispatch.current_item = "owner/target#ISSUE-WT42"
    assert dispatch.snapshot()["scope_is_current_item"] is True
    dispatch.current_item = "owner/other#ISSUE-42"
    assert dispatch.snapshot()["scope_is_current_item"] is False


def test_tui_short_pane_keeps_a_work_item_visible_with_progress():
    items = [_issue_item(i) for i in range(1, 11)]
    for item in items:
        item.updated_at = items[0].updated_at
    session = _tui_session(*items)
    session.autodispatch_sessions.return_value = [
        {
            "id": "running",
            "status": "running",
            "scope": "owner/repo",
            "current_item": None,
            "selected_group": None,
            "stage": "checking",
            "launch_reason": None,
            "iteration": 1,
            "elapsed_seconds": 0,
        },
        *[
            {"id": str(i), "status": "failed", "scope": "owner/repo", "error": "failed"}
            for i in range(3)
        ],
    ]
    opened = []
    app = WorkdashApp(session=session, open_callback=opened.append)

    async def exercise():
        async with app.run_test(size=(40, 7)) as pilot:
            table = app.query_one("#work-items", DataTable)
            progress = app.query_one("#autodispatch-progress", Static)
            status = app.query_one("#status-footer", Static)
            assert progress.region.height > 0
            assert table.region.height >= 2
            await pilot.press(*(["down"] * 9))
            await pilot.pause()
            rows = [strip.text for strip in app.screen._compositor.render_strips()]
            assert any("IS 10" in row for row in rows[table.region.y : status.region.y])
            await _choose_open(pilot, opened)
            assert opened[-1].number == 10

    asyncio.run(exercise())


@pytest.mark.parametrize("with_progress", [False, True])
def test_tui_selected_item_remains_visible_after_resize(with_progress):
    items = [_issue_item(i) for i in range(1, 41)]
    for item in items:
        item.updated_at = items[0].updated_at
    session = _tui_session(*items)
    session.autodispatch_sessions.return_value = (
        [{"id": "1", "status": "failed", "scope": "owner/repo", "error": "failed"}]
        if with_progress
        else []
    )
    opened = []
    app = WorkdashApp(session=session, open_callback=opened.append)

    async def exercise():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press(*(["down"] * 39))
            await pilot.resize_terminal(80, 10)
            await pilot.pause()
            rows = [strip.text for strip in app.screen._compositor.render_strips()]
            assert any("IS 40" in row for row in rows)
            await _choose_open(pilot, opened)
            assert opened[-1].number == 40

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(80, 24), (40, 7)])
def test_tui_scrolling_remains_visible_when_progress_changes(size):
    items = [_issue_item(i) for i in range(1, 41)]
    for item in items:
        item.updated_at = items[0].updated_at
    session = _tui_session(*items)
    session.autodispatch_sessions.return_value = []
    opened = []
    app = WorkdashApp(session=session, open_callback=opened.append)

    async def exercise():
        async with app.run_test(size=size) as pilot:
            table = app.query_one("#work-items", DataTable)
            progress = app.query_one("#autodispatch-progress", Static)
            status = app.query_one("#status-footer", Static)
            footer = app.query_one("#command-footer", Static)
            await pilot.press(*(["down"] * 39))
            for entries in (
                [],
                [{"id": "1", "status": "failed", "scope": "owner/repo", "error": "failed"}],
                [],
            ):
                session.autodispatch_sessions.return_value = entries
                app._update_autodispatch_progress()
                await pilot.pause()
                assert not entries or progress.region.y == 0
                assert table.region.bottom <= footer.region.y < status.region.y
                await _choose_open(pilot, opened)
                assert opened[-1].number == 40
                row_y = (
                    table.region.y + table._get_row_region(table.cursor_row).y - int(table.scroll_y)
                )
                assert table.region.y <= row_y < footer.region.y
                rows = [strip.text for strip in app.screen._compositor.render_strips()]
                assert any("Failed owner/repo" in row for row in rows) == bool(entries)
                assert "IS 40" in rows[row_y]
                for key, numbers in (("up", range(39, 0, -1)), ("down", range(2, 41))):
                    for number in numbers:
                        await pilot.press(key)
                        row_y = (
                            table.region.y
                            + table._get_row_region(table.cursor_row).y
                            - int(table.scroll_y)
                        )
                        assert table.region.y <= row_y < footer.region.y
                        rows = [strip.text for strip in app.screen._compositor.render_strips()]
                        assert f"IS {number} " in rows[row_y]
                    await _choose_open(pilot, opened)
                assert [item.number for item in opened[-2:]] == [1, 40]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "automerge,reviewed,merge_error",
    [(False, True, False), (True, True, False), (True, False, False), (True, True, True)],
)
def test_automerge_after_review(monkeypatch, automerge, reviewed, merge_error):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.side_effect = [
        {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pr_id": "owner/repo#PR-3",
            "pane_id": "terminal_1",
            "session": "s",
            "cwd": "/worktree",
        },
        {"status": "no_ready_task"},
    ]
    owner.review.side_effect = [
        {"status": "nothing_to_review"},
        {"status": "reviewed", "item_id": "other/repo#PR-2", "head_sha": "head"}
        if reviewed
        else {"status": "nothing_to_review"},
    ]
    owner._require_item.return_value = MagicMock(repo="other/repo", number=2)
    owner.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=(),
        changes_requested=False,
        review_threads_complete=True,
        mergeability="mergeable",
        ci_status="passing",
    )
    merge = owner.backend.github_client.merge_pull_request
    if merge_error:
        merge.side_effect = RuntimeError("merge rejected")
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=automerge)
    entry.run()
    assert entry.snapshot()["status"] == ("failed" if merge_error else "finished")
    assert owner.review.call_args_list[1].kwargs["target"] == "owner/repo#ISSUE-1"
    owner.git.has_unpublished_work.assert_not_called()
    if automerge and reviewed:
        owner._require_item.assert_called_once_with("other/repo#PR-2")
        merge.assert_called_once_with("other/repo", 2, "head")
    else:
        merge.assert_not_called()
    if merge_error:
        assert "merge rejected" in entry.snapshot()["error"]


@pytest.mark.parametrize("permission_error", [False, True])
def test_automerge_retries_conflicting_pull_request(monkeypatch, permission_error):
    from dataclasses import replace

    from workdash.autodispatch import _AutodispatchSession
    from workdash.github_client import MergeConflictError
    from workdash.work_state import PullRequestWorkState

    issue = _issue_item(1)
    pr = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=2,
        title="Fix",
        created_at=issue.created_at,
        updated_at=issue.updated_at,
        url="https://github.com/owner/repo/pull/2",
    )
    state = PullRequestWorkState(
        item=pr,
        state="OPEN",
        author="testuser",
        linked_issues=(issue,),
        head_sha="head",
        head_writable=True,
        base_ref="main",
        base_head_sha="base",
        mergeability="conflicting",
        review_decision=None,
        ci_status="passing",
        failing_checks=(),
        feedback=(),
    )
    owner = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session="s",
    )
    monkeypatch.setattr(owner, "prepare_autodispatch", lambda _: None)
    monkeypatch.setattr(
        owner,
        "review",
        MagicMock(
            return_value={"status": "reviewed", "item_id": "owner/repo#PR-2", "head_sha": "head"}
        ),
    )
    clean = replace(state, mergeability="mergeable")
    owner.backend.github_client.work_pull_request.side_effect = [clean, state, clean]
    owner.backend.github_client.merge_pull_request.side_effect = [
        RuntimeError("permission denied") if permission_error else MergeConflictError("conflict"),
        None,
    ]
    ready = MagicMock(
        side_effect=[
            [ReadyWork(issue)],
            [ReadyWork(issue)],
            [ReadyWork(issue, state)],
            [ReadyWork(issue, state)],
            [],
        ]
    )
    monkeypatch.setattr(owner, "_ready", ready)
    launches = []

    def code(**kwargs):
        kwargs["before_launch"]()
        launches.append(kwargs["target"])
        return {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "s",
            "launch_reason": "implement" if len(launches) == 1 else state.action,
        }

    monkeypatch.setattr(owner, "code", code)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    entry.run()
    if permission_error:
        assert entry.status == "failed"
        assert "permission denied" in entry.error
        assert len(launches) == 1
        return
    assert entry.status == "finished"
    assert entry.automerge is True
    assert len(launches) == 2
    assert ready.call_count == 5
    assert state.action == "fix_merge_conflicts"
    assert owner.backend.github_client.merge_pull_request.call_count == 2
    assert owner.backend.github_client.work_pull_request.call_count == 3


def test_tui_dispatch_replaces_selected_running_session():
    item = _issue_item(1)
    session = _tui_session(item)
    session.autodispatch_sessions.return_value = [
        {
            "id": "old",
            "status": "running",
            "scope": "owner/repo#ISSUE-1",
            "current_item": "owner/repo#ISSUE-1",
            "stage": "developing",
            "iteration": 1,
            "elapsed_seconds": 0,
            "launch_reason": None,
            "selected_group": None,
        }
    ]
    session._scope_identity.side_effect = lambda value: ("owner/repo", 1)
    session.start_autodispatch_for_item.return_value = {"id": "new", "scope": "owner/repo#ISSUE-1"}
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            assert "(m)erge" in app._command_hint().plain
            assert "(d)ispatch" not in app._command_hint().plain
            await pilot.press("d")
            await pilot.pause()
            session.start_autodispatch_for_item.assert_called_once_with(item)

    asyncio.run(exercise())


def test_command_footer_for_row_without_session():
    item = _issue_item(1)
    session = _tui_session(item)
    session.autodispatch_sessions.return_value = []
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test():
            footer = app.query_one("#command-footer", Static).render().plain
            assert "(d)ispatch" in footer
            assert "(m)erge" not in footer

    asyncio.run(exercise())


def test_tui_refuses_ambiguous_automerge_toggle():
    item = _issue_item(1)
    session = _tui_session(item)
    session._scope_identity.side_effect = lambda value: (
        ("owner/repo", 1) if "ISSUE-1" in value else None
    )
    session.autodispatch_sessions.return_value = [
        {
            "id": ident,
            "status": "running",
            "scope": scope,
            "current_item": "owner/repo#ISSUE-1",
            "stage": "developing",
            "iteration": 1,
            "elapsed_seconds": 0,
            "launch_reason": None,
            "selected_group": None,
        }
        for ident, scope in [("repo", "owner/repo"), ("issue", "owner/repo#ISSUE-1")]
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            await pilot.press("m")
            await pilot.pause()
            session.toggle_automerge.assert_not_called()
            assert "multiple" in app.query_one("#status-footer", Static).render().plain.lower()

    asyncio.run(exercise())


@pytest.mark.parametrize("merge_fails", [False, True])
def test_stopping_during_merge_refreshes_after_it_finishes(monkeypatch, merge_fails):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.return_value = {
        "status": "launched",
        "item_id": "owner/repo#ISSUE-1",
        "pane_id": "terminal_1",
        "session": "s",
        "cwd": "/worktree",
    }
    owner.review.return_value = {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": "head",
    }
    owner._require_item.return_value = MagicMock(repo="owner/repo", number=2)
    owner.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=(),
        changes_requested=False,
        review_threads_complete=True,
        mergeability="mergeable",
        ci_status="passing",
    )
    entered, release = threading.Event(), threading.Event()

    def merge(*args):
        entered.set()
        assert release.wait(2)
        if merge_fails:
            raise RuntimeError("merge rejected")

    owner.backend.github_client.merge_pull_request.side_effect = merge
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    entry.thread.start()
    assert entered.wait(2)
    entry.stop()
    owner.completed_autodispatch_item.reset_mock()
    release.set()
    entry.thread.join(2)
    assert not entry.thread.is_alive()
    owner.completed_autodispatch_item.assert_called_once_with("owner/repo#ISSUE-1")


@pytest.mark.parametrize("problem", ["merge_conflict", "review_conflict", "pending_ci"])
def test_automerge_redispatches_real_ready_work(monkeypatch, tmp_path, problem):
    from workdash.github_client import MergeConflictError

    github = install_fake_github(monkeypatch)
    issue = github.add_issue("owner/repo#1")
    pr = github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    client = GitHubClient()
    monkeypatch.setattr(client, "fetch_launch_context", lambda _: {})
    monkeypatch.setattr(client, "fetch_worktree_head", lambda _: ("branch", "owner/repo"))
    worktree = str(tmp_path / "owner_repo_1")
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **k: worktree)
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *a, **k: worktree)
    monkeypatch.setattr("workdash.control.get_merge_base", lambda *a, **k: None)
    panes, prompts = [], []

    def launch(repo, prompt, **kwargs):
        prompts.append(prompt)
        panes.append({"id": len(prompts), "title": "code_owner_repo_1", "pane_cwd": str(repo)})
        return SimpleNamespace(
            session="workdash", pane_id=None, pane_title=panes[-1]["title"], cwd=str(repo)
        )

    def merge(repo, number, sha):
        if len(prompts) == 1 and problem == "merge_conflict":
            pr["mergeable"] = "CONFLICTING"
            raise MergeConflictError("conflict")
        issue["state"] = "CLOSED"
        pr["state"] = "MERGED"

    monkeypatch.setattr(client, "merge_pull_request", merge)
    monkeypatch.setattr("workdash.launcher.zellij.load_zellij_panes", lambda _: list(panes))
    monkeypatch.setattr("workdash.agent_execution.executor.launch", launch)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            workdir=str(tmp_path),
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=client),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.git = MagicMock()

    def review(**_):
        if problem == "review_conflict" and len(prompts) == 1:
            pr["mergeable"] = "CONFLICTING"
        return {"status": "reviewed", "item_id": "owner/repo#PR-51", "head_sha": HEAD_SHA}

    monkeypatch.setattr(session, "review", review)
    session.start_autodispatch(scope="owner/repo", automerge=True)
    entry = next(iter(session._autodispatch.values()))

    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert entry.status != "failed", entry.error
            assert time.monotonic() < deadline, entry.snapshot()
            time.sleep(0.01)

    try:
        wait_for(lambda: len(prompts) == 1 and entry.stage == "developing")
        pr["checks"] = {
            HEAD_SHA: [
                check_run("tests", None, status="IN_PROGRESS")
                if problem == "pending_ci"
                else check_run("tests")
            ]
        }
        panes.clear()
        if problem == "pending_ci":
            wait_for(lambda: entry.stage == "waiting")
            assert len(prompts) == 1
            pr["checks"] = {HEAD_SHA: [check_run("tests", "FAILURE")]}
        wait_for(lambda: len(prompts) == 2 and entry.stage == "developing")
        if problem == "pending_ci":
            assert "has failing checks: tests" in prompts[1]
            pr["checks"] = {HEAD_SHA: [check_run("tests")]}
        else:
            assert "GitHub reports merge conflicts" in prompts[1]
            assert "resolve the conflicting files" in prompts[1]
            pr["mergeable"] = "MERGEABLE"
        panes.clear()
        entry.thread.join(5)
        assert not entry.thread.is_alive()
        assert entry.status == "finished", entry.error
        assert issue["state"] == "CLOSED"
    finally:
        entry.stop()
        entry.thread.join(3)


def test_review_feedback_redispatches_real_ready_work(monkeypatch, tmp_path):

    github = install_fake_github(monkeypatch)
    issue = github.add_issue("owner/repo#1")
    pr = github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    client = GitHubClient()
    monkeypatch.setattr(client, "fetch_launch_context", lambda _: {})
    monkeypatch.setattr(client, "fetch_worktree_head", lambda _: ("branch", "owner/repo"))
    worktree = str(tmp_path / "owner_repo_1")
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **k: worktree)
    monkeypatch.setattr("workdash.control.ensure_worktree", lambda *a, **k: worktree)
    monkeypatch.setattr("workdash.control.get_merge_base", lambda *a, **k: None)
    panes, prompts = [], []

    def launch(repo, prompt, **kwargs):
        prompts.append(prompt)
        panes.append({"id": len(prompts), "title": "code_owner_repo_1", "pane_cwd": str(repo)})
        return SimpleNamespace(
            session="workdash", pane_id=None, pane_title=panes[-1]["title"], cwd=str(repo)
        )

    def merge(repo, number, sha):
        issue["state"] = "CLOSED"
        pr["state"] = "MERGED"

    monkeypatch.setattr(client, "merge_pull_request", merge)
    monkeypatch.setattr("workdash.launcher.zellij.load_zellij_panes", lambda _: list(panes))
    monkeypatch.setattr("workdash.agent_execution.executor.launch", launch)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            workdir=str(tmp_path),
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=client),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.git = MagicMock()

    def review(**_):
        if len(prompts) == 1:
            pr["threads"].append(thread(comment("reviewer", created="2026-03-03T00:00:00Z")))
        return {"status": "reviewed", "item_id": "owner/repo#PR-51", "head_sha": HEAD_SHA}

    monkeypatch.setattr(session, "review", review)
    session.start_autodispatch(scope="owner/repo", automerge=True)
    entry = next(iter(session._autodispatch.values()))

    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert entry.status != "failed", entry.error
            assert time.monotonic() < deadline, entry.snapshot()
            time.sleep(0.01)

    try:
        wait_for(lambda: len(prompts) == 1 and entry.stage == "developing")
        pr["checks"] = {HEAD_SHA: [check_run("tests")]}
        panes.clear()
        wait_for(lambda: len(prompts) == 2 and entry.stage == "developing")
        assert "review" in prompts[1].lower()
        assert "https://github.com/comment/" in prompts[1]
        pr["threads"][0]["isResolved"] = True
        panes.clear()
        entry.thread.join(5)
        assert not entry.thread.is_alive()
        assert entry.status == "finished", entry.error
        assert issue["state"] == "CLOSED"
    finally:
        entry.stop()
        entry.thread.join(3)


@pytest.mark.parametrize("change", ["during_review", "merge_rejected", "enabled_during_review"])
def test_automerge_rereviews_head_when_needed(monkeypatch, change):
    from workdash.autodispatch import _AutodispatchSession
    from workdash.github_client import MergeHeadChangedError

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.side_effect = [
        {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "s",
        },
        {"status": "no_ready_task"},
    ]
    github = owner.backend.github_client
    state = SimpleNamespace(
        state="OPEN",
        head_sha="old",
        reasons=(),
        changes_requested=False,
        review_threads_complete=True,
        mergeability="mergeable",
        ci_status="passing",
    )
    github.work_pull_request.return_value = state
    owner._require_item.return_value = MagicMock(repo="owner/repo", number=2)
    entry = _AutodispatchSession(
        owner, "owner/repo", None, None, automerge=change != "enabled_during_review"
    )

    def review(**kwargs):
        head = state.head_sha
        if change == "enabled_during_review" and not kwargs["require_fresh"]:
            entry.automerge = True
            head = None
        elif change == "during_review" and head == "old":
            state.head_sha = "new"
        return {"status": "reviewed", "item_id": "owner/repo#PR-2", "head_sha": head}

    def merge(repo, number, head):
        if change == "merge_rejected" and head == "old":
            state.head_sha = "new"
            raise MergeHeadChangedError("SHA does not match (HTTP 409)")

    owner.review.side_effect = review
    github.merge_pull_request.side_effect = merge
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry.run()
    assert entry.status == "finished", entry.error
    assert owner.review.call_count == 2
    assert github.merge_pull_request.call_args.args[2] == (
        "old" if change == "enabled_during_review" else "new"
    )


@pytest.mark.parametrize(
    "reasons,ci,mergeability,expected",
    [
        (("address_review",), "passing", "mergeable", "finished"),
        (("fix_ci",), "failing", "mergeable", "finished"),
        (("fix_merge_conflicts",), "passing", "conflicting", "finished"),
    ],
)
def test_automerge_rechecks_readiness_after_review(
    monkeypatch, reasons, ci, mergeability, expected
):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.side_effect = [
        {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "s",
        },
        {"status": "no_ready_task"},
    ]
    owner.review.return_value = {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": "head",
    }
    owner._require_item.return_value = MagicMock(repo="owner/repo", number=2)
    owner.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=reasons,
        changes_requested=False,
        review_threads_complete=True,
        head_writable=True,
        ci_status=ci,
        mergeability=mergeability,
    )
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    entry.run()
    assert entry.status == expected
    assert owner.dispatch_once.call_count == (2 if reasons else 1)
    owner.backend.github_client.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("changes_requested,review_threads_complete", [(False, False)])
def test_automerge_fails_when_feedback_is_not_cleared(
    monkeypatch, changes_requested, review_threads_complete
):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.side_effect = [
        {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "s",
        },
        {"status": "no_ready_task"},
    ]
    owner.review.return_value = {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": "head",
    }
    owner.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=(),
        changes_requested=changes_requested,
        review_threads_complete=review_threads_complete,
        mergeability="mergeable",
        ci_status="passing",
    )
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    entry.run()
    assert entry.status == "failed"
    assert owner.dispatch_once.call_count == 1
    owner.backend.github_client.merge_pull_request.assert_not_called()


def test_automerge_stops_retrying_a_continually_changing_head(monkeypatch):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.return_value = {
        "status": "launched",
        "item_id": "owner/repo#ISSUE-1",
        "pane_id": "terminal_1",
        "session": "s",
    }
    owner.review.return_value = {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": "old",
    }
    owner.backend.github_client.work_pull_request.return_value = SimpleNamespace(
        state="OPEN", head_sha="new"
    )
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    entry.run()
    assert entry.status == "failed"
    assert "too often" in entry.error
    assert owner.review.call_count == 4
    owner.backend.github_client.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("force_push", [False, True])
def test_automerge_reviews_changed_git_head_before_merge(monkeypatch, tmp_path, force_push):
    from workdash.autodispatch import _AutodispatchSession

    source = tmp_path / "source"
    source.mkdir()

    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()

    git(source, "init", "-q")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.com")
    (source / "change.txt").write_text("base")
    git(source, "add", "change.txt")
    git(source, "commit", "-qm", "base")
    default_branch = git(source, "branch", "--show-current")
    git(source, "checkout", "-qb", "feature")
    (source / "change.txt").write_text("old change")
    git(source, "commit", "-qam", "old")
    old_head = git(source, "rev-parse", "HEAD")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(origin)], check=True)
    git(origin, "symbolic-ref", "HEAD", f"refs/heads/{default_branch}")
    git(source, "remote", "add", "origin", str(origin))
    workdir = tmp_path / "work"
    workdir.mkdir()
    clone = workdir / "owner_repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    git(clone, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    git(clone, "config", f"url.{origin}.insteadOf", "https://github.com/owner/repo.git")

    now = datetime.now(UTC)
    pr = WorkItem(
        WorkItemKind.TRACKED_PR,
        WorkItemType.PR,
        "owner/repo",
        2,
        "pr",
        now,
        now,
        "https://github.com/owner/repo/pull/2",
    )
    backend = MagicMock()
    client = backend.github_client
    client.fetch_launch_context.return_value = {"state": "OPEN"}
    client.fetch_worktree_head.return_value = ("feature", "owner/repo")
    local_heads = []

    def pull_request(*args, **kwargs):
        head = git(origin, "rev-parse", "refs/heads/feature")
        if force_push and head != old_head and not local_heads:
            worktree = workdir / "owner_repo_2"
            git(worktree, "config", "user.name", "Test")
            git(worktree, "config", "user.email", "test@example.com")
            (worktree / "commit.txt").write_text("local commit")
            git(worktree, "add", "commit.txt")
            git(worktree, "commit", "-qm", "unpublished")
            local_heads.append(git(worktree, "rev-parse", "HEAD"))
            (worktree / "local.txt").write_text("unpublished")
        return SimpleNamespace(
            state="OPEN",
            head_sha=head,
            reasons=(),
            changes_requested=False,
            review_threads_complete=True,
            head_writable=True,
            mergeability="mergeable",
            ci_status="passing",
        )

    client.work_pull_request.side_effect = pull_request
    session = WorkdashSession(
        config=WorkdashConfig(
            workdir=str(workdir), agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}
        ),
        backend=backend,
        work_items=[pr],
        suggestion_markers={},
        zellij_session="workdash",
    )
    backend.resolve_analyze_command_tokens.return_value = ["pi", "-p"]
    monkeypatch.setattr(session, "prepare_autodispatch", lambda _: None)
    monkeypatch.setattr(
        session,
        "dispatch_once",
        MagicMock(
            side_effect=[
                {
                    "status": "launched",
                    "item_id": "owner/repo#PR-2",
                    "pane_id": "terminal_1",
                    "session": "workdash",
                },
                {"status": "no_ready_task"},
            ]
        ),
    )
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    reviewed = []
    artifact_checkout = []

    def agent(*, prompt, cwd, **kwargs):
        reviewed.append((git(cwd, "rev-parse", "HEAD"), (Path(cwd) / "change.txt").read_text()))
        if len(reviewed) == 2:
            artifact_checkout.append(Path(cwd))
            (Path(cwd) / ".pytest_cache").mkdir()
            (Path(cwd) / ".pytest_cache" / "data").write_text("keep")
            exclude = Path(
                git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
            )
            exclude.write_text(".pytest_cache/\n")
        if len(reviewed) == 1:
            if force_push:
                git(source, "reset", "--hard", "HEAD^")
            (source / "change.txt").write_text("new change")
            git(source, "commit", "-qam", "new")
            git(source, "push", "-q", "--force" if force_push else "--", "origin", "feature")
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Review outcome: success\nreviewed"
        )
        return ""

    monkeypatch.setattr("workdash.agent_execution.executor.run_in_pane", agent)
    entry = _AutodispatchSession(session, "owner/repo", None, None, automerge=True)
    entry.run()
    new_head = git(origin, "rev-parse", "refs/heads/feature")
    assert new_head != old_head
    assert entry.status == "finished", entry.error
    assert reviewed == [(old_head, "old change"), (new_head, "new change")]
    client.merge_pull_request.assert_called_once_with("owner/repo", 2, new_head)
    assert (artifact_checkout[0] / ".pytest_cache" / "data").read_text() == "keep"
    git(clone, "worktree", "remove", "--force", str(artifact_checkout[0]))
    artifact_checkout[0].parent.rmdir()
    if force_push:
        worktree = workdir / "owner_repo_2"
        assert git(worktree, "rev-parse", "HEAD") == local_heads[0]
        assert (worktree / "commit.txt").read_text() == "local commit"
        assert (worktree / "local.txt").read_text() == "unpublished"


@pytest.mark.parametrize("incomplete", ["opinions", "thread"])
def test_automerge_rejects_incomplete_github_feedback(monkeypatch, incomplete):
    from gh_work_state_fake import AFTER_PUSH, review
    from workdash.autodispatch import _AutodispatchSession

    github = install_fake_github(monkeypatch)
    pr = github.add_pull_request("owner/repo#2")
    if incomplete == "opinions":
        pr["reviews"] = [review(f"reviewer{i}", "APPROVED", created=AFTER_PUSH) for i in range(101)]
    else:
        pr["threads"] = [
            thread(
                comment("reviewer", created=AFTER_PUSH),
                *[comment("author", created=AFTER_PUSH) | {"publishedAt": None} for _ in range(10)],
            )
        ]
    state = GitHubClient().evaluate_pull_requests(
        [("owner/repo", 2)], github_username="testuser", todo_repository="testuser/todos"
    )[0]
    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.return_value = {
        "status": "launched",
        "item_id": "owner/repo#ISSUE-1",
        "pane_id": "terminal_1",
        "session": "s",
    }
    owner.review.return_value = {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": HEAD_SHA,
    }
    owner.backend.github_client.work_pull_request.return_value = state
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    entry.run()
    assert entry.status == "failed"
    assert owner.dispatch_once.call_count == 1
    owner.backend.github_client.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("pending", ["ci", "unknown_ci", "mergeability", "review"])
@pytest.mark.parametrize("outcome", ["merge", "fix", "head", "stop", "toggle"])
def test_automerge_waits_for_readiness(monkeypatch, pending, outcome):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.side_effect = [
        {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "s",
        },
        {"status": "no_ready_task"},
    ]
    state = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=(),
        changes_requested=pending == "review",
        review_threads_complete=True,
        head_writable=True,
        ci_status={"ci": "pending", "unknown_ci": "unknown"}.get(pending, "passing"),
        mergeability="unknown" if pending == "mergeability" else "mergeable",
    )
    github = owner.backend.github_client
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    owner.review.side_effect = lambda **kwargs: {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": state.head_sha,
    }
    calls = 0

    def lookup(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1 and outcome == "stop":
            entry.stop()
        elif calls == 1 and outcome == "toggle":
            entry.automerge = False
        elif calls == 2:
            assert entry.status == "running"
            github.merge_pull_request.assert_not_called()
            assert owner.dispatch_once.call_count == 1
            state.ci_status = "passing"
            state.mergeability = "mergeable"
            state.changes_requested = False
            if outcome == "fix":
                state.reasons = ("fix_ci",)
            elif outcome == "head":
                state.head_sha = "new-head"
        return state

    github.work_pull_request.side_effect = lookup
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry.run()
    assert entry.status == ("stopped" if outcome == "stop" else "finished"), entry.error
    if outcome in {"merge", "head"}:
        assert github.merge_pull_request.call_args.args[2] == (
            "new-head" if outcome == "head" else "head"
        )
        assert owner.review.call_count == (2 if outcome == "head" else 1)
    else:
        github.merge_pull_request.assert_not_called()


@pytest.mark.parametrize("disable_before_lookup", [False, True])
def test_automerge_does_not_resume_after_disabled_observation(monkeypatch, disable_before_lookup):
    from workdash.autodispatch import _AutodispatchSession

    owner = MagicMock()
    owner.config.configured_code_agents.return_value = ["pi"]
    owner.config.configured_analyze_agents.return_value = ["pi"]
    owner.dispatch_once.side_effect = [
        {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "s",
        },
        {"status": "no_ready_task"},
    ]
    owner.review.return_value = {
        "status": "reviewed",
        "item_id": "owner/repo#PR-2",
        "head_sha": "head",
    }
    entry = _AutodispatchSession(owner, "owner/repo", None, None, automerge=True)
    github = owner.backend.github_client
    github.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=(),
        changes_requested=False,
        review_threads_complete=True,
        head_writable=True,
        ci_status="pending",
        mergeability="unknown",
    )
    if disable_before_lookup:

        def require_item(_):
            entry.automerge = False
            return MagicMock()

        owner._require_item.side_effect = require_item
    else:

        def wait(timeout):
            if entry.stage == "waiting":
                entry.automerge = False
            return False

        monkeypatch.setattr(entry.stop_event, "wait", wait)

    lock = MagicMock()

    def unlock(*args):
        if not entry.automerge:
            entry.automerge = True

    lock.__exit__.side_effect = unlock
    entry.lock = lock
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.001)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: [])
    entry.run()
    assert entry.status == "finished", entry.error
    github.merge_pull_request.assert_not_called()
    assert github.work_pull_request.call_count == (0 if disable_before_lookup else 1)
