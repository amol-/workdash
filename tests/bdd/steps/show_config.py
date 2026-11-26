"""Steps for discovering the local Workdash configuration."""

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.workdash as workdash_module
from workdash.config import AgentConfig

from .common import make_valid_config


@given("the user has configured Codex for analysis and pi for coding")
def _configured_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    config = make_valid_config(
        {"codex": AgentConfig(analyze="codex exec"), "pi": AgentConfig(launch="pi")}
    )
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)


@given(parsers.parse("`WORKDASH_HOST` is set to `{address}`"))
def _selected_server_address(address: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKDASH_HOST", address)


@when("the user runs `workdash show-config`", target_fixture="show_config_output")
def _run_show_config(capsys: pytest.CaptureFixture[str]) -> list[str]:
    assert workdash_module.main(["show-config"]) == 0
    return capsys.readouterr().out.splitlines()


@then("the command lists `codex` for analysis and `pi` for coding")
def _lists_agent_choices(show_config_output: list[str]) -> None:
    assert "Analysis agents: codex" in show_config_output
    assert "Code agents: pi" in show_config_output


@then(parsers.parse("the command shows `{address}` as the server address to use"))
def _shows_server_address(address: str, show_config_output: list[str]) -> None:
    assert f"Server: {address}" in show_config_output
