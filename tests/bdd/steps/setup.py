"""Step definitions for the setup (configure wizard) feature."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.config as config_module
import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig, load_config


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "config.json"


@given("the user has no configuration file")
def _no_config(scenario_state: dict[str, Any], config_path: Path) -> None:
    assert not config_path.exists()
    scenario_state["config_path"] = config_path
    # A typical machine: claude is installed, so the wizard needs no custom agent.
    scenario_state.setdefault(
        "on_path_tools", {"zellij", "gh", config_module._default_open_command(), "claude"}
    )
    scenario_state.setdefault(
        "input_responses",
        [
            "",  # open command default
            "",  # viewer terminal default
            "octocat",  # github username
            "~/projects",  # workdir
            "",  # todo repository default
        ],
    )


@given("a supported agent's command-line tool is on PATH")
def _agent_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).add("claude")


@given("the command-line tools of Claude and pi are on PATH")
def _claude_and_pi_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).update({"claude", "pi"})


@given("no supported agent's command-line tool is on PATH")
def _no_agent_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).difference_update({"claude", "codex", "pi"})
    # The user has not answered any agent prompt yet; the wizard waits for them.
    scenario_state["agent_answers"] = []


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
                "open": "existing-open",
                "open_needs_pane": False,
                "agents": {
                    "claude": {
                        "analyze": "existing-claude-analyze",
                        "launch": "existing-claude-launch",
                    },
                    "codex": {
                        "analyze": "existing-codex-analyze",
                        "launch": "existing-codex-launch",
                    },
                    "pi": {"analyze": "existing-pi-analyze", "launch": "existing-pi-launch"},
                },
            }
        ),
        encoding="utf-8",
    )
    scenario_state["config_path"] = config_path
    scenario_state["on_path_tools"] = {"zellij", "gh", "existing-open"}
    # Only the workdir and the todo repository are still empty.
    scenario_state["input_responses"] = ["~/src", ""]
    scenario_state["prior_config"] = load_config(config_path)


@given("the user submits empty answers for defaults and then provides a username")
def _empty_defaults_then_username(scenario_state: dict[str, Any]) -> None:
    scenario_state["on_path_tools"] = {
        "zellij",
        "gh",
        config_module._default_open_command(),
        "claude",
    }
    scenario_state["input_responses"] = [
        "",  # viewer terminal default
        "",  # open command default
        "",  # username has no default, prompt again
        "octocat",
        "",  # workdir default
        "",  # todo repository default
    ]


@given("the user accepts the default analysis viewer")
def _default_analysis_viewer(scenario_state: dict[str, Any]) -> None:
    scenario_state["analysis_answers"] = [""]


@given("the user chooses a custom HTML analysis viewer requiring a terminal pane")
def _custom_analysis_viewer(scenario_state: dict[str, Any]) -> None:
    scenario_state["on_path_tools"].add("term-view")
    scenario_state["analysis_answers"] = ["term-view --quiet", "html", "y"]


@then("the saved analysis viewer uses Markdown in a terminal pane")
def _default_analysis_saved(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["written_config"].analysis_viewer == {
        "command": "workdash markdownview",
        "format": "markdown",
        "needs_pane": True,
    }


@then("the saved analysis viewer uses that command and HTML in a terminal pane")
def _custom_analysis_saved(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["written_config"].analysis_viewer == {
        "command": "term-view --quiet",
        "format": "html",
        "needs_pane": True,
    }


@given("the platform opener is available")
def _platform_opener_available(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).add(config_module._default_open_command())


@given("the user has a configured viewer whose executable is unavailable")
def _unavailable_viewer(scenario_state: dict[str, Any], config_path: Path) -> None:
    _partial_configuration(scenario_state, config_path)
    raw = json.loads(config_path.read_text())
    raw["open"] = "unavailable-viewer"
    raw.pop("open_needs_pane")
    raw["repositories"] = ["owner/repo"]
    raw["workdir"] = "~/src"
    raw["todo_repository"] = "existing-user/todos"
    config_path.write_text(json.dumps(raw))
    scenario_state["prior_config"] = load_config(config_path)
    scenario_state["on_path_tools"].add("term-view")
    scenario_state["input_responses"] = [
        "",
        "bad 'quote",
        "unavailable-viewer",
        "term-view --quiet",
        "y",
    ]


@then("the wizard saves a working custom viewer and its terminal choice")
def _custom_viewer_saved(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    assert written.open_command == "term-view --quiet"
    assert written.open_needs_pane is True
    assert sum("Open command" in prompt for prompt in scenario_state["prompts"]) == 4
    assert "advanced viewer commands" in scenario_state["output"]


@given("the user already has a configuration with agents")
def _configuration_with_agents(scenario_state: dict[str, Any], config_path: Path) -> None:
    config_path.write_text(
        json.dumps(
            {
                "github_username": "existing-user",
                "open": "existing-open",
                "open_needs_pane": False,
                "agents": {
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                    "aider": {"analyze": "aider --yes", "launch": ""},
                    "claude": {"analyze": "", "launch": "claude"},
                },
                "repositories": ["existing-user/*"],
                "workdir": "~/src",
                "todo_repository": "existing-user/todos",
            }
        ),
        encoding="utf-8",
    )
    scenario_state["config_path"] = config_path
    # codex is installed but must not be added to an already configured set of agents.
    scenario_state["on_path_tools"] = {"zellij", "gh", "existing-open", "codex"}
    scenario_state["input_responses"] = []
    scenario_state["prior_config"] = load_config(config_path)


@given("Zellij is not installed on PATH")
def _zellij_not_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).discard("zellij")


@given("GitHub CLI is not installed on PATH")
def _gh_not_on_path(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("on_path_tools", set()).discard("gh")


class _UserHasNotAnswered(Exception):
    """The wizard asked an agent question the scenario's user has not answered (yet)."""


