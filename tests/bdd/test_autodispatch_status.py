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
    assert [work.item.number for work in session._ready("other/repo#3", True, panes=[]).items] == [
        4
    ]
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


@pytest.mark.usefixtures("dashboard_dispatch")
def test_blocker_is_reported_by_cli_and_info(scenario_state, capsys, monkeypatch):
    import json
    from unittest.mock import MagicMock

    from webob import Request

    from workdash.control import _make_turbogears_app
    from workdash.workdash import WorkdashCommands, _print_pane_info

    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("other/repo#4", state="CLOSED")
    github.add_issue("another/repo#5")
    github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4", "another/repo#5")
    github.page_size = 1
    start = session.start_autodispatch

    def start_before_initial_check(**kwargs):
        result = start(**kwargs)
        result["initial_check_complete"] = False
        return result

    monkeypatch.setattr(session, "start_autodispatch", start_before_initial_check)
    monkeypatch.setattr(
        session, "info", MagicMock(side_effect=RuntimeError("pane info unavailable"))
    )
    app = _make_turbogears_app(session)

    def request(endpoint, payload):
        response = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        ).get_response(app)
        assert response.status_int == 200
        return json.loads(response.body)["result"]

    commands = WorkdashCommands()
    commands._client = MagicMock(request=request)
    assert (
        commands.autodispatch_cli(
            target="other/repo#3", agent="pi", group=None, stop=False, json_output=False
        )
        == 0
    )
    assert "blocked by another/repo#5" in capsys.readouterr().out
    snapshot = session.autodispatch_sessions()[0]
    assert snapshot["status"] == "finished"
    assert snapshot["outcome"] == "blocked by another/repo#5"
    _print_pane_info({"session": "workdash", "panes": [], "autodispatch_sessions": [snapshot]})
    assert "blocked by another/repo#5" in capsys.readouterr().out


@pytest.mark.usefixtures("dashboard_dispatch")
@pytest.mark.parametrize("state", ["closed", "unblocked", "blocked"])
def test_no_ready_issue_reports_blocker_check_failure(scenario_state, monkeypatch, state):
    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("other/repo#4")
    if state == "closed":
        github.issues["other/repo#3"]["state"] = "CLOSED"
        github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)
    elif state == "unblocked":
        github.pull_requests["owner/repo#9"]["author"] = "someone-else"
    else:
        github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)

    client = session.backend.github_client
    from unittest.mock import MagicMock

    check = MagicMock(side_effect=RuntimeError("GitHub unavailable"))
    monkeypatch.setattr(client, "open_issue_blockers", check)
    result = session.start_autodispatch(scope="other/repo#3")
    deadline = time.monotonic() + 2
    while not result["initial_check_complete"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        result = session.autodispatch_sessions()[0]
    if state == "blocked":
        assert result["status"] == "failed"
        assert "GitHub unavailable" in result["error"]
        check.assert_called_once()
    else:
        assert result["status"] == "finished"
        assert result["outcome"] is None
        check.assert_not_called()


@pytest.mark.usefixtures("dashboard_dispatch")
def test_blockers_clear_after_readiness(scenario_state, monkeypatch):
    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("other/repo#4")
    github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)
    query = github._IssueBlockers

    def clear_blocker(query_text):
        github.issues["other/repo#4"]["state"] = "CLOSED"
        return query(query_text)

    monkeypatch.setattr(github, "_IssueBlockers", clear_blocker)
    result = session.start_autodispatch(scope="other/repo#3")
    deadline = time.monotonic() + 2
    while not result["initial_check_complete"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        result = session.autodispatch_sessions()[0]
    assert result["status"] == "finished"
    assert result["outcome"] is None


@pytest.mark.usefixtures("dashboard_dispatch")
@pytest.mark.parametrize("json_output", [False, True])
def test_blocker_lookup_failure_is_reported(scenario_state, monkeypatch, capsys, json_output):
    import json
    from unittest.mock import MagicMock

    from webob import Request

    from workdash.control import _make_turbogears_app
    from workdash.workdash import main

    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("other/repo#4")
    github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)

    def fail(query):
        raise RuntimeError("GitHub unavailable")

    monkeypatch.setattr(github, "_IssueBlockers", fail)
    app = _make_turbogears_app(session)

    def request(endpoint, payload):
        response = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        ).get_response(app)
        assert response.status_int == 200
        return json.loads(response.body)["result"]

    monkeypatch.setattr(
        "workdash.workdash.WorkdashControlClient",
        lambda *args, **kwargs: MagicMock(request=request),
    )
    args = ["autodispatch", "other/repo#3"] + (["--json"] if json_output else [])
    assert main(args) == 1
    output = capsys.readouterr()
    if json_output:
        result = json.loads(output.out)
        assert result["status"] == "failed"
        assert "GitHub unavailable" in result["error"]
    else:
        assert "GitHub unavailable" in output.out + output.err


