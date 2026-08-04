"""Step definitions for the setup (configure wizard) feature."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.config as config_module
import workdash.workdash as workdash_module
from workdash.config import WorkdashConfig, load_config


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


@given("the user has no configuration file")
def _no_config(scenario_state: dict[str, Any], config_path: Path) -> None:
    assert not config_path.exists()
    scenario_state["config_path"] = config_path
    scenario_state.setdefault("on_path_tools", {"zellij", "gh"})
    # Fresh-config scenario (S001): no agents on PATH, so the wizard offers agent defaults too.
    scenario_state.setdefault(
        "input_responses",
        [
            "claude -p",  # claude analyze
            "claude",  # claude launch
            "codex exec",  # codex analyze
            "codex",  # codex launch
            "pi",  # pi launch
            "octocat",  # github username
            "~/projects",  # workdir
            "",  # todo repository default
        ],
    )


@given("a supported coding agent's command-line tool is on PATH")
def _agent_on_path(scenario_state: dict[str, Any]) -> None:
    # Expose 'claude' only; codex will be prompted interactively.
    scenario_state.setdefault("on_path_tools", set()).add("claude")
    # Interactive inputs: codex analyze, codex launch, pi launch, username,
    # workdir, todo repository.
    scenario_state["input_responses"] = [
        "codex exec",
        "codex",
        "pi",
        "octocat",
        "~/projects",
        "",
    ]


@given("the user provides a GitHub username during configuration")
def _user_provides_username(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("input_responses", [])
    scenario_state["provided_username"] = "octocat"


@given("the configuration has no repositories selector")
def _no_repositories_selector(scenario_state: dict[str, Any]) -> None:
    scenario_state["repositories_empty"] = True


@given("the user already has a partial configuration")
def _partial_configuration(scenario_state: dict[str, Any], config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "github_username": "existing-user",
                "agents": {
                    "claude": {
                        "analyze": "existing-claude-analyze",
                        "launch": "existing-claude-launch",
                    },
                    "codex": {
                        "analyze": "existing-codex-analyze",
                        "launch": "existing-codex-launch",
                    },
                    "pi": {"launch": "existing-pi-launch"},
                },
            }
        ),
        encoding="utf-8",
    )
    scenario_state["config_path"] = config_path
    scenario_state["on_path_tools"] = {"zellij", "gh"}
    # Only the workdir and the todo repository are still empty.
    scenario_state["input_responses"] = ["~/src", ""]
    scenario_state["prior_config"] = load_config(config_path)


@given("the user submits empty answers for defaults and then provides a username")
def _empty_defaults_then_username(scenario_state: dict[str, Any]) -> None:
    scenario_state["on_path_tools"] = {"zellij", "gh"}
    scenario_state["input_responses"] = [
        "",  # claude analyze default
        "",  # claude launch default
        "",  # codex analyze default
        "",  # codex launch default
        "",  # pi launch default
        "",  # username has no default, prompt again
        "octocat",
        "",  # workdir default
        "",  # todo repository default
    ]


@given("Zellij is not installed on PATH")
def _zellij_not_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).discard("zellij")


@given("GitHub CLI is not installed on PATH")
def _gh_not_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).discard("gh")


def _run_configure_with_fakes(
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> WorkdashConfig:
    on_path_tools = scenario_state.get("on_path_tools", set())
    responses = iter(scenario_state.get("input_responses", []))
    prompts: list[str] = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        try:
            return next(responses)
        except StopIteration as exc:
            raise AssertionError(f"Unexpected prompt: {prompt!r}") from exc

    def which_fn(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in on_path_tools else None

    def install_zellij_fn() -> str:
        return scenario_state.get("installed_zellij_binary", "/tmp/workdash-config/bin/zellij")

    def install_gh_fn() -> str:
        return scenario_state.get("installed_gh_binary", "/tmp/workdash-config/bin/gh")

    # Route configure() through our fakes without altering production code.
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda: config_module.configure(
            config_path,
            input_fn=input_fn,
            which_fn=which_fn,
            install_zellij_fn=install_zellij_fn,
            install_gh_fn=install_gh_fn,
        ),
    )
    exit_code = workdash_module.main(["--configure"])
    scenario_state["exit_code"] = exit_code
    scenario_state["output"] = capsys.readouterr().out
    scenario_state["prompts"] = prompts
    written_config = load_config(config_path)
    scenario_state["written_config"] = written_config
    return written_config


# The "--configure" invocation is routed through the shared triage
# handler (triage._run_system_with_flag) which calls this helper when the
# flag is "--configure".


@when("the configuration wizard completes")
def _wizard_completes(
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Scenario S003: username is provided and repositories list is empty.
    scenario_state.setdefault("on_path_tools", {"zellij", "gh"})
    scenario_state["input_responses"] = [
        "my-claude -p",  # claude analyze
        "my-claude",  # claude launch
        "my-codex",  # codex analyze
        "my-codex",  # codex launch
        "my-pi",  # pi launch
        scenario_state["provided_username"],
        "~/projects",
        "",  # todo repository default
    ]
    _run_configure_with_fakes(scenario_state, config_path, monkeypatch, capsys)


@then("the system prompts the user for each missing globally required field")
def _prompts_for_missing(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    # Agent command prompts may also appear, but only these fields are globally required.
    assert any("GitHub username" in prompt for prompt in prompts), prompts
    assert any("Work directory" in prompt for prompt in prompts), prompts


@then(parsers.parse('the system writes the collected values to "{path_template}"'))
def _system_writes_values(
    path_template: str,
    scenario_state: dict[str, Any],
    config_path: Path,
) -> None:
    # The scenario uses the canonical path template but we wrote to a tmp
    # path to avoid touching real ~. Assert the wizard wrote a valid config
    # at the location we passed in.
    assert config_path.exists(), f"Configuration file was not created at {config_path}"
    parsed = json.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["github_username"] == "octocat"
    assert parsed["workdir"] == "~/projects"
    assert path_template.endswith("config.json")


@then("the system reports the saved configuration path")
def _reports_saved_path(scenario_state: dict[str, Any], config_path: Path) -> None:
    assert f"Configuration saved to {config_path}" in scenario_state["output"]


@then("the system fills in that agent's commands automatically")
def _auto_fill_agent_commands(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    assert written.claude.analyze == "claude -p"
    assert written.claude.launch == "claude"


@then("the system tells the user which Zellij binary was detected")
def _tells_detected_zellij_binary(scenario_state: dict[str, Any]) -> None:
    assert "Detected 'zellij' on PATH: /usr/bin/zellij" in scenario_state["output"]


@then("the system installs Zellij under the workdash configuration directory")
def _installs_zellij_under_config_dir(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "Zellij is not on PATH. Installing a local Zellij binary from" in output
    assert "To use a global Zellij instead" in output
    assert "Installed Zellij to: /tmp/workdash-config/bin/zellij" in output


@then("the system installs the GitHub CLI under the workdash configuration directory")
def _installs_gh_under_config_dir(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "GitHub CLI is not on PATH. Installing a local GitHub CLI binary from" in output
    assert "To use a global GitHub CLI instead" in output
    assert "Installed GitHub CLI to: /tmp/workdash-config/bin/gh" in output


@then("the system tells the user which commands were detected")
def _tells_detected(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "Detected 'claude'" in output


@then(parsers.parse('the repositories list contains "{template}"'))
def _repos_contain_template(template: str, scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    expected = template.replace("<username>", scenario_state["provided_username"])
    assert expected in written.repositories, (expected, written.repositories)


@then("the system tells the user what was set")
def _tells_user_what_was_set(scenario_state: dict[str, Any]) -> None:
    assert "Repositories set to" in scenario_state["output"]


@then("the system only prompts for fields that were empty")
def _only_prompts_empty(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    # Only the workdir and the todo repository were empty in the partial configuration.
    assert len(prompts) == 2, prompts
    assert "Work directory" in prompts[0]
    assert "Todo repository" in prompts[1]


@then("previously set fields are preserved in the saved configuration")
def _preserves_prior_fields(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    prior = scenario_state["prior_config"]
    assert written.github_username == prior.github_username
    assert written.claude == prior.claude
    assert written.codex == prior.codex


@then("the system writes default values for configurable fields")
def _writes_default_values(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    assert written.claude.analyze == "claude -p"
    assert written.claude.launch == "claude"
    assert written.codex.analyze == "codex exec"
    assert written.codex.launch == "codex"
    assert written.workdir == "~/wrk"


@then("the system prompts again for the GitHub username")
def _prompts_again_for_username(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    assert sum("GitHub username" in prompt for prompt in prompts) == 2, prompts
