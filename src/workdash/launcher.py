"""External launcher helpers."""

import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import markdown

from .config import LOCAL_BIN_PATH
from .github import GithubHelper
from .models import WorkItem, WorkItemKind, WorkItemType

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_BROWSER_OPEN_COMMANDS = ("xdg-open", "open")
_BROWSER_OPEN_TIMEOUT_SECONDS = 4
_WORKDASH_LOCAL_BIN = LOCAL_BIN_PATH


@dataclass(frozen=True)
class ZellijPaneLaunch:
    session: str | None
    pane_id: str | None
    pane_title: str
    cwd: str


def _load_prompt_template(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


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
            capture_output=True,
            text=True,
            timeout=_BROWSER_OPEN_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise RuntimeError(f"{command_name} is not installed or not on PATH.") from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Failed to open {kind} via {command_name}: command did not finish within "
            f"{_BROWSER_OPEN_TIMEOUT_SECONDS} seconds. Opening a browser may not be "
            "supported from this session."
        ) from error
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        if not details:
            details = f"exit code {error.returncode}"
        raise RuntimeError(f"Failed to open {kind} via {command_name}: {details}") from error


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


def _path_with_workdash_local_bin(env_path: str | None = None) -> str:
    path = env_path if env_path is not None else os.environ.get("PATH", "")
    local_bin = str(_WORKDASH_LOCAL_BIN)
    if not path:
        return local_bin
    parts = path.split(os.pathsep)
    if local_bin in parts:
        return path
    return os.pathsep.join([path, local_bin])


def inject_workdash_local_bin_into_path() -> None:
    """Add workdash's local bin directory to PATH without displacing global tools."""

    os.environ["PATH"] = _path_with_workdash_local_bin()


def _resolve_zellij_binary() -> str:
    inject_workdash_local_bin_into_path()
    zellij = shutil.which("zellij")
    if zellij is None:
        raise RuntimeError(
            "zellij is not installed or not configured. Run 'workdash --configure' to set it up."
        )
    return zellij


def exec_zellij_wrapped_workdash(argv: Sequence[str] | None) -> NoReturn:
    zellij = _resolve_zellij_binary()

    original_args = list(argv) if argv is not None else sys.argv[1:]
    session_name = f"workdash-{secrets.token_hex(4)}"
    workdash_command = _build_direct_workdash_command(original_args)
    layout_path = _write_zellij_startup_layout(workdash_command, session_name=session_name)
    command = [zellij, "--layout", layout_path]
    try:
        os.execvp(zellij, command)
    except OSError as error:
        raise RuntimeError(f"failed to start zellij: {error}") from error
    raise AssertionError("os.execvp returned unexpectedly")


