import json
import platform
import shlex
import shutil
import tarfile
import zipfile
from io import BytesIO

import pytest

from workdash.config import (
    AgentConfig,
    WorkdashAgentChoice,
    WorkdashConfig,
    WorkdashConfigValidationError,
    configure,
    install_gh_binary,
    install_zellij_binary,
    load_config,
    save_config,
    validate_config,
)


def test_load_config_returns_empty_when_file_missing(tmp_path):
    config_path = tmp_path / "workdash.json"

    config = load_config(config_path)

    assert config == WorkdashConfig()
    assert config.github_username == ""
    assert config.agents == {}
    assert config.repositories == ()
    assert config.workdir == ""
    assert not config_path.exists()


def test_load_config_reads_existing_config(tmp_path):
    config_path = tmp_path / "workdash.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "agents": {
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                    "aider": {"analyze": "aider --yes"},
                    "claude": {"launch": "claude"},
                    "ignored": "not an object",
                },
                "repositories": ["owner/repo"],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.github_username == "octocat"
    assert config.open_command == "xdg-open"
    assert config.open_needs_pane is False
    assert list(config.agents.items()) == [
        ("pi", AgentConfig(analyze="pi -p", launch="pi")),
        ("aider", AgentConfig(analyze="aider --yes")),
        ("claude", AgentConfig(launch="claude")),
    ]
    assert config.repositories == ("owner/repo",)
    assert config.workdir == ""


def test_load_config_returns_empty_for_missing_keys(tmp_path):
    config_path = tmp_path / "workdash.json"
    config_path.write_text(json.dumps({"github_username": "octocat"}), encoding="utf-8")

    config = load_config(config_path)

    assert config.github_username == "octocat"
    assert config.agents == {}
    assert config.repositories == ()


