import json
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
                    "pi": {"launch": "pi"},
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
    assert config.pi.launch == "pi"
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
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    save_config(config, config_path)

    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written == {
        "github_username": "octocat",
        "agents": {
            "claude": {"analyze": "claude -p", "launch": "claude"},
            "codex": {"analyze": "codex exec", "launch": "codex"},
            "pi": {"launch": "pi"},
        },
        "repositories": ["owner/repo"],
        "workdir": "~/src",
        "todo_repository": "octocat/todos",
    }


def test_validate_config_returns_empty_for_complete_config():
    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    assert validate_config(config) == []
    assert config.require_valid() is config


def test_require_valid_accepts_partial_agent_config():
    config = WorkdashConfig(
        github_username="octocat",
        codex=AgentConfig(analyze="codex exec"),
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    assert validate_config(config) == []
    assert config.require_valid() is config
    assert config.configured_analyze_agents() == ["codex"]
    assert config.configured_code_agents() == []
    assert config.tui_analyze_choices() == [
        WorkdashAgentChoice("1", "codex", "Analyze with ChatGPT Codex", "Codex")
    ]
    assert config.tui_code_choices() == [
        WorkdashAgentChoice("1", "vscode", "VSCode Copilot", "VSCode")
    ]


@pytest.mark.parametrize("agent", ["pi", "vscode", "typo"])
def test_analyze_agent_command_tokens_rejects_unsupported_agent_without_codex_fallback(
    agent: str,
):
    config = WorkdashConfig(codex=AgentConfig(analyze="codex exec"))

    with pytest.raises(ValueError, match=f"Unsupported analyze agent: '{agent}'"):
        config.analyze_agent_command_tokens(agent)


def test_require_valid_rejects_malformed_command_strings():
    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex 'broken", launch="codex"),
        pi=AgentConfig(launch="pi 'broken"),
        repositories=("owner/repo",),
        workdir="~/src",
        todo_repository="octocat/todos",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.missing_fields == ()
    assert error.value.invalid_fields == (
        "agents.codex.analyze: No closing quotation",
        "agents.pi.launch: No closing quotation",
    )
    assert str(error.value) == (
        "invalid configuration fields: agents.codex.analyze: No closing quotation, "
        "agents.pi.launch: No closing quotation"
    )


def test_require_valid_rejects_non_string_command_values():
    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze=["codex", "exec"], launch="codex"),
        pi=AgentConfig(launch="pi"),
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
        codex=AgentConfig(analyze="codex exec", launch=command),
        repositories=("owner/repo",),
        workdir="~/src",
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        config.require_valid()

    assert error.value.invalid_fields == ("agents.codex.launch: contains a blank shell token",)
    assert config.configured_code_agents() == []


def test_validate_config_returns_all_missing_fields():
    expected_missing = ["github_username", "repositories", "workdir", "todo_repository"]

    assert validate_config(WorkdashConfig()) == expected_missing
    with pytest.raises(WorkdashConfigValidationError) as error:
        WorkdashConfig().require_valid()
    assert error.value.missing_fields == tuple(expected_missing)
    assert str(error.value) == "missing configuration fields: " + ", ".join(expected_missing)


def test_require_valid_rejects_a_todo_repository_that_is_not_owner_slash_repo():
    """A hand-edited todo repository must fail at the config edge, not later in gh."""

    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
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


def test_validate_config_returns_subset_of_missing_fields():
    config = WorkdashConfig(
        github_username="octocat",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
    )

    assert validate_config(config) == ["repositories", "workdir", "todo_repository"]


