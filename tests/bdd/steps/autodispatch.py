"""Autodispatch behavior at the dispatch and pane boundaries."""

import json
import re
import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import Static

import workdash.control as control_module
from gh_work_state_fake import install_fake_github
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashControlError, WorkdashSession
from workdash.github_client import GitHubClient
from workdash.models import WorkItem, WorkItemKind, WorkItemType

from .common import install_zellij_agent_fakes, make_work_item, run_app
from .triage import is_linked_issues_query, linked_issues_response


@pytest.fixture
def autodispatch_state(monkeypatch):
    session = WorkdashSession(
        config=MagicMock(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.config.configured_code_agents.return_value = ["pi"]
    session.config.configured_analyze_agents.return_value = ["pi"]
    events = []
    monkeypatch.setattr(
        session, "review", lambda **kwargs: events.append(("review", kwargs["target"]))
    )
    calls = []
    panes = []

    def dispatch(*, scope, agent, group, before_launch):
        events.append(("dispatch", scope))
        calls.append((scope, group))
        if scope == "owner/empty":
            return {"status": "no_ready_task"}
        pane = f"terminal_{len(calls)}"
        panes.append({"id": len(calls), "title": pane})
        return {
            "status": "launched",
            "item_id": f"owner/repo#ISSUE-{len(calls)}",
            "pane_id": pane,
            "cwd": "/worktree",
            "session": "workdash",
        }

    polled = threading.Event()

    def load_panes(_):
        polled.set()
        return list(panes)

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", load_panes)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    yield {"session": session, "calls": calls, "panes": panes, "polled": polled, "events": events}
    session.stop_autodispatch_sessions()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


@given("an autodispatch server with two ready issues")
def server_two(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    dispatch = session.dispatch_once

    def limited(**kwargs):
        if len(autodispatch_state["calls"]) == 2:
            autodispatch_state["calls"].append((kwargs["scope"], kwargs["group"]))
            return {"status": "no_ready_task"}
        return dispatch(**kwargs)

    monkeypatch.setattr(session, "dispatch_once", limited)


@when("the user starts autodispatch in group `project`")
def start(autodispatch_state):
    autodispatch_state["id"] = autodispatch_state["session"].start_autodispatch(
        scope="owner/repo", group="project"
    )["id"]


@then("the first issue is dispatched in group `project`")
def first(autodispatch_state):
    wait_for(lambda: len(autodispatch_state["calls"]) == 1)
    assert autodispatch_state["calls"][0] == ("owner/repo", "project")


@when("its agent pane disappears")
def pane_disappears(autodispatch_state):
    wait_for(lambda: autodispatch_state["panes"])
    autodispatch_state["panes"].clear()


@then("the first issue is reviewed before the next dispatch")
def first_reviewed(autodispatch_state):
    wait_for(lambda: len(autodispatch_state["events"]) >= 3)
    assert autodispatch_state["events"][:3] == [
        ("dispatch", "owner/repo"),
        ("review", "owner/repo#ISSUE-1"),
        ("dispatch", "owner/repo"),
    ]


@then("the second issue is dispatched in group `project`")
def second(autodispatch_state):
    wait_for(lambda: len(autodispatch_state["calls"]) == 2)
    assert autodispatch_state["calls"][1] == ("owner/repo", "project")


@then("the autodispatch session finishes after no ready work remains")
def finished(autodispatch_state):
    session = autodispatch_state["session"]
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")
    assert len(autodispatch_state["calls"]) == 3


@given("an autodispatch server with two independent running sessions")
def independent(autodispatch_state):
    session = autodispatch_state["session"]
    autodispatch_state["ids"] = [
        session.start_autodispatch(scope=scope, group=group)["id"]
        for scope, group in [("owner/a", "a"), ("owner/b", "b")]
    ]
    wait_for(
        lambda: (
            len(snapshots := session.autodispatch_sessions()) == 2
            and all(entry["current_item"] for entry in snapshots)
        )
    )


@when("the user inspects autodispatch progress")
def inspect(autodispatch_state):
    autodispatch_state["progress"] = autodispatch_state["session"].autodispatch_sessions()


@then("both sessions show their group, target, current task and elapsed time")
def progress(autodispatch_state):
    sessions = autodispatch_state["progress"]
    assert {entry["group"] for entry in sessions} == {"a", "b"}
    assert {entry["scope"] for entry in sessions} == {"owner/a", "owner/b"}
    assert all(entry["current_item"] and entry["elapsed_seconds"] >= 0 for entry in sessions)


@when("the user stops one autodispatch session")
def stop(autodispatch_state):
    autodispatch_state["session"].stop_autodispatch(autodispatch_state["ids"][0])


@then("its agent pane remains open and the other session continues")
def remains(autodispatch_state):
    sessions = autodispatch_state["session"].autodispatch_sessions()
    assert [entry["status"] for entry in sessions] == ["stopped", "running"]
    assert len(autodispatch_state["panes"]) == 2
    panes = autodispatch_state["panes"]
    stopped_pane = int(sessions[0]["current_item"].rsplit("-", 1)[1])
    panes[:] = [pane for pane in panes if pane["id"] == stopped_pane]
    wait_for(lambda: len(autodispatch_state["calls"]) >= 3)
    wait_for(lambda: len(panes) == 2)
    assert any(pane["id"] == stopped_pane for pane in panes)
    panes[:] = [pane for pane in panes if pane["id"] != stopped_pane]
    autodispatch_state["polled"].clear()
    assert autodispatch_state["polled"].wait(2)
    assert autodispatch_state["calls"].count(("owner/a", "a")) == 1
    assert autodispatch_state["calls"].count(("owner/b", "b")) == 2
    assert [entry["status"] for entry in autodispatch_state["session"].autodispatch_sessions()] == [
        "stopped",
        "running",
    ]


@given("an autodispatch server with ready issues in different implementation repositories")
def cross_repo_server(scenario_state, monkeypatch):
    github = install_fake_github(monkeypatch)
    scenario_state["github"] = github
    github.add_issue("owner/parent#1", state="CLOSED")
    github.add_issue("owner/first#2", parent="owner/parent#1")
    github.add_issue("owner/second#3", parent="owner/parent#1")
    github.add_pull_request(
        "owner/implementation#51", closes=("owner/second#3",), mergeable="CONFLICTING"
    )
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser", agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}
        ),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    scenario_state["api_session"] = session
    launches = []
    panes = []

    def code(**kwargs):
        launches.append(kwargs)
        panes.append({"id": len(launches)})
        return {
            "status": "launched",
            "item_id": kwargs["target"],
            "pane_id": f"terminal_{len(launches)}",
            "session": "workdash",
        }

    monkeypatch.setattr(session, "code", code)
    monkeypatch.setattr(session, "review", MagicMock(return_value={"status": "nothing_to_review"}))
    monkeypatch.setattr("workdash.control.launcher.zellij.load_zellij_panes", lambda _: [])
    monkeypatch.setattr(
        "workdash.autodispatch.launcher.zellij.load_zellij_panes", lambda _: list(panes)
    )
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    scenario_state.update(auto_launches=launches, auto_panes=panes)


@when("the user starts autodispatch without a group")
def start_without_group(scenario_state):
    scenario_state["api_session"].start_autodispatch(scope="owner/parent#1")


@then("each issue is dispatched in its implementation repository's group")
def repository_groups(scenario_state):
    launches = scenario_state["auto_launches"]
    session = scenario_state["api_session"]
    try:
        wait_for(
            lambda: (
                bool(launches)
                and session.autodispatch_sessions()[0]["selected_group"] == launches[0]["group"]
            )
        )
        target = launches[0]["target"]
        issue = scenario_state["github"].issues[target.replace("#ISSUE-", "#")]
        issue["state"] = "CLOSED"
        scenario_state["auto_panes"].clear()
        wait_for(
            lambda: (
                len(launches) == 2
                and session.autodispatch_sessions()[0]["selected_group"] == launches[1]["group"]
            )
        )
        assert {launch["target"]: launch["group"] for launch in launches} == {
            "owner/first#ISSUE-2": "first",
            "owner/second#ISSUE-3": "implementation",
        }
        assert session.autodispatch_sessions()[0]["group"] is None
    finally:
        session.stop_autodispatch_sessions()


@given("an autodispatch session has failed")
def failed_session(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    autodispatch_state["failure"] = "dispatch failed: " + "x" * 200
    monkeypatch.setattr(session, "prepare_autodispatch", lambda _: None)
    monkeypatch.setattr(
        session,
        "dispatch_once",
        MagicMock(side_effect=RuntimeError(autodispatch_state["failure"])),
    )
    session.start_autodispatch(scope="owner/repo#42")
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "failed")


@then("the failure is visible in autodispatch progress")
def failure_visible(autodispatch_state):
    assert autodispatch_state["progress"][0]["error"] == autodispatch_state["failure"]
    # `workdash info` reports the whole error the TUI collapses onto one truncated line.
    [reported] = autodispatch_state["session"].info()["autodispatch_sessions"]
    assert reported["error"] == autodispatch_state["failure"]


@when("the user restarts autodispatch for the same scope")
def restart_failed(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    monkeypatch.setattr(
        session, "dispatch_once", MagicMock(return_value={"status": "no_ready_task"})
    )
    session.start_autodispatch(scope="owner/repo#ISSUE-42")


@then("the old failure is no longer shown")
def failure_removed(autodispatch_state):
    assert all(
        entry["status"] != "failed"
        for entry in autodispatch_state["session"].autodispatch_sessions()
    )


@given("an autodispatch session for a targeted todo issue URL in group `project`")
def progress_session(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    session.config.todo_repository = "owner/todos"
    release = threading.Event()
    autodispatch_state["release"] = release

    def dispatch(**kwargs):
        release.wait(2)
        return {
            "status": "launched",
            "item_id": "owner/target#ISSUE-WT42",
            "pane_id": "terminal_1",
            "session": "workdash",
            "group": "project",
            "launch_reason": "fix_ci",
        }

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    autodispatch_state["panes"].append({"id": 1})
    session.start_autodispatch(
        scope="http://github.com/owner/todos/issues/42/comments?x=1#note", group="project"
    )


@when("the user views its progress while checking and developing")
def view_progress(autodispatch_state):
    import asyncio

    from workdash.tui import WorkdashApp

    session = autodispatch_state["session"]
    app = WorkdashApp(work_items=[], session=session)

    async def view():
        async with app.run_test(size=(120, 30)):
            checking = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
            autodispatch_state["release"].set()
            wait_for(lambda: session.autodispatch_sessions()[0]["stage"] == "developing")
            app._update_autodispatch_progress()
            return checking, "\n".join(
                strip.text for strip in app.screen._compositor.render_strips()
            )

    autodispatch_state["visible_progress"] = asyncio.run(view())


@then(
    "the dashboard shows the group, stage, iteration and launch reason without repeating the issue"
)
def visible_progress(autodispatch_state):
    checking, developing = autodispatch_state["visible_progress"]
    assert "project  owner/todos#42  checking  1/" in checking
    assert "project  owner/todos#42  developing (fix ci)  1/" in developing
    assert "owner/target#ISSUE-WT42" not in developing


@pytest.fixture
def dashboard_dispatch(scenario_state, monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("other/repo#3")
    github.add_pull_request("owner/repo#9", closes=("other/repo#3",), mergeable="CONFLICTING")
    original_run = subprocess.run

    def run(command, *args, **kwargs):
        if not is_linked_issues_query(command):
            return original_run(command, *args, **kwargs)
        if scenario_state.get("linked_issue_lookup_denied"):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=json.dumps(
                    {
                        "data": {"p0": None},
                        "errors": [
                            {
                                "message": "Could not resolve to a Repository with the name "
                                "'owner/repo'.",
                                "path": ["p0"],
                            }
                        ],
                    }
                ),
                stderr="gh: Not Found (HTTP 404)",
            )
        closed_issues = {}
        for ref, pull_request in github.pull_requests.items():
            closed_issues[ref] = tuple(
                (repo, int(number))
                for repo, _, number in (close.partition("#") for close in pull_request["closes"])
            )
        return linked_issues_response(command, closed_issues=closed_issues)

    monkeypatch.setattr(subprocess, "run", run)
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            workdir=tmp_path,
            agents={"pi": AgentConfig(launch="pi", analyze="pi -p")},
        ),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", lambda item: ("branch", item.repo)
    )
    session.git = MagicMock()
    session.git.find_worktree_for_branch.return_value = None
    install_zellij_agent_fakes(scenario_state, monkeypatch)
    worktree = tmp_path / "owner_repo_9"
    worktree.mkdir()
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda *args, **kwargs: str(worktree))
    monkeypatch.setattr(control_module, "get_merge_base", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        control_module, "prepare_launch_agent_prompt", lambda *args, **kwargs: "PROMPT"
    )
    monkeypatch.setattr(session.autoclose, "supervise", lambda **kwargs: None)
    scenario_state["github"] = github
    scenario_state["dashboard_session"] = session
    yield
    session.stop_autodispatch_sessions()


@given("a ready issue selected in the dashboard")
def dashboard_issue(dashboard_dispatch, scenario_state):
    scenario_state["dashboard_item"] = make_work_item(repo="other/repo", number=3)
    scenario_state["dashboard_expected"] = "other/repo#ISSUE-3"


@given("the default coding agent cannot analyze but another agent can code and analyze")
def compatible_agent(scenario_state):
    agents = scenario_state["dashboard_session"].config.agents
    agents.clear()
    agents.update(
        {
            "code-only": AgentConfig(launch="code-only"),
            "pi": AgentConfig(launch="pi", analyze="pi -p"),
        }
    )


@given(
    "an authored pull request selected in the dashboard linked only to an issue in another repository"
)
def dashboard_pr(dashboard_dispatch, scenario_state):
    scenario_state["dashboard_item"] = make_work_item(
        item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR, number=9
    )
    scenario_state["dashboard_expected"] = "other/repo#ISSUE-3"


@given("an authored pull request without a linked issue in the dashboard")
def dashboard_pr_without_issue(dashboard_dispatch, scenario_state):
    dashboard_pr(dashboard_dispatch, scenario_state)
    scenario_state["github"].pull_requests["owner/repo#9"]["closes"] = ()


@given("an authored pull request linked to an issue that is not ready")
def dashboard_pr_not_ready(dashboard_dispatch, scenario_state):
    dashboard_pr(dashboard_dispatch, scenario_state)
    scenario_state["github"].issues["other/repo#3"]["state"] = "CLOSED"
    scenario_state["expect_no_ready"] = True


@given("an issue blocked by an open dependency selected in the dashboard")
def dashboard_blocked_issue(dashboard_dispatch, scenario_state):
    dashboard_issue(dashboard_dispatch, scenario_state)
    github = scenario_state["github"]
    github.add_issue("other/repo#4")
    github.issues["other/repo#3"]["blocked_by"] = ("other/repo#4",)
    scenario_state["expect_no_ready"] = True


@given(
    "an authored pull request selected in the dashboard linked to issues in several repositories"
)
def dashboard_pr_several_issues(dashboard_dispatch, scenario_state):
    github = scenario_state["github"]
    github.add_issue("beta/repo#1")
    github.add_issue("alpha/repo#9")
    github.add_issue("alpha/repo#3")
    github.pull_requests["owner/repo#9"]["closes"] = (
        "beta/repo#1",
        "alpha/repo#9",
        "alpha/repo#3",
    )
    scenario_state["dashboard_item"] = make_work_item(
        item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR, number=9
    )
    scenario_state["dashboard_expected"] = "alpha/repo#ISSUE-3"


@given("an authored pull request selected in the dashboard whose linked issues GitHub denies")
def dashboard_pr_denied(dashboard_dispatch, scenario_state):
    dashboard_pr(dashboard_dispatch, scenario_state)
    scenario_state["linked_issue_lookup_denied"] = True


@given(parsers.parse("a pull request of somebody else selected in the dashboard that is {kind}"))
def dashboard_other_pr(dashboard_dispatch, scenario_state, kind):
    kinds = {
        "waiting for my review": WorkItemKind.REVIEW_REQUESTED_PR,
        "in a tracked repository": WorkItemKind.TRACKED_PR,
    }
    scenario_state["dashboard_item"] = make_work_item(
        item_type=WorkItemType.PR, kind=kinds[kind], number=9
    )


@when("the user presses d and chooses an autodispatch agent")
@when("the user presses d to start autodispatch")
def dashboard_press_dispatch(scenario_state):
    session = scenario_state["dashboard_session"]
    item = scenario_state["dashboard_item"]
    session.work_items = [item]

    async def interact(app, pilot):
        footer = app.query_one("#status-footer", Static)
        before = footer.render().plain
        await pilot.press("d", "1")
        for _ in range(100):
            await pilot.pause(0.02)
            status = footer.render().plain
            if scenario_state.get("expect_no_ready"):
                if "no work available to dispatch" in status.lower():
                    break
            elif status != before:
                break
        scenario_state["dispatch_status"] = footer.render().plain

    run_app(work_items=[item], session=session, interactions=interact)


@when("the user cancels autodispatch agent selection")
def dashboard_cancel_dispatch(scenario_state):
    session = scenario_state["dashboard_session"]
    item = scenario_state["dashboard_item"]
    session.work_items = [item]

    async def interact(app, pilot):
        await pilot.press("d", "escape")

    run_app(work_items=[item], session=session, interactions=interact)


@then("no autodispatch session starts")
def dashboard_cancelled(scenario_state):
    assert scenario_state["dashboard_session"].autodispatch_sessions() == []


@then("an autodispatch session launches that issue")
@then("an autodispatch session launches that linked issue")
@then("an autodispatch session launches the lowest linked issue")
def dashboard_launched(scenario_state):
    session = scenario_state["dashboard_session"]
    wait_for(
        lambda: (
            session.autodispatch_sessions()
            and (
                session.autodispatch_sessions()[0]["current_item"]
                or session.autodispatch_sessions()[0]["status"] != "running"
            )
        )
    )
    snapshot = session.autodispatch_sessions()[0]
    expected = scenario_state["dashboard_expected"]
    assert snapshot["current_item"] == expected, snapshot
    assert snapshot["scope"] == expected
    assert len(scenario_state.get("agent_launches", [])) == 1


@then("an autodispatch session launches that issue with the compatible agent")
def dashboard_launched_compatible(scenario_state):
    dashboard_launched(scenario_state)
    assert scenario_state["dashboard_session"].autodispatch_sessions()[0]["agent"] == "pi"


@then("the dashboard reports that no work is available to dispatch")
def dashboard_no_ready_tasks(scenario_state):
    status = scenario_state["dispatch_status"].lower()
    assert "no work available to dispatch" in status
    assert "other/repo#3" in status


@then("the dashboard reports that the pull request has no linked issue and starts no session")
def dashboard_no_linked_issue(scenario_state):
    assert "without a linked issue" in scenario_state["dispatch_status"]
    assert scenario_state["dashboard_session"].autodispatch_sessions() == []


@then("the dashboard reports the linked issue lookup failure and starts no session")
def dashboard_linked_issue_failure(scenario_state):
    status = scenario_state["dispatch_status"]
    assert status.startswith("Dispatch failed:"), status
    assert "GitHub lookup for 'owner/repo#PR-9' failed: Could not resolve" in status, status
    assert scenario_state["dashboard_session"].autodispatch_sessions() == []


@then("the dashboard reports that dispatch is unavailable and starts no session")
def dashboard_dispatch_unavailable(scenario_state):
    assert "Dispatch is only available" in scenario_state["dispatch_status"]
    assert scenario_state["dashboard_session"].autodispatch_sessions() == []


@then("an autodispatch session starts for the selected issue")
@then("an autodispatch session starts for the linked issue")
def dashboard_session_started(scenario_state):
    session = scenario_state["dashboard_session"]
    wait_for(
        lambda: (
            session.autodispatch_sessions()
            and session.autodispatch_sessions()[0]["status"] != "running"
        )
    )
    snapshot = session.autodispatch_sessions()[0]
    assert snapshot["scope"] == scenario_state["dashboard_expected"]
    assert snapshot["status"] == "finished", snapshot
    assert snapshot["current_item"] is None
    assert scenario_state.get("agent_launches", []) == []


@when("the user's autodispatch session fails")
def dashboard_failure(scenario_state, monkeypatch):
    session = scenario_state["dashboard_session"]
    item = scenario_state["dashboard_item"]
    session.work_items = [item]
    monkeypatch.setattr(
        session, "dispatch_once", MagicMock(side_effect=RuntimeError("GitHub unavailable"))
    )

    async def interact(app, pilot):
        await pilot.press("d", "1")
        progress = app.query_one("#autodispatch-progress", Static)
        footer = app.query_one("#status-footer", Static)

        def reports_failure() -> bool:
            return all(
                "GitHub unavailable" in widget.render().plain for widget in (progress, footer)
            )

        # The footer keeps the dispatch action's "Started autodispatch" text until the
        # next progress tick reports the failure, which can follow the error line.
        for _ in range(150):
            await pilot.pause(0.02)
            if reports_failure():
                break
        scenario_state["failure_display"] = progress.render().plain
        scenario_state["failure_footer"] = footer.render().plain

    run_app(work_items=[item], session=session, interactions=interact)


@then("the dashboard shows the failed session and its error")
def dashboard_failure_visible(scenario_state):
    assert "failed" in scenario_state["failure_display"].lower()
    assert "GitHub unavailable" in scenario_state["failure_display"]
    assert "failed" in scenario_state["failure_footer"].lower()
    assert "GitHub unavailable" in scenario_state["failure_footer"]


def _issue(number: int) -> WorkItem:
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


@given("a coding agent is already running for issue `owner/repo#ISSUE-1`")
def coding_agent_running(scenario_state, monkeypatch, tmp_path):
    scenario_state["github"] = install_fake_github(monkeypatch)
    session = WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser", agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}
        ),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )

    def require_item(target):
        if target != "owner/repo#ISSUE-1":
            raise WorkdashControlError("invalid_target", target)
        return _issue(1)

    monkeypatch.setattr(session, "_require_item", require_item)
    worktree = tmp_path / "repo"
    worktree.mkdir()
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *a, **kw: str(worktree))
    panes = [
        {"id": 7, "title": f"code_{worktree.name}"},
        {"id": 9, "title": "code_other"},
    ]
    monkeypatch.setattr(session, "_live_panes", lambda: list(panes))

    def close_pane(_session, pane_id):
        panes[:] = [pane for pane in panes if f"terminal_{pane['id']}" != pane_id]

    close = MagicMock(side_effect=close_pane)
    monkeypatch.setattr("workdash.launcher.zellij.close_zellij_pane", close)
    scenario_state.update(agent_session=session, agent_panes=panes, close_pane=close, dispatched=[])

    def dispatch(**_):
        scenario_state["dispatched"].append(any(pane["id"] == 7 for pane in panes))
        return {"status": "no_ready_task"}

    monkeypatch.setattr(session, "dispatch_once", dispatch)


