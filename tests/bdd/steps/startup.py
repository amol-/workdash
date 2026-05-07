"""Step definitions for startup preflight scenarios."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, then, when

import workdash.launcher as launcher_module
import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig


@given("the GitHub CLI is not installed on PATH")
def _gh_missing(scenario_state: dict[str, Any]) -> None:
    scenario_state["_gh_missing"] = True


@given("Zellij is installed on PATH")
def _zellij_installed(scenario_state: dict[str, Any]) -> None:
    scenario_state["_zellij_installed"] = True


@given("the configuration file is missing a required field")
def _config_missing_fields(scenario_state: dict[str, Any]) -> None:
    # Construct a config that passes validation only for claude/codex but
    # leaves required fields empty — matches the "missing fields" spec.
    scenario_state["_incomplete_config"] = WorkdashConfig(
        github_username="",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        repositories=(),
        workdir="",
    )


@when("the user runs the system")
def _user_runs_system(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("ZELLIJ", "0")
    if scenario_state.get("_gh_missing"):
        monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: None)
        monkeypatch.setattr(
            workdash_module,
            "load_config",
            lambda: WorkdashConfig(
                github_username="testuser",
                claude=AgentConfig(analyze="claude -p", launch="claude"),
                codex=AgentConfig(analyze="codex exec", launch="codex"),
                repositories=("owner/repo",),
                workdir="~/wrk",
            ),
        )
    elif "_incomplete_config" in scenario_state:
        monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
        monkeypatch.setattr(
            workdash_module, "load_config", lambda: scenario_state["_incomplete_config"]
        )
    else:  # pragma: no cover - defensive; no current scenario hits this
        raise AssertionError("Preflight scenario setup missing")

    exit_code = workdash_module.main([])
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err


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

        def run(self) -> None:
            scenario_state["dashboard_started"] = True

    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: WorkdashConfig(
            github_username="testuser",
            claude=AgentConfig(analyze="claude -p", launch="claude"),
            codex=AgentConfig(analyze="codex exec", launch="codex"),
            repositories=("owner/repo",),
            workdir="~/wrk",
        ),
    )
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)


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

    monkeypatch.setattr(workdash_module.shutil, "which", fake_which)


@when("the user starts the interactive dashboard")
def _user_starts_interactive_dashboard(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_execvp(file: str, args: list[str]) -> None:
        exec_calls.append((file, args))
        raise SystemExit(0)

    _install_direct_start_fakes(scenario_state, monkeypatch)
    _install_startup_which(
        monkeypatch,
        zellij=bool(scenario_state.get("_zellij_installed")),
    )
    monkeypatch.setattr(launcher_module.os, "execvp", fake_execvp)
    monkeypatch.setattr(launcher_module.secrets, "token_hex", lambda length: "abc123ef")
    if scenario_state.get("outside_zellij"):
        monkeypatch.delenv("ZELLIJ", raising=False)
    else:
        monkeypatch.setenv("ZELLIJ", "0")

    try:
        exit_code = workdash_module.main([])
    except SystemExit as error:
        exit_code = int(error.code or 0)
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = exec_calls


@when("the user starts the interactive dashboard with `--direct`")
def _user_starts_interactive_dashboard_direct(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_direct_start_fakes(scenario_state, monkeypatch)
    _install_startup_which(monkeypatch, zellij=False)
    monkeypatch.setattr(
        workdash_module.os,
        "execvp",
        lambda _file, _args: (_ for _ in ()).throw(AssertionError("unexpected exec")),
    )
    monkeypatch.delenv("ZELLIJ", raising=False)

    exit_code = workdash_module.main(["--direct"])
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = []


@when("the user starts the non-interactive print mode")
def _user_starts_print_mode(
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
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: WorkdashConfig(
            github_username="testuser",
            claude=AgentConfig(analyze="claude -p", launch="claude"),
            codex=AgentConfig(analyze="codex exec", launch="codex"),
            repositories=("owner/repo",),
            workdir="~/wrk",
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

    exit_code = workdash_module.main(["--print"])
    captured = capsys.readouterr()
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = captured.out + captured.err
    scenario_state["exec_calls"] = []


@then("the system reports that the GitHub CLI is required")
def _reports_gh_required(scenario_state: dict[str, Any]) -> None:
    assert "gh CLI" in scenario_state["output"]


@then("the system replaces itself with a Zellij process")
def _replaces_with_zellij_process(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exec_calls"]
    file, command = scenario_state["exec_calls"][0]
    assert file == "/usr/bin/zellij"
    assert command[0] == "/usr/bin/zellij"


@then("the Zellij process starts a fresh workdash-prefixed session")
def _zellij_starts_fresh_workdash_session(scenario_state: dict[str, Any]) -> None:
    command = scenario_state["exec_calls"][0][1]
    assert command[:2] == ["/usr/bin/zellij", "--layout"]
    layout = _read_startup_layout(scenario_state)
    assert 'session_name "workdash-abc123ef"' in layout


@then("the Zellij process runs the dashboard with `--direct`")
def _zellij_runs_dashboard_direct(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert "--direct" in layout


def _read_startup_layout(scenario_state: dict[str, Any]) -> str:
    command = scenario_state["exec_calls"][0][1]
    layout_path = command[command.index("--layout") + 1]
    with open(layout_path, encoding="utf-8") as layout_file:
        return layout_file.read()


@then("the Zellij process is configured to quit on force close")
def _zellij_process_quits_on_force_close(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert 'on_force_close "quit"' in layout


@then("the Zellij process disables session resurrection state")
def _zellij_process_disables_resurrection_state(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert "session_serialization false" in layout
    assert "disable_session_metadata true" in layout


@then("the dashboard pane closes when the dashboard exits")
def _dashboard_pane_closes_when_dashboard_exits(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert "close_on_exit=true" in layout


@then("the dashboard exit terminates the replacement Zellij session")
def _dashboard_exit_terminates_replacement_session(scenario_state: dict[str, Any]) -> None:
    layout = _read_startup_layout(scenario_state)
    assert "/usr/bin/zellij kill-session workdash-abc123ef" in layout


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


@then("the system tells the user to run the configuration wizard")
def _tells_run_wizard(scenario_state: dict[str, Any]) -> None:
    assert "--configure" in scenario_state["output"]