def test_configure_fresh_auto_detects_and_asks_username(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    inputs = iter(["octocat", "~/projects", ""])  # username, workdir, todo repository default

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: (
            f"/usr/bin/{cmd}" if cmd in ("zellij", "gh", "claude", "codex", "pi") else None
        ),
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
    assert "Detected 'pi' on PATH, using launch: pi" in output
    assert config.pi.launch == "pi"
    assert "octocat/*" in output


def test_configure_asks_interactively_when_commands_not_on_path(tmp_path):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "my-claude -p",  # claude analyze
            "my-claude",  # claude launch
            "my-codex run",  # codex analyze
            "my-codex",  # codex launch
            "my-pi",  # pi launch
            "octocat",  # username
            "~/code",  # source_directory
            "octocat/notes",  # todo repository
        ]
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: f"/usr/local/bin/{cmd}" if cmd in ("zellij", "gh") else None,
    )
    assert config.claude.analyze == "my-claude -p"
    assert config.claude.launch == "my-claude"
    assert config.codex.analyze == "my-codex run"
    assert config.codex.launch == "my-codex"
    assert config.pi.launch == "my-pi"
    assert config.workdir == "~/code"
    assert config.todo_repository == "octocat/notes"
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
            "",  # pi launch default
            "octocat",
            "",  # workdir default
            "",  # todo repository default
        ]
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "gh") else None,
    )
    assert config.claude.analyze == "claude -p"
    assert config.claude.launch == "claude"
    assert config.codex.analyze == "codex exec"
    assert config.codex.launch == "codex"
    assert config.pi.launch == "pi"
    assert config.github_username == "octocat"
    assert config.workdir == "~/wrk"
    assert config.todo_repository == "octocat/todos"
    assert config.repositories == ("octocat/*",)


def test_configure_reprompts_for_required_fields_without_defaults(tmp_path):
    config_path = tmp_path / "config.json"
    prompts: list[str] = []
    # Username retry, then the workdir and todo repository defaults.
    inputs = iter(["", "octocat", "", ""])

    config = configure(
        config_path,
        input_fn=lambda prompt: (prompts.append(prompt), next(inputs))[1],
        which_fn=lambda cmd: (
            f"/usr/bin/{cmd}" if cmd in ("zellij", "gh", "claude", "codex", "pi") else None
        ),
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
                    "pi": {"launch": "existing-pi-launch"},
                },
            }
        ),
        encoding="utf-8",
    )
    inputs = iter(["~/src", ""])  # only the workdir and todo repository are empty

    config = configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: f"/opt/homebrew/bin/{cmd}" if cmd in ("zellij", "gh") else None,
    )
    assert config.github_username == "existing-user"
    assert config.claude.analyze == "existing-claude-analyze"
    assert config.claude.launch == "existing-claude-launch"
    assert config.codex.analyze == "existing-codex-analyze"
    assert config.codex.launch == "existing-codex-launch"
    assert config.pi.launch == "existing-pi-launch"
    assert config.workdir == "~/src"
    assert config.repositories == ("existing-user/*",)


def test_configure_reprompts_for_a_malformed_todo_repository(tmp_path):
    """A hand-broken todo repository would lock the user out, so the wizard asks again."""

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"launch": "pi"},
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
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "gh") else None,
    )

    assert prompts == ["Todo repository [octocat/todos]: "]
    assert config.todo_repository == "octocat/todos"
    assert config.require_valid() is config


def test_configure_preserves_existing_repositories(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "github_username": "octocat",
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"launch": "pi"},
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
                "todo_repository": "octocat/todos",
            }
        ),
        encoding="utf-8",
    )

    config = configure(
        config_path,
        input_fn=lambda prompt: (_ for _ in ()).throw(AssertionError("should not prompt")),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "gh") else None,
    )

    assert config.repositories == ("specific/repo",)
    assert config.workdir == "~/src"


def test_configure_installs_zellij_when_not_on_path(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "",  # claude analyze default
            "",  # claude launch default
            "",  # codex analyze default
            "",  # codex launch default
            "",  # pi launch default
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
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("gh", "claude", "codex") else None,
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
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"launch": "pi"},
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
        which_fn=lambda cmd: None,
        install_zellij_fn=fake_install,
        install_gh_fn=lambda: "/new/local/gh",
    )
    assert install_calls == ["install"]
    assert config.repositories == ("specific/repo",)


def test_configure_installs_gh_when_not_on_path(tmp_path, capsys):
    config_path = tmp_path / "config.json"
    inputs = iter(
        [
            "",  # claude analyze default
            "",  # claude launch default
            "",  # codex analyze default
            "",  # codex launch default
            "",  # pi launch default
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
        which_fn=lambda cmd: "/usr/bin/zellij" if cmd == "zellij" else None,
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
                "agents": {
                    "claude": {"analyze": "claude -p", "launch": "claude"},
                    "codex": {"analyze": "codex exec", "launch": "codex"},
                    "pi": {"launch": "pi"},
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
        which_fn=lambda cmd: None,
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