@given("issue `owner/repo#ISSUE-1` has ready work")
def issue_has_ready_work(scenario_state):
    scenario_state["github"].add_issue("owner/repo#1", state="OPEN")


@given("issue `owner/repo#ISSUE-1` has no ready work")
def issue_has_no_ready_work(scenario_state):
    scenario_state["github"].add_issue("owner/repo#1", state="CLOSED")


@when("the user starts autodispatch for issue `owner/repo#ISSUE-1`")
def start_for_issue(scenario_state):
    session = scenario_state["agent_session"]
    session.start_autodispatch(scope="owner/repo#ISSUE-1")
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")


@then("that coding agent pane is closed before the issue is dispatched")
def pane_closed_before_dispatch(scenario_state):
    assert scenario_state["dispatched"] == [False]
    assert [pane["id"] for pane in scenario_state["agent_panes"]] == [9]
    scenario_state["close_pane"].assert_called_once_with("workdash", "terminal_7")


@then("that coding agent pane stays open")
def pane_stays_open(scenario_state):
    assert [pane["id"] for pane in scenario_state["agent_panes"]] == [7, 9]
    scenario_state["close_pane"].assert_not_called()


@when("the user starts autodispatch for repository `owner/repo`")
def start_for_repository(scenario_state):
    session = scenario_state["agent_session"]
    session.start_autodispatch(scope="owner/repo")
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")


