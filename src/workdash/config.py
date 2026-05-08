"""Workdash configuration management."""

from __future__ import annotations

import json
import platform
import shutil
import stat
import tarfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "workdash" / "config.json"
LOCAL_BIN_PATH = CONFIG_PATH.parent / "bin"
ZELLIJ_INSTALL_PATH = LOCAL_BIN_PATH / "zellij"
GH_INSTALL_PATH = LOCAL_BIN_PATH / "gh"
GH_LATEST_RELEASE_API_URL = "https://api.github.com/repos/cli/cli/releases/latest"
GH_LATEST_RELEASE_PAGE_URL = "https://github.com/cli/cli/releases/latest"

_REQUIRED_FIELDS = ("github_username", "repositories", "workdir")
_DEFAULT_CLAUDE_ANALYZE = "claude -p"
_DEFAULT_CLAUDE_LAUNCH = "claude"
_DEFAULT_CODEX_ANALYZE = "codex exec"
_DEFAULT_CODEX_LAUNCH = "codex"
_DEFAULT_PI_LAUNCH = "pi"
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
    pi: AgentConfig = field(default_factory=AgentConfig)
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
                    "pi": {"launch": config.pi.launch},
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
    pi_config = AgentConfig()
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
        pi_raw = agents_raw.get("pi")
        if isinstance(pi_raw, dict):
            pi_config = AgentConfig(launch=pi_raw.get("launch", ""))
    return WorkdashConfig(
        github_username=raw.get("github_username", ""),
        claude=claude_config,
        codex=codex_config,
        pi=pi_config,
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
    if not config.pi.launch:
        missing.append("agents.pi.launch")
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


def _download_bytes(url: str, *, urlopen_fn: Callable[[str], object]) -> bytes:
    try:
        with urlopen_fn(url) as response:
            return response.read()
    except OSError as error:
        raise RuntimeError(f"Failed to download {url}: {error}") from error


def _write_executable(destination: Path, binary: bytes) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(binary)
    current_mode = destination.stat().st_mode
    destination.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(destination)


def install_zellij_binary(
    destination: Path = ZELLIJ_INSTALL_PATH,
    *,
    urlopen_fn: Callable[[str], object] = urllib.request.urlopen,
) -> str:
    """Download and install the latest Zellij release binary.

    :param Path destination: Path where the executable should be installed.
    :param urlopen_fn: Callable used to download the release archive.
    """

    url = _zellij_release_url()
    print(f"Downloading Zellij from {url}")
    archive_bytes = _download_bytes(url, urlopen_fn=urlopen_fn)

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

    return _write_executable(destination, binary)


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


def _gh_release_download_url(
    *,
    urlopen_fn: Callable[[str], object] = urllib.request.urlopen,
) -> str:
    archive_suffix, _extension = _gh_release_asset_patterns()
    release_bytes = _download_bytes(GH_LATEST_RELEASE_API_URL, urlopen_fn=urlopen_fn)
    try:
        release = json.loads(release_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Failed to parse GitHub CLI release metadata: {error}") from error
    assets = release.get("assets") if isinstance(release, dict) else None
    if not isinstance(assets, list):
        raise RuntimeError("GitHub CLI release metadata did not contain an assets list.")
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        download_url = asset.get("browser_download_url")
        if (
            isinstance(name, str)
            and name.endswith(archive_suffix)
            and isinstance(download_url, str)
        ):
            return download_url
    raise RuntimeError(
        f"GitHub CLI latest release did not contain an asset ending with {archive_suffix!r}."
    )


def install_gh_binary(
    destination: Path = GH_INSTALL_PATH,
    *,
    urlopen_fn: Callable[[str], object] = urllib.request.urlopen,
) -> str:
    """Download and install the latest GitHub CLI release binary.

    :param Path destination: Path where the executable should be installed.
    :param urlopen_fn: Callable used to download release metadata and archive bytes.
    """

    download_url = _gh_release_download_url(urlopen_fn=urlopen_fn)
    print(f"Downloading GitHub CLI from {download_url}")
    archive_bytes = _download_bytes(download_url, urlopen_fn=urlopen_fn)
    _archive_suffix, extension = _gh_release_asset_patterns()

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

    return _write_executable(destination, binary)


def configure(
    path: Path = CONFIG_PATH,
    *,
    input_fn: Callable[[str], str] = input,
    which_fn: Callable[[str], str | None] = shutil.which,
    install_zellij_fn: Callable[[], str] | None = None,
    install_gh_fn: Callable[[], str] | None = None,
) -> WorkdashConfig:
    """Interactive configuration that fills in missing fields.

    :param Path path: Config file path.
    :param input_fn: Callable used to prompt the user (default: builtin input).
    :param which_fn: Callable used to detect commands on PATH (default: shutil.which).
    """

    config = load_config(path)
    install_zellij = install_zellij_fn or (
        lambda: install_zellij_binary(path.parent / "bin" / "zellij")
    )
    install_gh = install_gh_fn or (lambda: install_gh_binary(path.parent / "bin" / "gh"))

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

    pi = config.pi
    if not pi.launch:
        if which_fn("pi"):
            print(f"Detected 'pi' on PATH, using launch: {_DEFAULT_PI_LAUNCH}")
            pi = AgentConfig(launch=_DEFAULT_PI_LAUNCH)
        else:
            pi = AgentConfig(
                launch=_prompt_with_default(input_fn, "pi launch command", _DEFAULT_PI_LAUNCH),
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
        pi=pi,
        repositories=repositories,
        workdir=workdir,
    )
    save_config(new_config, path)
    print(f"Configuration saved to {path}")
    return new_config