def test_load_config_raises_for_malformed_json(tmp_path):
    config_path = tmp_path / "workdash.json"
    config_path.write_text("{invalid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        load_config(config_path)


def test_load_config_raises_for_non_dict_json(tmp_path):
    config_path = tmp_path / "workdash.json"
    config_path.write_text('"hello"', encoding="utf-8")

    with pytest.raises(RuntimeError, match="must be a JSON object"):
        load_config(config_path)


def test_save_config_creates_parent_dirs_and_writes_json(tmp_path):
    config_path = tmp_path / "sub" / "dir" / "config.json"
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(analyze="pi -p", launch="pi"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    save_config(config, config_path)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert list(written["agents"]) == ["codex", "claude", "pi"]
    assert written == {
        "github_username": "octocat",
        "open": "xdg-open",
        "open_needs_pane": False,
        "agents": {
            "claude": {"analyze": "claude -p", "launch": "claude"},
            "codex": {"analyze": "codex exec", "launch": "codex"},
            "pi": {"analyze": "pi -p", "launch": "pi"},
        },
        "repositories": ["owner/repo"],
        "workdir": "~/src",
        "todo_repository": "octocat/todos",
    }


def test_validate_config_returns_empty_for_complete_config():
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(analyze="pi -p", launch="pi"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    assert validate_config(config) == []
    assert config.require_valid() is config


def test_require_valid_accepts_partial_agent_config():
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze="codex exec"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    assert validate_config(config) == []
    assert config.require_valid() is config
    assert config.configured_analyze_agents() == ["codex"]
    assert config.configured_code_agents() == []
    assert config.tui_analyze_choices() == [
        WorkdashAgentChoice("1", "codex", "Analyze with codex", "codex")
    ]
    assert config.tui_code_choices() == [
        WorkdashAgentChoice("1", "vscode", "VSCode Copilot", "VSCode")
    ]


def test_agents_are_offered_by_name_in_configuration_order():
    config = WorkdashConfig(
        agents={
            "pi": AgentConfig(analyze="pi --no-session --tools read,grep,find,ls -p", launch="pi"),
            "aider": AgentConfig(analyze="aider --yes"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
        },
    )

    assert config.configured_analyze_agents() == ["pi", "aider", "claude"]
    assert config.configured_code_agents() == ["pi", "claude"]
    assert config.tui_analyze_choices() == [
        WorkdashAgentChoice("1", "pi", "Analyze with pi", "pi"),
        WorkdashAgentChoice("2", "aider", "Analyze with aider", "aider"),
        WorkdashAgentChoice("3", "claude", "Analyze with claude", "claude"),
    ]
    assert config.tui_code_choices() == [
        WorkdashAgentChoice("1", "pi", "pi", "pi"),
        WorkdashAgentChoice("2", "claude", "claude", "claude"),
        WorkdashAgentChoice("3", "vscode", "VSCode Copilot", "VSCode"),
    ]
    assert config.analyze_agent_command_tokens("pi") == [
        "pi",
        "--no-session",
        "--tools",
        "read,grep,find,ls",
        "-p",
    ]
    assert config.code_agent_launch_command_tokens("claude") == ["claude"]


def test_agent_order_survives_a_config_save_and_load(tmp_path):
    config_path = tmp_path / "config.json"
    agents = {
        "pi": AgentConfig(analyze="pi -p", launch="pi"),
        "aider": AgentConfig(analyze="aider --yes"),
        "claude": AgentConfig(launch="claude"),
    }

    save_config(WorkdashConfig(agents=agents), config_path)

    assert list(load_config(config_path).agents.items()) == list(agents.items())


@pytest.mark.parametrize("agent", ["vscode", "typo"])
def test_analyze_agent_command_tokens_rejects_unsupported_agent_without_codex_fallback(
    agent: str,
):
    config = WorkdashConfig(agents={"codex": AgentConfig(analyze="codex exec")})

    with pytest.raises(ValueError, match=f"Unsupported analyze agent: '{agent}'"):
        config.analyze_agent_command_tokens(agent)


def test_require_valid_rejects_malformed_command_strings():
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze="codex 'broken", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(analyze="pi 'broken", launch="pi 'broken"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.missing_fields == ()
    assert error.value.invalid_fields == (
        "agents.codex.analyze: No closing quotation",
        "agents.pi.analyze: No closing quotation",
        "agents.pi.launch: No closing quotation",
    )
    assert str(error.value) == (
        "invalid configuration fields: agents.codex.analyze: No closing quotation, "
        "agents.pi.analyze: No closing quotation, agents.pi.launch: No closing quotation"
    )


def test_require_valid_rejects_malformed_open_command():
    config = WorkdashConfig(
        github_username="octocat",
        open_command="open 'broken",
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.invalid_fields == ("open: No closing quotation",)


def test_require_valid_rejects_non_string_command_values():
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze=["codex", "exec"], launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(analyze="pi -p", launch="pi"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.invalid_fields == ("agents.codex.analyze: expected a non-empty string",)


@pytest.mark.parametrize("command", ["''", '""'])
def test_require_valid_rejects_blank_command_tokens(command: str):
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch=command),
        },
        repositories=("owner/repo",),
        workdir="~/src",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.invalid_fields == ("agents.codex.launch: contains a blank shell token",)
    assert config.configured_code_agents() == []


def test_validate_config_returns_all_missing_fields():
    expected_missing = [
        "github_username",
        "open",
        "repositories",
        "workdir",
        "todo_repository",
        "agents",
    ]

    assert validate_config(WorkdashConfig()) == expected_missing
    with pytest.raises(WorkdashConfigValidationError) as error:
        WorkdashConfig().require_valid()
    assert error.value.missing_fields == tuple(expected_missing)
    assert str(error.value) == "missing configuration fields: " + ", ".join(expected_missing)


def test_require_valid_rejects_a_todo_repository_that_is_not_owner_slash_repo():
    """A hand-edited todo repository must fail at the config edge, not later in gh."""

    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="todos",
    )

    # The value is present, so the user must be told it is wrong, not absent.
    assert validate_config(config) == []
    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.missing_fields == ()
    assert str(error.value) == "invalid configuration fields: todo_repository: expected owner/repo"


def test_an_agent_without_any_command_does_not_count_as_an_agent():
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={"pi": AgentConfig(analyze="", launch=" ")},
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    assert validate_config(config) == ["agents"]
    with pytest.raises(WorkdashConfigValidationError, match="missing configuration fields: agents"):
        config.require_valid()


@pytest.mark.parametrize("name", ["cached", "vscode"])
def test_require_valid_rejects_reserved_agent_names(name: str):
    config = WorkdashConfig(
        github_username="octocat",
        open_command="xdg-open",
        agents={
            "pi": AgentConfig(launch="pi"),
            name: AgentConfig(analyze="aider --yes"),
        },
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.missing_fields == ()
    assert error.value.invalid_fields == (f"agents.{name}: reserved name",)


def test_validate_config_returns_subset_of_missing_fields():
    config = WorkdashConfig(
        github_username="octocat",
        agents={
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
        },
    )

    assert validate_config(config) == ["open", "repositories", "workdir", "todo_repository"]


def _scripted_input(*answers: str):
    """An ``input_fn`` that replays *answers* and records the prompts it was asked."""

    remaining = iter(answers)
    prompts: list[str] = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return next(remaining)

    return input_fn, prompts


def _on_path(*commands: str):
    return lambda cmd: f"/usr/bin/{cmd}" if cmd in commands else None


@pytest.fixture
def shell_with_programs(tmp_path, monkeypatch):
    """Make `/bin/sh` the user's shell and give it only fake `my-agent`/`other-agent` programs."""

    bin_dir = tmp_path / "shell-bin"
    bin_dir.mkdir()
    for program in ("my-agent", "other-agent"):
        (bin_dir / program).write_text("#!/bin/sh\n", encoding="utf-8")
        (bin_dir / program).chmod(0o755)
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv("ENV", raising=False)  # an interactive shell would source it


def test_configure_fresh_adds_detected_agents_in_order_and_asks_username(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    # open command, viewer terminal, no more analyze agents, no more code agents,
    # username, workdir, todo default
    input_fn, _ = _scripted_input("", "", "", "", "octocat", "~/projects", "")

    config = configure(
        config_path,
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "pi", "claude", "codex", "xdg-open"),
    )

    assert config.open_command == "xdg-open"
    assert config.github_username == "octocat"
    assert list(config.agents.items()) == [
        ("claude", AgentConfig(analyze="claude -p", launch="claude")),
        ("codex", AgentConfig(analyze="codex exec", launch="codex")),
        ("pi", AgentConfig(analyze="pi --no-session --tools read,grep,find,ls -p", launch="pi")),
    ]
    assert config.workdir == "~/projects"
    assert config.repositories == ("octocat/*",)
    assert list(json.loads(config_path.read_text(encoding="utf-8"))["agents"]) == [
        "claude",
        "codex",
        "pi",
    ]
    output = capsys.readouterr().out
    assert "Detected 'claude' on PATH" in output
    assert "Detected 'codex' on PATH" in output
    assert (
        "Detected 'pi' on PATH, using analyze: pi --no-session --tools read,grep,find,ls -p"
        in output
    )
    assert "octocat/*" in output


def test_configure_suggests_the_macos_open_command(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    input_fn, prompts = _scripted_input("", "", "", "", "octocat", "", "")

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    config = configure(
        config_path,
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "claude", "open"),
    )

    assert config.open_command == "open"
    assert prompts[:2] == [
        "Open command [open] (Enter to accept): ",
        "Does the viewer require its own terminal window? [y/N] ",
    ]
    assert prompts[-3:] == [
        "GitHub username: ",
        "Work directory [~/wrk]: ",
        "Todo repository [octocat/todos]: ",
    ]


def test_configure_reprompts_for_an_invalid_open_command(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "open 'broken",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
            }
        ),
        encoding="utf-8",
    )

    prompts: list[str] = []
    inputs = iter(["term-copy", ""])
    config = configure(
        config_path,
        input_fn=lambda prompt: (prompts.append(prompt), next(inputs))[1],
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "gh", "term-copy") else None,
    )

    assert config.open_command == "term-copy"
    assert config.open_needs_pane is False
    assert prompts == [
        "Open command (required): ",
        "Does the viewer require its own terminal window? [y/N] ",
    ]


@pytest.mark.parametrize("pane_choice", [None, "false", False, True])
def test_configure_repairs_viewer_executable_and_preserves_other_settings(
    tmp_path, capsys, pane_choice
):
    config_path = tmp_path / "config.json"
    original = WorkdashConfig(
        github_username="octocat",
        open_command="missing-viewer --quiet",
        agents={
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "pi": AgentConfig(launch="pi"),
        },
        repositories=("specific/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )
    save_config(original, config_path)
    raw = json.loads(config_path.read_text())
    if pane_choice is None:
        del raw["open_needs_pane"]
    else:
        raw["open_needs_pane"] = pane_choice
    config_path.write_text(json.dumps(raw))
    viewer = tmp_path / "safe viewer"
    viewer.write_text("#!/bin/sh\nexit 0\n")
    viewer.chmod(0o755)
    not_executable = tmp_path / "not-executable"
    not_executable.write_text("not executable")
    command = f'{shlex.quote(str(viewer))} --label "two words"'
    responses = ["", "viewer 'broken", "missing-viewer", str(not_executable), command]
    if not isinstance(pane_choice, bool):
        responses += ["maybe", "y"]
    inputs = iter(responses)
    prompts = []

    def which(cmd):
        if cmd in {"zellij", "gh"}:
            return f"/usr/bin/{cmd}"
        return shutil.which(cmd, path=str(tmp_path))

    configured = configure(
        config_path,
        input_fn=lambda prompt: (prompts.append(prompt), next(inputs))[1],
        which_fn=which,
    )
    expected = json.loads(config_path.read_text())
    assert expected == {
        **raw,
        "open": command,
        "open_needs_pane": pane_choice if isinstance(pane_choice, bool) else True,
    }
    assert configured.require_valid() is configured
    assert sum("Open command" in prompt for prompt in prompts) == 5
    assert sum("Does the viewer" in prompt for prompt in prompts) == (
        0 if isinstance(pane_choice, bool) else 2
    )
    assert "executable exists" in capsys.readouterr().out


def test_configure_adds_custom_agents_that_the_users_shell_can_run(
    tmp_path, capsys, shell_with_programs
):
    config_path = tmp_path / "config.json"
    input_fn, prompts = _scripted_input(
        "term-copy",  # open command
        "y",  # viewer terminal
        "not-installed -p",  # analyze command no program matches: asked again
        "my-agent 'broken",  # not a valid command line: asked again
        "FOO=1",  # only an environment assignment, no program: asked again
        "FOO=1 my-agent -p",  # analyze command, run with an environment variable
        "mine",  # its name
        "",  # no more analyze agents
        "my-agent",  # code command of the same program...
        "mine",  # ...under the same name: one agent holding both commands
        "other-agent",  # a second code agent
        "other",
        "",  # no more code agents
        "octocat",
        "~/code",
        "octocat/notes",
    )

    config = configure(
        config_path,
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "term-copy"),
    )

    assert list(config.agents.items()) == [
        ("mine", AgentConfig(analyze="FOO=1 my-agent -p", launch="my-agent")),
        ("other", AgentConfig(launch="other-agent")),
    ]
    assert load_config(config_path).agents == config.agents
    output = capsys.readouterr().out
    assert "`not-installed` was not found" in output
    assert "`FOO=1` was not found" in output
    assert "Invalid command: No closing quotation" in output
    assert sum(prompt.startswith("Agents for analyze") for prompt in prompts) == 5
    assert config.open_command == "term-copy"
    assert config.open_needs_pane is True
    assert config.workdir == "~/code"
    assert config.todo_repository == "octocat/notes"
    assert config.github_username == "octocat"
    assert config.repositories == ("octocat/*",)


def test_configure_accepts_commands_that_are_shell_aliases_or_functions(
    tmp_path, monkeypatch, capsys, shell_with_programs
):
    shell_startup = tmp_path / "shell-startup"
    shell_startup.write_text("myagent() { :; }\nalias myalias=my-agent\n", encoding="utf-8")
    monkeypatch.setenv("ENV", str(shell_startup))
    input_fn, _ = _scripted_input(
        "", "", "myagent -p", "fn", "myalias -p", "al", "", "", "octocat", "", ""
    )

    config = configure(
        tmp_path / "config.json",
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "xdg-open"),
    )

    assert config.agents == {
        "fn": AgentConfig(analyze="myagent -p"),
        "al": AgentConfig(analyze="myalias -p"),
    }
    assert "was not found" not in capsys.readouterr().out


def test_configure_rejects_commands_when_the_shell_cannot_be_run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SHELL", str(tmp_path / "no-such-shell"))
    input_fn, _ = _scripted_input("", "", "my-claude -p", "", "", "octocat", "", "")

    config = configure(
        tmp_path / "config.json",
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "xdg-open", "claude"),
    )

    assert "`my-claude` was not found" in capsys.readouterr().out
    assert list(config.agents) == ["claude"]


def test_configure_asks_again_for_names_that_are_empty_reserved_or_taken(
    tmp_path, capsys, shell_with_programs
):
    config_path = tmp_path / "config.json"
    input_fn, _ = _scripted_input(
        "",  # open command
        "",  # viewer terminal
        "my-agent -p",
        "",  # a name is required
        "vscode",  # reserved
        "cached",  # reserved
        "mine",
        "my-agent -x",
        "mine",  # already has an analyze command in this section
        "other",
        "",  # no more analyze agents
        "",  # no code agents
        "octocat",
        "",
        "",
    )

    config = configure(
        config_path, input_fn=input_fn, which_fn=_on_path("zellij", "gh", "xdg-open")
    )

    assert config.agents == {
        "mine": AgentConfig(analyze="my-agent -p"),
        "other": AgentConfig(analyze="my-agent -x"),
    }
    output = capsys.readouterr().out
    assert "The agent name is required" in output
    assert "'vscode' is reserved" in output
    assert "'cached' is reserved" in output
    assert "'mine' is already used in this section" in output


def test_configure_requires_an_agent_before_saving(tmp_path, capsys, shell_with_programs):
    config_path = tmp_path / "config.json"
    input_fn, prompts = _scripted_input(
        "",  # open command
        "",  # viewer terminal
        "",  # no analyze agent
        "",  # no code agent: the wizard asks again
        "my-agent -p",
        "mine",
        "",
        "",
        "octocat",
        "",
        "",
    )

    config = configure(
        config_path, input_fn=input_fn, which_fn=_on_path("zellij", "gh", "xdg-open")
    )

    assert "At least one agent is required" in capsys.readouterr().out
    assert sum(prompt.startswith("Agents for analyze") for prompt in prompts) == 3
    assert config.agents == {"mine": AgentConfig(analyze="my-agent -p")}


def test_configure_counts_detected_agents_toward_the_section_limits(
    tmp_path, capsys, shell_with_programs
):
    config_path = tmp_path / "config.json"
    analyze_agents = [("my-agent -p", f"analyze-{number}") for number in range(6)]
    code_agents = [("my-agent", f"code-{number}") for number in range(5)]
    input_fn, prompts = _scripted_input(
        "",  # open command
        "",  # viewer terminal
        *[answer for pair in analyze_agents for answer in pair],  # 3 detected + 6 = 9, full
        *[answer for pair in code_agents for answer in pair],  # 3 detected + 5 = 8, full
        "octocat",
        "",
        "",
    )

    config = configure(
        config_path,
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "xdg-open", "claude", "codex", "pi"),
    )

    assert sum(prompt.startswith("Agents for analyze") for prompt in prompts) == 6
    assert sum(prompt.startswith("Agents for code") for prompt in prompts) == 5
    assert sum(1 for agent in config.agents.values() if agent.analyze) == 9
    assert sum(1 for agent in config.agents.values() if agent.launch) == 8
    assert capsys.readouterr().out.count("section is full") == 2