@given("an autodispatch session is running for `owner/repo`")
def running_session(autodispatch_state):
    session = autodispatch_state["session"]
    session.start_autodispatch(scope="owner/repo")
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "running")


@when("the user starts autodispatch again for `owner/repo`")
def start_again(autodispatch_state):
    session = autodispatch_state["session"]
    [previous] = session.autodispatch_sessions()
    autodispatch_state["previous"] = session._autodispatch[previous["id"]]
    autodispatch_state["id"] = session.start_autodispatch(scope="owner/repo")["id"]


@then("the previous session is stopped and replaced")
def previous_replaced(autodispatch_state):
    session = autodispatch_state["session"]
    assert autodispatch_state["previous"].status == "stopped"
    assert [entry["id"] for entry in session.autodispatch_sessions()] == [autodispatch_state["id"]]


@given("an autodispatch session developing an issue in a selected group")
def info_developing_session(monkeypatch):
    client = MagicMock()
    client.request.return_value = {
        "session": "workdash",
        "panes": [],
        "autodispatch_sessions": [
            {
                "id": "session-1",
                "status": "running",
                "group": None,
                "selected_group": "project",
                "scope": "owner/repo",
                "current_item": "owner/repo#ISSUE-42",
                "stage": "developing",
                "launch_reason": "fix_ci",
                "iteration": 2,
                "elapsed_seconds": 31,
            }
        ],
    }
    monkeypatch.setattr("workdash.workdash.WorkdashControlClient", lambda *a, **kw: client)


