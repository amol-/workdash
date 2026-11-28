"""Autodispatch behavior at the dispatch and pane boundaries."""

import json
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import DataTable, Static
from webob import Request

import workdash.control as control_module
from gh_work_state_fake import install_fake_github
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession, _make_turbogears_app
from workdash.github_client import GitHubClient
from workdash.models import WorkItemKind, WorkItemType
from workdash.workdash import main

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
    monkeypatch.setattr(session, "review", lambda **_: {"status": "nothing_to_review"})
    calls = []
    panes = []

    def dispatch(*, scope, agent, group, before_launch, on_ready):
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
            "tab_name": group,
            "session": "workdash",
        }

    polled = threading.Event()

    def load_panes(_):
        snapshot = list(panes)
        if threading.current_thread().name.startswith("autodispatch-"):
            polled.set()
        return snapshot

    monkeypatch.setattr(session, "dispatch_once", dispatch)
    monkeypatch.setattr("workdash.autodispatch.launcher.zellij.load_zellij_panes", load_panes)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    yield {"session": session, "calls": calls, "panes": panes, "polled": polled}
    session.stop_autodispatch_sessions()


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


@given("an autodispatch server with two ready issues")
def server_two(dashboard_dispatch, scenario_state, monkeypatch, request):
    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2")
    github.pull_requests.clear()
    monkeypatch.setattr(
        session.backend.github_client, "fetch_launch_context", lambda _: {"state": "OPEN"}
    )
    release = threading.Event()

    def review(*, target, **kwargs):
        if target == "owner/repo#ISSUE-1":
            assert release.wait(5)
        return {"status": "nothing_to_review"}

    request.addfinalizer(release.set)
    scenario_state["release_review"] = release
    monkeypatch.setattr(session, "review", review)
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    _install_cli(monkeypatch, session)


@when("the user runs `workdash autodispatch owner/repo --group project`")
def start_project():
    assert main(["autodispatch", "owner/repo", "--group", "project"]) == 0


@then(parsers.parse("`workdash info` shows the {ordinal} issue developing in group `project`"))
def developing_in_project(scenario_state, capsys, ordinal):
    session = scenario_state["dashboard_session"]
    number = {"first": 1, "second": 2}[ordinal]
    wait_for(
        lambda: (
            (entry := session.autodispatch_sessions()[0])["stage"] == "developing"
            and entry["current_item"] == f"owner/repo#ISSUE-{number}"
        )
    )
    capsys.readouterr()
    assert main(["info"]) == 0
    output = capsys.readouterr().out
    assert f"group=project scope=owner/repo item=owner/repo#{number} stage=developing" in output
    assert "tab=project" in output


@when(parsers.parse("the {ordinal} agent pane exits"))
def finish_issue(scenario_state, ordinal):
    number = {"first": 1, "second": 2}[ordinal]
    scenario_state["github"].issues[f"owner/repo#{number}"]["state"] = "CLOSED"
    scenario_state["panes"].clear()


@then("`workdash info` shows the first issue being reviewed")
def reviewing_first(scenario_state, capsys):
    session = scenario_state["dashboard_session"]
    wait_for(lambda: session.autodispatch_sessions()[0]["stage"] == "reviewing")
    assert main(["info"]) == 0
    assert "item=owner/repo#1 stage=reviewing" in capsys.readouterr().out


@when("the review completes")
def finish_review(scenario_state):
    scenario_state["release_review"].set()


@then("`workdash info` shows the session has finished")
def session_finishes(scenario_state, capsys):
    session = scenario_state["dashboard_session"]
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")
    assert main(["info"]) == 0
    assert "finished group=project scope=owner/repo" in capsys.readouterr().out


