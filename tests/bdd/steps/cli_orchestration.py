"""Step definitions for CLI orchestration feature scenarios."""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, then, when

from workdash.backend import compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.repo_worktree import worktree_path

from .common import NOW_UTC, make_work_item, set_session_items


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
        todo_repository="testuser/todos",
    )


def _run_workdash(
    argv: list[str],
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.control as control_module
    import workdash.launcher as launcher_module
    import workdash.workdash as workdash_module

    workdir = scenario_state.get("workdir", "/tmp/workdash-bdd")
    if scenario_state.get("only_codex_analyze_configured"):
        config = WorkdashConfig(
            github_username="testuser",
            codex=AgentConfig(analyze=scenario_state.get("codex_analyze", "codex exec")),
            repositories=("owner/repo",),
            workdir=workdir,
            todo_repository="testuser/todos",
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
            if "analysis_path" not in scenario_state:
                raise AssertionError("scenario must set analysis_path from tmp_path")
            analysis_path = Path(scenario_state["analysis_path"])
            analysis_path.parent.mkdir(parents=True, exist_ok=True)
            analysis_path.write_text(
                scenario_state.get("analysis_content", "analysis body\n"), encoding="utf-8"
            )
            return str(analysis_path)

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
                    result = scenario_state["api_session"].analyze(
                        target=payload["target"],
                        agent=payload.get("agent"),
                    )
                    scenario_state["last_control_result"] = result
                    return result
                if endpoint == "code":
                    return scenario_state["api_session"].code(
                        target=payload["target"],
                        agent=payload.get("agent"),
                    )
                if endpoint == "terminal":
                    return scenario_state["api_session"].terminal(
                        target=payload["target"],
                    )
                if endpoint == "pane/content":
                    return scenario_state["api_session"].pane_content(
                        pane_id=payload["pane_id"],
                        full=bool(payload.get("full", False)),
                    )
                if endpoint == "pane/send":
                    return scenario_state["api_session"].pane_send(
                        pane_id=payload["pane_id"],
                        data=payload["data"],
                        raw=bool(payload.get("raw", False)),
                    )
                raise AssertionError(f"Unexpected control endpoint: {endpoint}")

        monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
        monkeypatch.setattr(
            control_module,
            "load_zellij_panes",
            lambda _session: list(scenario_state.get("panes", [])),
        )
        monkeypatch.setattr(
            launcher_module,
            "load_zellij_panes",
            lambda _session: list(scenario_state.get("panes", [])),
        )
        if "known_worktree_path" in scenario_state:
            monkeypatch.setattr(
                control_module,
                "existing_worktree_path",
                lambda _workdir, _item: scenario_state["known_worktree_path"],
            )
        monkeypatch.setattr(control_module, "get_merge_base", lambda _path: None)
        monkeypatch.setattr(
            control_module,
            "prepare_launch_agent_prompt",
            lambda *args, **kwargs: "PROMPT",
        )
        monkeypatch.setattr(
            control_module,
            "launch_agent_context",
            lambda repo, prompt, agent_command_tokens=None, *, zellij_session=None: (
                scenario_state.setdefault("launch_calls", []).append(
                    (repo, prompt, agent_command_tokens, zellij_session)
                )
                or SimpleNamespace(
                    session=zellij_session,
                    pane_id="terminal_23",
                    pane_title="code_owner_repo_1",
                    cwd=repo,
                )
            ),
        )
        monkeypatch.setattr(
            control_module,
            "launch_terminal_context",
            lambda repo_path, *, zellij_session=None: (
                scenario_state.setdefault("terminal_launch_calls", []).append(
                    (repo_path, zellij_session)
                )
                or SimpleNamespace(
                    session=zellij_session,
                    pane_id="terminal_23",
                    pane_title="terminal_owner_repo_1",
                    cwd=repo_path,
                )
            ),
        )
        monkeypatch.setattr(
            control_module,
            "dump_zellij_pane",
            lambda session, pane_id, *, full=False: (
                scenario_state.setdefault("pane_content_calls", []).append((session, pane_id, full))
                or scenario_state.get("pane_content", "pane text\n")
            ),
        )

        def fake_send_zellij_pane_input(session, pane_id, data, *, raw=False):
            scenario_state.setdefault("pane_send_calls", []).append((session, pane_id, data, raw))

        monkeypatch.setattr(control_module, "send_zellij_pane_input", fake_send_zellij_pane_input)

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    if scenario_state.get("client_missing_tools"):
        monkeypatch.setattr(
            workdash_module,
            "_check_gh_preflight",
            lambda: (_ for _ in ()).throw(AssertionError("unexpected GitHub preflight")),
        )
        monkeypatch.setattr(
            workdash_module,
            "_select_workdash_session",
            lambda _session: (_ for _ in ()).throw(AssertionError("unexpected Zellij inspection")),
        )
    else:
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


@given(
    "that session has an agent pane in the worktree of the issue an authored pull request closes"
)
def _session_has_agent_pane_in_the_linked_issue_worktree(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    # No `known_worktree_path`: the real worktree lookup must recognize the
    # issue-numbered directory as the pull request's checkout.
    scenario_state["workdir"] = str(tmp_path / "wrk")
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        number=42149,
        title="Implement the issue",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    item.linked_issue = (item.repo, 41830)
    work_items[:] = [item]
    set_session_items(scenario_state, work_items)
    cwd = worktree_path(scenario_state["workdir"], item.repo, 41830)
    cwd.mkdir(parents=True, exist_ok=True)
    scenario_state.setdefault("git_origins", {})[str(cwd)] = item.repo
    scenario_state.setdefault("panes", []).append(
        {
            "id": 1,
            "title": "code_owner_repo_41830",
            "pane_cwd": str(cwd),
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


@given("the current Workdash items include an assigned issue")
def _current_items_include_assigned_issue(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _current_items_include_uncached_issue(scenario_state, work_items, tmp_path)


@given("only the Codex analyze command is configured")
def _only_codex_analyze_command_is_configured(scenario_state: dict[str, Any]) -> None:
    scenario_state["only_codex_analyze_configured"] = True


@given("the generated analysis content is returned by the server")
def _generated_analysis_content_is_returned_by_server(scenario_state: dict[str, Any]) -> None:
    scenario_state["analysis_content"] = "analysis body\n"


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


@when("the user runs `workdash analyze owner/repo#ISSUE-99 --agent codex --json`")
def _run_analyze_unknown_issue_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["analyze", "owner/repo#ISSUE-99", "--agent", "codex", "--json"],
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


@when("the user runs `workdash terminal owner/repo#ISSUE-1 --json`")
def _run_terminal_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["terminal", "owner/repo#ISSUE-1", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash terminal owner/repo#ISSUE-1`")
def _run_terminal(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["terminal", "owner/repo#ISSUE-1"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash terminal owner/repo#ISSUE-99 --json`")
def _run_terminal_unknown_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["terminal", "owner/repo#ISSUE-99", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash read terminal_23`")
def _run_read(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["read", "terminal_23"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash read terminal_23 --full --json`")
def _run_read_full_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["read", "terminal_23", "--full", "--json"], scenario_state, monkeypatch, capsys)


@when('the user runs `workdash write terminal_23 "continue"`')
def _run_write(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["write", "terminal_23", "continue"], scenario_state, monkeypatch, capsys)


@when('the user runs `workdash write terminal_23 "continue" --raw --json`')
def _run_write_raw_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["write", "terminal_23", "continue", "--raw", "--json"],
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


@then("the pane is mapped to that authored pull request")
def _pane_mapped_to_authored_pull_request(scenario_state: dict[str, Any]) -> None:
    assert "item=owner/repo#PR-42149" in scenario_state["stdout"], scenario_state["stdout"]


@then("both panes are mapped to the matching Workdash item")
def _both_panes_mapped(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["stdout"].count("item=owner/repo#ISSUE-1") == 2


@then("the system does not choose a session")
def _does_not_choose_session(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0
    assert "Session:" not in scenario_state["stdout"]


@then("the command reports that `workdash --server` must be running")
def _reports_server_required(scenario_state: dict[str, Any]) -> None:
    assert "No Workdash server is reachable" in scenario_state["stderr"]
    assert "workdash --server" in scenario_state["stderr"]


@then("the command exits with a non-zero status")
def _exits_non_zero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0


@then("the system reports that the work item is unknown")
def _reports_unknown_item(scenario_state: dict[str, Any]) -> None:
    assert "No dashboard item matches" in scenario_state["stderr"]


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


@then("the server analysis response includes markdown content as base64 with a file name")
def _server_analysis_response_includes_content(scenario_state: dict[str, Any]) -> None:
    result = scenario_state["last_control_result"]
    assert result["content_type"] == "text/markdown"
    assert result["file_name"] == Path(scenario_state["analysis_path"]).name
    assert base64.b64decode(result["file_content"]) == scenario_state.get(
        "analysis_content", "analysis body\n"
    ).encode("utf-8")
    assert "content_encoding" not in result


@then(
    "the system returns JSON with the item ID, selected agent, local analysis path, and cache status"
)
def _returns_analyze_json(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    analysis_path = Path(payload.pop("analysis_path"))
    try:
        assert payload == {
            "item_id": "owner/repo#ISSUE-1",
            "agent": "codex",
            "cache_used": False,
            "status": "generated",
        }
        assert analysis_path.name.startswith("workdash-analysis-")
        assert analysis_path.suffix == ".md"
        assert analysis_path.read_text(encoding="utf-8") == scenario_state.get(
            "analysis_content", "analysis body\n"
        )
    finally:
        analysis_path.unlink(missing_ok=True)


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
@then(
    "the system returns JSON with the item ID, selected agent, selected session, cwd, pane title, "
    "and pane ID"
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


@then("the system returns JSON with the item ID, session, cwd, pane title, and pane ID")
def _returns_terminal_json(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert payload == {
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "cwd": str(Path(scenario_state["workdir"]) / "owner_repo_1"),
        "pane_title": "terminal_owner_repo_1",
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


@given("`workdash show-config` reports only `codex` as an analysis agent")
def _show_config_reports_only_codex_analyze(scenario_state: dict[str, Any]) -> None:
    config = WorkdashConfig(
        github_username="testuser",
        codex=AgentConfig(analyze="codex exec"),
        repositories=("owner/repo",),
        workdir=scenario_state.get("workdir", "/tmp/workdash-bdd"),
        todo_repository="testuser/todos",
    )
    scenario_state["api_session"].config = config


@given("the current Workdash items do not include `owner/repo#ISSUE-99`")
def _current_items_do_not_include_issue_99(scenario_state: dict[str, Any]) -> None:
    assert all(item.number != 99 for item in scenario_state.get("work_items", []))


@given("the client process cannot find GitHub CLI or Zellij on PATH")
def _client_process_cannot_find_github_or_zellij(scenario_state: dict[str, Any]) -> None:
    scenario_state["client_missing_tools"] = True


@then(
    "the command requests pane information from the local Workdash server with ordinary panes included"
)
def _requests_pane_information_with_ordinary_panes(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {"endpoint": "info", "payload": {"include_all_panes": True}}
    ]


@then("the command requests code launch from the local Workdash server")
def _requests_code_launch(scenario_state: dict[str, Any]) -> None:
    control_requests = scenario_state.get("control_requests", [])
    assert any(req["endpoint"] == "code" for req in control_requests)


@then("the command requests terminal launch from the local Workdash server")
def _requests_terminal_launch(scenario_state: dict[str, Any]) -> None:
    control_requests = scenario_state.get("control_requests", [])
    assert any(req["endpoint"] == "terminal" for req in control_requests)


@then("the command requests pane content from the local Workdash server")
def _requests_pane_content(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {"endpoint": "pane/content", "payload": {"pane_id": "terminal_23", "full": False}}
    ]


@then("the command requests full pane content from the local Workdash server")
def _requests_full_pane_content(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {"endpoint": "pane/content", "payload": {"pane_id": "terminal_23", "full": True}}
    ]


@then("the command sends pane input through the local Workdash server")
def _sends_pane_input(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {
            "endpoint": "pane/send",
            "payload": {"pane_id": "terminal_23", "data": "continue", "raw": False},
        }
    ]


@then("the command sends raw pane input through the local Workdash server")
def _sends_raw_pane_input(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {
            "endpoint": "pane/send",
            "payload": {"pane_id": "terminal_23", "data": "continue", "raw": True},
        }
    ]


@then("the system prints the pane text for direct agent use")
def _prints_pane_text(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["stdout"] == scenario_state.get("pane_content", "pane text\n")


@then("the system returns JSON with the pane ID, content, and full flag")
def _returns_pane_content_json(scenario_state: dict[str, Any]) -> None:
    assert json.loads(scenario_state["stdout"]) == {
        "pane_id": "terminal_23",
        "content": scenario_state.get("pane_content", "pane text\n"),
        "full": True,
    }


@then("the system confirms that the pane input was accepted")
def _confirms_pane_input_accepted(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["stdout"] == "Accepted input for terminal_23 (raw: no).\n"


@then("the system returns JSON with the pane ID, raw flag, and accepted status")
def _returns_pane_send_json(scenario_state: dict[str, Any]) -> None:
    assert json.loads(scenario_state["stdout"]) == {
        "pane_id": "terminal_23",
        "raw": True,
        "accepted": True,
    }


@then(
    "the system returns JSON with the item ID, selected agent, local analysis path, cache status, "
    "and no server file content fields"
)
def _returns_analyze_json_with_local_path(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    analysis_path = Path(payload.pop("analysis_path"))
    try:
        assert payload == {
            "item_id": "owner/repo#ISSUE-1",
            "agent": "codex",
            "cache_used": False,
            "status": "generated",
        }
        assert "file_content" not in payload
        assert "file_name" not in payload
        assert "content_type" not in payload
        assert "copy_path" not in payload
        assert analysis_path.name.startswith("workdash-analysis-")
        assert analysis_path.suffix == ".md"
        assert analysis_path.read_text(encoding="utf-8") == "analysis body\n"
    finally:
        analysis_path.unlink(missing_ok=True)


@then("the command still sends the request to the local Workdash server")
def _still_sends_request_to_server(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests"), scenario_state


@then("the command formats the server response")
def _formats_server_response(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    assert "Session: workdash-main" in scenario_state["stdout"]


@then("the system reports that the work item is unknown")
def _reports_unknown_work_item(scenario_state: dict[str, Any]) -> None:
    assert "No dashboard item matches" in scenario_state["stderr"]
    assert "owner/repo#ISSUE-99" in scenario_state["stderr"]