def test_configure_asks_about_agents_when_the_only_entry_has_no_commands(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "agents": {"pi": {"analyze": "", "launch": ""}},
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
            }
        ),
        encoding="utf-8",
    )
    input_fn, _ = _scripted_input("", "", "")

    config = configure(
        config_path, input_fn=input_fn, which_fn=_on_path("zellij", "gh", "claude", "xdg-open")
    )

    assert config.agents == {"claude": AgentConfig(analyze="claude -p", launch="claude")}


def test_configure_reprompts_for_required_fields_without_defaults(tmp_path):
    config_path = tmp_path / "config.json"
    # Viewer defaults, both agent sections ended, username retry, workdir and todo defaults.
    input_fn, prompts = _scripted_input("", "", "", "", "", "octocat", "", "")

    config = configure(
        config_path,
        input_fn=input_fn,
        which_fn=_on_path("zellij", "gh", "claude", "xdg-open"),
    )

    assert config.github_username == "octocat"
    assert config.workdir == "~/wrk"
    assert sum("GitHub username" in prompt for prompt in prompts) == 2


def test_configure_fills_only_missing_fields(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "existing-user",
                "open": "existing-open",
                "open_needs_pane": True,
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
    inputs = iter(["~/src", ""])  # only the workdir and todo repository are empty

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: (
            f"/opt/homebrew/bin/{cmd}" if cmd in ("zellij", "gh", "existing-open") else None
        ),
    )
    assert config.github_username == "existing-user"
    assert list(config.agents.items()) == [
        ("claude", AgentConfig("existing-claude-analyze", "existing-claude-launch")),
        ("codex", AgentConfig("existing-codex-analyze", "existing-codex-launch")),
        ("pi", AgentConfig("existing-pi-analyze", "existing-pi-launch")),
    ]
    assert config.workdir == "~/src"
    assert config.repositories == ("existing-user/*",)


