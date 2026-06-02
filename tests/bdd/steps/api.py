"""Step definitions for local JSON control API scenarios."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from webob import Request

from workdash.backend import compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import (
    WorkdashSession,
    _localhost_only_wsgi_app,
    _make_turbogears_app,
    format_work_item_id,
)
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.repo_worktree import worktree_path

from .common import NOW_UTC, make_work_item


def _api_config(tmp_path: Path) -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir=str(tmp_path / "wrk"),
    ).require_valid()


def _seed_dashboard_item(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> WorkItem:
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        number=1,
        title="Fix the issue",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    work_items[:] = [item]
    _set_session_items(scenario_state, work_items)
    return item


def _set_session_items(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    scenario_state["work_items"] = list(work_items)
    markers = compute_suggestion_markers(list(work_items))
    scenario_state["suggestion_markers"] = markers
    session = scenario_state.get("api_session")
    if session is not None:
        session.work_items = list(work_items)
        session.suggestion_markers = dict(markers)


def _ensure_api_session(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> WorkdashSession:
    session = scenario_state.get("api_session")
    if session is not None:
        return session
    if not work_items:
        _seed_dashboard_item(work_items, scenario_state)
    backend = _FakeApiBackend(scenario_state, tmp_path)
    session = WorkdashSession(
        config=_api_config(tmp_path),
        backend=backend,  # type: ignore[arg-type]
        work_items=list(work_items),
        suggestion_markers=compute_suggestion_markers(list(work_items)),
        zellij_session=scenario_state.get("zellij_session", "workdash-main"),
    )
    scenario_state["api_session"] = session
    scenario_state["api_backend"] = backend
    return session


def _call_api(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    endpoint: str,
    payload: dict[str, object] | None = None,
) -> None:
    app = _localhost_only_wsgi_app(
        _make_turbogears_app(_ensure_api_session(scenario_state, work_items, tmp_path))
    )
    request = Request.blank(
        f"/api/v0/{endpoint}",
        method="POST",
        content_type="application/json",
        body=json.dumps(payload or {}).encode("utf-8"),
    )
    request.environ["REMOTE_ADDR"] = scenario_state.get("remote_addr", "127.0.0.1")
    response = request.get_response(app)
    scenario_state["api_status"] = response.status_int
    scenario_state["api_payload"] = json.loads(response.text)


def _api_result(scenario_state: dict[str, Any]) -> dict[str, Any]:
    assert scenario_state["api_status"] == 200, scenario_state["api_payload"]
    payload = scenario_state["api_payload"]
    assert payload["ok"] is True
    return payload["result"]


class _FakeApiBackend:
    def __init__(self, scenario_state: dict[str, Any], tmp_path: Path) -> None:
        self._state = scenario_state
        self.analysis_cache = SimpleNamespace(
            build_analysis_path=lambda _item: tmp_path / "cached-analysis.md"
        )

    def load_items(self, progress_callback=None):
        self._state["github_fetches"] = self._state.get("github_fetches", 0) + 1
        assert progress_callback is None
        items = list(self._state.get("refreshed_items", self._state.get("work_items", [])))
        self._state["work_items"] = items
        return items, compute_suggestion_markers(items)

    def analyze_item(self, item: WorkItem, tool: str = "codex") -> str | None:
        self._state.setdefault("analyze_calls", []).append((format_work_item_id(item), tool))
        if tool == "cached":
            return None
        return self._state.get("analysis_path", "/tmp/workdash-analysis.md")


@given("no server-backed Workdash session is already running")
def _no_server_backed_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["server_port_busy"] = False
    scenario_state["sessions"] = ["workdash-main"]


@given(parsers.parse("a server-backed Workdash session is already running on `{address}`"))
def _server_backed_session_already_running(address: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["server_port_busy"] = True
    scenario_state["server_address"] = address
    scenario_state["sessions"] = ["workdash-main"]


@given("a server-backed Workdash session has loaded dashboard items")
def _server_session_has_loaded_dashboard_items(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _seed_dashboard_item(work_items, scenario_state)
    _ensure_api_session(scenario_state, work_items, tmp_path)


@given("a server-backed Workdash session is running")
def _server_session_running(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _seed_dashboard_item(work_items, scenario_state)
    _ensure_api_session(scenario_state, work_items, tmp_path)


@given("the Workdash Zellij session has live Workdash-owned panes")
def _workdash_zellij_session_has_live_panes(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    item = _seed_dashboard_item(work_items, scenario_state)
    path = worktree_path(_api_config(tmp_path).workdir, item.repo, item.number)
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
    item = _seed_dashboard_item(work_items, scenario_state)
    assert format_work_item_id(item) == item_id


@given(parsers.parse("the current dashboard items do not include `{item_id}`"))
def _current_dashboard_items_do_not_include(
    item_id: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    _seed_dashboard_item(work_items, scenario_state)
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

        monkeypatch.setattr(workdash_module.shutil, "which", fake_which)
        monkeypatch.setattr(workdash_module.subprocess, "run", lambda *args, **kwargs: None)
        monkeypatch.setattr("workdash.launcher.shutil.which", fake_which)
        monkeypatch.setattr("workdash.launcher.os.execvp", fake_execvp)
        monkeypatch.setattr("workdash.launcher.secrets.token_hex", lambda _length: "abc123ef")
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
        def __init__(self, session) -> None:
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

        def run(self) -> None:
            scenario_state["tui_started"] = True

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(
        workdash_module, "load_config", lambda: _api_config(Path("/tmp/workdash-bdd"))
    )
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", FakeControlServer)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(workdash_module, "list_workdash_sessions", lambda: ["workdash-main"])

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
        "workdash.control.load_zellij_panes",
        lambda _session: list(scenario_state.get("panes", [])),
    )
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path",
        lambda _workdir, _item: scenario_state.get("known_worktree_path"),
    )
    _call_api(scenario_state, work_items, tmp_path, "info")


@when("a client requests the show-config API")
def _client_requests_show_config_api(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _call_api(scenario_state, work_items, tmp_path, "show-config")


@when(parsers.parse("a client requests analysis for `{item_id}` with agent `{agent}`"))
def _client_requests_analysis(
    item_id: str,
    agent: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_state["analysis_path"] = str(tmp_path / "analysis.md")
    monkeypatch.setattr(
        "workdash.control.ensure_worktree",
        lambda workdir, item: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(tmp_path / "wrk" / "owner_repo_1")
        ),
    )
    _call_api(scenario_state, work_items, tmp_path, "analyze", {"target": item_id, "agent": agent})


@when(parsers.parse("a client requests code for `{item_id}` with agent `{agent}`"))
def _client_requests_code(
    item_id: str,
    agent: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.ensure_worktree",
        lambda workdir, item: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(tmp_path / "wrk" / "owner_repo_1")
        ),
    )
    monkeypatch.setattr("workdash.control.get_merge_base", lambda _path: None)
    monkeypatch.setattr(
        "workdash.control.prepare_launch_agent_prompt", lambda *args, **kwargs: "PROMPT"
    )

    def fake_launch(repo, prompt, agent_command_tokens=None, *, zellij_session=None):
        scenario_state.setdefault("launch_calls", []).append(
            (repo, prompt, agent_command_tokens, zellij_session)
        )
        return SimpleNamespace(
            session=zellij_session,
            pane_id="terminal_23",
            pane_title="code_owner_repo_1",
            cwd=repo,
        )

    monkeypatch.setattr("workdash.control.launch_agent_context", fake_launch)
    _call_api(scenario_state, work_items, tmp_path, "code", {"target": item_id, "agent": agent})


@when(parsers.parse("a client requests pane content for `{pane_id}`"))
def _client_requests_pane_content(
    pane_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "workdash.control.dump_zellij_pane",
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
        "workdash.control.dump_zellij_pane",
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
        "workdash.control.send_zellij_pane_input",
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
        "workdash.control.send_zellij_pane_input",
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


@when("a client requests a pane action for a pane ID that Zellij rejects")
def _client_requests_rejected_pane_action(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def zellij_rejects(_session, _pane_id, *, full=False):
        raise RuntimeError("Zellij rejected terminal_404")

    monkeypatch.setattr("workdash.control.dump_zellij_pane", zellij_rejects)
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
    "each item includes its Workdash item ID, type, kind, repository, number, title, URL, "
    "timestamps, and suggested status"
)
def _api_items_include_contract_fields(scenario_state: dict[str, Any]) -> None:
    required = {
        "id",
        "type",
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
    assert scenario_state["api_session"].work_items == scenario_state["refreshed_items"]


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


@then("the API returns the item ID, selected agent, analysis path, and cache status")
def _api_returns_analysis_result(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result == {
        "item_id": "owner/repo#ISSUE-1",
        "path": scenario_state["analysis_path"],
        "agent": "codex",
        "cache_used": False,
        "status": "generated",
    }


@then("the API returns an error saying the work item is unknown")
def _api_returns_unknown_item_error(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_status"] == 404
    payload = scenario_state["api_payload"]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "unknown_item"
    assert "No dashboard item matches" in payload["error"]["message"]


@then("the server does not fetch the item outside the current dashboard state")
def _server_does_not_fetch_unknown_item(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("github_fetches", 0) == 0
    assert scenario_state.get("analyze_calls", []) == []


@then("the server does not prepare a worktree")
def _server_does_not_prepare_worktree(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("ensure_calls", []) == []


@then("the server launches the selected configured terminal-backed agent for the known item")
def _server_launches_selected_coding_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("launch_calls") == [
        (
            str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_1"),
            "PROMPT",
            ["pi"],
            "workdash-main",
        )
    ]


@then("the API returns the item ID, selected agent, selected session, cwd, pane title, and pane ID")
def _api_returns_code_result(scenario_state: dict[str, Any]) -> None:
    result = _api_result(scenario_state)
    assert result == {
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": "pi",
        "cwd": str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_1"),
        "pane_title": "code_owner_repo_1",
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
