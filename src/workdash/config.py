"""Workdash configuration management."""

from __future__ import annotations

import errno
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from io import BytesIO
from ipaddress import ip_network
from pathlib import Path
from typing import TYPE_CHECKING

from .git import is_repository_name
from .repo_resolver import validate_repository_selectors

if TYPE_CHECKING:
    from .github_client import GitHubClient

CONFIG_PATH = Path.home() / ".config" / "workdash" / "config.json"
LOCAL_BIN_PATH = CONFIG_PATH.parent / "bin"
ZELLIJ_INSTALL_PATH = LOCAL_BIN_PATH / "zellij"
GH_INSTALL_PATH = LOCAL_BIN_PATH / "gh"
GH_LATEST_RELEASE_API_URL = "https://api.github.com/repos/cli/cli/releases/latest"
GH_LATEST_RELEASE_PAGE_URL = "https://github.com/cli/cli/releases/latest"

_REQUIRED_FIELDS = ("github_username", "repositories", "workdir")
_ANALYSIS_VIEWER_COMMAND = "workdash view"
# (executable name, subcommand) pairs that no longer exist, so a configuration naming one
# is repaired. Compared as tokens, so a full path or extra arguments are repaired too.
_REMOVED_VIEWER_COMMANDS = frozenset({("workdash", "mdview"), ("workdash", "markdownview")})
# Agents the wizard detects on PATH, in detection order, with their (analyze, launch) commands.
_DETECTABLE_AGENTS = {
    "claude": ("claude -p", "claude"),
    "codex": ("codex exec", "codex"),
    "pi": ("pi -p", "pi"),
}
# Names that mean something else inside Workdash (the analyze dialog's cached analysis and
# the code dialog's editor launch).
_RESERVED_AGENT_NAMES = frozenset({"cached", "vscode"})
# The TUI dialogs number agent entries with single keys, so a section holds at most
# nine agents. The code menu's edit, branchdiff, terminal and review entries take letters.
_MAX_AGENTS = 9
# What each section of the wizard's agent step asks for: the command's purpose and an example.
_AGENT_SECTION_HELP = {
    "analyze": ("commands that run once and print the analysis", "claude -p"),
    "launch": ("commands that start an interactive coding session", "claude"),
}
_DEFAULT_WORKDIR = "~/wrk"


def _default_open_command() -> str:
    return "open" if platform.system() == "Darwin" else "xdg-open"


class WorkdashConfigValidationError(RuntimeError):
    """Raised when runtime configuration is incomplete or invalid."""

    def __init__(
        self,
        missing_fields: list[str],
        invalid_fields: list[str] | None = None,
    ) -> None:
        self.missing_fields = tuple(missing_fields)
        self.invalid_fields = tuple(invalid_fields or [])
        messages = []
        if self.missing_fields:
            messages.append(f"missing configuration fields: {', '.join(self.missing_fields)}")
        if self.invalid_fields:
            messages.append(f"invalid configuration fields: {', '.join(self.invalid_fields)}")
        super().__init__("; ".join(messages))


@dataclass(frozen=True)
class AgentConfig:
    """Per-agent command configuration."""

    analyze: str = ""
    launch: str = ""


@dataclass(frozen=True)
class WorkdashAgentChoice:
    """A TUI action the user can choose."""

    key: str
    agent: str
    label: str
    tool_label: str