def test_configure_reprompts_for_a_malformed_todo_repository(tmp_path):
    """A hand-broken todo repository would lock the user out, so the wizard asks again."""

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "todos",
            }
        ),
        encoding="utf-8",
    )

    prompts = []

    def input_fn(prompt: str) -> str:
        prompts.append(prompt)
        return ""

    config = configure(
        config_path,
        input_fn=input_fn,
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "gh", "xdg-open") else None,
    )

    assert prompts == [
        "Does the viewer require its own terminal window? [y/N] ",
        "Todo repository [octocat/todos]: ",
    ]
    assert config.todo_repository == "octocat/todos"
    assert config.require_valid() is config


def test_configure_preserves_existing_repositories(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
                "open_needs_pane": False,
            }
        ),
        encoding="utf-8",
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "gh", "xdg-open") else None,
    )

    assert config.repositories == ("specific/repo",)
    assert config.workdir == "~/src"


def test_configure_tells_where_to_edit_agents_even_when_agents_are_configured(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "open_needs_pane": False,
                "agents": {"claude": {"analyze": "claude -p", "launch": "claude"}},
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
            }
        ),
        encoding="utf-8",
    )

    configure(
        config_path,
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
        which_fn=_on_path("zellij", "gh", "xdg-open"),
    )

    assert (
        f"To add or edit agents later, edit 'agents' in {config_path}." in capsys.readouterr().out
    )


