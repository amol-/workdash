import json
import tarfile
from io import BytesIO

import pytest

from workdash.config import (
    AgentConfig,
    WorkdashConfig,
    configure,
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
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "claude", "codex") else None,
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
        which_fn=lambda cmd: "/usr/local/bin/zellij" if cmd == "zellij" else None,
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
        which_fn=lambda cmd: "/usr/bin/zellij" if cmd == "zellij" else None,
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
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("zellij", "claude", "codex") else None,
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
        which_fn=lambda cmd: "/opt/homebrew/bin/zellij" if cmd == "zellij" else None,
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
        which_fn=lambda cmd: "/usr/bin/zellij" if cmd == "zellij" else None,
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
            "octocat",
            "",  # workdir default
        ]
    )
    install_calls: list[str] = []

    def fake_install() -> str:
        install_calls.append("install")
        return str(tmp_path / "bin" / "zellij")

    configure(
        config_path,
        input_fn=lambda prompt: next(inputs),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("claude", "codex") else None,
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
                },
                "repositories": ["specific/repo"],
                "workdir": "~/src",
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
