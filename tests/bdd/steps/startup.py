"""Step definitions for startup preflight scenarios."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
from http.client import HTTPConnection
from importlib.metadata import version
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

import workdash.control as control_module
import workdash.github_client as github_client_module
import workdash.launcher as launcher_module
import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig, load_config, save_config
from workdash.control import probe_dashboard_session
from workdash.dashboard_record import DashboardRecord, DashboardRecords


@given("the GitHub CLI is not installed on PATH")
def _gh_missing(scenario_state: dict[str, Any]) -> None:
    scenario_state["_gh_missing"] = True


@given("the GitHub CLI is installed but not authenticated")
def _gh_unauthenticated(scenario_state: dict[str, Any]) -> None:
    scenario_state["_gh_unauthenticated"] = True


@given("Zellij is installed on PATH")
def _zellij_installed(scenario_state: dict[str, Any]) -> None:
    scenario_state["_zellij_installed"] = True
    scenario_state.setdefault("on_path_tools", set()).add("zellij")


@given("the configuration file is missing a required field")
def _config_missing_fields(scenario_state: dict[str, Any]) -> None:
    # Construct a config that passes validation only for the agents but
    # leaves required fields empty — matches the "missing fields" spec.
    scenario_state["_incomplete_config"] = WorkdashConfig(
        github_username="",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(launch="pi"),
        },
        repositories=(),
        workdir="",
    )


@given("the configuration file has no open command")
def _config_has_no_open(scenario_state: dict[str, Any]) -> None:
    scenario_state["_malformed_config"] = WorkdashConfig(
        github_username="testuser",
        agents={"claude": AgentConfig(analyze="claude -p", launch="claude")},
        repositories=("owner/repo",),
        workdir="~/wrk",
        todo_repository="testuser/todos",
    )


@given("the configuration file configures no agent")
def _config_has_no_agent(scenario_state: dict[str, Any]) -> None:
    scenario_state["_incomplete_config"] = WorkdashConfig(
        github_username="testuser",
        open_command="xdg-open",
        analysis_viewer={
            "command": "workdash view",
            "format": "markdown",
            "needs_pane": True,
        },
        repositories=("owner/repo",),
        workdir="~/wrk",
        todo_repository="testuser/todos",
    )


@given("the configuration file has a malformed configured agent command")
def _config_has_malformed_agent_command(scenario_state: dict[str, Any]) -> None:
    scenario_state["_malformed_config"] = WorkdashConfig(
        github_username="testuser",
        open_command="xdg-open",
        analysis_viewer={
            "command": "workdash view",
            "format": "markdown",
            "needs_pane": True,
        },
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(launch="pi 'broken"),
        },
        repositories=("owner/repo",),
        workdir="~/wrk",
        todo_repository="testuser/todos",
    )


@when("the user runs the system")
@when("the user runs the system normally")
def _run_system(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_path: Path,
) -> None:
    config_path.touch()
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    if "_malformed_config" in scenario_state:
        monkeypatch.delenv("ZELLIJ", raising=False)
    else:
        monkeypatch.setenv("ZELLIJ", "0")
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    exec_calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        launcher_module.os,
        "execvp",
        lambda file, args: exec_calls.append((file, args)),
    )
    if scenario_state.get("_gh_missing"):
        monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            workdash_module,
            "load_config",
            lambda: WorkdashConfig(
                github_username="testuser",
                open_command="xdg-open",
                analysis_viewer={
                    "command": "workdash view",
                    "format": "markdown",
                    "needs_pane": True,
                },
                agents={
                    "codex": AgentConfig(analyze="codex exec", launch="codex"),
                    "claude": AgentConfig(analyze="claude -p", launch="claude"),
                    "pi": AgentConfig(launch="pi"),
                },
                repositories=("owner/repo",),
                workdir="~/wrk",
                todo_repository="testuser/todos",
            ),
        )
    elif scenario_state.get("_gh_unauthenticated"):
        monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
        monkeypatch.setattr(
            github_client_module.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                github_client_module.subprocess.CalledProcessError(1, ["gh", "auth", "status"])
            ),
        )
        monkeypatch.setattr(
            workdash_module,
            "load_config",
            lambda: WorkdashConfig(
                github_username="testuser",
                open_command="xdg-open",
                analysis_viewer={
                    "command": "workdash view",
                    "format": "markdown",
                    "needs_pane": True,
                },
                agents={
                    "codex": AgentConfig(analyze="codex exec", launch="codex"),
                    "claude": AgentConfig(analyze="claude -p", launch="claude"),
                    "pi": AgentConfig(launch="pi"),
                },
                repositories=("owner/repo",),
                workdir="~/wrk",
                todo_repository="testuser/todos",
            ),
        )
    elif "_incomplete_config" in scenario_state or "_malformed_config" in scenario_state:
        monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
        monkeypatch.setattr(github_client_module.subprocess, "run", lambda *args, **kwargs: None)
        config = scenario_state.get("_incomplete_config") or scenario_state["_malformed_config"]
        monkeypatch.setattr(workdash_module, "load_config", lambda: config)
        if "_malformed_config" in scenario_state:
            monkeypatch.setattr(
                workdash_module.launcher.zellij,
                "exec_zellij_wrapped_workdash",
                lambda _argv, preflight: preflight(),
            )
        monkeypatch.setattr(
            workdash_module,
            "WorkdashBackend",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("backend should not load invalid runtime config")
            ),
        )
    else:  # pragma: no cover - defensive; no current scenario hits this
        raise AssertionError("Preflight scenario setup missing")

    exit_code = workdash_module.main([])
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = exec_calls


def _install_direct_start_fakes(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            if progress_callback is not None:
                progress_callback("loading...")
            return [], {}

        def analyze_item(self, _item, tool="codex"):
            return None

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, mouse: bool) -> None:
            scenario_state["dashboard_started"] = True

    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: WorkdashConfig(
            github_username="testuser",
            open_command="xdg-open",
            analysis_viewer={
                "command": "workdash view",
                "format": "markdown",
                "needs_pane": True,
            },
            agents={
                "codex": AgentConfig(analyze="codex exec", launch="codex"),
                "claude": AgentConfig(analyze="claude -p", launch="claude"),
                "pi": AgentConfig(launch="pi"),
            },
            repositories=("owner/repo",),
            workdir="~/wrk",
            todo_repository="testuser/todos",
            listens=scenario_state.get("configured_listeners", ()),
        ),
    )
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    from unittest.mock import MagicMock

    monkeypatch.setattr(workdash_module, "WorkdashControlServer", MagicMock())


def _install_startup_which(
    monkeypatch: pytest.MonkeyPatch,
    *,
    zellij: bool,
) -> None:
    def fake_which(command: str) -> str | None:
        if command == "gh":
            return "/usr/bin/gh"
        if command == "zellij" and zellij:
            return "/usr/bin/zellij"
        return None

    monkeypatch.setattr(github_client_module.shutil, "which", fake_which)


def _start_interactive_dashboard(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_path: Path,
    argv: list[str] | None = None,
) -> None:
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        exec_calls.append((file, args))
        raise SystemExit(0)

    _install_direct_start_fakes(scenario_state, monkeypatch)
    if not config_path.exists():
        save_config(workdash_module.load_config(), config_path)
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(workdash_module, "load_config", lambda: load_config(config_path))
    _install_startup_which(
        monkeypatch,
        zellij=bool(scenario_state.get("_zellij_installed")),
    )
    monkeypatch.setattr(launcher_module.os, "execvp", fake_execvp)

    def run(command, **kwargs):
        scenario_state.setdefault("zellij_actions", []).append(command)
        output = "project [Created now]\n" if scenario_state.get("named_dashboard") else ""
        if scenario_state.get("named_dashboard") == "exited" and "list-sessions" in command:
            output = "project [Created now] (EXITED - attach to resurrect)\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(launcher_module.subprocess, "run", run)
    if scenario_state.get("named_dashboard"):

        def load_panes(session):
            if scenario_state["named_dashboard"] == "exited":
                assert any(
                    action[0] == "/usr/bin/zellij" and action[-3:] == ["attach", "-b", session]
                    for action in scenario_state["zellij_actions"]
                )
            return [
                {"id": 2, "title": "workdash tui", "tab_name": "dashboard", "tab_id": 3},
                {"id": 5, "title": "work", "tab_name": "project", "tab_id": 4},
            ]

        monkeypatch.setattr(launcher_module.Zellij, "load_zellij_panes", load_panes)
        monkeypatch.setattr(launcher_module.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    monkeypatch.delenv("ZELLIJ_PANE_ID", raising=False)
    if scenario_state.get("inside_dashboard_pane"):
        monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    if scenario_state.get("outside_zellij"):
        monkeypatch.delenv("ZELLIJ", raising=False)
    else:
        monkeypatch.setenv("ZELLIJ", "0")
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    try:
        exit_code = workdash_module.main(argv or [])
    except SystemExit as error:
        exit_code = int(error.code or 0)
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = exec_calls


@given("the configuration file contains invalid JSON")
def _invalid_config_json(config_path: Path) -> None:
    config_path.write_text("{invalid json", encoding="utf-8")


@then("the system reports invalid configuration JSON in one line")
def _reports_invalid_config_json(scenario_state: dict[str, Any], config_path: Path) -> None:
    lines = scenario_state["output"].splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f"Error: Configuration at {config_path} is not valid JSON:")


@when("the user starts the interactive dashboard")
def _user_starts_interactive_dashboard(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config_path: Path,
) -> None:
    _start_interactive_dashboard(scenario_state, monkeypatch, capsys, config_path)


@when("the user starts a server-backed client command")
@when("the user runs a server-backed client command")
def _user_starts_server_backed_client_command(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            scenario_state.setdefault("control_requests", []).append(
                {"endpoint": endpoint, "payload": dict(payload or {})}
            )
            assert endpoint == "info"
            return {"session": "workdash-main", "panes": []}

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    monkeypatch.setattr(
        workdash_module.GitHubClient,
        "check_authentication",
        lambda self: (_ for _ in ()).throw(AssertionError("unexpected GitHub preflight")),
    )
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "exec_zellij_wrapped_workdash",
        lambda _argv: (_ for _ in ()).throw(AssertionError("unexpected Zellij wrapper")),
    )

    exit_code = workdash_module.main(["info"])
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = []


@when("the user starts the non-interactive list command")
def _user_starts_list_command(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [], {}

    _install_startup_which(monkeypatch, zellij=False)
    monkeypatch.setattr(github_client_module.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: WorkdashConfig(
            github_username="testuser",
            open_command="xdg-open",
            analysis_viewer={
                "command": "workdash view",
                "format": "markdown",
                "needs_pane": True,
            },
            agents={
                "codex": AgentConfig(analyze="codex exec", launch="codex"),
                "claude": AgentConfig(analyze="claude -p", launch="claude"),
                "pi": AgentConfig(launch="pi"),
            },
            repositories=("owner/repo",),
            workdir="~/wrk",
            todo_repository="testuser/todos",
        ),
    )
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module.os,
        "execvp",
        lambda _file, _args: (_ for _ in ()).throw(AssertionError("unexpected exec")),
    )
    if scenario_state.get("outside_zellij"):
        monkeypatch.delenv("ZELLIJ", raising=False)
    else:
        monkeypatch.setenv("ZELLIJ", "0")
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    exit_code = workdash_module.main(["list"])
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = []


@then("the system reports that the GitHub CLI is required")
def _reports_gh_required(scenario_state: dict[str, Any]) -> None:
    assert "gh CLI" in scenario_state["output"]


@then("the system tells the user to authenticate GitHub CLI")
def _reports_gh_auth_required(scenario_state: dict[str, Any]) -> None:
    assert "gh CLI is not authenticated" in scenario_state["output"]
    assert (
        "Run this command to authenticate the GitHub CLI used by workdash:\n"
        "  /usr/bin/gh auth login\n"
    ) in scenario_state["output"]


@then("the system replaces itself with a Zellij process")
def _replaces_with_zellij_process(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exec_calls"]
    file, command = scenario_state["exec_calls"][0]
    assert file == "/usr/bin/zellij"
    assert command[0] == "/usr/bin/zellij"


def _read_startup_layout(scenario_state: dict[str, Any]) -> str:
    command = scenario_state["exec_calls"][0][1]
    layout_path = command[command.index("--layout") + 1]
    with open(layout_path, encoding="utf-8") as layout_file:
        return layout_file.read()


@then("the first tab is named `dashboard`")
def _first_tab_is_named_workdash(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    tab_names = re.findall(r'^\s*tab name="([^"]*)"', layout, re.MULTILINE)
    assert tab_names[0] == "dashboard"


@then("the Zellij session shows the tab bar at the top")
def _session_shows_tab_bar_at_top(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert layout.index("zellij:tab-bar") < layout.index("children")


@then("the Zellij session shows the status bar at the bottom")
def _session_shows_status_bar_at_bottom(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert layout.index("children") < layout.index("zellij:status-bar")


@then("the system starts the dashboard directly")
def _starts_dashboard_directly(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("exec_calls") == []
    assert scenario_state.get("dashboard_started") is True


@then("the system does not replace itself with Zellij")
def _does_not_replace_with_zellij(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("exec_calls") == []


@then("the system prints work items directly")
def _prints_work_items_directly(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0
    assert scenario_state["output"] == "No work items found.\n"


@then("the system lists the missing fields")
def _lists_missing(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "missing configuration fields" in output
    assert "github_username" in output
    assert "repositories" in output
    assert "workdir" in output


@then("the system lists `agents` as a missing field")
def _lists_agents_missing(scenario_state: dict[str, Any]) -> None:
    assert "missing configuration fields: agents" in scenario_state["output"]


@then("the system tells the user to run the configuration wizard")
def _tells_run_wizard(scenario_state: dict[str, Any]) -> None:
    assert "--configure" in scenario_state["output"]


@then("the system reports the malformed configuration with wizard guidance")
def _reports_malformed_config_with_wizard_guidance(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "invalid configuration fields" in output
    assert "agents.pi.launch" in output
    assert "workdash --configure" in output
    assert "Traceback" not in output


@given("the named session `project` already has a running dashboard")
def _named_session_running(scenario_state):
    scenario_state.update(
        outside_zellij=True,
        _zellij_installed=True,
        named_dashboard="running",
        dashboard_at_address="project",
    )


@given("the named session `project` has a stopped dashboard")
def _named_session_stopped(scenario_state):
    scenario_state.update(outside_zellij=True, _zellij_installed=True, named_dashboard="stopped")


@given("the selected Zellij session has exited with existing work panes")
def _named_session_exited(scenario_state):
    scenario_state.update(outside_zellij=True, _zellij_installed=True, named_dashboard="exited")


@then("the existing session is recovered with its work panes and a dashboard")
def _recovered_session(scenario_state):
    actions = scenario_state["zellij_actions"]
    recovery = next(
        i for i, command in enumerate(actions) if command[-3:] == ["attach", "-b", "project"]
    )
    dashboard = next(i for i, command in enumerate(actions) if "new-pane" in command)
    assert recovery < dashboard
    assert "-f" not in actions[recovery]
    assert not any("close-pane" in command or "terminal_5" in command for command in actions)
    assert scenario_state["exec_calls"][0][1] == ["/usr/bin/zellij", "attach", "project"]


@when("the user starts the dashboard in the named session")
@when("the user starts the dashboard in the named session with configured listeners")
def _start_named_session(scenario_state, monkeypatch, capsys, config_path, request):
    scenario_state["configured_listeners"] = ("127.0.0.2@127.0.0.4",)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        scenario_state["port"] = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{scenario_state['port']}")
    _serve_dashboard_answers(scenario_state, monkeypatch, request)
    monkeypatch.setattr(launcher_module.sys, "argv", ["workdash"])
    _start_interactive_dashboard(
        scenario_state,
        monkeypatch,
        capsys,
        config_path,
        argv=["--session", "project"],
    )


def _serve_dashboard_answers(scenario_state, monkeypatch, request):
    """Answer dashboard identity probes from the servers and records the scenario describes."""

    if scenario_state.get("version_mismatch"):
        import threading
        from wsgiref.simple_server import make_server

        app = control_module._source_and_header_guarded_wsgi_app(
            lambda environ, start_response: [], server_version="different-version"
        )
        httpd = make_server("127.0.0.1", scenario_state["port"], app)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        request.addfinalizer(lambda: (httpd.shutdown(), httpd.server_close(), thread.join()))
        monkeypatch.setattr(control_module, "probe_dashboard_session", probe_dashboard_session)
        return
    if scenario_state.get("unidentifiable_dashboard"):
        _serve_unidentified_address(scenario_state["port"], monkeypatch, request)
        return

    answers = {}
    if scenario_state.get("recorded_dashboard"):
        address = f"127.0.0.2:{scenario_state['port']}"
        DashboardRecords().save(
            DashboardRecord(session="project", pid=os.getpid(), address=address)
        )
        answers[address] = "project"
    if "dashboard_at_address" in scenario_state:
        answers[f"127.0.0.1:{scenario_state['port']}"] = scenario_state["dashboard_at_address"]
    if not answers:
        return

    monkeypatch.setattr(
        control_module, "probe_dashboard_session", lambda address: answers.get(address)
    )


def _serve_unidentified_address(port, monkeypatch, request):
    """Answer the start address with a server that does not prove which session it serves."""

    import threading
    from wsgiref.simple_server import make_server

    def unproven_identity(environ, start_response):
        start_response("404 Not Found", [("Content-Type", "application/json")])
        return [b'{"ok": false, "error": {"code": "not_found", "message": "No such endpoint."}}']

    httpd = make_server("127.0.0.1", port, unproven_identity)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    request.addfinalizer(lambda: (httpd.shutdown(), httpd.server_close(), thread.join()))
    # The suite stubs the probe; this scenario needs the real classification.
    monkeypatch.setattr(control_module, "probe_dashboard_session", probe_dashboard_session)


@then("the system attaches to the named session")
def _attaches_named_session(scenario_state):
    assert scenario_state["exec_calls"][0][1] == ["/usr/bin/zellij", "attach", "project"]


@then("the system reports that the new options are ignored")
def _reports_options_ignored(scenario_state):
    assert "options are ignored" in scenario_state["output"]


@then("the system exits successfully")
def _exits_successfully(scenario_state):
    assert scenario_state["exit_code"] == 0
    assert scenario_state.get("dashboard_started") is not True


@then("the new pane is named `workdash tui` and the replaced pane is not renamed")
def _new_pane_is_named_workdash_tui(scenario_state):
    actions = scenario_state["zellij_actions"]
    launch = next(command for command in actions if "new-pane" in command)
    assert launch[launch.index("--name") + 1] == "workdash tui"
    assert not any("rename-pane" in command for command in actions)


@given("the system is running inside a Zellij session whose dashboard is already running")
def _inside_zellij_with_running_dashboard(scenario_state, monkeypatch):
    scenario_state["inside_dashboard_pane"] = True

    def dashboard_is_running(session):
        assert session == "workdash-test"
        return True

    monkeypatch.setattr(launcher_module, "dashboard_is_running", dashboard_is_running)
    monkeypatch.setattr(
        launcher_module.zellij,
        "name_dashboard_pane",
        lambda session, pane: pytest.fail("a running dashboard keeps its pane name"),
    )


@then("the system starts a dashboard in that session")
def _starts_dashboard_in_named_session(scenario_state):
    command = next(c for c in scenario_state["zellij_actions"] if "new-pane" in c)
    assert command[1:3] == ["--session", "project"]


@then("the restarted dashboard accepts configured clients and rejects other clients")
def _restarted_dashboard_accepts_configured_clients(scenario_state, monkeypatch, tmp_path):
    monkeypatch.setattr(
        workdash_module, "WorkdashControlServer", control_module.WorkdashControlServer
    )
    monkeypatch.setattr(control_module, "SERVER_LOG_PATH", tmp_path / "workdash.log")
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "project")
    command = next(c for c in scenario_state["zellij_actions"] if "new-pane" in c)
    launched = command[command.index("--") + 1 :]
    assert launched[:2] == ["env", f"WORKDASH_HOST=127.0.0.1:{scenario_state['port']}"]
    monkeypatch.setenv(*launched[1].split("=", 1))
    assert launched[2:] == ["workdash", "--persistent-pane", "--session", "project"]
    port = scenario_state["port"]

    class Dashboard:
        def __init__(self, **kwargs):
            pass

        def run(self, *, mouse):
            for source, expected in (("127.0.0.4", 200), ("127.0.0.9", 403)):
                connection = HTTPConnection(
                    "127.0.0.2", port, timeout=2, source_address=(source, 0)
                )
                try:
                    connection.request(
                        "POST",
                        "/api/v0/list",
                        "{}",
                        {"Content-Type": "application/json", "X-Workdash": version("workdash")},
                    )
                    response = connection.getresponse()
                    assert response.status == expected
                    payload = json.loads(response.read())
                    if expected == 200:
                        assert payload["result"]["items"] == []
                    else:
                        assert payload["error"]["code"] == "forbidden"
                finally:
                    connection.close()

    monkeypatch.setattr(workdash_module, "WorkdashApp", Dashboard)
    with pytest.raises(SystemExit) as exit_info:
        workdash_module.main(launched[3:])
    assert exit_info.value.code == 0


@given("the named session `project` already has a running dashboard on another address")
def _named_session_running_elsewhere(scenario_state):
    scenario_state.update(
        outside_zellij=True,
        _zellij_installed=True,
        named_dashboard="running",
        recorded_dashboard=True,
    )


@given("its address answers without proving which session it serves")
def _address_answers_unidentifiably(scenario_state):
    scenario_state["unidentifiable_dashboard"] = True


@then("the system reports that it cannot tell whether a dashboard is running")
def _reports_unknown_dashboard(scenario_state):
    assert "Cannot tell whether a Workdash dashboard is running" in scenario_state["output"]


@given("the named session `project` has no dashboard")
def _named_session_without_dashboard(scenario_state):
    scenario_state.update(outside_zellij=True, _zellij_installed=True, named_dashboard="stopped")


@given("its address serves session `other`")
def _address_serves_other(scenario_state):
    scenario_state["dashboard_at_address"] = "other"


@then("the system reports the other session and the address it serves")
def _reports_the_other_session(scenario_state):
    output = scenario_state["output"]
    assert "'other'" in output
    assert f"127.0.0.1:{scenario_state['port']}" in output


@then("the system does not start a dashboard")
def _does_not_start_a_dashboard(scenario_state):
    assert not any("new-pane" in command for command in scenario_state["zellij_actions"])
    assert not any("--layout" in command for _file, command in scenario_state["exec_calls"])


@then("the Zellij session detaches when the terminal closes")
def _session_detaches_on_terminal_close(scenario_state):
    assert 'on_force_close "detach"' in _read_startup_layout(scenario_state)


@then("the dashboard is above a shell pane")
def _dashboard_above_shell(scenario_state):
    layout = _read_startup_layout(scenario_state)
    assert re.search(
        r'split_direction="horizontal".*pane name="workdash tui".*pane size="25%"',
        layout,
        re.DOTALL,
    )


@given("its address serves a dashboard with a different Workdash version")
def _address_version_mismatch(scenario_state):
    scenario_state["version_mismatch"] = True


@then("the system reports the version mismatch and restart guidance")
def _reports_version_mismatch(scenario_state):
    output = scenario_state["output"]
    assert "version" in output and "restart the dashboard" in output
    assert "Cannot tell" not in output
    assert f"127.0.0.1:{scenario_state['port']}" in output


@given(
    "a running dashboard is in another tab of the named session",
    target_fixture="real_dashboard_focus",
)
def _dashboard_in_other_tab(tmp_path):
    from test_zellij_integration import _attach_running_dashboard_focus_harness

    if not shutil.which("zellij"):
        pytest.skip("Zellij is not installed")
    harness = _attach_running_dashboard_focus_harness(tmp_path, True, new_last_input=True)
    try:
        next(harness)
        yield harness
    finally:
        harness.close()


@when("the user returns to that session with Workdash and sends the most recent input")
def _return_to_dashboard_session(real_dashboard_focus):
    next(real_dashboard_focus)


@then("the returning client's input reaches the dashboard pane")
def _dashboard_receives_input(real_dashboard_focus):
    with pytest.raises(StopIteration):
        next(real_dashboard_focus)