def _run_configure_with_fakes(
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> WorkdashConfig:
    on_path_tools = scenario_state.get("on_path_tools", set())
    responses = iter(scenario_state.get("input_responses", []))
    # By default the user ends both agent sections without adding agents.
    agent_answers = iter(scenario_state.get("agent_answers", ["", ""]))
    prompts: list[str] = []
    analysis_answers = iter(scenario_state.get("analysis_answers", [""]))

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        print(prompt)
        if prompt.startswith(("Analysis viewer [", "Analysis format", "Does the analysis viewer")):
            return next(analysis_answers)
        if prompt.startswith(("Agents for", "Agent name")):
            try:
                return next(agent_answers)
            except StopIteration as exc:
                raise _UserHasNotAnswered(prompt) from exc
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
        lambda github_client: config_module.configure(
            config_path,
            github_client=github_client,
            input_fn=input_fn,
            which_fn=which_fn,
            install_zellij_fn=install_zellij_fn,
            install_gh_fn=install_gh_fn,
        ),
    )
    # Agents run through the user's shell, so the wizard checks programs with it: a
    # private PATH holding only an `aider` executable keeps the check hermetic.
    shell_bin = config_path.parent / "shell-bin"
    shell_bin.mkdir(exist_ok=True)
    (shell_bin / "aider").write_text("#!/bin/sh\n", encoding="utf-8")
    (shell_bin / "aider").chmod(0o755)
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.delenv("ENV", raising=False)  # an interactive shell would source it
    monkeypatch.setenv("PATH", str(shell_bin))
    try:
        scenario_state["exit_code"] = workdash_module.main(["--configure"])
    except _UserHasNotAnswered:
        scenario_state["exit_code"] = None
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
    scenario_state.setdefault(
        "on_path_tools", {"zellij", "gh", config_module._default_open_command(), "claude"}
    )
    scenario_state["input_responses"] = [
        "",  # viewer terminal default
        "",  # open command default
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
    # The viewer, workdir and todo repository were empty.
    assert len(prompts) == 3, prompts
    assert "Analysis viewer" in prompts[0]
    assert "Work directory" in prompts[1]
    assert "Todo repository" in prompts[2]


@then("previously set fields are preserved in the saved configuration")
def _preserves_prior_fields(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    prior = scenario_state["prior_config"]
    assert written.github_username == prior.github_username
    assert written.agents == prior.agents


@then("the system writes default values for configurable fields")
def _writes_default_values(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    assert written.agents == {"claude": AgentConfig(analyze="claude -p", launch="claude")}
    assert written.open_command == config_module._default_open_command()
    assert written.workdir == "~/wrk"


@then("submitting an empty open command stores the platform default")
def _empty_open_command_stores_default(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["written_config"].open_command == config_module._default_open_command()
    assert scenario_state["written_config"].open_needs_pane is False
    assert any("Enter to accept" in prompt for prompt in scenario_state["prompts"])


@then("the system prompts again for the GitHub username")
def _prompts_again_for_username(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    assert sum("GitHub username" in prompt for prompt in prompts) == 2, prompts


# -- Agents ------------------------------------------------------------------


def _user_answers_agent_prompts(
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *answers: str,
) -> None:
    """Give the wizard more answers by replaying it from the start with every answer so far."""

    scenario_state.setdefault("agent_answers", []).extend(answers)
    config_path.unlink(missing_ok=True)
    _run_configure_with_fakes(scenario_state, config_path, monkeypatch, capsys)


def _section(output: str, title: str) -> str:
    """The output printed for the section *title*, from its header to the next section's."""

    parts = re.split(r"(?=Agents for \w+ \(at most)", output)
    return next(part for part in parts if part.startswith(f"{title} (at most"))


@when(parsers.parse("the user adds the analyze command `{command}` named `{name}`"))
def _user_adds_analyze_command(
    command: str,
    name: str,
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _user_answers_agent_prompts(scenario_state, config_path, monkeypatch, capsys, command, name)


@when(parsers.parse("the user enters the analyze command `{command}`, which no program matches"))
def _user_enters_unknown_analyze_command(
    command: str,
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _user_answers_agent_prompts(scenario_state, config_path, monkeypatch, capsys, command)


@when("the user adds no more agents")
@when("the user adds no agents")
def _user_adds_no_agents(
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _user_answers_agent_prompts(scenario_state, config_path, monkeypatch, capsys, "", "")


@when(parsers.parse("the user adds {count:d} analyze commands"))
def _user_adds_analyze_commands(
    count: int,
    scenario_state: dict[str, Any],
    config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = [answer for number in range(count) for answer in ("aider --yes", f"aider-{number}")]
    _user_answers_agent_prompts(scenario_state, config_path, monkeypatch, capsys, *answers)


@then("the system tells the user which agents were detected")
def _tells_detected_agents(scenario_state: dict[str, Any]) -> None:
    assert "Detected 'claude' on PATH" in scenario_state["output"]


@then("the system adds each detected agent with its default analyze and launch commands")
def _adds_detected_agents_with_defaults(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["written_config"].agents == {
        "claude": AgentConfig(analyze="claude -p", launch="claude")
    }


@then(parsers.parse('the system shows an "{title}" section listing `{first}` and `{second}`'))
def _shows_section_listing_agents(
    title: str, first: str, second: str, scenario_state: dict[str, Any]
) -> None:
    # The listing lines, not the header, whose example command also names an agent.
    section = _section(scenario_state["output"], title)
    for name in (first, second):
        assert f"  {name}:" in section, section


@then("each section states how many agents it accepts")
def _sections_state_their_limits(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "at most 9" in _section(output, "Agents for analyze")
    assert "at most 9" in _section(output, "Agents for code")


@then("each section asks for another command until the user answers with an empty line")
def _sections_ask_until_empty_line(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    for title in ("Agents for analyze", "Agents for code"):
        assert sum(prompt.startswith(title) for prompt in prompts) == 1, prompts
    assert scenario_state["exit_code"] == 0


@then("the saved configuration has an agent named `aider` with the analyze command `aider --yes`")
def _saved_configuration_has_aider(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["written_config"].agents["aider"].analyze == "aider --yes"


@then("that agent has no launch command")
def _agent_has_no_launch_command(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["written_config"].agents["aider"].launch == ""


@then(
    "before asking for an agent command the system tells the user that the task prompt is "
    "appended as the command's last argument"
)
def _tells_prompt_is_appended(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    first_command_prompt = next(
        prompt for prompt in scenario_state["prompts"] if prompt.startswith("Agents for")
    )
    notice = output.index("task prompt")
    assert "last argument" in output[notice : notice + 200]
    assert notice < output.index(first_command_prompt)


@then("each section tells the user what kind of command it asks for, with an example")
def _sections_say_what_they_ask_for(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    for title, purpose in (
        ("Agents for analyze", "print the analysis"),
        ("Agents for code", "interactive coding session"),
    ):
        header = _section(output, title).splitlines()[0]
        assert purpose in header, header
        assert re.search(r"e\.g\. \S+", header), header


@then("the system tells the user that no supported agent was detected")
def _tells_no_agent_was_detected(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    line = next(line for line in output.splitlines() if "No supported agent found" in line)
    assert all(name in line for name in ("claude", "codex", "pi")), line
    assert output.index(line) < output.index("At least one agent")


@then("the system tells the user that at least one agent is required")
def _tells_one_agent_is_required(scenario_state: dict[str, Any]) -> None:
    assert "At least one agent is required" in scenario_state["output"]


@then("the system asks for agents again")
def _asks_for_agents_again(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    assert sum(prompt.startswith("Agents for analyze") for prompt in prompts) == 2, prompts


@then("no configuration is saved until an agent has been added")
def _no_configuration_saved(scenario_state: dict[str, Any], config_path: Path) -> None:
    assert not config_path.exists()


@then(parsers.parse("the system tells the user that `{program}` was not found"))
def _tells_program_was_not_found(program: str, scenario_state: dict[str, Any]) -> None:
    assert f"`{program}` was not found" in scenario_state["output"]


@then("the system asks for the analyze command again")
def _asks_for_analyze_command_again(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    assert sum(prompt.startswith("Agents for analyze") for prompt in prompts) == 2, prompts
    assert prompts[-1].startswith("Agents for analyze"), prompts


@then("the system tells the user that the analyze section is full")
def _tells_analyze_section_is_full(scenario_state: dict[str, Any]) -> None:
    assert "section is full" in _section(scenario_state["output"], "Agents for analyze")


@then("the system does not ask for another analyze command")
def _does_not_ask_for_another_analyze_command(scenario_state: dict[str, Any]) -> None:
    prompts = scenario_state["prompts"]
    assert sum(prompt.startswith("Agents for analyze") for prompt in prompts) == 9, prompts


@then(
    parsers.parse(
        'the system tells the user that agents can be added or edited later in "{path_template}"'
    )
)
def _tells_agents_can_be_edited_later(
    path_template: str, scenario_state: dict[str, Any], config_path: Path
) -> None:
    # The scenario names the canonical path; the wizard ran against a tmp path.
    hint = next(line for line in scenario_state["output"].splitlines() if "edit 'agents'" in line)
    assert str(config_path) in hint, hint
    assert scenario_state["output"].index(hint) < scenario_state["output"].index("GitHub username")


@then("the system does not ask about agents")
def _does_not_ask_about_agents(scenario_state: dict[str, Any]) -> None:
    assert not any(prompt.startswith("Agent") for prompt in scenario_state["prompts"])
    assert "Agents for" not in scenario_state["output"]


@then("the saved configuration keeps the same agents in the same order")
def _saved_configuration_keeps_agents_in_order(scenario_state: dict[str, Any]) -> None:
    written = scenario_state["written_config"]
    assert list(written.agents.items()) == list(scenario_state["prior_config"].agents.items())
    assert list(written.agents) == ["pi", "aider", "claude"]
