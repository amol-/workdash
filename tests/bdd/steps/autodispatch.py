"""Autodispatch behavior at the dispatch and pane boundaries."""

import threading
import time
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, then, when

from workdash.control import WorkdashSession


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
    from gh_work_state_fake import install_fake_github
    from workdash.config import AgentConfig, WorkdashConfig
    from workdash.github_client import GitHubClient

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
