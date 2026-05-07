"""Workdash configuration management."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "workdash" / "config.json"

_REQUIRED_FIELDS = ("github_username", "repositories", "workdir")
_DEFAULT_CLAUDE_ANALYZE = "claude -p"
_DEFAULT_CLAUDE_LAUNCH = "claude"
_DEFAULT_CODEX_ANALYZE = "codex exec"
_DEFAULT_CODEX_LAUNCH = "codex"
_DEFAULT_WORKDIR = "~/wrk"


@dataclass(frozen=True)
class AgentConfig:
    """Per-agent command configuration."""

    analyze: str = ""
    launch: str = ""


@dataclass(frozen=True)
class WorkdashConfig:
    """Runtime configuration loaded from ~/.config/workdash/config.json."""

    github_username: str = ""
    claude: AgentConfig = field(default_factory=AgentConfig)
    codex: AgentConfig = field(default_factory=AgentConfig)
    repositories: tuple[str, ...] = ()
    workdir: str = ""


def _config_to_json(config: WorkdashConfig) -> str:
    return (
        json.dumps(
            {
                "github_username": config.github_username,
                "agents": {
                    "claude": {"analyze": config.claude.analyze, "launch": config.claude.launch},
                    "codex": {"analyze": config.codex.analyze, "launch": config.codex.launch},
                },
                "repositories": list(config.repositories),
                "workdir": config.workdir,
            },
            indent=2,
            ensure_ascii=True,
        )
        + "\n"
    )


def load_config(path: Path = CONFIG_PATH) -> WorkdashConfig:
    """Load config from *path*; missing file returns an empty configuration.

    Raises :class:`RuntimeError` when the file exists but cannot be read or
    parsed, so a corrupted config surfaces an actionable error instead of
    silently reverting to defaults.
    """

    if not path.exists():
        return WorkdashConfig()
    try:
        contents = path.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Failed to read configuration at {path}: {error}") from error
    try:
        raw = json.loads(contents)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Configuration at {path} is not valid JSON: {error.msg} "
            f"(line {error.lineno}, column {error.colno})"
        ) from error
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"Configuration at {path} must be a JSON object, got {type(raw).__name__}."
        )
    repositories_raw = raw.get("repositories")
    repositories = tuple(repositories_raw) if isinstance(repositories_raw, list) else ()
    agents_raw = raw.get("agents")
    claude_config = AgentConfig()
    codex_config = AgentConfig()
    if isinstance(agents_raw, dict):
        claude_raw = agents_raw.get("claude")
        if isinstance(claude_raw, dict):
            claude_config = AgentConfig(
                analyze=claude_raw.get("analyze", ""),
                launch=claude_raw.get("launch", ""),
            )
        codex_raw = agents_raw.get("codex")
        if isinstance(codex_raw, dict):
            codex_config = AgentConfig(
                analyze=codex_raw.get("analyze", ""),
                launch=codex_raw.get("launch", ""),
            )
    return WorkdashConfig(
        github_username=raw.get("github_username", ""),
        claude=claude_config,
        codex=codex_config,
        repositories=repositories,
        workdir=raw.get("workdir", "") or raw.get("source_directory", ""),
    )


def save_config(config: WorkdashConfig, path: Path = CONFIG_PATH) -> None:
    """Write *config* to *path*, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_config_to_json(config), encoding="utf-8")


def validate_config(config: WorkdashConfig) -> list[str]:
    """Return names of required fields that are empty or missing."""

    missing: list[str] = []
    if not config.github_username:
        missing.append("github_username")
    if not config.repositories:
        missing.append("repositories")
    if not config.workdir:
        missing.append("workdir")
    if not config.claude.analyze:
        missing.append("agents.claude.analyze")
    if not config.claude.launch:
        missing.append("agents.claude.launch")
    if not config.codex.analyze:
        missing.append("agents.codex.analyze")
    if not config.codex.launch:
        missing.append("agents.codex.launch")
    return missing


def _prompt_with_default(input_fn: Callable[[str], str], label: str, default: str) -> str:
    response = input_fn(f"{label} [{default}]: ").strip()
    return response or default


def _prompt_required(input_fn: Callable[[str], str], label: str) -> str:
    while True:
        response = input_fn(f"{label}: ").strip()
        if response:
            return response
        print(f"{label} is required.")


def configure(
    path: Path = CONFIG_PATH,
    *,
    input_fn: Callable[[str], str] = input,
    which_fn: Callable[[str], str | None] = shutil.which,
) -> WorkdashConfig:
    """Interactive configuration that fills in missing fields.

    :param Path path: Config file path.
    :param input_fn: Callable used to prompt the user (default: builtin input).
    :param which_fn: Callable used to detect commands on PATH (default: shutil.which).
    """

    config = load_config(path)

    claude = config.claude
    if not claude.analyze or not claude.launch:
        if which_fn("claude"):
            if not claude.analyze:
                print(f"Detected 'claude' on PATH, using analyze: {_DEFAULT_CLAUDE_ANALYZE}")
            if not claude.launch:
                print(f"Detected 'claude' on PATH, using launch: {_DEFAULT_CLAUDE_LAUNCH}")
            claude = AgentConfig(
                analyze=claude.analyze or _DEFAULT_CLAUDE_ANALYZE,
                launch=claude.launch or _DEFAULT_CLAUDE_LAUNCH,
            )
        else:
            claude = AgentConfig(
                analyze=claude.analyze
                or _prompt_with_default(
                    input_fn, "Claude analyze command", _DEFAULT_CLAUDE_ANALYZE
                ),
                launch=claude.launch
                or _prompt_with_default(input_fn, "Claude launch command", _DEFAULT_CLAUDE_LAUNCH),
            )

    codex = config.codex
    if not codex.analyze or not codex.launch:
        if which_fn("codex"):
            if not codex.analyze:
                print(f"Detected 'codex' on PATH, using analyze: {_DEFAULT_CODEX_ANALYZE}")
            if not codex.launch:
                print(f"Detected 'codex' on PATH, using launch: {_DEFAULT_CODEX_LAUNCH}")
            codex = AgentConfig(
                analyze=codex.analyze or _DEFAULT_CODEX_ANALYZE,
                launch=codex.launch or _DEFAULT_CODEX_LAUNCH,
            )
        else:
            codex = AgentConfig(
                analyze=codex.analyze
                or _prompt_with_default(input_fn, "Codex analyze command", _DEFAULT_CODEX_ANALYZE),
                launch=codex.launch
                or _prompt_with_default(input_fn, "Codex launch command", _DEFAULT_CODEX_LAUNCH),
            )

    github_username = config.github_username
    if not github_username:
        github_username = _prompt_required(input_fn, "GitHub username")

    workdir = config.workdir
    if not workdir:
        workdir = _prompt_with_default(input_fn, "Work directory", _DEFAULT_WORKDIR)

    repositories = config.repositories
    if not repositories and github_username:
        repositories = (f"{github_username}/*",)
        print(f"Repositories set to: {github_username}/*")

    new_config = WorkdashConfig(
        github_username=github_username,
        claude=claude,
        codex=codex,
        repositories=repositories,
        workdir=workdir,
    )
    save_config(new_config, path)
    print(f"Configuration saved to {path}")
    return new_config
