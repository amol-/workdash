"""Step definitions for local JSON control API scenarios."""

from __future__ import annotations

import json
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
from workdash.models import WorkItem, WorkItemType, worktree_item_number
from workdash.repo_worktree import worktree_path

from .common import (
    ensure_api_session,
    github_item_node,
    install_github_item_lookup,
    install_zellij_agent_fakes,
    seed_dashboard_item,
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

    monkeypatch.setattr("workdash.control.prepare_launch_agent_prompt", lambda *_a, **_kw: "PROMPT")
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


@given("an agent pane is running in that worktree")
def _agent_pane_running_in_worktree(scenario_state: dict[str, Any]) -> None:
    path = scenario_state["existing_worktree"]
    scenario_state.setdefault("panes", []).append(
        {"id": 7, "title": f"code_{Path(path).name}", "pane_cwd": path}
    )


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


@then("no coding agent is launched")
def _no_agent_launched(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("agent_launches", []) == []