def test_configure_installs_zellij_when_not_on_path(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "",  # open command default
            "",  # viewer terminal default
            "",  # no more analyze agents
            "",  # no more code agents
            "octocat",
            "",  # workdir default
            "",  # todo repository default
        ]
    )
    install_calls: list[str] = []

    def fake_install() -> str:
        install_calls.append("install")
        return str(tmp_path / "bin" / "zellij")

    configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: (
            f"/usr/bin/{cmd}" if cmd in ("gh", "claude", "codex", "xdg-open") else None
        ),
        install_zellij_fn=fake_install,
    )

    assert install_calls == ["install"]
    output = capsys.readouterr().out
    assert "Zellij is not on PATH. Installing a local Zellij binary from" in output
    assert "To use a global Zellij instead" in output
    assert "Installed Zellij to:" in output


def test_configure_redownloads_zellij_when_no_global_binary_exists(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "open_needs_pane": False,
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
            }
        ),
        encoding="utf-8",
    )
    install_calls: list[str] = []

    def fake_install() -> str:
        install_calls.append("install")
        return "/new/local/zellij"

    config = configure(
        config_path,
        input_fn=lambda prompt: (_ for _ in ()).throw(
            AssertionError(f"Unexpected prompt {prompt}")
        ),
        which_fn=lambda cmd: "/usr/bin/xdg-open" if cmd == "xdg-open" else None,
        install_zellij_fn=fake_install,
        install_gh_fn=lambda: "/new/local/gh",
    )
    assert install_calls == ["install"]
    assert config.repositories == ("specific/repo",)