@when("the user runs workdash info for that session")
def run_info_for_session(capsys):
    from workdash.workdash import main

    assert main(["info"]) == 0


@then("info shows the group, scope, current item, stage, launch reason, iteration and elapsed time")
def info_shows_progress(capsys):
    output = capsys.readouterr().out
    for value in ("project", "owner/repo", "42", "developing", "fix ci", "2", "31s"):
        assert value in output


@given("an autodispatch session with automerge enabled")
def automerge_session(autodispatch_state, monkeypatch, *, enabled=True, scope="owner/repo"):
    session = autodispatch_state["session"]
    github = session.backend.github_client
    merged = set()
    monkeypatch.setattr(
        github, "merge_pull_request", lambda repo, number, sha: merged.add((repo, number))
    )
    autodispatch_state["merged"] = merged
    github.work_pull_request.return_value = SimpleNamespace(
        state="OPEN",
        head_sha="head",
        reasons=(),
        changes_requested=False,
        review_threads_complete=True,
        mergeability="mergeable",
        ci_status="passing",
    )
    dispatch = session.dispatch_once
    monkeypatch.setattr(
        session,
        "dispatch_once",
        lambda **kwargs: (
            {"status": "no_ready_task"} if autodispatch_state["calls"] else dispatch(**kwargs)
        ),
    )
    session._require_item = MagicMock(
        return_value=make_work_item(item_type=WorkItemType.PR, number=2)
    )

    def review(**kwargs):
        head = github.work_pull_request.return_value.head_sha
        autodispatch_state.setdefault("reviewed_heads", []).append(head)
        if autodispatch_state.get("change_during_review"):
            github.work_pull_request.return_value.head_sha = "new-head"
            autodispatch_state["change_during_review"] = False
        return {
            "status": "reviewed",
            "item_id": "owner/repo#PR-2",
            "content": "Review completed.",
            "head_sha": head,
        }

    monkeypatch.setattr(session, "review", review)
    session.start_autodispatch(scope=scope, automerge=enabled)
    wait_for(lambda: bool(autodispatch_state["panes"]))


