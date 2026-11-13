"""Autodispatch behavior at the dispatch and pane boundaries."""

import json
import subprocess
import threading
import time
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import Static

import workdash.control as control_module
from gh_work_state_fake import install_fake_github
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession
from workdash.github_client import GitHubClient
from workdash.models import WorkItemKind, WorkItemType

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


@when("the user presses d to start autodispatch")
def dashboard_press_dispatch(scenario_state):
    session = scenario_state["dashboard_session"]
    item = scenario_state["dashboard_item"]
    session.work_items = [item]

    async def interact(app, pilot):
        footer = app.query_one("#status-footer", Static)
        before = footer.render().plain
        await pilot.press("d")
        for _ in range(100):
            await pilot.pause(0.02)
            if footer.render().plain != before:
                break
        scenario_state["dispatch_status"] = footer.render().plain

    run_app(work_items=[item], session=session, interactions=interact)


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
        await pilot.press("d")
        for _ in range(100):
            await pilot.pause(0.02)
            if (
                "GitHub unavailable"
                in app.query_one("#autodispatch-progress", Static).render().plain
            ):
                break
        scenario_state["failure_display"] = (
            app.query_one("#autodispatch-progress", Static).render().plain
        )
        scenario_state["failure_footer"] = app.query_one("#status-footer", Static).render().plain

    run_app(work_items=[item], session=session, interactions=interact)


@then("the dashboard shows the failed session and its error")
def dashboard_failure_visible(scenario_state):
    assert "failed" in scenario_state["failure_display"].lower()
    assert "GitHub unavailable" in scenario_state["failure_display"]
    assert "failed" in scenario_state["failure_footer"].lower()
    assert "GitHub unavailable" in scenario_state["failure_footer"]