@pytest.mark.usefixtures("dashboard_dispatch")
@pytest.mark.parametrize("seen_running", [False, True])
def test_cli_session_blocker_reaches_dashboard_status_once(scenario_state, seen_running):
    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("other/repo#4")
    github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)
    app = WorkdashApp(session=session)

    async def exercise():
        async with app.run_test() as pilot:
            if seen_running:
                # Hold the initial lookup until the dashboard observes the new session.
                import threading
                from unittest.mock import patch

                ready = threading.Event()
                original = session.dispatch_once
                with patch.object(
                    session,
                    "dispatch_once",
                    side_effect=lambda **kw: (ready.wait(2), original(**kw))[1],
                ):
                    session.start_autodispatch(scope="other/repo#3")
                    await app._poll_autodispatch_progress()
                    ready.set()
                    for _ in range(100):
                        if session.autodispatch_sessions()[0]["status"] == "finished":
                            break
                        await pilot.pause(0.02)
            else:
                session.start_autodispatch(scope="other/repo#3")
                for _ in range(100):
                    if session.autodispatch_sessions()[0]["status"] == "finished":
                        break
                    await pilot.pause(0.02)
            await app._poll_autodispatch_progress()
            assert (
                "blocked by other/repo#4" in app.query_one("#status-footer", Static).render().plain
            )
            app._update_status("Other status")
            await app._poll_autodispatch_progress()
            assert app.query_one("#status-footer", Static).render().plain == "Other status"

    asyncio.run(exercise())


@pytest.mark.usefixtures("dashboard_dispatch")
def test_no_ready_issue_with_only_closed_blocker(scenario_state):
    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("other/repo#4", state="CLOSED")
    github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)
    github.pull_requests["owner/repo#9"]["author"] = "someone-else"
    session.start_autodispatch(scope="other/repo#3")
    deadline = time.monotonic() + 2
    while not (result := session.autodispatch_sessions()[0])["initial_check_complete"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert result["status"] == "finished"
    assert result["outcome"] is None


@pytest.mark.usefixtures("dashboard_dispatch")
def test_cli_issue_readiness_failure_exits_nonzero(scenario_state, monkeypatch, capsys):
    import json
    from unittest.mock import MagicMock

    from webob import Request

    from workdash.control import _make_turbogears_app
    from workdash.workdash import main

    session = scenario_state["dashboard_session"]
    monkeypatch.setattr(
        session, "dispatch_once", MagicMock(side_effect=RuntimeError("readiness unavailable"))
    )
    app = _make_turbogears_app(session)

    def request(endpoint, payload):
        response = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        ).get_response(app)
        assert response.status_int == 200
        return json.loads(response.body)["result"]

    monkeypatch.setattr(
        "workdash.workdash.WorkdashControlClient",
        lambda *args, **kwargs: MagicMock(request=request),
    )
    assert main(["autodispatch", "other/repo#3"]) == 1
    assert "readiness unavailable" in capsys.readouterr().err


@pytest.mark.usefixtures("dashboard_dispatch")
def test_cli_repository_scope_does_not_poll(scenario_state, monkeypatch, capsys):
    import json
    import threading
    from unittest.mock import MagicMock

    from webob import Request

    from workdash.control import _make_turbogears_app
    from workdash.workdash import main

    session = scenario_state["dashboard_session"]
    ready = threading.Event()
    monkeypatch.setattr(
        session, "dispatch_once", lambda **kwargs: (ready.wait(2), {"status": "no_ready_task"})[1]
    )
    app = _make_turbogears_app(session)
    endpoints = []

    def request(endpoint, payload):
        endpoints.append(endpoint)
        response = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        ).get_response(app)
        assert response.status_int == 200
        return json.loads(response.body)["result"]

    monkeypatch.setattr(
        "workdash.workdash.WorkdashControlClient",
        lambda *args, **kwargs: MagicMock(request=request),
    )
    try:
        assert main(["autodispatch", "owner/repo"]) == 0
        assert endpoints == ["autodispatch"]
        assert "running" in capsys.readouterr().out
    finally:
        ready.set()


@pytest.mark.usefixtures("dashboard_dispatch")
@pytest.mark.parametrize("json_output", [False, True])
def test_cli_stops_failed_session_successfully(scenario_state, monkeypatch, capsys, json_output):
    import json
    from unittest.mock import MagicMock

    from webob import Request

    from workdash.control import _make_turbogears_app
    from workdash.workdash import main

    session = scenario_state["dashboard_session"]
    monkeypatch.setattr(
        session, "dispatch_once", MagicMock(side_effect=RuntimeError("readiness unavailable"))
    )
    started = session.start_autodispatch(scope="other/repo#3")
    deadline = time.monotonic() + 2
    while session.autodispatch_status(started["id"])["session"]["status"] != "failed":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    app = _make_turbogears_app(session)

    def request(endpoint, payload):
        response = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        ).get_response(app)
        assert response.status_int == 200
        return json.loads(response.body)["result"]

    monkeypatch.setattr(
        "workdash.workdash.WorkdashControlClient",
        lambda *args, **kwargs: MagicMock(request=request),
    )
    args = ["autodispatch-stop", started["id"]] + (["--json"] if json_output else [])
    assert main(args) == 0
    output = capsys.readouterr()
    if json_output:
        assert json.loads(output.out)["status"] == "failed"
        assert "readiness unavailable" in json.loads(output.out)["error"]
    else:
        assert "failed" in output.out
        assert "readiness unavailable" in output.err