def test_configure_installs_gh_when_not_on_path(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "",  # open command default
            "",  # viewer terminal default
            "",  # no more analyze agents
            "",  # no more code agents
            "octocat",
            "",  # workdir default
            "",  # todo repository default
        ]
    )
    install_calls: list[str] = []

    def fake_install() -> str:
        install_calls.append("install")
        return str(tmp_path / "bin" / "gh")

    configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "xdg-open", "claude") else None,
        install_gh_fn=fake_install,
    )

    assert install_calls == ["install"]
    output = capsys.readouterr().out
    assert "GitHub CLI is not on PATH. Installing a local GitHub CLI binary from" in output
    assert "To use a global GitHub CLI instead" in output
    assert "Installed GitHub CLI to:" in output


def test_configure_redownloads_gh_when_no_global_binary_exists(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "open": "xdg-open",
                "open_needs_pane": False,
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"analyze": "pi -p", "launch": "pi"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
            }
        ),
        encoding="utf-8",
    )
    install_calls: list[str] = []

    def fake_install() -> str:
        install_calls.append("install")
        return "/new/local/gh"

    config = configure(
        config_path,
        input_fn=lambda prompt: (_ for _ in ()).throw(
            AssertionError(f"Unexpected prompt {prompt}")
        ),
        which_fn=lambda cmd: "/usr/bin/xdg-open" if cmd == "xdg-open" else None,
        install_zellij_fn=lambda: "/new/local/zellij",
        install_gh_fn=fake_install,
    )
    assert install_calls == ["install"]
    assert config.repositories == ("specific/repo",)