def list_workdash_sessions() -> list[str]:
    zellij = _resolve_zellij_binary()
    try:
        completed = subprocess.run(
            [zellij, "list-sessions", "--no-formatting"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        if "no active zellij sessions found" in details.lower():
            return []
        raise RuntimeError(
            f"Failed to list Zellij sessions: {details or error.returncode}"
        ) from error

    sessions = []
    for line in completed.stdout.splitlines():
        if "(EXITED" in line:
            continue
        session_name = line.split(" [Created ", maxsplit=1)[0].strip()
        if session_name.startswith("workdash"):
            sessions.append(session_name)
    return sessions


def load_zellij_panes(session: str) -> list[dict[str, object]]:
    zellij = _resolve_zellij_binary()
    try:
        completed = subprocess.run(
            [
                zellij,
                "--session",
                session,
                "action",
                "list-panes",
                "--json",
                "--all",
                "--command",
                "--state",
                "--tab",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        raise RuntimeError(
            f"Failed to list Zellij panes for {session}: {details or error.returncode}"
        ) from error
    try:
        panes = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to parse Zellij pane JSON: {error.msg}") from error
    if not isinstance(panes, list):
        raise RuntimeError("Zellij pane JSON must be a list.")
    return [pane for pane in panes if isinstance(pane, dict)]


def dump_zellij_pane(session: str, pane_id: str, *, full: bool = False) -> str:
    """Dump a Zellij pane viewport or scrollback."""

    zellij = _resolve_zellij_binary()
    command = [zellij, "--session", session, "action", "dump-screen", "--pane-id", pane_id]
    if full:
        command.append("--full")
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        raise RuntimeError(
            f"Failed to dump Zellij pane {pane_id} in {session}: {details or error.returncode}"
        ) from error
    return completed.stdout


def send_zellij_pane_input(session: str, pane_id: str, data: str, *, raw: bool = False) -> None:
    """Send characters to a Zellij pane, optionally followed by Enter."""

    zellij = _resolve_zellij_binary()
    try:
        subprocess.run(
            [
                zellij,
                "--session",
                session,
                "action",
                "write-chars",
                "--pane-id",
                pane_id,
                "--",
                data,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if not raw:
            subprocess.run(
                [
                    zellij,
                    "--session",
                    session,
                    "action",
                    "send-keys",
                    "--pane-id",
                    pane_id,
                    "Enter",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        raise RuntimeError(
            f"Failed to send input to Zellij pane {pane_id} in {session}: "
            f"{details or error.returncode}"
        ) from error


def _build_direct_workdash_command(original_args: Sequence[str]) -> list[str]:
    current_entrypoint = Path(sys.argv[0])
    if current_entrypoint.name in {"__main__.py", "workdash.py"} and (
        current_entrypoint.parent.name == "workdash"
    ):
        return [sys.executable, "-m", "workdash", "--direct", *original_args]
    return [sys.argv[0] or "workdash", "--direct", *original_args]


def _quote_kdl_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_zellij_startup_layout(workdash_command: Sequence[str], *, session_name: str) -> str:
    if not workdash_command:
        raise ValueError("Workdash command must not be empty.")
    if not session_name:
        raise ValueError("Zellij session name must not be empty.")

    command = _quote_kdl_string(workdash_command[0])
    args = " ".join(_quote_kdl_string(argument) for argument in workdash_command[1:])
    layout = (
        f"session_name {_quote_kdl_string(session_name)}\n"
        'on_force_close "quit"\n'
        "session_serialization false\n"
        "disable_session_metadata true\n"
        "show_startup_tips false\n"
        "attach_to_session false\n"
        "layout {\n"
        '    tab name="workdash" {\n'
        f"        pane command={command} close_on_exit=true {{\n"
        f"            args {args}\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="workdash-zellij-",
        suffix=".kdl",
        delete=False,
    ) as layout_file:
        layout_file.write(layout)
        return layout_file.name


def _launch_zellij_command(
    repo_path: str,
    command: list[str],
    *,
    context: str,
    work_action: str,
    zellij_session: str | None = None,
) -> ZellijPaneLaunch:
    target_session = (zellij_session or "").strip()
    if not target_session and not os.getenv("ZELLIJ", "").strip():
        raise RuntimeError(
            f"{context}: terminal-backed work actions require an active Zellij session. "
            "Start workdash normally, or run it inside Zellij."
        )
    zellij = _resolve_zellij_binary()
    pane_title = _zellij_pane_name_for_work_action(work_action, repo_path)
    before_panes = (
        {_zellij_pane_identity(pane) for pane in load_zellij_panes(target_session)}
        if target_session
        else set()
    )
    _run_launch_command(
        _build_zellij_new_pane_command(
            zellij,
            repo_path,
            command,
            work_action=work_action,
            zellij_session=target_session or None,
        ),
        context=context,
    )
    pane_id = None
    if target_session:
        new_panes = [
            pane
            for pane in load_zellij_panes(target_session)
            if pane.get("is_plugin") is not True and _zellij_pane_identity(pane) not in before_panes
        ]
        matching_panes = [
            pane
            for pane in new_panes
            if pane.get("title") == pane_title
            and isinstance(pane.get("pane_cwd"), str)
            and os.path.realpath(str(pane.get("pane_cwd"))) == os.path.realpath(repo_path)
        ]
        candidates = matching_panes or new_panes
        if len(candidates) == 1:
            pane_id = _format_zellij_pane_id(candidates[0].get("id"))
    return ZellijPaneLaunch(
        session=target_session or None,
        pane_id=pane_id,
        pane_title=pane_title,
        cwd=repo_path,
    )


def _build_zellij_new_pane_command(
    zellij: str,
    repo_path: str,
    command: list[str],
    *,
    work_action: str,
    zellij_session: str | None = None,
) -> list[str]:
    session_args = ["--session", zellij_session] if zellij_session is not None else []
    return [
        zellij,
        *session_args,
        "action",
        "new-pane",
        *_zellij_pane_name_argument(work_action, repo_path),
        "--cwd",
        repo_path,
        "--",
        *command,
    ]


def _zellij_pane_name_argument(work_action: str, repo_path: str) -> list[str]:
    return ["--name", _zellij_pane_name_for_work_action(work_action, repo_path)]


def _zellij_pane_name_for_work_action(work_action: str, repo_path: str) -> str:
    return f"{work_action}_{Path(repo_path).name}"


def _zellij_pane_identity(pane: dict[str, object]) -> tuple[bool, object]:
    return (pane.get("is_plugin") is True, pane.get("id"))


def _format_zellij_pane_id(pane_id: object) -> str | None:
    if pane_id is None or pane_id == "":
        return None
    pane_id_text = str(pane_id)
    if pane_id_text.startswith("terminal_"):
        return pane_id_text
    return f"terminal_{pane_id_text}"


def collect_launch_github_context(item: WorkItem) -> dict[str, Any]:
    """Collect key GitHub context for launching an interactive Codex session."""

    return GithubHelper().fetch_launch_context(item)


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
    *,
    zellij_session: str | None = None,
) -> ZellijPaneLaunch:
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("Prompt must be a non-empty string.")

    command_tokens = agent_command_tokens if agent_command_tokens is not None else ["codex"]
    if not command_tokens or any(
        not isinstance(token, str) or not token.strip() for token in command_tokens
    ):
        raise ValueError("Agent command tokens must be a non-empty list of non-empty strings.")
    user_shell = os.environ.get("SHELL", "/bin/sh")
    agent_command = [user_shell, "-ic", shlex.join([*command_tokens, prompt])]
    return _launch_zellij_command(
        repo_path,
        agent_command,
        context="Failed to launch coding agent in zellij",
        work_action="code",
        zellij_session=zellij_session,
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


def launch_terminal_context(repo_path: str) -> ZellijPaneLaunch:
    """Open a terminal in zellij rooted at the given repository path."""

    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    user_shell = os.environ.get("SHELL", "/bin/sh")
    shell_command = [user_shell, "-i"]
    return _launch_zellij_command(
        repo_path,
        shell_command,
        context="Failed to launch terminal in zellij",
        work_action="terminal",
    )


def launch_branchdiff_context(repo_path: str, item: WorkItem | None = None) -> ZellijPaneLaunch:
    """Open branchdiff TUI in zellij rooted at the given repository path.

    This launches the `workdash branchdiff` command as a standalone subprocess
    in a new zellij pane. The command will display a side-by-side diff viewer
    for the git repository at repo_path.
    """
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")

    branchdiff_command = ["workdash", "branchdiff"]
    if item is not None and item.item_type == WorkItemType.PR:
        branchdiff_command.append(_resolve_branchdiff_pr_base_ref(item))

    user_shell = os.environ.get("SHELL", "/bin/sh")
    command = [user_shell, "-ic", shlex.join(branchdiff_command)]
    return _launch_zellij_command(
        repo_path,
        command,
        context="Failed to launch branchdiff in zellij",
        work_action="diff",
    )


def _resolve_branchdiff_pr_base_ref(item: WorkItem) -> str:
    base_ref_name, head_repo = GithubHelper().fetch_branchdiff_base(item)
    remote = "origin" if head_repo == item.repo else "upstream"
    return f"{remote}/{base_ref_name}"


def focus_zellij_pane(session: str, pane_id: str) -> None:
    """Focus a Zellij pane by its ID using zellij action focus-pane-id.

    :param session: Zellij session name
    :param pane_id: Pane ID to focus
    :raises RuntimeError: If the focus command fails
    """
    zellij = _resolve_zellij_binary()
    try:
        subprocess.run(
            [
                zellij,
                "--session",
                session,
                "action",
                "focus-pane-id",
                pane_id,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        raise RuntimeError(
            f"Failed to focus Zellij pane {pane_id} in {session}: {details or error.returncode}"
        ) from error