@dataclass(frozen=True)
class WorkdashConfig:
    """Runtime configuration loaded from ~/.config/workdash/config.json."""

    github_username: str = ""
    open_command: str = ""
    open_needs_pane: bool = False
    analysis_viewer: dict = field(default_factory=dict)
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    repositories: tuple[str, ...] = ()
    workdir: str = ""
    todo_repository: str = ""
    listens: tuple[str, ...] = ()

    def listener_addresses(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """Parse configured listeners and their accepted source networks."""
        if not isinstance(self.listens, (list, tuple)):
            raise ValueError("listens must be a list")
        listeners = []
        for listener in self.listens:
            if not isinstance(listener, str):
                raise ValueError("listener entries must be strings")
            host, separator, sources = listener.partition("@")
            if not host or not separator or ":" in host or host in ("*", "0.0.0.0"):
                raise ValueError(
                    f"invalid listener {listener!r}: expected HOST@IP1,IP2 without wildcard binds or custom ports"
                )
            try:
                ips = tuple(
                    str(net.network_address)
                    if (net := ip_network(source, strict=False)).num_addresses == 1
                    else str(net)
                    for source in sources.split(",")
                )
            except ValueError as error:
                raise ValueError(f"invalid listener {listener!r}: {error}") from error
            listeners.append((host, ips))
        return tuple(listeners)

    def require_valid(self) -> WorkdashConfig:
        missing = validate_config(self)
        invalid = _invalid_config_command_fields(self)
        try:
            self.listener_addresses()
        except ValueError as error:
            invalid.append(f"listens: {error}")
        if not isinstance(self.open_needs_pane, bool):
            invalid.append("open_needs_pane: expected a boolean")
        viewer_problem = _analysis_viewer_problem(self.analysis_viewer)
        if self.analysis_viewer and viewer_problem:
            invalid.append(f"analysis_viewer: {viewer_problem}")
        # A hand-edited todo repository that is present but not owner/repo is
        # invalid rather than missing, so the message tells the user what to fix.
        if self.todo_repository and not is_repository_name(self.todo_repository):
            invalid.append("todo_repository: expected owner/repo")
        invalid.extend(
            f"agents.{name}: reserved name" for name in self.agents if name in _RESERVED_AGENT_NAMES
        )
        if missing or invalid:
            raise WorkdashConfigValidationError(missing, invalid)
        return self

    def configured_analyze_agents(self) -> list[str]:
        return [
            name for name, agent in self.agents.items() if _is_configured_command(agent.analyze)
        ]

    def configured_code_agents(self) -> list[str]:
        return [name for name, agent in self.agents.items() if _is_configured_command(agent.launch)]

    def tui_analyze_choices(self) -> list[WorkdashAgentChoice]:
        return _numbered_agent_choices(
            [(name, f"Analyze with {name}", name) for name in self.configured_analyze_agents()]
        )

    def tui_code_choices(self) -> list[WorkdashAgentChoice]:
        return _numbered_agent_choices(
            [(name, name, name) for name in self.configured_code_agents()]
        )

    def analyze_agent_command_tokens(self, agent: str) -> list[str]:
        try:
            command = self.agents[agent].analyze
        except KeyError as error:
            raise ValueError(f"Unsupported analyze agent: {agent!r}") from error
        return _config_command_tokens(f"agents.{agent}.analyze", command)

    def code_agent_launch_command_tokens(self, agent: str) -> list[str]:
        try:
            command = self.agents[agent].launch
        except KeyError as error:
            raise ValueError(f"Unsupported coding agent: {agent!r}") from error
        return _config_command_tokens(f"agents.{agent}.launch", command)


def _config_to_json(config: WorkdashConfig) -> str:
    return (
        json.dumps(
            {
                "github_username": config.github_username,
                "open": config.open_command,
                "open_needs_pane": config.open_needs_pane,
                "analysis_viewer": config.analysis_viewer,
                "agents": {
                    name: {"analyze": agent.analyze, "launch": agent.launch}
                    for name, agent in config.agents.items()
                },
                "repositories": list(config.repositories),
                "workdir": config.workdir,
                "todo_repository": config.todo_repository,
                "listens": list(config.listens),
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

    return _config_from_json(_read_config_json(path))


def save_config(
    config: WorkdashConfig, path: Path = CONFIG_PATH, *, create_only: bool = False
) -> None:
    """Write *config* to *path*, creating parent directories as needed."""

    path.parent.mkdir(parents=True, exist_ok=True)
    contents = _config_to_json(config)
    if not create_only:
        path.write_text(contents, encoding="utf-8")
        return

    staged_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as staged:
            staged_path = Path(staged.name)
            staged.write(contents)
        # Publish only complete contents without replacing a competing configuration.
        try:
            os.link(staged_path, path)
        except FileExistsError as error:
            raise RuntimeError(
                f"Configuration already exists at {path}. Your answers were not saved."
            ) from error
        except OSError as error:
            if error.errno in (errno.EOPNOTSUPP, errno.ENOTSUP):
                reason = "the filesystem does not support hard links"
            elif error.errno == errno.EPERM:
                reason = "creating a hard link was denied"
            else:
                raise
            raise RuntimeError(
                f"Automatic setup could not save {path} because {reason}. "
                "Run 'workdash --configure' to save the configuration."
            ) from error
    finally:
        if staged_path is not None:
            try:
                staged_path.unlink(missing_ok=True)
            except OSError as error:
                print(
                    f"Warning: Could not remove temporary configuration {staged_path}: {error}",
                    file=sys.stderr,
                )


def _read_config_json(path: Path) -> dict:
    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
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
    return raw


def _config_from_json(raw: dict) -> WorkdashConfig:
    repositories_raw = raw.get("repositories")
    repositories = tuple(repositories_raw) if isinstance(repositories_raw, list) else ()
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, dict):
        agents_raw = {}
    return WorkdashConfig(
        github_username=raw.get("github_username", ""),
        open_command=raw.get("open", ""),
        open_needs_pane=raw.get("open_needs_pane", False),
        analysis_viewer=raw.get("analysis_viewer", {}),
        agents={
            name: AgentConfig(analyze=entry.get("analyze", ""), launch=entry.get("launch", ""))
            for name, entry in agents_raw.items()
            if isinstance(entry, dict)
        },
        repositories=repositories,
        workdir=raw.get("workdir", "") or raw.get("source_directory", ""),
        todo_repository=raw.get("todo_repository", ""),
        listens=tuple(raw["listens"])
        if isinstance(raw.get("listens"), list)
        else raw.get("listens", ()),
    )


def validate_config(config: WorkdashConfig) -> list[str]:
    """Return names of required fields that are empty or missing."""

    missing: list[str] = []
    if not config.github_username:
        missing.append("github_username")
    if not config.open_command or (
        isinstance(config.open_command, str) and not config.open_command.strip()
    ):
        missing.append("open")
    if not config.analysis_viewer:
        missing.append("analysis_viewer")
    if not config.repositories:
        missing.append("repositories")
    if not config.workdir:
        missing.append("workdir")
    if not config.todo_repository:
        missing.append("todo_repository")
    if not _has_agent(config.agents):
        missing.append("agents")
    return missing


def _has_agent(agents: dict[str, AgentConfig]) -> bool:
    """An agent needs at least one non-blank command; an entry with none is not an agent."""

    return any(
        isinstance(command, str) and command.strip()
        for agent in agents.values()
        for command in (agent.analyze, agent.launch)
    )


def _numbered_agent_choices(choices: list[tuple[str, str, str]]) -> list[WorkdashAgentChoice]:
    return [
        WorkdashAgentChoice(str(index), agent, label, tool_label)
        for index, (agent, label, tool_label) in enumerate(choices, start=1)
    ]


def _config_command_fields(config: WorkdashConfig) -> tuple[tuple[str, object], ...]:
    fields: list[tuple[str, object]] = [("open", config.open_command)]
    for name, agent in config.agents.items():
        fields.append((f"agents.{name}.analyze", agent.analyze))
        fields.append((f"agents.{name}.launch", agent.launch))
    return tuple(fields)


def _invalid_config_command_fields(config: WorkdashConfig) -> list[str]:
    invalid: list[str] = []
    for field_name, command in _config_command_fields(config):
        if isinstance(command, str) and not command.strip():
            continue
        problem = _config_command_problem(command)
        if problem is not None:
            invalid.append(f"{field_name}: {problem}")
    return invalid


def _is_configured_command(command: object) -> bool:
    return isinstance(command, str) and command.strip() and _config_command_problem(command) is None


def _analysis_viewer_problem(viewer: object) -> str | None:
    if (
        not isinstance(viewer, dict)
        or not _is_configured_command(viewer.get("command"))
        or viewer.get("format") not in ("markdown", "html")
        or not isinstance(viewer.get("needs_pane"), bool)
    ):
        return "expected command, format (markdown or html), and needs_pane boolean"
    return _removed_viewer_problem(viewer["command"])


def _removed_viewer_problem(command: str) -> str | None:
    tokens = shlex.split(command)
    if len(tokens) > 1 and (Path(tokens[0]).name, tokens[1]) in _REMOVED_VIEWER_COMMANDS:
        return f"{command!r} was removed; use {_ANALYSIS_VIEWER_COMMAND}"
    return None


def _config_command_tokens(field: str, command: object) -> list[str]:
    problem = _config_command_problem(command)
    if problem is not None:
        raise WorkdashConfigValidationError([], [f"{field}: {problem}"])
    return shlex.split(command)


def _config_command_problem(command: object) -> str | None:
    if not isinstance(command, str) or not command.strip():
        return "expected a non-empty string"
    try:
        tokens = shlex.split(command)
    except ValueError as error:
        return str(error)
    if any(not token.strip() for token in tokens):
        return "contains a blank shell token"
    return None


def _prompt_with_default(input_fn: Callable[[str], str], label: str, default: str) -> str:
    response = input_fn(f"{label} [{default}]: ").strip()
    return response or default


def _zellij_release_target() -> str:
    machine = platform.machine()
    if machine == "arm64":
        arch = "aarch64"
    elif machine in {"x86_64", "aarch64"}:
        arch = machine
    else:
        raise RuntimeError(f"Unsupported CPU architecture for Zellij: {machine}")

    system_name = platform.system()
    if system_name == "Linux":
        system = "unknown-linux-musl"
    elif system_name == "Darwin":
        system = "apple-darwin"
    else:
        raise RuntimeError(f"Unsupported operating system for Zellij: {system_name}")
    return f"{arch}-{system}"


def _zellij_release_url() -> str:
    target = _zellij_release_target()
    return f"https://github.com/zellij-org/zellij/releases/latest/download/zellij-{target}.tar.gz"


def _write_executable(destination: Path, binary: bytes, *, atomic: bool) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    execute_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    if not atomic:
        destination.write_bytes(binary)
        destination.chmod(destination.stat().st_mode | execute_bits)
        return str(destination)

    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        staged = Path(directory) / destination.name
        staged.write_bytes(binary)
        try:
            mode = destination.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is None or stat.S_ISLNK(mode):
            mode = staged.stat().st_mode
        staged.chmod(stat.S_IMODE(mode) | execute_bits)
        staged.replace(destination)
    return str(destination)


def install_zellij_binary(
    destination: Path = ZELLIJ_INSTALL_PATH,
    *,
    github_client: GitHubClient | None = None,
    atomic: bool = False,
) -> str:
    """Download and install the latest Zellij release binary.

    :param Path destination: Path where the executable should be installed.
    """

    from .github_client import GitHubClient

    url = _zellij_release_url()
    print(f"Downloading Zellij from {url}")
    if github_client is None:
        github_client = GitHubClient()
    archive_bytes = github_client.download_release(url)

    try:
        with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
            member = next(
                (
                    entry
                    for entry in archive.getmembers()
                    if Path(entry.name).name == "zellij" and entry.isfile()
                ),
                None,
            )
            if member is None:
                raise RuntimeError("Downloaded Zellij archive did not contain a zellij binary.")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise RuntimeError("Failed to extract the Zellij binary from the archive.")
            binary = extracted.read()
    except tarfile.TarError as error:
        raise RuntimeError(f"Failed to extract Zellij archive: {error}") from error

    return _write_executable(destination, binary, atomic=atomic)


def _gh_release_asset_patterns() -> tuple[str, str]:
    machine = platform.machine()
    if machine in {"x86_64", "amd64"}:
        arch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise RuntimeError(f"Unsupported CPU architecture for GitHub CLI: {machine}")

    system_name = platform.system()
    if system_name == "Linux":
        os_name = "linux"
        extension = ".tar.gz"
    elif system_name == "Darwin":
        os_name = "macOS"
        extension = ".zip"
    else:
        raise RuntimeError(f"Unsupported operating system for GitHub CLI: {system_name}")
    return f"_{os_name}_{arch}{extension}", extension


def install_gh_binary(
    destination: Path = GH_INSTALL_PATH,
    *,
    github_client: GitHubClient | None = None,
    atomic: bool = False,
) -> str:
    """Download and install the latest GitHub CLI release binary.

    :param Path destination: Path where the executable should be installed.
    """

    from .github_client import GitHubClient

    if github_client is None:
        github_client = GitHubClient()
    archive_suffix, extension = _gh_release_asset_patterns()
    download_url = github_client.release_asset_url(GH_LATEST_RELEASE_API_URL, archive_suffix)
    print(f"Downloading GitHub CLI from {download_url}")
    archive_bytes = github_client.download_release(download_url)

    try:
        if extension == ".zip":
            with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
                member_name = next(
                    (
                        name
                        for name in archive.namelist()
                        if Path(name).name == "gh" and not name.endswith("/")
                    ),
                    None,
                )
                if member_name is None:
                    raise RuntimeError("Downloaded GitHub CLI archive did not contain gh.")
                binary = archive.read(member_name)
        else:
            with tarfile.open(fileobj=BytesIO(archive_bytes), mode="r:gz") as archive:
                member = next(
                    (
                        entry
                        for entry in archive.getmembers()
                        if Path(entry.name).name == "gh" and entry.isfile()
                    ),
                    None,
                )
                if member is None:
                    raise RuntimeError("Downloaded GitHub CLI archive did not contain gh.")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise RuntimeError("Failed to extract the GitHub CLI binary.")
                binary = extracted.read()
    except (tarfile.TarError, zipfile.BadZipFile) as error:
        raise RuntimeError(f"Failed to extract GitHub CLI archive: {error}") from error

    return _write_executable(destination, binary, atomic=atomic)


def configure(
    path: Path = CONFIG_PATH,
    *,
    input_fn: Callable[[str], str] = input,
    which_fn: Callable[[str], str | None] = shutil.which,
    install_zellij_fn: Callable[[], str] | None = None,
    install_gh_fn: Callable[[], str] | None = None,
    github_client: GitHubClient | None = None,
    create_only: bool = False,
) -> WorkdashConfig:
    """Interactive configuration that fills in missing fields.

    :param Path path: Config file path.
    :param bool create_only: Start with defaults and refuse to overwrite a file.
    :param input_fn: Callable used to prompt the user (default: builtin input).
    :param which_fn: Callable used to detect commands on PATH (default: shutil.which).
    """

    raw = {} if create_only else _read_config_json(path)
    config = _config_from_json(raw)
    if not isinstance(config.listens, (list, tuple)):
        raise WorkdashConfigValidationError([], [f"listens: expected a list; edit {path}"])
    install_zellij = install_zellij_fn or (
        lambda: install_zellij_binary(
            path.parent / "bin" / "zellij", github_client=github_client, atomic=create_only
        )
    )
    install_gh = install_gh_fn or (
        lambda: install_gh_binary(
            path.parent / "bin" / "gh", github_client=github_client, atomic=create_only
        )
    )

    detected_zellij = which_fn("zellij")
    if detected_zellij:
        print(f"Detected 'zellij' on PATH: {detected_zellij}")
    else:
        print(
            f"Zellij is not on PATH. Installing a local Zellij binary from {_zellij_release_url()}."
        )
        print("To use a global Zellij instead, install it separately and make sure it is on PATH.")
        installed_zellij = install_zellij()
        print(f"Installed Zellij to: {installed_zellij}")

    detected_gh = which_fn("gh")
    if detected_gh:
        print(f"Detected 'gh' on PATH: {detected_gh}")
    else:
        print(
            f"GitHub CLI is not on PATH. Installing a local GitHub CLI binary from "
            f"{GH_LATEST_RELEASE_PAGE_URL}."
        )
        print(
            "To use a global GitHub CLI instead, install it separately and make sure it is on PATH."
        )
        installed_gh = install_gh()
        print(f"Installed GitHub CLI to: {installed_gh}")

    open_command = config.open_command
    suggested_open_command = _default_open_command()
    if not which_fn(suggested_open_command):
        suggested_open_command = ""
    while not (_is_configured_command(open_command) and which_fn(shlex.split(open_command)[0])):
        if suggested_open_command:
            open_command = (
                input_fn(f"Open command [{suggested_open_command}] (Enter to accept): ").strip()
                or suggested_open_command
            )
        else:
            open_command = input_fn("Open command (required): ").strip()
        if not (_is_configured_command(open_command) and which_fn(shlex.split(open_command)[0])):
            print("Enter a valid command whose executable exists on PATH or at an explicit path.")

    # Legacy runtime configs default to direct opening, but setup must ask for the choice.
    open_needs_pane = raw.get("open_needs_pane")
    while not isinstance(open_needs_pane, bool):
        response = (
            input_fn("Does the viewer require its own terminal window? [y/N] ").strip().lower()
        )
        if response in {"", "n", "no", "y", "yes"}:
            open_needs_pane = response in {"y", "yes"}
        else:
            print("Please answer yes or no.")
    print(f"For advanced viewer commands, edit 'open' and 'open_needs_pane' in {path}.")

    analysis_viewer = config.analysis_viewer
    if _analysis_viewer_problem(analysis_viewer):
        command = _prompt_with_default(input_fn, "Analysis viewer", _ANALYSIS_VIEWER_COMMAND)
        while True:
            if _is_configured_command(command):
                removed = _removed_viewer_problem(command)
                if removed is None:
                    break
                print(removed)
            command = input_fn("Analysis viewer command (required): ").strip()
        if command == _ANALYSIS_VIEWER_COMMAND:
            analysis_viewer = {"command": command, "format": "markdown", "needs_pane": True}
        else:
            fmt = input_fn("Analysis format (markdown/html): ").strip().lower()
            while fmt not in ("markdown", "html"):
                fmt = input_fn("Analysis format (markdown/html): ").strip().lower()
            pane = (
                input_fn("Does the analysis viewer require its own terminal window? [y/N] ")
                .strip()
                .lower()
            )
            while pane not in ("", "y", "yes", "n", "no"):
                pane = input_fn("Please answer yes or no: ").strip().lower()
            analysis_viewer = {
                "command": command,
                "format": fmt,
                "needs_pane": pane in ("y", "yes"),
            }

    agents = config.agents
    if not _has_agent(agents):
        agents = {}
        for name, (analyze, launch) in _DETECTABLE_AGENTS.items():
            if which_fn(name):
                print(f"Detected '{name}' on PATH, using analyze: {analyze}, launch: {launch}")
                agents[name] = AgentConfig(analyze=analyze, launch=launch)
        if not agents:
            print(f"No supported agent found on PATH (looked for {', '.join(_DETECTABLE_AGENTS)}).")
        print("Each agent command is run with the task prompt appended as its last argument.")
        while True:
            _prompt_agents(input_fn, agents, "Agents for analyze", "analyze", _MAX_AGENTS)
            _prompt_agents(input_fn, agents, "Agents for code", "launch", _MAX_AGENTS)
            if agents:
                break
            print("At least one agent is required.")
    print(f"To add or edit agents later, edit 'agents' in {path}.")

    repositories = config.repositories
    github_username = config.github_username
    while not github_username:
        response = input_fn("GitHub username: ").strip()
        if not response:
            print("GitHub username is required.")
            continue
        if len(response) > 39 or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9-]*|[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*_[A-Za-z0-9]{3,8}",
            response,
        ):
            print(
                "Enter a GitHub username of 1–39 characters: start with an ASCII letter or digit "
                "and use only ASCII letters, digits, or hyphens. Managed usernames use single "
                "hyphens between letters and digits before the underscore and the 3–8 character "
                "alphanumeric enterprise shortcode."
            )
            continue
        github_username = response

    workdir = config.workdir
    if not workdir:
        workdir = _prompt_with_default(input_fn, "Work directory", _DEFAULT_WORKDIR)

    if not repositories:
        with suppress(ValueError):
            repositories = tuple(validate_repository_selectors([f"{github_username}/*".lower()]))
        while not repositories:
            response = input_fn("Repository selector (owner/repo or owner/*): ")
            with suppress(ValueError):
                repositories = tuple(validate_repository_selectors([response]))
            if not repositories:
                print("Enter a repository selector in owner/repo or owner/* form.")
        print(f"Repositories set to: {repositories[0]}")

    todo_repository = config.todo_repository
    todo_default = f"{github_username}/todos"
    # A managed login can identify the user without being a usable default repository owner.
    try:
        validate_repository_selectors([todo_default.lower()])
    except ValueError:
        todo_default = ""
    while not is_repository_name(todo_repository):
        if is_repository_name(todo_default):
            todo_repository = _prompt_with_default(input_fn, "Todo repository", todo_default)
        else:
            todo_repository = input_fn("Todo repository (owner/repo required): ").strip()
        if not is_repository_name(todo_repository):
            print("Enter a todo repository in owner/repo form.")

    new_config = WorkdashConfig(
        github_username=github_username,
        open_command=open_command,
        open_needs_pane=open_needs_pane,
        analysis_viewer=analysis_viewer,
        agents=agents,
        repositories=repositories,
        workdir=workdir,
        todo_repository=todo_repository,
        listens=config.listens,
    )
    save_config(new_config, path, create_only=create_only)
    print(
        f'Configuration saved to {path}. Add trusted LAN listeners to "listens", e.g. "10.0.0.50@10.0.0.213".'
    )
    return new_config


def _prompt_agents(
    input_fn: Callable[[str], str],
    agents: dict[str, AgentConfig],
    title: str,
    command_field: str,
    limit: int,
) -> None:
    """Ask for more agents of one kind until an empty answer or until the section is full.

    :param dict agents: Agents collected so far; extended in place.
    :param str title: Section title shown to the user.
    :param str command_field: Command this section collects: ``analyze`` or ``launch``.
    :param int limit: Maximum number of agents with that command.
    """

    purpose, example = _AGENT_SECTION_HELP[command_field]
    print(f"{title} (at most {limit}), {purpose}, e.g. {example}:")
    for name, agent in agents.items():
        if getattr(agent, command_field):
            print(f"  {name}: {getattr(agent, command_field)}")
    while sum(1 for agent in agents.values() if getattr(agent, command_field)) < limit:
        command = input_fn(f"{title}, add a command (empty to finish): ").strip()
        if not command:
            return
        problem = _config_command_problem(command)
        if problem is not None:
            print(f"Invalid command: {problem}")
            continue
        # Leading NAME=value tokens set the environment; the program is the next token.
        program = next(
            (token for token in shlex.split(command) if not re.match(r"[A-Za-z_]\w*=", token)),
            command,
        )
        if not _shell_finds_program(program):
            print(f"`{program}` was not found by your shell.")
            continue
        while True:
            name = input_fn(f"Agent name for `{command}`: ").strip()
            existing = agents.get(name, AgentConfig())
            if not name:
                print("The agent name is required.")
            elif name in _RESERVED_AGENT_NAMES:
                print(f"'{name}' is reserved, choose another name.")
            elif getattr(existing, command_field):
                print(f"'{name}' is already used in this section, choose another name.")
            else:
                break
        agents[name] = replace(existing, **{command_field: command})
    print(f'The "{title}" section is full ({limit} agents).')


def _shell_finds_program(program: str) -> bool:
    """Whether the user's shell resolves *program*.

    Agents run through ``$SHELL -ic``, so aliases and shell functions work as agents
    even though they are not executables on PATH. A shell that cannot start or does
    not answer within 10 seconds resolves nothing.
    """

    try:
        result = subprocess.run(
            [os.environ.get("SHELL", "/bin/sh"), "-ic", f"command -v {shlex.quote(program)}"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0