@when("its completed pull request has been reviewed successfully")
def automerge_reviewed(autodispatch_state):
    autodispatch_state["panes"].clear()
    wait_for(
        lambda: autodispatch_state["session"].autodispatch_sessions()[0]["status"] == "finished"
    )


@then("the pull request is merged")
def automerge_merged(autodispatch_state):
    assert autodispatch_state["merged"] == {("owner/repo", 2)}


@given("an autodispatch session for the selected dashboard work")
def dashboard_automerge(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    item = make_work_item(item_type=WorkItemType.ISSUE, number=1)
    session.work_items = [item]
    autodispatch_state["item"] = item
    automerge_session(autodispatch_state, monkeypatch, enabled=False, scope=f"{item.repo}#ISSUE-1")


@when("the user toggles automerge")
def dashboard_toggle_automerge(autodispatch_state):
    session = autodispatch_state["session"]

    async def interact(app, pilot):
        autodispatch_state["command_off"] = app.query_one("#command-footer", Static).render().plain
        await pilot.press("m")
        await pilot.pause()
        autodispatch_state["command_on"] = app.query_one("#command-footer", Static).render().plain
        autodispatch_state["enabled"] = session.autodispatch_sessions()[0]["automerge"]
        autodispatch_state["footer"] = app.query_one("#status-footer", Static).render().plain
        autodispatch_state["progress_text"] = (
            app.query_one("#autodispatch-progress", Static).render().plain
        )
        await pilot.press("m")
        await pilot.pause()
        autodispatch_state["disabled_footer"] = (
            app.query_one("#status-footer", Static).render().plain
        )
        autodispatch_state["command_disabled"] = (
            app.query_one("#command-footer", Static).render().plain
        )

    run_app(work_items=[autodispatch_state["item"]], session=session, interactions=interact)


@given("the dashboard has different sessions selected in the work table and progress")
def dashboard_different_sessions(autodispatch_state):
    session = autodispatch_state["session"]
    item = make_work_item(item_type=WorkItemType.ISSUE, number=1)
    session.work_items = [item]
    autodispatch_state["item"] = item
    first = session.start_autodispatch(scope="owner/repo#ISSUE-1")["id"]
    wait_for(lambda: len(autodispatch_state["panes"]) == 1)
    second = session.start_autodispatch(scope="owner/repo#ISSUE-2")["id"]
    wait_for(lambda: len(autodispatch_state["panes"]) == 2)
    autodispatch_state["ids"] = (first, second)


@when("the user presses m with progress focused")
def toggle_progress_automerge(autodispatch_state):
    async def interact(app, pilot):
        await pilot.press("shift+tab")
        autodispatch_state["focused"] = app.query_one("#autodispatch-scroll").has_focus
        await pilot.press("down")
        autodispatch_state["index"] = app._progress_index
        autodispatch_state["hint_before"] = app.query_one("#command-footer", Static).render().plain
        await pilot.press("m")
        await pilot.pause()
        autodispatch_state["footer"] = app.query_one("#status-footer", Static).render().plain
        autodispatch_state["hint_after"] = app.query_one("#command-footer", Static).render().plain
        autodispatch_state["after_progress"] = {
            entry["id"]: entry["automerge"]
            for entry in autodispatch_state["session"].autodispatch_sessions()
        }
        await pilot.press("tab")
        autodispatch_state["table_hint"] = app.query_one("#command-footer", Static).render().plain
        await pilot.press("m")
        autodispatch_state["table_footer"] = app.query_one("#status-footer", Static).render().plain

    run_app(
        work_items=[autodispatch_state["item"]],
        session=autodispatch_state["session"],
        interactions=interact,
    )


@then("only the selected progress session changes and the dashboard identifies it")
def progress_automerge_visible(autodispatch_state):
    first, second = autodispatch_state["ids"]
    assert autodispatch_state["focused"]
    assert autodispatch_state["index"] == 1
    entries = {
        entry["id"]: entry for entry in autodispatch_state["session"].autodispatch_sessions()
    }
    assert not autodispatch_state["after_progress"][first]
    assert autodispatch_state["after_progress"][second]
    assert entries[first]["automerge"]
    assert entries[second]["automerge"]
    assert second in autodispatch_state["footer"]
    assert "enabled" in autodispatch_state["footer"]
    assert first in autodispatch_state["table_footer"]


@then("the command hint shows the selected session's automerge state")
def progress_automerge_hint(autodispatch_state):
    assert "(m)erge off" not in autodispatch_state["hint_before"]
    assert "(m)erge" in autodispatch_state["hint_before"]
    assert "(m)erge off" in autodispatch_state["hint_after"]
    assert "(d)ispatch" not in autodispatch_state["hint_after"]
    assert "(m)erge" in autodispatch_state["table_hint"]
    assert "(m)erge off" not in autodispatch_state["table_hint"]


@when(parsers.parse("the user toggles automerge {times}"))
def dashboard_toggle_for_merge(autodispatch_state, times):
    async def interact(app, pilot):
        for _ in range(1 if times == "once" else 2):
            await pilot.press("m")
            await pilot.pause()

    run_app(
        work_items=[autodispatch_state["item"]],
        session=autodispatch_state["session"],
        interactions=interact,
    )


@then(parsers.parse("after toggling, the pull request is {outcome}"))
def dashboard_merge_outcome(autodispatch_state, outcome):
    assert autodispatch_state["merged"] == ({("owner/repo", 2)} if outcome == "merged" else set())


@then("the dashboard reports and displays the new automerge state")
def dashboard_automerge_visible(autodispatch_state):
    assert "(m)erge" in autodispatch_state["command_off"]
    assert "(m)erge off" in autodispatch_state["command_on"]
    assert "(d)ispatch" not in autodispatch_state["command_on"]
    assert autodispatch_state["enabled"]
    assert "enabled" in autodispatch_state["footer"]
    assert "[automerge]" in autodispatch_state["progress_text"]
    assert "disabled" in autodispatch_state["disabled_footer"]
    assert "(m)erge" in autodispatch_state["command_disabled"]
    assert "(m)erge off" not in autodispatch_state["command_disabled"]
    assert not autodispatch_state["session"].autodispatch_sessions()[0]["automerge"]


@when("the pull request changes during review")
def automerge_head_changes(autodispatch_state):
    session = autodispatch_state["session"]
    autodispatch_state["change_during_review"] = True
    autodispatch_state["panes"].clear()
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] in ("finished", "failed"))


