"""Step definitions for startup preflight scenarios."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_bdd import given, then, when

import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig


@given("the GitHub CLI is not installed on PATH")
def _gh_missing(scenario_state: dict[str, Any]) -> None:
    scenario_state["_gh_missing"] = True


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


@then("the system reports that the GitHub CLI is required")
def _reports_gh_required(scenario_state: dict[str, Any]) -> None:
    assert "gh CLI" in scenario_state["output"]


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
