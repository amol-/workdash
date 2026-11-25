"""Step definitions for local JSON control API scenarios."""

from __future__ import annotations

import json
import subprocess
import threading
from contextlib import nullcontext
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from webob import Request

import workdash.control as control_module
from workdash.control import (
    _make_turbogears_app,
    _source_and_header_guarded_wsgi_app,
    format_work_item_id,
)
from workdash.models import WorkItem, WorkItemKind, WorkItemType, worktree_item_number
from workdash.repo_worktree import worktree_path

from .common import (
    ensure_api_session,
    github_item_node,
    install_github_item_lookup,
    install_zellij_agent_fakes,
    make_work_item,
    seed_dashboard_item,
    set_session_items,
)


def _call_api(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    endpoint: str,
    payload: dict[str, object] | None = None,
) -> None:
    app = _source_and_header_guarded_wsgi_app(
        _make_turbogears_app(ensure_api_session(scenario_state, work_items, tmp_path))
    )
    request = Request.blank(
        f"/api/v0/{endpoint}",
        method="POST",
        content_type="application/json",
        body=json.dumps(payload or {}).encode("utf-8"),
    )
    request.headers["X-Workdash"] = version("workdash")
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    scenario_state["api_status"] = response.status_int
    scenario_state["api_payload"] = json.loads(response.text)
    scenario_state.setdefault("api_responses", []).append(
        (response.status_int, scenario_state["api_payload"])
    )


