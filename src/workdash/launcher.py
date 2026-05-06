"""External launcher helpers."""

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

import markdown

from .models import WorkItem, WorkItemKind, WorkItemType

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_BROWSER_OPEN_COMMANDS = ("xdg-open", "open")


def _load_prompt_template(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


_ISSUE_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt"
)
_PR_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
    "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName"
)


def _resolve_browser_open_command() -> str:
    for command in _BROWSER_OPEN_COMMANDS:
        if shutil.which(command) is not None:
            return command
    raise RuntimeError("Neither xdg-open nor open is installed or on PATH.")


def _run_browser_open(target: str, *, kind: str) -> None:
    command_name = _resolve_browser_open_command()
    try:
        subprocess.run(
            [command_name, target],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"{command_name} is not installed or not on PATH.") from error
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Failed to open {kind} via {command_name}: exit code {error.returncode}"
        ) from error


def open_in_browser(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string.")
    _run_browser_open(url, kind="URL")


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       max-width: 52em; margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #1f2328; }}
pre {{ background: #f6f8fa; padding: 1em; overflow-x: auto; border-radius: 6px; }}
code {{ background: #f0f0f0; padding: 0.2em 0.4em; border-radius: 3px; font-size: 0.9em; }}
pre code {{ background: none; padding: 0; }}
blockquote {{ border-left: 4px solid #d0d7de; margin: 0; padding: 0.5em 1em; color: #656d76; }}
table {{ border-collapse: collapse; }} th, td {{ border: 1px solid #d0d7de; padding: 6px 13px; }}
</style></head>
<body>{body}</body></html>
"""


def open_markdown(path: str) -> None:
    """Render a markdown file to HTML and open it in the browser."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("Path must be a non-empty string.")
    md_path = Path(path)
    md_content = md_path.read_text(encoding="utf-8")
    html_body = markdown.markdown(md_content, extensions=["fenced_code", "tables", "codehilite"])
    html = _HTML_TEMPLATE.format(title=md_path.stem, body=html_body)
    html_path = md_path.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")
    _run_browser_open(str(html_path), kind="file")


def _run_launch_command(command: list[str], *, context: str) -> None:
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError(f"{context}: required command '{command[0]}' is not on PATH.") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise RuntimeError(f"{context}: {message}") from error


def _resolve_terminal_emulator() -> str | None:
    for terminal in ("ptyxis", "konsole"):
        if shutil.which(terminal) is not None:
            return terminal
    return None


def _run_gh_context_command(*, item: WorkItem, command: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "Failed to gather launch context with gh: gh CLI is not installed or not on PATH."
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        raise RuntimeError(
            f"Failed to gather launch context for {item.item_type.value} "
            f"{item.repo}#{item.number}: "
            f"{stderr or f'process exited with code {error.returncode}'}"
        ) from error
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Failed to parse launch context JSON for {item.item_type.value} "
            f"{item.repo}#{item.number}: {error.msg}"
        ) from error
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Invalid launch context payload for {item.item_type.value} "
            f"{item.repo}#{item.number}: expected a JSON object."
        )
    return payload


def collect_launch_github_context(item: WorkItem) -> dict[str, Any]:
    """Collect key GitHub context for launching an interactive Codex session."""

    if item.item_type == WorkItemType.ISSUE:
        return _run_gh_context_command(
            item=item,
            command=[
                "gh",
                "issue",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _ISSUE_CONTEXT_JSON_FIELDS,
            ],
        )
    return _run_gh_context_command(
        item=item,
        command=[
            "gh",
            "pr",
            "view",
            str(item.number),
            "--repo",
            item.repo,
            "--json",
            _PR_CONTEXT_JSON_FIELDS,
        ],
    )


def build_launch_agent_prompt(
    *,
    item: WorkItem,
    github_context: dict[str, Any],
    repo_path: str,
    analysis_path: str | None = None,
    merge_base: str | None = None,
) -> str:
    """Build the initial interactive launch prompt for a coding agent."""

    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    if not isinstance(github_context, dict):
        raise ValueError("GitHub context must be a JSON object.")

    if item.kind == WorkItemKind.REVIEW_REQUESTED_PR:
        template_name = "launch_review.txt"
    elif item.item_type == WorkItemType.ISSUE:
        template_name = "launch_issue.txt"
    else:
        template_name = "launch_pr.txt"

    analysis_section = ""
    if analysis_path:
        analysis_section = (
            "\nPREVIOUS ANALYSIS:\n"
            f"A previous analysis of this work item is available at: {analysis_path}\n"
            "Read it before proceeding — it contains summary, context, and recommendations\n"
            "that will help you get started faster.\n"
        )

    merge_base_line = ""
    if merge_base:
        merge_base_line = (
            f"\n- git merge-base: {merge_base}"
            "\n- use this as the diff target to see only this branch's changes:"
            f" git diff {merge_base}..HEAD"
        )

    template = _load_prompt_template(template_name)
    return template.format(
        item_type=item.item_type.value,
        kind=item.kind.value,
        repo=item.repo,
        number=item.number,
        title=item.title,
        url=item.url,
        repo_path=repo_path,
        analysis_section=analysis_section,
        merge_base_section=merge_base_line,
        github_context_json=json.dumps(github_context, ensure_ascii=True, indent=2, sort_keys=True),
    )


def prepare_launch_agent_prompt(
    item: WorkItem,
    repo_path: str,
    analysis_path: str | None = None,
    merge_base: str | None = None,
) -> str:
    """Collect GitHub context and return the launch prompt text for a coding agent."""

    return build_launch_agent_prompt(
        item=item,
        github_context=collect_launch_github_context(item),
        repo_path=repo_path,
        analysis_path=analysis_path,
        merge_base=merge_base,
    )


def launch_agent_context(
    repo_path: str,
    prompt: str,
    agent_command_tokens: list[str] | None = None,
) -> None:
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be a non-empty string.")

    command_tokens = agent_command_tokens if agent_command_tokens is not None else ["codex"]
    if not command_tokens or any(
        not isinstance(token, str) or not token.strip() for token in command_tokens
    ):
        raise ValueError("Agent command tokens must be a non-empty list of non-empty strings.")
    if shutil.which("zellij") is None:
        raise RuntimeError(
            "zellij is not installed or not on PATH. Download it from https://zellij.dev"
        )

    user_shell = os.environ.get("SHELL", "/bin/sh")
    agent_command = [user_shell, "-ic", shlex.join([*command_tokens, prompt])]
    if os.getenv("ZELLIJ"):
        _run_launch_command(
            ["zellij", "action", "new-pane", "--cwd", repo_path, "--", *agent_command],
            context="Failed to launch coding agent in zellij",
        )
        return

    terminal = _resolve_terminal_emulator()
    if terminal is None:
        raise RuntimeError("No supported terminal emulator found. Install ptyxis or konsole.")

    zellij_command = [
        "zellij",
        "--session",
        "workdash-agent",
        "run",
        "--cwd",
        repo_path,
        "--",
        *agent_command,
    ]
    terminal_commands = {
        "ptyxis": ["ptyxis", "--new-window", "-d", repo_path, "--", *zellij_command],
        "konsole": ["konsole", "--new-window", "--workdir", repo_path, "-e", *zellij_command],
    }
    _run_launch_command(
        terminal_commands[terminal],
        context=f"Failed to launch coding agent via {terminal}",
    )


def launch_vscode_context(repo_path: str, prompt: str) -> None:
    """Open VSCode on the repository and start a Copilot chat session."""
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be a non-empty string.")
    _run_launch_command(
        ["code", "--new-window", repo_path],
        context="Failed to open VSCode",
    )
    _run_launch_command(
        ["code", "chat", prompt, "--reuse-window"],
        context="Failed to start Copilot chat",
    )


def launch_terminal_context(repo_path: str) -> None:
    """Open a terminal in zellij rooted at the given repository path."""

    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    if shutil.which("zellij") is None:
        raise RuntimeError(
            "zellij is not installed or not on PATH. Download it from https://zellij.dev"
        )

    user_shell = os.environ.get("SHELL", "/bin/sh")
    shell_command = [user_shell, "-i"]
    if os.getenv("ZELLIJ"):
        _run_launch_command(
            ["zellij", "action", "new-pane", "--cwd", repo_path, "--", *shell_command],
            context="Failed to launch terminal in zellij",
        )
        return

    terminal = _resolve_terminal_emulator()
    if terminal is None:
        raise RuntimeError("No supported terminal emulator found. Install ptyxis or konsole.")

    zellij_command = [
        "zellij",
        "--session",
        "workdash-terminal",
        "run",
        "--cwd",
        repo_path,
        "--",
        *shell_command,
    ]
    terminal_commands = {
        "ptyxis": ["ptyxis", "--new-window", "-d", repo_path, "--", *zellij_command],
        "konsole": ["konsole", "--new-window", "--workdir", repo_path, "-e", *zellij_command],
    }
    _run_launch_command(
        terminal_commands[terminal],
        context=f"Failed to launch terminal via {terminal}",
    )
