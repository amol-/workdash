"""Step definitions for show-config feature scenarios."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest_bdd import given, then, when

from workdash.config import AgentConfig, WorkdashConfig


@given("the configuration has Codex analysis configured")
def _config_has_codex_analyze(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("config_overrides", {})["codex_analyze"] = "codex exec"


@given("the configuration has Codex and pi coding configured")
def _config_has_codex_and_pi_launch(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("config_overrides", {})["codex_launch"] = "codex"
    scenario_state.setdefault("config_overrides", {})["pi_launch"] = "pi"


@given("the configuration has no Claude analyze command")
def _config_has_no_claude_analyze(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("config_overrides", {})["claude_analyze"] = ""


@given("the configuration has no pi launch command")
def _config_has_no_pi_launch(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("config_overrides", {})["pi_launch"] = ""


@when("the user runs `workdash show-config --json`")
def _run_show_config_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.workdash as workdash_module

    # Build config based on overrides
    config_overrides = scenario_state.get("config_overrides", {})
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze=config_overrides.get("claude_analyze", ""), launch=""),
        codex=AgentConfig(
            analyze=config_overrides.get("codex_analyze", ""),
            launch=config_overrides.get("codex_launch", ""),
        ),
        pi=AgentConfig(
            analyze="",
            launch=config_overrides.get("pi_launch", ""),
        ),
        repositories=("owner/repo",),
        workdir="/tmp/workdash-bdd",
    )

    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module.WorkdashConfig, "require_valid", lambda self: self)

    commands = workdash_module.WorkdashCommands()
    scenario_state["exit_code"] = commands.show_config(json_output=True)
    captured = capsys.readouterr()
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err


@when("the user runs `workdash show-config`")
def _run_show_config(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.workdash as workdash_module

    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir="/tmp/workdash-bdd",
    )

    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module.WorkdashConfig, "require_valid", lambda self: self)

    commands = workdash_module.WorkdashCommands()
    scenario_state["exit_code"] = commands.show_config(json_output=False)
    captured = capsys.readouterr()
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err


@then("the system reports `codex` as an analysis agent")
def _reports_codex_analyze_agent(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert "codex" in payload["agents"]["analyze"]


@then("the system reports `codex` and `pi` as coding agents")
def _reports_codex_and_pi_code_agents(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    code_agents = payload["agents"]["code"]
    assert "codex" in code_agents
    assert "pi" in code_agents


@then("the system reports the server host `127.0.0.1`")
def _reports_server_host(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert payload["server"]["host"] == "127.0.0.1"


@then("the system reports the server port `8765`")
def _reports_server_port(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert payload["server"]["port"] == 8765


@then("the system reports the configured automation options")
def _reports_configured_automation_options(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "Analysis agents:" in output
    assert "Code agents:" in output
    assert "Server:" in output


@then("the system does not report `claude` as an analysis agent")
def _does_not_report_claude_analyze_agent(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert "claude" not in payload["agents"]["analyze"]


@then("the system does not report `pi` as a coding agent")
def _does_not_report_pi_code_agent(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert "pi" not in payload["agents"]["code"]
