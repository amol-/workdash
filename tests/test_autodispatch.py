import asyncio
import json
import queue
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from webob import Request

from gh_work_state_fake import install_fake_github
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession, _make_turbogears_app
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import WorkdashApp


def test_api_starts_reports_and_stops_session_without_closing_pane(monkeypatch):
    waits = queue.Queue()
    original_wait = threading.Event.wait

    def observe_wait(event, timeout=None):
        if threading.current_thread().name.startswith("autodispatch-"):
            waits.put(timeout)
        return original_wait(event, timeout)

    monkeypatch.setattr(threading.Event, "wait", observe_wait)
    session = WorkdashSession(
        config=WorkdashConfig(),
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

    def worktree(_workdir, item):
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
    monkeypatch.setattr("workdash.control.get_merge_base", lambda _: None)
    monkeypatch.setattr("workdash.launcher.collect_launch_github_context", lambda _: {})
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
            agents={"pi": AgentConfig(launch="pi")},
        ),
        backend=MagicMock(),
        work_items=items,
        suggestion_markers={},
        zellij_session="workdash",
    )
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
        assert [work["issue_id"] for work in session.ready(scope="owner/repo")["items"]] == [
            "owner/repo#ISSUE-2"
        ]
        while not polls.empty():
            polls.get_nowait()
        polls.get(timeout=3)
        polls.get(timeout=3)
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
        wait_for("owner/repo#ISSUE-2")
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


def test_failed_pane_lookup_fails_session_without_dispatching_again(monkeypatch):
    session = WorkdashSession(
        config=WorkdashConfig(),
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


def test_tui_shows_running_sessions_and_remains_interactive():
    session = MagicMock()
    now = datetime.now(UTC)
    session.work_items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=42,
            title="Visible task",
            created_at=now,
            updated_at=now,
            url="https://github.com/owner/repo/issues/42",
        )
    ]
    session.suggestion_markers = {}
    session.autodispatch_sessions.return_value = [
        {
            "status": "running",
            "scope": "owner/a",
            "group": "first",
            "current_item": "owner/a#ISSUE-1",
            "elapsed_seconds": 5,
        },
        {
            "status": "running",
            "scope": "owner/b",
            "group": None,
            "current_item": "owner/b#ISSUE-2",
            "elapsed_seconds": 10,
        },
    ]
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            rows = [strip.text for strip in app.screen._compositor.render_strips()]
            assert any("first" in row and "ISSUE-1" in row and "5s" in row for row in rows)
            assert any("automatic" in row and "ISSUE-2" in row and "10s" in row for row in rows)
            assert any("Visible task" in row for row in rows)
            assert next(i for i, row in enumerate(rows) if "automatic" in row) < next(
                i for i, row in enumerate(rows) if "Visible task" in row
            )
            await pilot.press("/")
            assert len(app.screen_stack) > 1

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
