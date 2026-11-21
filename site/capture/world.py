"""Create an isolated installation and demo workspace for captures."""

import json
import os
import shutil
import socket
import subprocess
from pathlib import Path


def build(root: Path, work: Path):
    work.mkdir(parents=True, exist_ok=True)
    home = work / "home"
    home.mkdir()
    (home / ".config/zellij").mkdir(parents=True)
    config_dir = home / ".config/workdash"
    config_dir.mkdir()
    bin_dir = work / "bin"
    bin_dir.mkdir()
    for name in ("gh", "fixtures.json"):
        shutil.copy2(root / "site/capture" / name, bin_dir / name)
    shutil.copy2(root / "site/capture/agent", bin_dir / "agent")
    (bin_dir / "zellij").symlink_to(shutil.which("zellij"))
    config = {
        "github_username": "demo",
        "repositories": ["demo/project"],
        "workdir": str(home / "wrk"),
        "todo_repository": "demo/todos",
        "open": "xdg-open",
        "analysis_viewer": {"command": "workdash view", "format": "markdown", "needs_pane": True},
        "agents": {"demo": {"analyze": "agent", "launch": "agent"}},
        "listens": [],
    }
    (config_dir / "config.json").write_text(json.dumps(config))
    zellij_config = work / "config.kdl"
    zellij_config.write_text("show_startup_tips false\nshow_release_notes false\n")
    snapshot = work / "snapshot"
    snapshot.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy2(root / name, snapshot / name)
    shutil.copytree(
        root / "src", snapshot / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info")
    )
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "PYTHONPATH", "VIRTUAL_ENV"):
        env.pop(key, None)
    env.update(
        HOME=str(home),
        XDG_CACHE_HOME=str(work / "cache"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local/share"),
        XDG_STATE_HOME=str(home / ".local/state"),
        GIT_TERMINAL_PROMPT="0",
        ZELLIJ_CONFIG_FILE=str(zellij_config),
        # Zellij socket paths are limited to 108 bytes, so the sockets live beside
        # the shot's scratch directory instead of inside its longer path.
        ZELLIJ_SOCKET_DIR=str(work.parent / "sockets"),
        GIT_CONFIG_GLOBAL=str(work / "gitconfig"),
        TERM="xterm-256color",
    )
    subprocess.run(["uv", "venv", str(work / "venv")], env=env, check=True)
    subprocess.run(
        ["uv", "pip", "install", "--python", str(work / "venv/bin/python"), str(snapshot)],
        env=env,
        check=True,
    )
    env.update(
        GIT_AUTHOR_NAME="Demo",
        GIT_AUTHOR_EMAIL="demo@example.com",
        GIT_COMMITTER_NAME="Demo",
        GIT_COMMITTER_EMAIL="demo@example.com",
    )

    def git(*args):
        return subprocess.run(
            ["git", *args], env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    repo = work / "origin.git"
    git("init", "--bare", "--initial-branch=main", str(repo))
    checkout = home / "wrk/demo_project"
    checkout.parent.mkdir(parents=True)
    git("clone", str(repo), str(checkout))
    (checkout / "README.md").write_text("# Shared workspace\n")
    git("-C", str(checkout), "add", "README.md")
    git("-C", str(checkout), "commit", "-m", "Initial workspace")
    git("-C", str(checkout), "push", "-u", "origin", "main")
    git("-C", str(checkout), "switch", "-c", "issue-42")
    (checkout / "README.md").write_text(
        "# Shared workspace\n\nCoordinate work and share progress.\n"
    )
    git("-C", str(checkout), "commit", "-am", "Describe the workspace")
    git("-C", str(checkout), "switch", "main")
    git("-C", str(checkout), "remote", "set-url", "origin", "https://github.com/demo/project.git")
    git("config", "--global", f"url.{repo}.insteadOf", "https://github.com/demo/project.git")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(work / "venv/bin/python"), "pillow"],
        env=env,
        check=True,
    )
    env["PATH"] = f"{bin_dir}:{work / 'venv/bin'}:/usr/bin:/bin"
    subprocess.run(
        [
            str(work / "venv/bin/python"),
            "-c",
            "from pathlib import Path\n"
            "from workdash.analysis_cache import AnalysisCache\n"
            "from workdash.github_client import GitHubClient\n"
            "item = GitHubClient().fetch_item('demo/project', 42, github_username='demo', todo_repository='demo/todos')\n"
            "AnalysisCache(Path.home() / '.config/workdash/cache').save(item, "
            "'# Shared workspace analysis\\n\\nImprove the handoff notes and verify the contributor workflow.\\n')\n",
        ],
        env=env,
        check=True,
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        env["WORKDASH_HOST"] = f"127.0.0.1:{sock.getsockname()[1]}"
    shell = bin_dir / "dashboard-shell"
    shell.write_text('#!/bin/sh\nif [ "$#" -gt 0 ]; then exec /bin/sh "$@"; fi\nexec workdash\n')
    shell.chmod(0o755)
    env["SHELL"] = str(shell)
    return env
