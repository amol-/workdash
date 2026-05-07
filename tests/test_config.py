import json

import pytest

from workdash.config import (
    AgentConfig,
    WorkdashConfig,
    configure,
    load_config,
    save_config,
    validate_config,
)


def test_load_config_returns_empty_when_file_missing(tmp_path):
    config_path = tmp_path / "workdash.json"

    config = load_config(config_path)

    assert config == WorkdashConfig()
    assert config.github_username == ""
    assert config.claude == AgentConfig()
    assert config.codex == AgentConfig()
    assert config.repositories == ()
    assert config.workdir == ""
    assert not config_path.exists()


def test_load_config_reads_existing_config(tmp_path):
    config_path = tmp_path / "workdash.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                },
                "repositories": ["owner/repo"],
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.github_username == "octocat"
    assert config.claude.analyze == "claude -p"
    assert config.claude.launch == "claude"
    assert config.codex.analyze == "codex exec"
    assert config.codex.launch == "codex"
    assert config.repositories == ("owner/repo",)
    assert config.workdir == ""


def test_load_config_returns_empty_for_missing_keys(tmp_path):
    config_path = tmp_path / "workdash.json"
    config_path.write_text(json.dumps({"github_username": "octocat"}), encoding="utf-8")

    config = load_config(config_path)

    assert config.github_username == "octocat"
    assert config.claude == AgentConfig()
    assert config.codex == AgentConfig()
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
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        repositories=("owner/repo",),
        workdir="~/src",
    )

    save_config(config, config_path)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written == {
        "github_username": "octocat",
        "agents": {
            "claude": {"analyze": "claude -p", "launch": "claude"},
            "codex": {"analyze": "codex exec", "launch": "codex"},
        },
        "repositories": ["owner/repo"],
        "workdir": "~/src",
    }


def test_validate_config_returns_empty_for_complete_config():
    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        repositories=("owner/repo",),
        workdir="~/src",
    )

    assert validate_config(config) == []


def test_validate_config_returns_all_missing_fields():
    assert validate_config(WorkdashConfig()) == [
        "github_username",
        "repositories",
        "workdir",
        "agents.claude.analyze",
        "agents.claude.launch",
        "agents.codex.analyze",
        "agents.codex.launch",
    ]


def test_validate_config_returns_subset_of_missing_fields():
    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
    )

    assert validate_config(config) == [
        "repositories",
        "workdir",
        "agents.codex.analyze",
        "agents.codex.launch",
    ]


def test_configure_fresh_auto_detects_and_asks_username(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    inputs = iter(["octocat", "~/projects"])

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("claude", "codex") else None,
    )

    assert config.github_username == "octocat"
    assert config.claude.analyze == "claude -p"
    assert config.claude.launch == "claude"
    assert config.codex.analyze == "codex exec"
    assert config.codex.launch == "codex"
    assert config.workdir == "~/projects"
    assert config.repositories == ("octocat/*",)
    assert config_path.exists()
    output = capsys.readouterr().out
    assert "Detected 'claude'" in output
    assert "Detected 'codex'" in output
    assert "octocat/*" in output


def test_configure_asks_interactively_when_commands_not_on_path(tmp_path):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "my-claude -p",  # claude analyze
            "my-claude",  # claude launch
            "my-codex run",  # codex analyze
            "my-codex",  # codex launch
            "octocat",  # username
            "~/code",  # source_directory
        ]
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: None,
    )

    assert config.claude.analyze == "my-claude -p"
    assert config.claude.launch == "my-claude"
    assert config.codex.analyze == "my-codex run"
    assert config.codex.launch == "my-codex"
    assert config.workdir == "~/code"
    assert config.github_username == "octocat"
    assert config.repositories == ("octocat/*",)


def test_configure_accepts_defaults_for_empty_optional_responses(tmp_path):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "",  # claude analyze default
            "",  # claude launch default
            "",  # codex analyze default
            "",  # codex launch default
            "octocat",
            "",  # workdir default
        ]
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: None,
    )

    assert config.claude.analyze == "claude -p"
    assert config.claude.launch == "claude"
    assert config.codex.analyze == "codex exec"
    assert config.codex.launch == "codex"
    assert config.github_username == "octocat"
    assert config.workdir == "~/wrk"
    assert config.repositories == ("octocat/*",)


def test_configure_reprompts_for_required_fields_without_defaults(tmp_path):
    config_path = tmp_path / "config.json"
    prompts: list[str] = []
    inputs = iter(["", "octocat", ""])  # username retry, workdir default

    config = configure(
        config_path,
        input_fn=lambda prompt: (prompts.append(prompt), next(inputs))[1],
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("claude", "codex") else None,
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
                "agents": {
                    "claude": {
                        "analyze": "existing-claude-analyze",
                        "launch": "existing-claude-launch",
                    },
                    "codex": {
                        "analyze": "existing-codex-analyze",
                        "launch": "existing-codex-launch",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    inputs = iter(["~/src"])  # only workdir prompted

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: None,
    )

    assert config.github_username == "existing-user"
    assert config.claude.analyze == "existing-claude-analyze"
    assert config.claude.launch == "existing-claude-launch"
    assert config.codex.analyze == "existing-codex-analyze"
    assert config.codex.launch == "existing-codex-launch"
    assert config.workdir == "~/src"
    assert config.repositories == ("existing-user/*",)


def test_configure_preserves_existing_repositories(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
            }
        ),
        encoding="utf-8",
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
        which_fn=lambda cmd: None,
    )

    assert config.repositories == ("specific/repo",)
    assert config.workdir == "~/src"