@given("an autodispatch server with two independent running sessions")
def independent(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    session.config.workdir = None
    _install_cli(monkeypatch, session)
    for scope, group in [("owner/a", "a"), ("owner/b", "b")]:
        session.start_autodispatch(scope=scope, group=group)
        wait_for(lambda: session.autodispatch_sessions()[-1]["stage"] == "developing")


@when(
    "the user runs `workdash info` to inspect autodispatch sessions",
    target_fixture="autodispatch_info_output",
)
def run_info(capsys):
    capsys.readouterr()
    assert main(["info"]) == 0
    return capsys.readouterr().out


@then("both sessions show their group, scope and current task")
def progress(autodispatch_state, autodispatch_info_output):
    entries = [
        line for line in autodispatch_info_output.splitlines() if line.startswith("Autodispatch ")
    ]
    assert len(entries) == 2, entries
    first, second = entries
    assert "running group=a scope=owner/a item=owner/repo#1" in first
    assert "running group=b scope=owner/b item=owner/repo#2" in second
    autodispatch_state["stop_id"] = first.split()[1]


@when("the user runs `workdash autodispatch-stop` with one session's ID")
def stop_one(autodispatch_state, capsys):
    session_id = autodispatch_state["stop_id"]
    assert main(["autodispatch-stop", session_id]) == 0
    assert f"Autodispatch {session_id}: stopped" in capsys.readouterr().out


@then("its agent pane remains open and the other session continues")
def remains(autodispatch_state, capsys):
    panes = autodispatch_state["panes"]
    assert {pane["id"] for pane in panes} == {1, 2}
    panes[:] = [pane for pane in panes if pane["id"] != 2]
    session = autodispatch_state["session"]
    wait_for(lambda: session.autodispatch_sessions()[-1]["current_item"] == "owner/repo#ISSUE-3")
    assert {pane["id"] for pane in panes} == {1, 3}
    assert main(["info"]) == 0
    output = capsys.readouterr().out
    assert f"Autodispatch {autodispatch_state['stop_id']} stopped" in output
    assert "running group=b scope=owner/b item=owner/repo#3" in output
    panes[:] = [pane for pane in panes if pane["id"] != 1]
    autodispatch_state["polled"].clear()
    assert autodispatch_state["polled"].wait(2)
    assert autodispatch_state["calls"] == [("owner/a", "a"), ("owner/b", "b"), ("owner/b", "b")]


@then("the failed session and its error are visible")
def failure_visible(autodispatch_state, autodispatch_info_output):
    output = autodispatch_info_output
    assert "failed group=automatic scope=owner/repo#42" in output, repr(output)
    assert autodispatch_state["failure"] in output


@when("the user resolves the failure and runs `workdash autodispatch` for the same scope")
def restart_failed(autodispatch_state, monkeypatch, capsys):
    session = autodispatch_state["session"]
    monkeypatch.setattr(
        session, "dispatch_once", MagicMock(return_value={"status": "no_ready_task"})
    )
    assert main(["autodispatch", "owner/repo#42"]) == 0
    assert "finished (owner/repo#42, automatic)" in capsys.readouterr().out


@then("`workdash info` shows the replacement instead of the failure")
def failure_removed(autodispatch_state, capsys):
    assert main(["info"]) == 0
    output = capsys.readouterr().out
    assert "finished group=automatic scope=owner/repo#42" in output
    assert autodispatch_state["failure"] not in output
    assert output.count("Autodispatch ") == 1


@given("a coding agent is already working on ready issue `owner/repo#ISSUE-1`")
def coding_agent_running(dashboard_dispatch, scenario_state, monkeypatch):
    session = scenario_state["dashboard_session"]
    github = scenario_state["github"]
    github.add_issue("owner/repo#1")
    github.pull_requests.clear()
    monkeypatch.setattr(
        session.backend.github_client, "fetch_launch_context", lambda _: {"state": "OPEN"}
    )
    worktree = session.config.workdir / "owner_repo_9"
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *a, **kw: str(worktree))
    panes = scenario_state["panes"]
    panes.extend(
        [
            {"id": 7, "title": f"code_{worktree.name}", "pane_cwd": str(worktree)},
            {"id": 9, "title": "code_other"},
        ]
    )

    def close(_session, pane_id):
        panes[:] = [pane for pane in panes if f"terminal_{pane['id']}" != pane_id]

    monkeypatch.setattr(control_module.launcher.zellij, "close_zellij_pane", close)
    _install_cli(monkeypatch, session)


@when("the user runs `workdash autodispatch owner/repo#ISSUE-1`")
def start_for_issue():
    assert main(["autodispatch", "owner/repo#ISSUE-1"]) == 0


@then("the existing coding pane closes and autodispatch takes over the issue")
def issue_taken_over(scenario_state, capsys):
    session = scenario_state["dashboard_session"]
    wait_for(lambda: session.autodispatch_sessions()[0]["stage"] == "developing")
    capsys.readouterr()
    assert main(["info"]) == 0
    output = capsys.readouterr().out
    assert "scope=owner/repo#1 stage=developing" in output
    assert "terminal_7" not in output
    assert {pane["id"] for pane in scenario_state["panes"]} == {9, 23}