def _install_fake_ensure_worktree(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record worktree preparation and answer with the directory Workdash names for the item."""

    def ensure_worktree(workdir: str, item: WorkItem, **_kwargs) -> str:
        scenario_state.setdefault("ensure_calls", []).append((workdir, item))
        repo = item.todo_target or item.repo
        todo = item.todo_target is not None
        return str(worktree_path(workdir, repo, worktree_item_number(item), todo=todo))

    monkeypatch.setattr("workdash.control.ensure_worktree", ensure_worktree)
    monkeypatch.setattr(
        "workdash.control.analysis_worktree",
        lambda *args, **kwargs: nullcontext(ensure_worktree(*args, **kwargs)),
    )


def _install_code_fakes(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_ensure_worktree(scenario_state, monkeypatch)
    monkeypatch.setattr(
        "workdash.github_client.GitHubClient.fetch_worktree_head",
        lambda self, item: ("pr-branch", item.repo),
    )
    monkeypatch.setattr(
        "workdash.control.GitHelper",
        lambda: SimpleNamespace(
            branch_tracks_repository=lambda *_: True, find_worktree_for_branch=lambda *_: None
        ),
    )
    if "api_session" in scenario_state:
        scenario_state["api_session"].git = control_module.GitHelper()
    monkeypatch.setattr("workdash.control.get_merge_base", lambda _path, **kwargs: None)

    def prepare(item, repo_path, **kwargs):
        scenario_state["prompt_analysis"] = kwargs.get("analysis_path")
        if kwargs.get("analysis_path") and item.kind in {
            WorkItemKind.REVIEW_REQUESTED_PR,
            WorkItemKind.TRACKED_PR,
        }:
            from workdash.launcher import build_launch_agent_prompt

            return build_launch_agent_prompt(
                item=item,
                github_context={},
                repo_path=repo_path,
                analysis_path=kwargs["analysis_path"],
            )
        return "PROMPT"

    monkeypatch.setattr("workdash.control.prepare_launch_agent_prompt", prepare)
    install_zellij_agent_fakes(scenario_state, monkeypatch)


def _api_result(scenario_state: dict[str, Any]) -> dict[str, Any]:
    assert scenario_state["api_status"] == 200, scenario_state["api_payload"]
    payload = scenario_state["api_payload"]
    assert payload["ok"] is True
    return payload["result"]


@given(parsers.parse("the current dashboard items include `{item_id}`"))
def _current_dashboard_items_include(
    item_id: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    item = seed_dashboard_item(work_items, scenario_state)
    assert format_work_item_id(item) == item_id


@given(parsers.parse("`workdash info` reports pane ID `{pane_id}`"))
def _workdash_info_reports_pane_id(pane_id: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["pane_id"] = pane_id


@when(parsers.parse("a client requests code for `{item_id}` with agent `{agent}`"))
def _client_requests_code(
    item_id: str,
    agent: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_code_fakes(scenario_state, monkeypatch)
    _call_api(scenario_state, work_items, tmp_path, "code", {"target": item_id, "agent": agent})


@when(
    parsers.parse(
        "a client requests code with autostart `{value}` for `{item_id}` with agent `{agent}`"
    )
)
def _client_requests_code_with_autostart(
    value: str,
    item_id: str,
    agent: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_code_fakes(scenario_state, monkeypatch)
    _call_api(
        scenario_state,
        work_items,
        tmp_path,
        "code",
        {"target": item_id, "agent": agent, "autostart": json.loads(value)},
    )


@then("the response reports status `already_running`")
def _response_reports_already_running(scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state)["status"] == "already_running"


@then(parsers.parse("the response is for `{item_id}`"))
def _response_is_for(item_id: str, scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state)["item_id"] == item_id


@then(parsers.parse("the agent works in the checkout of pull request `{ref}`"))
def _agent_works_in_pull_request_checkout(ref: str, scenario_state: dict[str, Any]) -> None:
    [(_workdir, item)] = scenario_state["ensure_calls"]
    assert (f"{item.repo}#{item.number}", item.item_type) == (ref, WorkItemType.PR)


@then(parsers.parse("the agent works in the checkout of issue `{ref}`"))
def _agent_works_in_issue_checkout(ref: str, scenario_state: dict[str, Any]) -> None:
    [(_workdir, item)] = scenario_state["ensure_calls"]
    assert (f"{item.repo}#{item.number}", item.item_type) == (ref, WorkItemType.ISSUE)


@when(parsers.parse("two clients request code for `{item_id}` at the same time"))
def _two_clients_request_code(
    item_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_code_fakes(scenario_state, monkeypatch)
    # A slow launch keeps the first request busy while the second one arrives.
    scenario_state["launch_delay"] = 0.05
    clients = [
        threading.Thread(
            target=_call_api,
            args=(
                scenario_state,
                work_items,
                tmp_path,
                "code",
                {"target": item_id, "agent": agent},
            ),
        )
        for agent in ("pi", "claude")
    ]
    for client in clients:
        client.start()
    for client in clients:
        client.join(timeout=5)
    assert not any(client.is_alive() for client in clients)


@when(parsers.parse("a client requests terminal for `{item_id}`"))
def _client_requests_terminal(
    item_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_ensure_worktree(scenario_state, monkeypatch)

    def fake_launch_terminal(repo_path, *, zellij_session=None):
        scenario_state.setdefault("terminal_launch_calls", []).append((repo_path, zellij_session))
        return SimpleNamespace(
            session=zellij_session,
            pane_id="terminal_23",
            pane_title="terminal_owner_repo_1",
            cwd=repo_path,
        )

    monkeypatch.setattr("workdash.control.launch_terminal_context", fake_launch_terminal)
    _call_api(scenario_state, work_items, tmp_path, "terminal", {"target": item_id})


@then(parsers.parse("the API reports the Zellij session `{session}` in its `session` field"))
def _api_reports_the_zellij_session(session: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_status"] == 200
    assert scenario_state["api_payload"]["result"]["session"] == session


@then("the server does not prepare a worktree")
def _server_does_not_prepare_worktree(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("ensure_calls", []) == []


# -- Explicit targets outside the dashboard ----------------------------------


@given(parsers.parse("GitHub has the accessible {kind} `{item_id}`"))
def _github_has_accessible_item(
    kind: str, item_id: str, scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _, label_number = item_id.partition("#")
    number = int(label_number.rpartition("-")[2])
    item_type = WorkItemType.ISSUE if kind == "issue" else WorkItemType.PR
    scenario_state.setdefault("github_items", {})[f"{repo}#{number}"] = github_item_node(
        item_type, repo, number
    )
    install_github_item_lookup(scenario_state, monkeypatch)


@given(parsers.parse("GitHub answers item lookups with {answer}"))
def _github_answers_item_lookups(
    answer: str, scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_state["github_answer"] = answer
    install_github_item_lookup(scenario_state, monkeypatch)


@given(
    parsers.parse(
        "the current dashboard items include `{pull_request_id}` which shares the worktree of "
        "`{issue_id}`"
    )
)
def _dashboard_includes_pull_request_sharing_worktree(
    pull_request_id: str,
    issue_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gh_work_state_fake import install_fake_github

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#5", closes=("owner/repo#1",))
    issue = seed_dashboard_item(work_items, scenario_state)
    pull_request = make_work_item(
        item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR, number=5, title="Fix it"
    )
    pull_request.closing_issue_numbers = (issue.number,)
    work_items.append(pull_request)
    set_session_items(scenario_state, work_items)
    assert [format_work_item_id(item) for item in work_items] == [issue_id, pull_request_id]


@given(parsers.parse("the worktree of `{item_id}` already exists"))
def _worktree_already_exists(
    item_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    repo, _, label_number = item_id.partition("#")
    path = worktree_path(session.config.workdir, repo, int(label_number.rpartition("-")[2]))
    path.mkdir(parents=True, exist_ok=True)
    scenario_state["existing_worktree"] = str(path)
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path",
        lambda _workdir, item, **kwargs: path if format_work_item_id(item) == item_id else None,
    )


@given("an exited agent pane is left in that worktree")
def _exited_agent_pane_in_worktree(scenario_state: dict[str, Any]) -> None:
    path = scenario_state["existing_worktree"]
    scenario_state.setdefault("panes", []).append(
        {"id": 3, "title": f"code_{Path(path).name}", "pane_cwd": path, "exited": True}
    )


@given("an agent pane is running in that worktree")
def _agent_pane_running_in_worktree(scenario_state: dict[str, Any]) -> None:
    path = scenario_state["existing_worktree"]
    scenario_state.setdefault("panes", []).append(
        {"id": 7, "title": f"code_{Path(path).name}", "pane_cwd": path}
    )


@given("a terminal pane is open in that worktree")
def _terminal_pane_in_worktree(scenario_state: dict[str, Any]) -> None:
    path = scenario_state["existing_worktree"]
    scenario_state.setdefault("panes", []).append(
        {"id": 4, "title": f"terminal_{Path(path).name}", "pane_cwd": path}
    )


@given("an agent pane is running in another item's worktree")
def _agent_pane_in_other_worktree(scenario_state: dict[str, Any]) -> None:
    other = Path(scenario_state["existing_worktree"]).with_name("owner_repo_2")
    scenario_state.setdefault("panes", []).append(
        {"id": 5, "title": f"code_{other.name}", "pane_cwd": str(other)}
    )


@given("Zellij cannot report the panes of the Workdash Zellij session")
def _zellij_cannot_report_panes(scenario_state: dict[str, Any]) -> None:
    scenario_state["zellij_unavailable"] = True


def _install_unusable_pane_listings(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    count: int,
) -> None:
    import workdash.launcher as launcher_module

    scenario_state["zellij_reads_real_panes"] = True
    panes = scenario_state.setdefault("panes", [])
    answers = [
        subprocess.CompletedProcess([], 0, stdout="Session 'workdash-main' not found.\n")
    ] * count

    def run(command, **_kwargs):
        if answers:
            return answers.pop()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(panes))

    def assert_unusable_answers_consumed():
        assert not answers, "an unusable pane listing was never read"

    monkeypatch.setattr(launcher_module, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(launcher_module, "_resolve_zellij_binary", lambda: "zellij")
    monkeypatch.setattr(launcher_module, "_ZELLIJ_PANE_READ_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        launcher_module.zellij, "load_zellij_panes", launcher_module.Zellij.load_zellij_panes
    )
    request.addfinalizer(assert_unusable_answers_consumed)


@given("Zellij answers the first pane listing unusably")
def _zellij_answers_first_listing_unusably(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _install_unusable_pane_listings(scenario_state, monkeypatch, request, 1)


@given("Zellij answers the first two pane listings unusably")
def _zellij_answers_first_two_listings_unusably(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    _install_unusable_pane_listings(scenario_state, monkeypatch, request, 2)


@given("launched agent panes never become visible in Zellij")
def _launched_panes_never_visible(scenario_state: dict[str, Any]) -> None:
    scenario_state["launched_panes_invisible"] = True


@then(parsers.parse("the server runs {action} for `{item_id}` in its worktree"))
def _server_runs_action_in_worktree(
    action: str, item_id: str, scenario_state: dict[str, Any]
) -> None:
    result = _api_result(scenario_state)
    assert result["item_id"] == item_id
    [(workdir, item)] = scenario_state["ensure_calls"]
    assert format_work_item_id(item) == item_id
    repo = item.todo_target or item.repo
    worktree = str(
        worktree_path(workdir, repo, worktree_item_number(item), todo=bool(item.todo_target))
    )
    if action == "analysis":
        assert scenario_state["analyze_calls"][-1] == (item_id, "codex")
    elif action == "code":
        assert [launch[0] for launch in scenario_state["agent_launches"]] == [worktree]
        assert result["status"] == "launched"
    else:
        assert scenario_state["terminal_launch_calls"] == [(worktree, "workdash-main")]


@then(parsers.parse("the API returns error `{code}` with HTTP status {status:d}"))
def _api_returns_error(code: str, status: int, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_status"] == status, scenario_state["api_payload"]
    assert scenario_state["api_payload"]["error"]["code"] == code


# -- Coding claims -------------------------------------------------------------


def _code_results(scenario_state: dict[str, Any]) -> list[dict[str, Any]]:
    responses = scenario_state["api_responses"]
    assert all(status == 200 for status, _payload in responses), responses
    return [payload["result"] for _status, payload in responses]


@then("exactly one coding agent is launched")
def _exactly_one_agent_launched(scenario_state: dict[str, Any]) -> None:
    assert len(scenario_state.get("agent_launches", [])) == 1, scenario_state.get("agent_launches")


@then("no coding agent is launched")
def _no_agent_launched(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("agent_launches", []) == []


@then("one response reports status `launched` with the new pane ID")
def _one_response_launched(scenario_state: dict[str, Any]) -> None:
    launched = [
        result for result in _code_results(scenario_state) if result["status"] == "launched"
    ]
    assert [result["pane_id"] for result in launched] == ["terminal_23"]


@then("the other response reports status `already_running` with the same pane ID")
def _other_response_already_running(scenario_state: dict[str, Any]) -> None:
    running = [
        result for result in _code_results(scenario_state) if result["status"] == "already_running"
    ]
    assert [result["pane_id"] for result in running] == ["terminal_23"]


@then("the last response reports status `already_running` with the pane ID of the first agent")
def _last_response_reports_first_agent(scenario_state: dict[str, Any]) -> None:
    first, last = _code_results(scenario_state)
    assert first["status"] == "launched"
    assert (last["status"], last["pane_id"]) == ("already_running", first["pane_id"])


@then(parsers.parse("the last response does not report `{agent}` as the running agent"))
def _last_response_does_not_report_agent(agent: str, scenario_state: dict[str, Any]) -> None:
    assert _code_results(scenario_state)[-1]["agent"] != agent


@then("the last response reports status `launched` with a new pane ID")
def _last_response_launched_new_pane(scenario_state: dict[str, Any]) -> None:
    result = _code_results(scenario_state)[-1]
    assert (result["status"], result["pane_id"]) == ("launched", "terminal_23")


@then("the coding agent starts in the existing worktree")
def _agent_starts_in_existing_worktree(scenario_state: dict[str, Any]) -> None:
    [launch] = scenario_state["agent_launches"]
    assert launch[0] == scenario_state["existing_worktree"]


@then("the API returns an error saying Zellij pane state is unavailable")
def _api_reports_zellij_unavailable(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_status"] == 503
    assert scenario_state["api_payload"]["error"]["code"] == "zellij_unavailable"


@then("both responses are errors saying the launch could not be confirmed")
def _both_responses_unconfirmed(scenario_state: dict[str, Any]) -> None:
    responses = scenario_state["api_responses"]
    assert [(status, payload["error"]["code"]) for status, payload in responses] == [
        (409, "launch_unconfirmed"),
        (409, "launch_unconfirmed"),
    ]