def test_install_zellij_binary_downloads_latest_platform_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    archive_bytes = BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        payload = b"fake-zellij"
        info = tarfile.TarInfo("zellij")
        info.size = len(payload)
        archive.addfile(info, BytesIO(payload))
    urls: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return None

        def read(self) -> bytes:
            return archive_bytes.getvalue()

    def fake_urlopen(url: str):
        urls.append(url)
        return FakeResponse()

    monkeypatch.setattr("workdash.config.platform.machine", lambda: "arm64")
    monkeypatch.setattr("workdash.config.platform.system", lambda: "Darwin")
    destination = tmp_path / "config" / "workdash" / "bin" / "zellij"

    installed = install_zellij_binary(destination, urlopen_fn=fake_urlopen)

    assert installed == str(destination)
    assert destination.read_bytes() == b"fake-zellij"
    assert destination.stat().st_mode & 0o111
    assert urls == [
        "https://github.com/zellij-org/zellij/releases/latest/download/"
        "zellij-aarch64-apple-darwin.tar.gz"
    ]


def test_install_gh_binary_downloads_latest_platform_archive(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    release_payload = json.dumps(
        {
            "assets": [
                {
                    "name": "gh_2.90.0_macOS_arm64.zip",
                    "browser_download_url": "https://example.test/gh_2.90.0_macOS_arm64.zip",
                }
            ]
        }
    ).encode("utf-8")
    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, mode="w") as archive:
        archive.writestr("gh_2.90.0_macOS_arm64/bin/gh", b"fake-gh")
    urls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _traceback):
            return None

        def read(self) -> bytes:
            return self._payload

    def fake_urlopen(url: str):
        urls.append(url)
        if url.endswith("/releases/latest"):
            return FakeResponse(release_payload)
        return FakeResponse(archive_bytes.getvalue())

    monkeypatch.setattr("workdash.config.platform.machine", lambda: "arm64")
    monkeypatch.setattr("workdash.config.platform.system", lambda: "Darwin")
    destination = tmp_path / "config" / "workdash" / "bin" / "gh"

    installed = install_gh_binary(destination, urlopen_fn=fake_urlopen)

    assert installed == str(destination)
    assert destination.read_bytes() == b"fake-gh"
    assert destination.stat().st_mode & 0o111
    assert urls == [
        "https://api.github.com/repos/cli/cli/releases/latest",
        "https://example.test/gh_2.90.0_macOS_arm64.zip",
    ]