@then("the new head is reviewed before the pull request is merged")
def automerge_head_reviewed(autodispatch_state):
    assert autodispatch_state["reviewed_heads"] == ["head", "new-head"]
    assert autodispatch_state["merged"] == {("owner/repo", 2)}


@when("the review cannot post a major finding")
def failed_review_posting(autodispatch_state, monkeypatch, tmp_path):
    from workdash.review import review_pull_request

    session = autodispatch_state["session"]
    item = make_work_item(item_type=WorkItemType.PR, number=2)
    git = MagicMock()
    git.get_git_path.return_value = tmp_path / "exclude"

    def agent(*, prompt, **kwargs):
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Review outcome: failure\nGitHub rejected the major finding."
        )
        return ""

    monkeypatch.setattr("workdash.agent_execution.executor.run_in_pane", agent)
    monkeypatch.setattr(
        session,
        "review",
        lambda **kwargs: review_pull_request(
            item,
            context={},
            cwd=str(tmp_path),
            tokens=["pi"],
            document=tmp_path / "review.md",
            git=git,
            zellij_session="s",
            group=None,
        ),
    )
    autodispatch_state["panes"].clear()
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "failed")


@then("the session fails without merging the pull request")
def review_failure_prevents_merge(autodispatch_state):
    assert (
        "review outcome not confirmed"
        in autodispatch_state["session"].autodispatch_sessions()[0]["error"]
    )
    assert not autodispatch_state["merged"]


