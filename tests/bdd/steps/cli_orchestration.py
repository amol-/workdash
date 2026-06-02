"""Step definitions for CLI orchestration feature scenarios."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

from workdash.backend import compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.repo_worktree import worktree_path

from .common import NOW_UTC, make_work_item


def _cli_config(
    workdir: str,
    *,
    codex_analyze: str = "codex exec",
    codex_launch: str = "codex",
) -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze=codex_analyze, launch=codex_launch),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir=workdir,
    )


def _run_workdash(
    argv: list[str],
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.control as control_module
    import workdash.workdash as workdash_module

    workdir = scenario_state.get("workdir", "/tmp/workdash-bdd")
    if scenario_state.get("only_codex_analyze_configured"):
        config = WorkdashConfig(
            github_username="testuser",
            codex=AgentConfig(analyze=scenario_state.get("codex_analyze", "codex exec")),
            repositories=("owner/repo",),
            workdir=workdir,
        )
    else:
        config = _cli_config(
            workdir,
            codex_analyze=scenario_state.get("codex_analyze", "codex exec"),
            codex_launch=scenario_state.get("codex_launch", "codex"),
        )
    items = list(scenario_state.get("work_items", []))
    markers = compute_suggestion_markers(items)
    real_backend_class = workdash_module.WorkdashBackend

    class FakeAnalyzer:
        def analyze(self, _item, command_tokens=None):  # pragma: no cover - should not run
            raise AssertionError("analyzer should not run with malformed command")

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.real = real_backend_class(config=config, analyzer=FakeAnalyzer())
            self.analysis_cache = self.real.analysis_cache

        def load_items(self, progress_callback=None):
            scenario_state["backend_loads"] = scenario_state.get("backend_loads", 0) + 1
            assert progress_callback is None
            return list(items), dict(markers)

        def resolve_analyze_command_tokens(self, tool="codex"):
            return self.real.resolve_analyze_command_tokens(tool)

        def analyze_item(self, item, tool="codex"):
            scenario_state.setdefault("analyze_calls", []).append((item, tool))
            if tool == "cached":
                return scenario_state.get("cached_analysis_path") if item.analysis else None
            return scenario_state.get("analysis_path", "/tmp/workdash-analysis.md")

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            cwd = Path(kwargs["cwd"]).resolve()
            if str(kwargs["cwd"]) in scenario_state.get("git_origins", {}):
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{cwd}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            repo = scenario_state.get("git_origins", {}).get(str(kwargs.get("cwd")))
            if repo is not None:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"https://github.com/{repo}.git\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    if scenario_state.get("api_session") is not None:

        class FakeControlClient:
            def request(
                self, endpoint: str, payload: dict[str, object] | None = None
            ) -> dict[str, object]:
                payload = payload or {}
                scenario_state.setdefault("control_requests", []).append(
                    {"endpoint": endpoint, "payload": dict(payload)}
                )
                if endpoint == "info":
                    return scenario_state["api_session"].info(
                        include_all_panes=bool(payload.get("include_all_panes", False))
                    )
                if endpoint == "analyze":
                    return scenario_state["api_session"].analyze(
                        target=payload["target"],
                        agent=payload.get("agent"),
                    )
                raise AssertionError(f"Unexpected control endpoint: {endpoint}")

        monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
        monkeypatch.setattr(
            control_module,
            "load_zellij_panes",
            lambda _session: list(scenario_state.get("panes", [])),
        )
        monkeypatch.setattr(
            control_module,
            "existing_worktree_path",
            lambda _workdir, _item: scenario_state.get("known_worktree_path"),
        )

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda workdir, item: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(Path(workdir) / "owner_repo_1")
        ),
    )
    # Also mock repo_worktree.ensure_worktree for control.py which imports it directly
    import workdash.repo_worktree as repo_worktree_module
    monkeypatch.setattr(
        repo_worktree_module,
        "ensure_worktree",
        lambda workdir, item: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(Path(workdir) / "owner_repo_1")
        ),
    )
    # Mock control_module.ensure_worktree since control.py imports it
    monkeypatch.setattr(
        control_module,
        "ensure_worktree",
        lambda workdir, item: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(Path(workdir) / "owner_repo_1")
        ),
    )
    monkeypatch.setattr(
        workdash_module,
        "list_workdash_sessions",
        lambda: list(scenario_state.get("sessions", [])),
    )

    scenario_state["exit_code"] = workdash_module.main(argv)
    captured = capsys.readouterr()
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err


def _seed_info_item(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> tuple[WorkItem, str]:
    scenario_state.setdefault("workdir", str(tmp_path / "wrk"))
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        number=1,
        title="Fix the issue",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    work_items[:] = [item]
    scenario_state["work_items"] = list(work_items)
    path = worktree_path(scenario_state["workdir"], item.repo, item.number)
    path.mkdir(parents=True, exist_ok=True)
    scenario_state.setdefault("git_origins", {})[str(path)] = item.repo
    return item, str(path)


@given("exactly one active Workdash-owned Zellij session exists")
def _one_workdash_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = ["workdash-main"]


@given("multiple active Workdash-owned Zellij sessions exist")
def _multiple_workdash_sessions(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = ["workdash-main", "workdash-alt"]


@given("no active Workdash-owned Zellij session exists")
def _no_workdash_sessions(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = []


@given("an active Workdash-owned Zellij session exists")
def _a_workdash_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = ["workdash-main"]


@given("that session has a `code_owner_repo_1` pane in a known worktree")
def _session_has_agent_pane(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _item, cwd = _seed_info_item(scenario_state, work_items, tmp_path)
    scenario_state["known_worktree_path"] = cwd
    scenario_state.setdefault("panes", []).append(
        {
            "id": 1,
            "title": "code_owner_repo_1",
            "pane_cwd": cwd,
            "pane_command": "pi",
            "tab_id": 7,
            "tab_name": "work",
            "state": "Running",
            "exited": False,
        }
    )


@given("that session has a `terminal_owner_repo_1` pane in the same known worktree")
def _session_has_terminal_pane(scenario_state: dict[str, Any]) -> None:
    cwd = scenario_state["panes"][0]["pane_cwd"]
    scenario_state["panes"].append(
        {
            "id": 2,
            "title": "terminal_owner_repo_1",
            "pane_cwd": cwd,
            "terminal_command": "bash",
            "tab_id": 7,
            "tab_name": "work",
            "state": "Running",
            "exited": False,
        }
    )


@given("that session has Workdash terminal-backed panes")
def _session_has_terminal_backed_panes(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _item, cwd = _seed_info_item(scenario_state, work_items, tmp_path)
    scenario_state["known_worktree_path"] = cwd
    scenario_state["panes"] = [
        {
            "id": 3,
            "title": "code_owner_repo_1",
            "pane_cwd": cwd,
            "pane_command": "codex",
            "tab_id": 8,
            "tab_name": "agents",
        },
        {
            "id": 4,
            "title": "terminal_owner_repo_1",
            "pane_cwd": cwd,
            "terminal_command": "bash",
            "tab_id": 8,
            "tab_name": "agents",
        },
    ]


@given("that session has ordinary live, exited, held, and plugin panes")
def _session_has_ordinary_live_exited_held_and_plugin_panes(
    scenario_state: dict[str, Any],
) -> None:
    cwd = scenario_state["panes"][0]["pane_cwd"]
    scenario_state["panes"].extend(
        [
            {
                "id": 11,
                "title": "shell",
                "pane_cwd": cwd,
                "pane_command": "bash",
                "tab_id": 11,
                "tab_name": "scratch",
                "state": "Running",
                "exited": False,
                "is_plugin": False,
            },
            {
                "id": 12,
                "title": "old-shell",
                "pane_cwd": cwd,
                "state": "Exited",
                "exited": True,
            },
            {
                "id": 13,
                "title": "held-shell",
                "pane_cwd": cwd,
                "state": "Exited",
                "exited": False,
                "is_held": True,
            },
            {
                "id": 14,
                "title": "status",
                "pane_cwd": cwd,
                "is_plugin": True,
            },
        ]
    )


@given("that session has a Workdash-named pane whose cwd does not match a known worktree")
def _session_has_unmapped_pane(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _seed_info_item(scenario_state, work_items, tmp_path)
    scenario_state["panes"] = [
        {
            "id": 5,
            "title": "code_owner_repo_1",
            "pane_cwd": str(tmp_path / "unmapped"),
            "pane_command": "pi",
            "tab_id": 9,
            "tab_name": "scratch",
            "state": "Running",
        }
    ]


@given("that session has live, exited, and held Workdash terminal-backed panes")
def _session_has_live_exited_and_held_panes(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _item, cwd = _seed_info_item(scenario_state, work_items, tmp_path)
    scenario_state["panes"] = [
        {
            "id": 6,
            "title": "code_owner_repo_1",
            "pane_cwd": cwd,
            "pane_command": "codex",
            "tab_id": 10,
            "tab_name": "agents",
            "state": "Running",
            "exited": False,
        },
        {
            "id": 7,
            "title": "code_owner_repo_2",
            "pane_cwd": cwd,
            "pane_command": "codex",
            "tab_id": 10,
            "tab_name": "agents",
            "state": "Exited",
            "exited": True,
        },
        {
            "id": 8,
            "title": "terminal_owner_repo_3",
            "pane_cwd": cwd,
            "terminal_command": "bash",
            "tab_id": 10,
            "tab_name": "agents",
            "state": "Exited",
            "exited": True,
        },
        {
            "id": 9,
            "title": "terminal_owner_repo_4",
            "pane_cwd": cwd,
            "terminal_command": "bash",
            "tab_id": 10,
            "tab_name": "agents",
            "state": "Exited",
            "exited": False,
            "is_held": True,
        },
    ]


@given("the current Workdash items include an assigned issue without cached analysis")
def _current_items_include_uncached_issue(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    scenario_state.setdefault("workdir", str(tmp_path / "wrk"))
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        number=1,
        title="Fix the issue",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    work_items[:] = [item]
    scenario_state["work_items"] = list(work_items)
    scenario_state["analysis_path"] = str(tmp_path / "analysis.md")


@given("the configured Codex analyze command is malformed")
def _configured_codex_analyze_command_is_malformed(scenario_state: dict[str, Any]) -> None:
    scenario_state["codex_analyze"] = "codex 'broken"


@given("only the Codex analyze command is configured")
def _only_codex_analyze_command_is_configured(scenario_state: dict[str, Any]) -> None:
    scenario_state["only_codex_analyze_configured"] = True


@given("the configured Codex launch command is malformed")
def _configured_codex_launch_command_is_malformed(scenario_state: dict[str, Any]) -> None:
    scenario_state["codex_launch"] = "codex 'broken"


@when("the user runs `workdash info`")
def _run_info(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash info --json`")
def _run_info_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info", "--json"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash --json info`")
def _run_top_level_json_info(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["--json", "info"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash info --all --json`")
def _run_info_all_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info", "--all", "--json"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`")
def _run_analyze_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["analyze", "owner/repo#ISSUE-1", "--agent", "codex", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash code owner/repo#ISSUE-1 --agent codex --json`")
def _run_code_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["code", "owner/repo#ISSUE-1", "--agent", "codex", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash code owner/repo#ISSUE-1 --agent vscode --json`")
def _run_code_vscode_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["code", "owner/repo#ISSUE-1", "--agent", "vscode", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs an orchestration command")
@when("the user runs a server-backed orchestration command")
def _run_orchestration_command(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info"], scenario_state, monkeypatch, capsys)


@then("the command requests pane information from the local Workdash server")
def _requests_pane_information(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {"endpoint": "info", "payload": {"include_all_panes": False}}
    ]


@then("the command requests analysis from the local Workdash server")
def _requests_analysis(scenario_state: dict[str, Any]) -> None:
    control_requests = scenario_state.get("control_requests", [])
    assert any(req["endpoint"] == "analyze" for req in control_requests)


@then("the system reports the Workdash session name")
@then("the system reports the Workdash-owned session name")
def _reports_session_name(scenario_state: dict[str, Any]) -> None:
    assert "Session: workdash-main" in scenario_state["stdout"]


@then(
    "the system reports the agent pane with its Zellij pane identifier, title, cwd, command, "
    "tab, and state"
)
def _reports_agent_pane(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["stdout"]
    assert "agent" in output
    assert "terminal_1" in output
    assert "code_owner_repo_1" in output
    assert f"cwd={scenario_state['panes'][0]['pane_cwd']}" in output
    assert "command=pi" in output
    assert "tab=work" in output
    assert "state=Running" in output


@then(
    "the system reports the terminal pane with its Zellij pane identifier, title, cwd, command, "
    "tab, and state"
)
def _reports_terminal_pane(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["stdout"]
    assert "terminal" in output
    assert "terminal_2" in output
    assert "terminal_owner_repo_1" in output
    assert f"cwd={scenario_state['panes'][1]['pane_cwd']}" in output
    assert "command=bash" in output
    assert "tab=work" in output
    assert "state=Running" in output


@then("both panes are mapped to the matching Workdash item")
def _both_panes_mapped(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["stdout"].count("item=owner/repo#ISSUE-1") == 2


@then("the system does not choose a session")
def _does_not_choose_session(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0
    assert "Session:" not in scenario_state["stdout"]


@then("the system lists the candidate Workdash-owned sessions")
def _lists_candidate_sessions(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "workdash-main" in output
    assert "workdash-alt" in output


@then("the system asks the user to pass `--session`")
def _asks_for_session(scenario_state: dict[str, Any]) -> None:
    assert "--session" in scenario_state["output"]


@then("the system returns JSON pane records")
def _returns_json_panes(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    payload = json.loads(scenario_state["stdout"])
    scenario_state["json_payload"] = payload
    assert payload["session"] == "workdash-main"
    assert isinstance(payload.get("panes"), list)
    assert payload["panes"]


@then(
    "each record includes the session, tab, pane identifier, title, cwd, command, pane kind, "
    "state, and mapped item when known"
)
def _json_panes_have_contract(scenario_state: dict[str, Any]) -> None:
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
    for record in scenario_state["json_payload"]["panes"]:
        assert required <= set(record), record
        assert record["state"] == "running"
        assert record["item"] == "owner/repo#ISSUE-1"


@then("the system reports the raw pane information")
def _reports_raw_pane_information(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    scenario_state["json_payload"] = payload
    pane = payload["panes"][0]
    assert pane["pane_id"] == "terminal_5"
    assert pane["title"] == "code_owner_repo_1"
    assert pane["cwd"].endswith("unmapped")
    assert pane["command"] == "pi"
    assert pane["state"] == "Running"


@then("the pane item mapping is marked unknown")
def _pane_mapping_unknown(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["json_payload"]["panes"][0]["item"] == "unknown"


@then("the system does not report exited or held panes")
def _does_not_report_exited_or_held_panes(scenario_state: dict[str, Any]) -> None:
    payload = scenario_state["json_payload"]
    assert [pane["title"] for pane in payload["panes"]] == ["code_owner_repo_1"]


@then("the system reports the ordinary live pane as unknown kind with raw pane information")
def _reports_ordinary_live_pane_as_unknown(scenario_state: dict[str, Any]) -> None:
    payload = scenario_state["json_payload"]
    shell = next(pane for pane in payload["panes"] if pane["title"] == "shell")
    assert shell["pane_id"] == "terminal_11"
    assert shell["kind"] == "unknown"
    assert shell["item"] == "unknown"
    assert shell["cwd"] == scenario_state["panes"][0]["pane_cwd"]
    assert shell["command"] == "bash"
    assert shell["tab_name"] == "scratch"


@then("the system does not report exited, held, or plugin panes")
def _does_not_report_exited_held_or_plugin_panes(scenario_state: dict[str, Any]) -> None:
    titles = [pane["title"] for pane in scenario_state["json_payload"]["panes"]]
    assert "old-shell" not in titles
    assert "held-shell" not in titles
    assert "status" not in titles


@then("the system analyzes the current item with the selected configured agent")
def _analyzes_with_selected_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    item = scenario_state["work_items"][0]
    assert scenario_state["analyze_calls"] == [(item, "cached"), (item, "codex")]
    assert scenario_state["ensure_calls"] == [(scenario_state["workdir"], item)]


@then("the system returns JSON with the item ID, selected agent, analysis path, and cache status")
def _returns_analyze_json(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert payload == {
        "item_id": "owner/repo#ISSUE-1",
        "path": scenario_state["analysis_path"],
        "agent": "codex",
        "cache_used": False,
        "status": "generated",
    }


@then("the system launches code for the current item with the selected configured agent")
def _launches_code_with_selected_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    item = scenario_state["work_items"][0]
    assert scenario_state["ensure_calls"] == [(scenario_state["workdir"], item)]
    assert scenario_state["launch_calls"] == [
        (
            str(Path(scenario_state["workdir"]) / "owner_repo_1"),
            "PROMPT",
            ["codex"],
            "workdash-main",
        )
    ]


@then(
    "the system returns JSON with the item id, selected agent, selected session, cwd, pane title, "
    "and pane id"
)
def _returns_code_json(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert payload == {
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": "codex",
        "cwd": str(Path(scenario_state["workdir"]) / "owner_repo_1"),
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_23",
    }


@then("the system reports that the coding agent is not a configured terminal-backed agent")
def _reports_non_terminal_agent_rejected(scenario_state: dict[str, Any]) -> None:
    assert "not a configured terminal-backed agent" in scenario_state["stderr"]
    assert "vscode" in scenario_state["stderr"]


@then("the system reports that an active Workdash-owned Zellij session is required")
def _reports_session_required(scenario_state: dict[str, Any]) -> None:
    assert "active Workdash-owned Zellij session" in scenario_state["output"]
    assert "required" in scenario_state["output"]


@then("the system reports the malformed agent command with config guidance")
def _reports_malformed_agent_command_with_guidance(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["stdout"] == ""
    assert scenario_state["stderr"].startswith("Error: invalid configuration fields")
    assert "agents.codex." in scenario_state["stderr"]
    assert "No closing quotation" in scenario_state["stderr"]
    assert "workdash --configure" in scenario_state["stderr"]
    assert "Traceback" not in scenario_state["output"]


@then("the system does not load backend items")
def _does_not_load_backend_items(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("backend_loads", 0) == 0


@then("the system does not prepare a worktree")
def _does_not_prepare_worktree(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("ensure_calls", []) == []