def _install_cli(monkeypatch, session):
    app = _make_turbogears_app(session)

    def request(_client, endpoint, payload):
        response = Request.blank(
            f"/api/v0/{endpoint}",
            method="POST",
            content_type="application/json",
            body=json.dumps(payload).encode(),
        ).get_response(app)
        assert response.status_int == 200, response.text
        return json.loads(response.body)["result"]

    monkeypatch.setattr(control_module.WorkdashControlClient, "request", request)


@given("an autodispatch session has failed")
def failed_cli_session(autodispatch_state, monkeypatch):
    failed_session(autodispatch_state, monkeypatch)
    session = autodispatch_state["session"]
    session.config.workdir = None
    _install_cli(monkeypatch, session)


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


@given("a ready issue selected in the dashboard and another item to triage")
def dashboard_issue_and_triage_item(dashboard_dispatch, scenario_state):
    dashboard_issue(dashboard_dispatch, scenario_state)
    scenario_state["triage_item"] = make_work_item(number=4, title="Update docs")


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


@when("the user presses d and chooses an autodispatch agent")
@when("the user presses d, chooses agent 1, and uses / to find the other item once work begins")
def dashboard_press_dispatch(scenario_state):
    session = scenario_state["dashboard_session"]
    item = scenario_state["dashboard_item"]
    triage_item = scenario_state.get("triage_item")
    session.work_items = [item] if triage_item is None else [item, triage_item]

    async def interact(app, pilot):
        footer = app.query_one("#status-footer", Static)
        before = footer.render().plain
        await pilot.press("d", "1")
        for _ in range(100):
            await pilot.pause(0.02)
            status = footer.render().plain
            if scenario_state.get("expect_no_ready"):
                if (
                    "no work available to dispatch" in status.lower()
                    or "blocked by" in status.lower()
                ):
                    break
            elif status != before:
                break
        scenario_state["dispatch_status"] = footer.render().plain
        scenario_state["dispatch_progress"] = (
            app.query_one("#autodispatch-progress", Static).render().plain
        )

        if triage_item is not None:
            progress = app.query_one("#autodispatch-progress", Static)
            for _ in range(100):
                if "developing" in progress.render().plain:
                    break
                await pilot.pause(0.02)
            assert "developing" in progress.render().plain
            await pilot.press("/", "d", "o", "c", "s", "enter")
            table = app.query_one("#work-items", DataTable)
            scenario_state["triage_titles"] = [
                str(table.get_row_at(index)[2]).lstrip("* ") for index in range(table.row_count)
            ]
            scenario_state["progress_after_triage"] = progress.render().plain

    run_app(work_items=session.work_items, session=session, interactions=interact)


@then("the dashboard has announced the start and shows the other item while autodispatch continues")
def dashboard_continues_triage(scenario_state):
    assert scenario_state["dispatch_status"].startswith("Started autodispatch")
    assert scenario_state["triage_titles"] == [scenario_state["triage_item"].title]
    assert (
        scenario_state["dashboard_expected"].replace("#ISSUE-", "#")
        in scenario_state["progress_after_triage"]
    )
    assert "developing" in scenario_state["progress_after_triage"]


@when("the user presses d and then Escape to cancel agent selection")
def dashboard_cancel_dispatch(scenario_state):
    session = scenario_state["dashboard_session"]
    item = scenario_state["dashboard_item"]
    session.work_items = [item]

    async def interact(app, pilot):
        await pilot.press("d")
        assert app.screen is not app.screen_stack[0]
        await pilot.press("escape")
        assert app.query_one("#work-items", DataTable).has_focus

    run_app(work_items=[item], session=session, interactions=interact)


@then("the work table is available again and no autodispatch session starts")
def dashboard_cancelled(scenario_state):
    assert scenario_state["dashboard_session"].autodispatch_sessions() == []


@then("an autodispatch session launches that linked issue")
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


@then("the dashboard reports that the issue is blocked by its open dependency")
def dashboard_blocked_by_dependency(scenario_state):
    assert "blocked by other/repo#4" in scenario_state["dispatch_status"].lower()
    assert "blocked by other/repo#4" in scenario_state["dispatch_progress"].lower()
    snapshot = scenario_state["dashboard_session"].autodispatch_sessions()[0]
    assert snapshot["outcome"] == "blocked by other/repo#4"
    assert not scenario_state.get("agent_launches")


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