@when("a reviewer has requested changes and has been asked to review again")
def reviewer_awaited(autodispatch_state):
    session = autodispatch_state["session"]
    session.backend.github_client.work_pull_request.return_value.changes_requested = True
    autodispatch_state["panes"].clear()
    wait_for(lambda: session.backend.github_client.work_pull_request.called)


@then("the session waits without merging or dispatching again")
def waiting_prevents_merge_and_dispatch(autodispatch_state):
    assert autodispatch_state["session"].autodispatch_sessions()[0]["status"] == "running"
    assert not autodispatch_state["merged"]
    assert len(autodispatch_state["calls"]) == 1


@when("the reviewer approves the pull request")
def reviewer_approves(autodispatch_state):
    session = autodispatch_state["session"]
    session.backend.github_client.work_pull_request.return_value.changes_requested = False
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")


@given("an autodispatch session has blocked after three reminders without a pull request")
def blocked_session(scenario_state, work_items, tmp_path, monkeypatch, caplog):
    import workdash.autoclose as autoclose_module
    from workdash.autodispatch import _AutodispatchSession

    from .autoclose import _supervised_coding_pane_without_pull_request

    scenario_state["real_monotonic"] = autoclose_module.time.monotonic
    scenario_state["github"] = install_fake_github(monkeypatch)
    _supervised_coding_pane_without_pull_request(
        "owner/repo#42", scenario_state, work_items, tmp_path, monkeypatch, caplog
    )
    session = scenario_state["api_session"]
    activity = _AutodispatchSession(session, "owner/repo", None, "pi")
    activity.pane = (session.zellij_session, scenario_state["supervised_pane_id"])
    session._autodispatch[activity.id] = activity
    scenario_state["work"] = tmp_path / "owner_repo_42" / "unpublished.txt"
    scenario_state["work"].write_text("keep this")
    for _ in range(40):
        scenario_state["clock"]["now"] += 30
        session.autoclose.poll_once()
    assert len(scenario_state["sent"]) == 3
    assert session.info()["autodispatch_sessions"][0]["status"] == "blocked"


