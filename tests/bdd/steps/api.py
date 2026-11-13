"""Step definitions for local JSON control API scenarios."""

from __future__ import annotations

import base64
import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from webob import Request

import workdash.control as control_module
import workdash.github_client as github_client_module
from workdash.analysis_cache import AnalysisCache
from workdash.backend import compute_suggestion_markers
from workdash.control import _make_turbogears_app, _source_filtered_wsgi_app, format_work_item_id
from workdash.models import WorkItem, WorkItemKind, WorkItemType, worktree_item_number
from workdash.repo_worktree import worktree_path

from .common import (
    NOW_UTC,
    api_config,
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
    app = _source_filtered_wsgi_app(
        _make_turbogears_app(ensure_api_session(scenario_state, work_items, tmp_path)),
        scenario_state.get("accepted_ips", ()),
    )
    request = Request.blank(
        f"/api/v0/{endpoint}",
        method="POST",
        content_type="application/json",
        body=json.dumps(payload or {}).encode("utf-8"),
    )
    request.environ["REMOTE_ADDR"] = scenario_state.get("remote_addr", "127.0.0.1")
    request.headers.update(scenario_state.get("forwarding_headers", {}))
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


@given("no server-backed Workdash session is already running")
def _no_server_backed_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["server_port_busy"] = False
    scenario_state["sessions"] = ["workdash-main"]


@given(parsers.parse("a server-backed Workdash session is already running on `{address}`"))
def _server_backed_session_already_running(address: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["server_port_busy"] = True
    scenario_state["server_address"] = address
    scenario_state["sessions"] = ["workdash-main"]


@given("the Workdash Zellij session has live Workdash-owned panes")
def _workdash_zellij_session_has_live_panes(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    item = seed_dashboard_item(work_items, scenario_state)
    path = worktree_path(api_config(tmp_path).workdir, item.repo, item.number)
    path.mkdir(parents=True, exist_ok=True)
    scenario_state["known_worktree_path"] = path
    scenario_state["panes"] = [
        {
            "id": 23,
            "title": "code_owner_repo_1",
            "pane_cwd": str(path),
            "pane_command": "codex",
            "tab_id": 7,
            "tab_name": "work",
            "state": "Running",
            "exited": False,
        }
    ]


@given(parsers.parse("the current dashboard items include `{item_id}`"))
def _current_dashboard_items_include(
    item_id: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    item = seed_dashboard_item(work_items, scenario_state)
    assert format_work_item_id(item) == item_id


@given(parsers.parse("the current dashboard items do not include `{item_id}`"))
def _current_dashboard_items_do_not_include(
    item_id: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    seed_dashboard_item(work_items, scenario_state)
    assert all(format_work_item_id(item) != item_id for item in work_items)


@given(parsers.parse("`workdash info` reports pane ID `{pane_id}`"))
def _workdash_info_reports_pane_id(pane_id: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["pane_id"] = pane_id


@when("the user starts Workdash with `--server`")
@when("the user starts another Workdash session with `--server`")
def _user_starts_workdash_with_server(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.workdash as workdash_module

    if scenario_state.get("outside_zellij"):
        exec_calls: list[tuple[str, list[str]]] = []

        def fake_which(command: str) -> str | None:
            if command in {"gh", "zellij"}:
                return f"/usr/bin/{command}"
            return None

        def fake_execvp(file: str, args: list[str]) -> None:
            exec_calls.append((file, args))
            raise SystemExit(0)

        monkeypatch.setattr(github_client_module.shutil, "which", fake_which)
        monkeypatch.setattr(github_client_module.subprocess, "run", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            workdash_module, "load_config", lambda: api_config(Path("/tmp/workdash-bdd"))
        )
        monkeypatch.setattr("workdash.launcher.shutil.which", fake_which)
        monkeypatch.setattr("workdash.launcher.os.execvp", fake_execvp)
        monkeypatch.setattr(
            "workdash.launcher.subprocess.run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""),
        )
        monkeypatch.delenv("ZELLIJ", raising=False)
        try:
            scenario_state["exit_code"] = workdash_module.main(["--server"])
        except SystemExit as error:
            scenario_state["exit_code"] = int(error.code or 0)
        captured = capsys.readouterr()
        scenario_state["output"] = captured.out + captured.err
        scenario_state["exec_calls"] = exec_calls
        return

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            scenario_state["initial_dashboard_loaded"] = True
            if progress_callback is not None:
                progress_callback("loading...")
            item = make_work_item(number=1, title="Fix the issue", updated_at=NOW_UTC)
            return [item], compute_suggestion_markers([item])

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeControlServer:
        def __init__(self, session, *, listeners=()) -> None:
            assert listeners == ()
            self.session = session

        def start(self) -> None:
            if scenario_state.get("server_port_busy"):
                raise RuntimeError(
                    "Workdash server port 8765 is already in use. "
                    "Is another `workdash --server` running?"
                )
            scenario_state["json_api_started"] = True
            scenario_state["server_session"] = self.session

        def stop(self) -> None:
            scenario_state["json_api_stopped"] = True

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            scenario_state["tui_work_items"] = kwargs["work_items"]
            scenario_state["tui_suggestion_markers"] = kwargs["suggestion_markers"]

        def run(self, *, mouse: bool) -> None:
            scenario_state["tui_started"] = True

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(
        workdash_module, "load_config", lambda: api_config(Path("/tmp/workdash-bdd"))
    )
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", FakeControlServer)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(
        workdash_module.launcher.zellij, "list_workdash_sessions", lambda: ["workdash-main"]
    )
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")

    scenario_state["exit_code"] = workdash_module.main(["--server"])
    captured = capsys.readouterr()
    scenario_state["output"] = captured.out + captured.err


@when("a client requests the list API without refresh")
def _client_requests_list_without_refresh(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    scenario_state["github_fetches"] = 0
    _call_api(scenario_state, work_items, tmp_path, "list", {"refresh": False})


@when("a client requests the list API with refresh enabled")
def _client_requests_list_with_refresh(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    scenario_state["refreshed_items"] = [
        make_work_item(
            item_type=WorkItemType.PR,
            kind=WorkItemKind.TRACKED_PR,
            number=2,
            title="Fresh item",
            created_at=NOW_UTC,
            updated_at=NOW_UTC,
        )
    ]
    _call_api(scenario_state, work_items, tmp_path, "list", {"refresh": True})


@when("a client requests the info API")
def _client_requests_info_api(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.load_zellij_panes",
        lambda _session: list(scenario_state.get("panes", [])),
    )
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path",
        lambda _workdir, _item, **kwargs: scenario_state.get("known_worktree_path"),
    )
    _call_api(scenario_state, work_items, tmp_path, "info")


@when("a client requests the show-config API")
def _client_requests_show_config_api(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _call_api(scenario_state, work_items, tmp_path, "show-config")


@when(parsers.parse("a client requests analysis for `{item_id}` without an agent"))
def _client_requests_analysis_without_agent(
    item_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request_analysis(item_id, None, scenario_state, work_items, tmp_path, monkeypatch)


@when(parsers.parse("a client requests analysis for `{item_id}` with agent `{agent}`"))
def _client_requests_analysis(
    item_id: str,
    agent: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _request_analysis(item_id, agent, scenario_state, work_items, tmp_path, monkeypatch)


def _request_analysis(
    item_id: str,
    agent: str | None,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_state["analysis_path"] = str(tmp_path / "analysis.md")
    _install_fake_ensure_worktree(scenario_state, monkeypatch)
    payload = {"target": item_id} if agent is None else {"target": item_id, "agent": agent}
    _call_api(scenario_state, work_items, tmp_path, "analyze", payload)


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


@then("the agent is briefed with exactly the original prompt")
def _agent_briefed_with_original_prompt(scenario_state: dict[str, Any]) -> None:
    assert [launch[1] for launch in scenario_state["agent_launches"]] == ["PROMPT"]


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


@when(parsers.parse("a client requests pane content for `{pane_id}`"))
def _client_requests_pane_content(
    pane_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.dump_zellij_pane",
        lambda session, requested_pane_id, *, full=False: (
            scenario_state.setdefault("pane_content_calls", []).append(
                (session, requested_pane_id, full)
            )
            or "visible output"
        ),
    )
    _call_api(scenario_state, work_items, tmp_path, "pane/content", {"pane_id": pane_id})


@when(parsers.parse("a client requests full pane content for `{pane_id}`"))
def _client_requests_full_pane_content(
    pane_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.dump_zellij_pane",
        lambda session, requested_pane_id, *, full=False: (
            scenario_state.setdefault("pane_content_calls", []).append(
                (session, requested_pane_id, full)
            )
            or "full output"
        ),
    )
    _call_api(
        scenario_state, work_items, tmp_path, "pane/content", {"pane_id": pane_id, "full": True}
    )


@when(parsers.parse("a client sends `{data}` to pane `{pane_id}`"))
def _client_sends_to_pane(
    data: str,
    pane_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.send_zellij_pane_input",
        lambda session, requested_pane_id, sent_data, *, raw=False: scenario_state.setdefault(
            "pane_send_calls", []
        ).append((session, requested_pane_id, sent_data, raw)),
    )
    _call_api(
        scenario_state,
        work_items,
        tmp_path,
        "pane/send",
        {"pane_id": pane_id, "data": data},
    )


@when(parsers.parse("a client sends raw `{data}` to pane `{pane_id}`"))
def _client_sends_raw_to_pane(
    data: str,
    pane_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.send_zellij_pane_input",
        lambda session, requested_pane_id, sent_data, *, raw=False: scenario_state.setdefault(
            "pane_send_calls", []
        ).append((session, requested_pane_id, sent_data, raw)),
    )
    _call_api(
        scenario_state,
        work_items,
        tmp_path,
        "pane/send",
        {"pane_id": pane_id, "data": data, "raw": True},
    )


@when(parsers.parse("a client requests pane close for `{pane_id}`"))
def _client_requests_pane_close(
    pane_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.close_zellij_pane",
        lambda session, requested_pane_id: scenario_state.setdefault("pane_close_calls", []).append(
            (session, requested_pane_id)
        ),
    )
    _call_api(scenario_state, work_items, tmp_path, "pane/close", {"pane_id": pane_id})


@when("a client requests pane close for a pane ID that Zellij rejects")
def _client_requests_rejected_pane_close(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def zellij_rejects_close(_session, _pane_id):
        raise RuntimeError("Zellij rejected terminal_404")

    monkeypatch.setattr("workdash.control.launcher.zellij.close_zellij_pane", zellij_rejects_close)
    _call_api(scenario_state, work_items, tmp_path, "pane/close", {"pane_id": "terminal_404"})


@when("a client requests a pane action for a pane ID that Zellij rejects")
def _client_requests_rejected_pane_action(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def zellij_rejects(_session, _pane_id, *, full=False):
        raise RuntimeError("Zellij rejected terminal_404")

    monkeypatch.setattr("workdash.control.launcher.zellij.dump_zellij_pane", zellij_rejects)
    _call_api(scenario_state, work_items, tmp_path, "pane/content", {"pane_id": "terminal_404"})


@then("the system loads the initial dashboard items")
def _system_loads_initial_dashboard_items(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("initial_dashboard_loaded") is True


@then(parsers.parse("the system starts the JSON API on `{address}`"))
def _system_starts_json_api(address: str, scenario_state: dict[str, Any]) -> None:
    assert address == "127.0.0.1:8765"
    assert scenario_state.get("json_api_started") is True


@then("the system starts the TUI using the same dashboard state")
def _system_starts_tui_with_same_dashboard_state(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("tui_started") is True
    assert scenario_state["tui_work_items"] is scenario_state["server_session"].work_items
    assert (
        scenario_state["tui_suggestion_markers"]
        is scenario_state["server_session"].suggestion_markers
    )


@then("the system reports that the Workdash server port is already in use")
def _system_reports_server_port_in_use(scenario_state: dict[str, Any]) -> None:
    assert "Workdash server port 8765 is already in use" in scenario_state["output"]


@then("the second session exits with a non-zero status")
def _second_session_exits_nonzero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0


@then("the Zellij process runs the dashboard with `--direct --server`")
def _zellij_runs_dashboard_direct_server(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert "--direct" in layout
    assert "--server" in layout


@then("the JSON API belongs to the dashboard process inside Zellij")
def _json_api_belongs_to_dashboard_process_inside_zellij(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert "workdash" in layout
    assert "--direct" in layout and "--server" in layout


@then("the API returns the current in-memory work items")
def _api_returns_current_in_memory_items(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert [item["id"] for item in result["items"]] == [
        format_work_item_id(item) for item in scenario_state["work_items"]
    ]


@then(
    "each item includes its Workdash item ID, API type, display type, kind, repository, "
    "number, title, URL, timestamps, and suggested status"
)
def _api_items_include_contract_fields(scenario_state: dict[str, Any]) -> None:
    required = {
        "id",
        "type",
        "display_type",
        "kind",
        "repo",
        "number",
        "title",
        "url",
        "created_at",
        "updated_at",
        "suggested",
    }
    for item in _api_result(scenario_state)["items"]:
        assert required <= set(item), item


@then("the API does not fetch GitHub before responding")
def _api_does_not_fetch_github(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("github_fetches", 0) == 0


@then("the server refreshes dashboard items from GitHub")
def _server_refreshes_dashboard_items(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("github_fetches", 0) == 1


@then("the API returns the refreshed work items")
def _api_returns_refreshed_work_items(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert [item["id"] for item in result["items"]] == [
        format_work_item_id(item) for item in scenario_state["refreshed_items"]
    ]


@then("the refreshed work items become the shared dashboard state")
def _refreshed_items_become_shared_state(scenario_state: dict[str, Any]) -> None:
    assert [format_work_item_id(item) for item in scenario_state["api_session"].work_items] == [
        format_work_item_id(item) for item in scenario_state["refreshed_items"]
    ]


@then("the live TUI reflects the refreshed state when it can safely repaint")
def _live_tui_reflects_refreshed_state_when_safe(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("tui_refresh_callbacks") == ["refresh_from_session"]
    assert [format_work_item_id(item) for item in scenario_state["tui_work_items"]] == [
        format_work_item_id(item) for item in scenario_state["refreshed_items"]
    ]
    assert scenario_state["tui_suggestion_markers"] == compute_suggestion_markers(
        scenario_state["refreshed_items"]
    )


@then("the API returns pane records from the live Zellij session")
def _api_returns_pane_records(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    scenario_state["pane_records"] = result["panes"]
    assert result["session"] == "workdash-main"
    assert result["panes"]


@then("the API returns the configured analysis agents")
def _api_returns_configured_analysis_agents(scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state)["agents"]["analyze"] == ["codex", "claude"]


@then("the API returns the configured coding agents")
def _api_returns_configured_coding_agents(scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state)["agents"]["code"] == ["codex", "claude", "pi"]


@then("the API returns the server host and port")
def _api_returns_server_host_and_port(scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state)["server"] == {"host": "127.0.0.1", "port": 8765}


@then(
    "each pane record includes the session, tab, pane ID, title, cwd, command, pane kind, "
    "state, and mapped Workdash item when known"
)
def _pane_records_include_contract_fields(scenario_state: dict[str, Any]) -> None:
    required = {
        "session",
        "tab_id",
        "tab_name",
        "pane_id",
        "title",
        "cwd",
        "command",
        "kind",
        "state",
        "item",
    }
    for pane in scenario_state["pane_records"]:
        assert required <= set(pane), pane
        assert pane["item"] == "owner/repo#ISSUE-1"


@then("the server analyzes the known item with the selected configured agent")
def _server_analyzes_known_item_with_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("analyze_calls") == [
        ("owner/repo#ISSUE-1", "cached"),
        ("owner/repo#ISSUE-1", "codex"),
    ]


@then(parsers.parse("the server analyzes the known item with `{agent}`"))
def _server_analyzes_known_item_with_named_agent(
    agent: str, scenario_state: dict[str, Any]
) -> None:
    assert scenario_state.get("analyze_calls") == [
        ("owner/repo#ISSUE-1", "cached"),
        ("owner/repo#ISSUE-1", agent),
    ]


@then(parsers.parse("the API returns `{agent}` as the selected agent"))
def _api_returns_selected_agent(agent: str, scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state)["agent"] == agent


@then(
    "the API returns the item ID, selected agent, analysis URL and path, cache status, and base64 markdown content"
)
def _api_returns_analysis_result(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result == {
        "item_id": "owner/repo#ISSUE-1",
        "path": scenario_state["analysis_path"],
        "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
        "agent": "codex",
        "cache_used": False,
        "status": "generated",
        "content_type": "text/markdown",
        "file_name": "analysis.md",
        "file_content": base64.b64encode(b"analysis body\n").decode("ascii"),
    }
    assert "content_encoding" not in result


@then("the server does not prepare a worktree")
def _server_does_not_prepare_worktree(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("ensure_calls", []) == []


@then("the server launches the selected configured agent for the known item")
def _server_launches_selected_coding_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("agent_launches") == [
        (
            str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_1"),
            "PROMPT",
            ["pi"],
            "workdash-main",
        )
    ]


@then(
    "the API returns status `launched` with the item ID, selected agent, selected session, cwd, "
    "pane title, and pane ID"
)
def _api_returns_code_result(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result == {
        "status": "launched",
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": "pi",
        "cwd": str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_1"),
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_23",
        "tab_name": None,
    }


@then("the server opens a plain terminal in the item's worktree")
def _server_opens_plain_terminal(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("terminal_launch_calls") == [
        (str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_1"), "workdash-main"),
    ]


@then("the API returns the item ID, session, cwd, pane title, and pane ID")
def _api_returns_terminal_result(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result == {
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "cwd": str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_1"),
        "pane_title": "terminal_owner_repo_1",
        "pane_id": "terminal_23",
    }


@then("the server asks Zellij for the current visible pane content")
def _server_asks_zellij_for_visible_content(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_content_calls"] == [("workdash-main", "terminal_23", False)]


@then("the server asks Zellij for the pane content including scrollback")
def _server_asks_zellij_for_full_content(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_content_calls"] == [("workdash-main", "terminal_23", True)]


@then("the API returns the pane ID and captured content")
def _api_returns_pane_content(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result["pane_id"] == "terminal_23"
    assert result["content"]


@then(parsers.parse("the server sends `{data}` to that pane"))
def _server_sends_data_to_pane(data: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_send_calls"][0][:3] == ("workdash-main", "terminal_23", data)


@then("the server sends a trailing Enter to that pane")
def _server_sends_trailing_enter(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_send_calls"][0][3] is False


@then(parsers.parse("the server sends exactly `{data}` to that pane"))
def _server_sends_exactly_data_to_pane(data: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_send_calls"][0] == ("workdash-main", "terminal_23", data, True)


@then("the server does not send a trailing Enter")
def _server_does_not_send_trailing_enter(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_send_calls"][0][3] is True


@then("the API reports that the input was accepted")
def _api_reports_input_accepted(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result["pane_id"] == "terminal_23"
    assert result["accepted"] is True


@then("the server asks Zellij to close pane `terminal_26`")
def _server_asks_zellij_to_close_pane(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["pane_close_calls"] == [("workdash-main", "terminal_26")]


@then("the API returns the pane ID and accepted status")
def _api_returns_pane_close_result(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result["pane_id"] == "terminal_26"
    assert result["accepted"] is True


@then("the API returns an error with an appropriate HTTP status")
def _api_returns_error_with_http_status(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_status"] >= 400
    assert scenario_state["api_payload"]["ok"] is False


@then("the error message includes the Zellij failure in user-readable form")
def _error_message_includes_zellij_failure(scenario_state: dict[str, Any]) -> None:
    assert "Zellij rejected terminal_404" in scenario_state["api_payload"]["error"]["message"]


def _read_startup_layout(scenario_state: dict[str, Any]) -> str:
    command = scenario_state["exec_calls"][0][1]
    layout_path = command[command.index("--layout") + 1]
    with open(layout_path, encoding="utf-8") as layout_file:
        return layout_file.read()


@given("listeners accept source IPs `192.0.2.10` and `192.0.2.11`")
def _listeners_accept_sources(scenario_state, work_items, tmp_path) -> None:
    seed_dashboard_item(work_items, scenario_state)
    scenario_state["accepted_ips"] = ("192.0.2.10", "192.0.2.11")
    ensure_api_session(scenario_state, work_items, tmp_path)


@when(parsers.parse("a client at `{source}` requests the list API"))
def _source_requests_list(source, scenario_state, work_items, tmp_path) -> None:
    scenario_state["remote_addr"] = source
    _call_api(scenario_state, work_items, tmp_path, "list")


@when(
    parsers.parse(
        "a client at `{source}` requests a todo with a forwarding header claiming `{claimed}`"
    )
)
def _unlisted_source_requests_todo(
    source, claimed, scenario_state, work_items, tmp_path, monkeypatch
):
    scenario_state["remote_addr"] = source
    scenario_state["forwarding_headers"] = {
        "X-Forwarded-For": claimed,
        "Forwarded": f"for={claimed}",
    }
    scenario_state["todo_gh_commands"] = []
    create_todo = MagicMock(side_effect=AssertionError("denied request executed a todo"))
    monkeypatch.setattr("workdash.github_client.GitHubClient.create_todo", create_todo)
    _call_api(scenario_state, work_items, tmp_path, "todo", {"text": "Forbidden todo"})
    create_todo.assert_not_called()


@then("the API returns a forbidden error")
def _api_returns_forbidden(scenario_state) -> None:
    assert scenario_state["api_status"] == 403
    assert scenario_state["api_payload"]["error"]["code"] == "forbidden"


@given("the user sets `WORKDASH_HOST` to `127.0.0.1` with an available custom port")
def _custom_server_address(monkeypatch, scenario_state, loopback_aliases):
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    scenario_state["custom_port"] = port


@when("the user starts a server with a listener on `127.0.0.1` accepting `127.0.0.2`")
def _start_custom_server(scenario_state, work_items, tmp_path, request):
    from workdash.control import WorkdashControlServer

    seed_dashboard_item(work_items, scenario_state)
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    server = WorkdashControlServer(session, listeners=(("127.0.0.1", ("127.0.0.2",)),))
    server.start()
    request.addfinalizer(server.stop)


@then("a client using `WORKDASH_HOST` can list the server's work items")
def _default_client_lists_custom_server(scenario_state):
    from workdash.control import WorkdashControlClient

    result = WorkdashControlClient().request("list")
    assert result["items"][0]["id"] == "owner/repo#ISSUE-1"


@then("the listener accepts `127.0.0.2` and denies `127.0.0.3`")
def _source_can_access_custom_server(scenario_state):
    from http.client import HTTPConnection

    for source, status in (("127.0.0.2", 200), ("127.0.0.3", 403)):
        connection = HTTPConnection(
            "127.0.0.1", scenario_state["custom_port"], source_address=(source, 0)
        )
        try:
            connection.request("POST", "/api/v0/list", "{}", {"Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == status
            if status == 200:
                assert (
                    json.loads(response.read())["result"]["items"][0]["id"] == "owner/repo#ISSUE-1"
                )
        finally:
            connection.close()


@given("two Zellij sessions including a non-Workdash session are active")
def _two_workdash_sessions(scenario_state, monkeypatch):
    import workdash.workdash as cli

    scenario_state["dashboard_sessions"] = ["workdash-one", "demo"]
    monkeypatch.setattr(cli.launcher.zellij, "list_workdash_sessions", lambda: ["workdash-one"])
    monkeypatch.setenv("ZELLIJ", "0")


@when("a server-backed dashboard is started inside each session on a different address")
def _start_two_server_dashboards(scenario_state, monkeypatch, tmp_path, request):
    import socket
    import threading
    import time

    import workdash.control as control
    import workdash.workdash as cli

    stopped = threading.Event()
    threads = []
    ports = []

    class Backend:
        def __init__(self, config, github_client):
            self.github_client = github_client

        def load_items(self, progress_callback=None):
            return [], {}

    class App:
        def __init__(self, **kwargs):
            self.is_running = False

        def run(self, *, mouse):
            stopped.wait()

    monkeypatch.setattr(cli.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(cli, "load_config", lambda: api_config(tmp_path))
    monkeypatch.setattr(cli, "WorkdashBackend", Backend)
    monkeypatch.setattr(cli, "WorkdashApp", App)
    monkeypatch.setattr(control.launcher.zellij, "load_zellij_panes", lambda session: [])

    def cleanup():
        stopped.set()
        for thread in threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in threads)

    request.addfinalizer(cleanup)
    for session in scenario_state["dashboard_sessions"]:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        ports.append(port)
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", session)
        monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
        thread = threading.Thread(target=lambda: cli.main(["--direct", "--server"]), daemon=True)
        threads.append(thread)
        thread.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail(f"dashboard {session} did not start its server")
    scenario_state["dashboard_ports"] = ports


@then("both servers respond and each dashboard uses its own Zellij session")
def _dashboards_use_own_sessions(scenario_state):
    from workdash.control import WorkdashControlClient

    for port, session in zip(
        scenario_state["dashboard_ports"], scenario_state["dashboard_sessions"]
    ):
        result = WorkdashControlClient(f"127.0.0.1:{port}").request("info")
        assert result["session"] == session


@given("the server uses a local primary IP address and an available port")
def _server_uses_local_primary(
    monkeypatch, scenario_state, work_items, tmp_path, request, loopback_aliases
):
    import socket

    from workdash.control import WorkdashControlServer

    host = "127.0.0.2"
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"{host}:{port}")
    seed_dashboard_item(work_items, scenario_state)
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    server = WorkdashControlServer(session)
    server.start()
    request.addfinalizer(server.stop)
    scenario_state["primary_address"] = (host, port)


@when("a client from that primary IP requests the list API")
def _primary_requests_list(scenario_state):
    from http.client import HTTPConnection

    host, port = scenario_state["primary_address"]
    connection = HTTPConnection(host, port, source_address=(host, 0), timeout=3)
    try:
        connection.request("POST", "/api/v0/list", "{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        scenario_state["api_status"] = response.status
        scenario_state["api_payload"] = json.loads(response.read())
    finally:
        connection.close()


@then("an unlisted source cannot access that server")
def _unlisted_source_cannot_access_primary(scenario_state):
    from http.client import HTTPConnection

    host, port = scenario_state["primary_address"]
    connection = HTTPConnection(host, port, source_address=("127.0.0.3", 0), timeout=3)
    try:
        connection.request("POST", "/api/v0/list", "{}", {"Content-Type": "application/json"})
        response = connection.getresponse()
        assert response.status == 403
        assert json.loads(response.read())["error"]["code"] == "forbidden"
    finally:
        connection.close()


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


@given(parsers.parse("the current dashboard items include a pull request that closes `{issue}`"))
def _dashboard_includes_closing_pull_request(
    issue: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    pull_request = make_work_item(
        item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR, number=5, title="Fix it"
    )
    pull_request.closing_issue_numbers = (int(issue.rpartition("#")[2]),)
    work_items.append(pull_request)
    set_session_items(scenario_state, work_items)


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


@then("the dashboard list and the included items are unchanged")
def _dashboard_and_included_items_unchanged(scenario_state: dict[str, Any]) -> None:
    session = scenario_state["api_session"]
    assert [item["id"] for item in session.list_items()["items"]] == ["owner/repo#ISSUE-1"]
    assert scenario_state["api_backend"].included_items_store.load() == []


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


@given(
    "a server-backed Workdash session has an item with a worktree artifact and an alias inside .workdash"
)
def _artifact_item(scenario_state, work_items, tmp_path, monkeypatch):
    item = make_work_item(number=1)
    work_items[:] = [item]
    set_session_items(scenario_state, work_items)
    worktree = tmp_path / "wrk" / "owner_repo_1"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/repo.git",
        ],
        check=True,
    )
    artifacts = worktree / ".workdash"
    artifacts.mkdir(parents=True)
    (artifacts / "PR_REVIEW.md").write_text("Review contents")
    (artifacts / "alias.md").symlink_to("PR_REVIEW.md")


@when("a client lists and reads the item's worktree artifacts")
def _get_artifacts(scenario_state, work_items, tmp_path):
    app = _source_filtered_wsgi_app(
        _make_turbogears_app(ensure_api_session(scenario_state, work_items, tmp_path))
    )
    responses = []
    for suffix in ("", ".workdash/PR_REVIEW.md", ".workdash/alias.md"):
        req = Request.blank(f"/api/items/owner/repo%23ISSUE-1/files/{suffix}")
        req.environ["REMOTE_ADDR"] = "127.0.0.1"
        response = req.get_response(app)
        _ = response.body
        responses.append(response)
    scenario_state["artifact_responses"] = responses


@then("the listing names the artifact and its alias and the API returns their contents")
def _check_artifacts(scenario_state):
    listing, artifact, alias = scenario_state["artifact_responses"]
    assert listing.status_int == 200
    assert listing.json["result"]["entries"] == ["PR_REVIEW.md", "alias.md"]
    assert artifact.status_int == alias.status_int == 200
    assert artifact.text == alias.text == "Review contents"


@given("a server-backed Workdash session has an item with a cached analysis")
def _item_with_cached_analysis(scenario_state, work_items, tmp_path):
    item = make_work_item(number=1)
    work_items[:] = [item]
    set_session_items(scenario_state, work_items)
    cache = AnalysisCache(tmp_path / "analysis-cache")
    cache.save(item, "# cached analysis\nbody\n")
    item.analysis = cache.load_analysis(item)
    item.analyzed_at = cache.load_analysis_date(item)
    scenario_state["analysis_cache"] = cache
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    session.backend.analysis_cache = cache


@when("a client requests the item's analysis URL")
def _request_analysis_url(scenario_state, work_items, tmp_path):
    app = _source_filtered_wsgi_app(
        _make_turbogears_app(ensure_api_session(scenario_state, work_items, tmp_path))
    )
    req = Request.blank("/api/items/owner/repo%23ISSUE-1/analysis")
    req.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = req.get_response(app)
    scenario_state["analysis_response"] = response


@then("the API returns the cached markdown as text/markdown")
def _analysis_served(scenario_state):
    response = scenario_state["analysis_response"]
    assert response.status_int == 200
    assert response.content_type == "text/markdown"
    assert response.text == "# cached analysis\nbody\n"


@then("the API returns a not found error")
def _analysis_not_found(scenario_state):
    response = scenario_state["analysis_response"]
    assert response.status_int == 404
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == "not_found"