@given("an autodispatch session is running for `owner/repo`")
def running_session(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    session.config.workdir = None
    _install_cli(monkeypatch, session)
    started = session.start_autodispatch(scope="owner/repo")
    autodispatch_state["previous_id"] = started["id"]
    wait_for(lambda: session.autodispatch_sessions()[0]["stage"] == "developing")


@when("the user runs `workdash autodispatch owner/repo --group project` again")
def start_again(autodispatch_state, capsys):
    assert main(["autodispatch", "owner/repo", "--group", "project"]) == 0
    output = capsys.readouterr().out
    assert "running (owner/repo, project)" in output
    autodispatch_state["replacement_id"] = output.split()[1].removesuffix(":")


@then("`workdash info` shows only the replacement session in group `project`")
def previous_replaced(autodispatch_state, capsys):
    replacement = autodispatch_state["replacement_id"]
    assert replacement != autodispatch_state["previous_id"]
    assert main(["info"]) == 0
    output = capsys.readouterr().out
    assert output.count("Autodispatch ") == 1
    assert f"Autodispatch {replacement} running group=project scope=owner/repo" in output


@given("ready work in `owner/repo` whose pull request will need a re-review")
def rereview_needed(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    session.config.workdir = None
    _install_cli(monkeypatch, session)
    prepare_merge(autodispatch_state, monkeypatch)
    session.backend.github_client.work_pull_request.return_value.changes_requested = True


@when("the user runs `workdash autodispatch owner/repo --automerge`")
def opt_into_automerge(autodispatch_state, capsys):
    assert main(["autodispatch", "owner/repo", "--automerge"]) == 0
    assert "running (owner/repo, automatic)" in capsys.readouterr().out
    wait_for(lambda: bool(autodispatch_state["panes"]))


@when("the completed pull request has been reviewed by Workdash")
def finish_work_before_rereview(autodispatch_state):
    session = autodispatch_state["session"]
    autodispatch_state["panes"].clear()
    wait_for(lambda: session.autodispatch_sessions()[0]["stage"] == "waiting")


@then("the session waits without merging or dispatching again")
def waiting_prevents_merge_and_dispatch(autodispatch_state, capsys):
    assert main(["info"]) == 0
    assert "running group=automatic scope=owner/repo item=owner/repo#1 stage=waiting" in (
        capsys.readouterr().out
    )
    assert not autodispatch_state["merged"]
    autodispatch_state["session"].dispatch_once.assert_called_once()


@when("the reviewer approves the pull request")
def reviewer_approves(autodispatch_state):
    session = autodispatch_state["session"]
    session.backend.github_client.work_pull_request.return_value.changes_requested = False
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")


@then("the pull request is merged")
def automerge_merged(autodispatch_state):
    assert autodispatch_state["merged"] == {("owner/repo", 2)}


def prepare_merge(autodispatch_state, monkeypatch):
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
        MagicMock(
            side_effect=lambda **kwargs: (
                {"status": "no_ready_task"} if autodispatch_state["calls"] else dispatch(**kwargs)
            )
        ),
    )
    session._require_item = MagicMock(
        return_value=make_work_item(item_type=WorkItemType.PR, number=2)
    )

    def review(**kwargs):
        head = github.work_pull_request.return_value.head_sha
        return {
            "status": "reviewed",
            "item_id": "owner/repo#PR-2",
            "content": "Review completed.",
            "head_sha": head,
        }

    monkeypatch.setattr(session, "review", MagicMock(side_effect=review))


@when("its completed pull request has been reviewed successfully")
def automerge_reviewed(autodispatch_state):
    session = autodispatch_state["session"]
    autodispatch_state["panes"].clear()
    wait_for(lambda: session.autodispatch_sessions()[0]["status"] == "finished")
    session.review.assert_called_once()


@given("an autodispatch session for the selected dashboard work")
def dashboard_automerge(autodispatch_state, monkeypatch):
    session = autodispatch_state["session"]
    item = make_work_item(item_type=WorkItemType.ISSUE, number=1)
    session.work_items = [item]
    autodispatch_state["item"] = item
    prepare_merge(autodispatch_state, monkeypatch)
    session.start_autodispatch(scope=f"{item.repo}#ISSUE-1")
    wait_for(lambda: bool(autodispatch_state["panes"]))


@when("the user presses m to turn automerge on and then m to turn it back off")
def dashboard_toggle_automerge(autodispatch_state):
    session = autodispatch_state["session"]

    async def interact(app, pilot):
        autodispatch_state["command_off"] = app.query_one("#command-footer", Static).render().plain
        await pilot.press("m")
        autodispatch_state["command_on"] = app.query_one("#command-footer", Static).render().plain
        autodispatch_state["enabled"] = session.autodispatch_sessions()[0]["automerge"]
        autodispatch_state["footer"] = app.query_one("#status-footer", Static).render().plain
        autodispatch_state["progress_text"] = (
            app.query_one("#autodispatch-progress", Static).render().plain
        )
        await pilot.press("m")
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


@when(
    "the user presses Shift+Tab, Down and m for the session in progress, then Tab and m for the work item"
)
def toggle_progress_automerge(autodispatch_state):
    async def interact(app, pilot):
        await pilot.press("shift+tab")
        autodispatch_state["focused"] = app.query_one("#autodispatch-scroll").has_focus
        await pilot.press("down")
        autodispatch_state["index"] = app._progress_index
        autodispatch_state["hint_before"] = app.query_one("#command-footer", Static).render().plain
        await pilot.press("m")
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
    assert "(f)ocus" in autodispatch_state["hint_before"]
    assert "(m)erge off" not in autodispatch_state["hint_before"]
    assert "(m)erge" in autodispatch_state["hint_before"]
    assert "(m)erge off" in autodispatch_state["hint_after"]
    assert "(d)ispatch" not in autodispatch_state["hint_after"]


@then("the dashboard identifies the work item's session after returning to the table")
def work_item_automerge_visible(autodispatch_state):
    first, _ = autodispatch_state["ids"]
    assert first in autodispatch_state["table_footer"]
    assert "enabled" in autodispatch_state["table_footer"]
    assert "(f)ocus" not in autodispatch_state["table_hint"]
    assert "(v)iew" in autodispatch_state["table_hint"]
    assert "(m)erge" in autodispatch_state["table_hint"]
    assert "(m)erge off" not in autodispatch_state["table_hint"]


@then("the pull request is left open")
def dashboard_merge_outcome(autodispatch_state):
    assert not autodispatch_state["merged"]


@then("the dashboard reports and displays both changes")
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


@given("a blocked autodispatch session visible in dashboard progress")
def blocked_session(scenario_state, work_items, tmp_path, monkeypatch):
    import workdash.autoclose as autoclose_module
    from workdash.autodispatch import _AutodispatchSession

    from .autoclose import _supervised_coding_pane_without_pull_request

    scenario_state["real_monotonic"] = autoclose_module.time.monotonic
    scenario_state["github"] = install_fake_github(monkeypatch)
    _supervised_coding_pane_without_pull_request(scenario_state, work_items, tmp_path, monkeypatch)
    session = scenario_state["api_session"]
    activity = _AutodispatchSession(session, "owner/repo", None, "pi")
    activity.pane = (session.zellij_session, scenario_state["supervised_pane_id"])
    activity.stage = "developing"
    activity.current_item = "owner/repo#ISSUE-42"
    session._autodispatch[activity.id] = activity
    scenario_state["work"] = tmp_path / "owner_repo_42" / "unpublished.txt"
    scenario_state["work"].write_text("keep this")
    for _ in range(40):
        scenario_state["clock"]["now"] += 30
        session.autoclose.poll_once()
    assert len(scenario_state["sent"]) == 3


@when("the user presses Shift+Tab and f, then types guidance directly into the focused agent pane")
def guide_blocked_agent(scenario_state, work_items, monkeypatch):
    import workdash.autoclose as autoclose_module

    session = scenario_state["api_session"]
    focus = MagicMock()
    monkeypatch.setattr(control_module.launcher.zellij, "focus_zellij_pane", focus)

    async def interact(app, pilot):
        progress = app.query_one("#autodispatch-progress", Static)
        assert "blocked" in progress.render().plain
        await pilot.press("shift+tab", "f")
        pane_id = scenario_state["supervised_pane_id"]
        footer = app.query_one("#status-footer", Static)
        for _ in range(100):
            if f"Focused pane {pane_id}" in footer.render().plain:
                break
            await pilot.pause(0.02)
        assert f"Focused pane {pane_id}" in footer.render().plain
        assert "blocked" in progress.render().plain
        focus.assert_called_once_with(session.zellij_session, pane_id)

        # Typing directly into Zellij is observed by Workdash only as changed pane content.
        scenario_state["screen"] = "Please continue with the implementation.\n"
        with monkeypatch.context() as patch:
            patch.setattr(
                autoclose_module.time, "monotonic", lambda: scenario_state["clock"]["now"]
            )
            scenario_state["clock"]["now"] += 30
            session.autoclose.poll_once()
        await app._poll_autodispatch_progress()
        scenario_state["resumed_progress"] = progress.render().plain

    with monkeypatch.context() as patch:
        patch.setattr(autoclose_module.time, "monotonic", scenario_state["real_monotonic"])
        run_app(
            work_items=work_items,
            session=session,
            interactions=interact,
            focus_callback=session.focus_pane,
        )


@then("the dashboard shows that the session has resumed")
def blocked_session_resumes(scenario_state):
    progress = scenario_state["resumed_progress"]
    assert "owner/repo#42" in progress
    assert "developing" in progress
    assert "blocked" not in progress


@when("the user presses Shift+Tab and s to stop the blocked session")
def stop_blocked_session(scenario_state, work_items, monkeypatch):
    import workdash.autoclose as autoclose_module

    async def stop(app, pilot):
        progress = app.query_one("#autodispatch-progress", Static)
        assert "blocked" in progress.render().plain
        await pilot.press("shift+tab", "s")
        await app._poll_autodispatch_progress()
        scenario_state["progress_visible"] = app.query_one("#autodispatch-scroll").display

    with monkeypatch.context() as patch:
        patch.setattr(autoclose_module.time, "monotonic", scenario_state["real_monotonic"])
        run_app(work_items=work_items, session=scenario_state["api_session"], interactions=stop)


@then("the session disappears from progress")
def stopped_session_disappears(scenario_state):
    assert not scenario_state["progress_visible"]


@then("its agent pane closes and its worktree is preserved")
def blocked_pane_closed(scenario_state):
    assert scenario_state["closed"] == [scenario_state["supervised_pane_id"]]
    assert scenario_state["api_session"].info()["autodispatch_sessions"][0]["status"] == "stopped"
    assert scenario_state["work"].read_text() == "keep this"


@given("an autodispatch session is reviewing after its coding pane closes")
def reviewing_after_pane_closes(autodispatch_state, monkeypatch, request):
    session = autodispatch_state["session"]
    entered, release = threading.Event(), threading.Event()

    def review(**kwargs):
        kwargs["pane_callback"]("terminal_99")
        entered.set()
        assert release.wait(3)
        return {"status": "nothing_to_review"}

    monkeypatch.setattr(session, "review", review)
    request.addfinalizer(release.set)
    session.start_autodispatch(scope="owner/repo")
    wait_for(lambda: len(autodispatch_state["panes"]) == 1)
    autodispatch_state["panes"].clear()
    assert entered.wait(3)


@when("the user presses Shift+Tab and f on the session in progress")
def focus_review_session(autodispatch_state):
    focused = MagicMock()

    async def interact(app, pilot):
        await pilot.press("shift+tab", "f")

    run_app(
        work_items=[],
        session=autodispatch_state["session"],
        interactions=interact,
        focus_callback=focused,
    )
    autodispatch_state["focused_review"] = focused


@then("the active review pane is focused")
def review_pane_focused(autodispatch_state):
    autodispatch_state["focused_review"].assert_called_once_with("terminal_99")


@given("a failed autodispatch session is visible in progress")
def failed_selected_in_progress(autodispatch_state, monkeypatch):
    failed_session(autodispatch_state, monkeypatch)


@when("the user presses Shift+Tab and s on the failed session")
def stop_selected_progress(autodispatch_state):
    async def interact(app, pilot):
        await pilot.press("shift+tab", "s")
        autodispatch_state["progress_after_stop"] = (
            app.query_one("#autodispatch-progress", Static).render().plain
        )
        await app._poll_autodispatch_progress()
        autodispatch_state["progress_after_poll"] = (
            app.query_one("#autodispatch-progress", Static).render().plain
        )

    run_app(work_items=[], session=autodispatch_state["session"], interactions=interact)


@then("the failed session disappears from progress")
def failed_session_disappears(autodispatch_state):
    assert "Failed:" not in autodispatch_state["progress_after_stop"]
    assert "Failed:" not in autodispatch_state["progress_after_poll"]
    assert autodispatch_state["session"].autodispatch_sessions()[0]["status"] == "failed"