@then("the blocked session is visible in info and on the dashboard")
def blocked_visible(scenario_state, work_items, monkeypatch, capsys):
    import workdash.autoclose as autoclose_module
    from workdash.workdash import main

    session = scenario_state["api_session"]
    client = MagicMock()
    client.request.return_value = session.info()
    monkeypatch.setattr("workdash.workdash.WorkdashControlClient", lambda *a, **kw: client)
    assert main(["info"]) == 0
    entry = client.request.return_value["autodispatch_sessions"][0]
    assert f"Autodispatch {entry['id']} blocked" in capsys.readouterr().out

    async def check(app, pilot):
        assert "blocked" in app.query_one("#autodispatch-progress", Static).render().plain

    with monkeypatch.context() as patch:
        patch.setattr(autoclose_module.time, "monotonic", scenario_state["real_monotonic"])
        run_app(work_items=work_items, session=session, interactions=check)


@when("its agent pane content changes")
def blocked_pane_changes(scenario_state):
    scenario_state["screen"] = "agent resumed work"
    scenario_state["clock"]["now"] += 30
    scenario_state["api_session"].autoclose.poll_once()


@when("the user sends input to its agent pane through Workdash")
def send_blocked_pane_input(scenario_state):
    scenario_state["api_session"].pane_send(
        pane_id=scenario_state["supervised_pane_id"], data="continue"
    )


@then("the session resumes running")
def blocked_session_resumes(scenario_state):
    assert scenario_state["api_session"].info()["autodispatch_sessions"][0]["status"] == "running"


@when("the user stops the blocked session from the dashboard")
def stop_blocked_session(scenario_state, work_items, monkeypatch):
    import workdash.autoclose as autoclose_module

    async def stop(app, pilot):
        assert "blocked" in app.query_one("#autodispatch-progress", Static).render().plain
        await pilot.press("shift+tab", "s")

    with monkeypatch.context() as patch:
        patch.setattr(autoclose_module.time, "monotonic", scenario_state["real_monotonic"])
        run_app(work_items=work_items, session=scenario_state["api_session"], interactions=stop)


@then("its agent pane closes and its worktree is preserved")
def blocked_pane_closed(scenario_state):
    assert scenario_state["closed"] == [scenario_state["supervised_pane_id"]]
    assert scenario_state["api_session"].info()["autodispatch_sessions"][0]["status"] == "stopped"
    assert scenario_state["work"].read_text() == "keep this"
