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


def _cli_config(workdir: str) -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
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
    import workdash.workdash as workdash_module

    config = _cli_config(scenario_state.get("workdir", "/tmp/workdash-bdd"))
    items = list(scenario_state.get("work_items", []))
    markers = compute_suggestion_markers(items)

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return list(items), dict(markers)

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "config", "--get", "remote.origin.url"]:
            repo = scenario_state.get("git_origins", {}).get(str(kwargs.get("cwd")))
            if repo is not None:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"https://github.com/{repo}.git\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "list_workdash_sessions",
        lambda: list(scenario_state.get("sessions", [])),
    )
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: list(scenario_state.get("panes", [])),
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


@given("that session has live and exited Workdash terminal-backed panes")
def _session_has_live_and_exited_panes(
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
    ]


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


@when("the user runs an orchestration command")
def _run_orchestration_command(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info"], scenario_state, monkeypatch, capsys)


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


@then("the system does not report exited panes")
def _does_not_report_exited_panes(scenario_state: dict[str, Any]) -> None:
    payload = scenario_state["json_payload"]
    assert [pane["title"] for pane in payload["panes"]] == ["code_owner_repo_1"]


@then("the system reports that an active Workdash-owned Zellij session is required")
def _reports_session_required(scenario_state: dict[str, Any]) -> None:
    assert "active Workdash-owned Zellij session" in scenario_state["output"]
    assert "required" in scenario_state["output"]
