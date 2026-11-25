"""External launcher helpers."""

import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import markdown
import nh3

from .agent_context import render_github_context
from .config import LOCAL_BIN_PATH
from .dashboard_record import DashboardRecords
from .github_client import GitHubClient
from .models import REVIEW_KINDS, WorkItem, WorkItemKind, WorkItemType, display_repo

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_BROWSER_OPEN_COMMANDS = ("xdg-open", "open")
_ITEM_DOCUMENT_COMMAND = ("workdash", "view")
_BROWSER_OPEN_TIMEOUT_SECONDS = 4
_WORKDASH_LOCAL_BIN = LOCAL_BIN_PATH
# A busy Zellij server can take several seconds to answer a listing, so repeatable
# commands get a generous timeout. A pane listing is still attempted up to three
# times, two seconds apart, before it counts as unavailable.
_ZELLIJ_PANE_READ_ATTEMPTS = 3
_ZELLIJ_PANE_READ_RETRY_SECONDS = 2.0
_ZELLIJ_COMMAND_TIMEOUT_SECONDS = 10.0
# A pane launch proves itself only once the pane writes its own ID, so a launch is
# retried up to three times, waiting up to five seconds per attempt for that proof.
_ZELLIJ_PANE_LAUNCH_ATTEMPTS = 3
_ZELLIJ_PANE_MARKER_POLL_SECONDS = 0.2
_ZELLIJ_PANE_MARKER_TIMEOUT_SECONDS = 5.0
_PANE_WRAPPER_NAME = "workdash-pane"
_ITEM_VIEW_WRAPPER_NAME = "workdash-view"
# Terminal control sequences in text captured from a pane or an error message.
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# Concurrent launches for one group must not each create their own tab.
_GROUP_TAB_LOCK = threading.Lock()


class ZellijBinaryUnavailableError(RuntimeError):
    """No executable Zellij client was found on PATH or in Workdash's bin directory."""


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


def _run_browser_open(
    target: str,
    *,
    command: str | None = None,
    kind: str,
    needs_pane: bool = False,
    zellij_session: str | None = None,
    direct_nonblocking: bool = False,
    focus: bool = False,
) -> str:
    try:
        command_tokens = (
            shlex.split(command) if command is not None else [_resolve_browser_open_command()]
        )
    except ValueError as error:
        raise RuntimeError(f"Failed to open {kind} via {command!r}: {error}") from error
    if not command_tokens:
        raise RuntimeError(f"Failed to open {kind}: command is empty.")
    command_name = command or command_tokens[0]
    if needs_pane:
        session = zellij_session or os.getenv("ZELLIJ_SESSION_NAME")
        if not session:
            raise RuntimeError(
                f"Failed to open {kind} via {command_name}: viewer requires an active Zellij session."
            )
        try:
            if command_tokens == list(_ITEM_DOCUMENT_COMMAND):
                executable = shutil.which(command_tokens[0])
                if executable:
                    command_tokens[0] = os.path.abspath(executable)
            executable = command_tokens[0]
            if command == " ".join(_ITEM_DOCUMENT_COMMAND):
                command_tokens = [
                    "/bin/sh",
                    "-c",
                    '"$@" || { printf "\\nPress Enter to close this pane.\\n"; read -r _; }',
                    _ITEM_VIEW_WRAPPER_NAME,
                    *command_tokens,
                ]
            pane_id = zellij.open_viewer(session, [*command_tokens, target], executable=executable)
            if focus:
                zellij.focus_zellij_pane(session, pane_id)
        except subprocess.CalledProcessError as error:
            details = (error.stderr or "").strip() or (error.stdout or "").strip()
            raise RuntimeError(
                f"Failed to open {kind} via {command_name}: {details or error}"
            ) from error
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Failed to open {kind} via {command_name}: {error}") from error
        return command_name
    try:
        if direct_nonblocking:
            process = subprocess.Popen(
                [*command_tokens, target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                returncode = process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                threading.Thread(target=process.wait, daemon=True).start()
            else:
                if returncode:
                    raise RuntimeError(
                        f"Failed to open {kind} via {command_name}: exit code {returncode}"
                    )
            return command_name
        subprocess.run(
            [*command_tokens, target],
            check=True,
            capture_output=True,
            text=True,
            timeout=_BROWSER_OPEN_TIMEOUT_SECONDS,
        )
    except OSError as error:
        raise RuntimeError(f"Failed to open {kind} via {command_name}: {error}") from error
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
    return command_name


def open_in_browser(
    url: str,
    command: str | None = None,
    *,
    needs_pane: bool = False,
    zellij_session: str | None = None,
    direct_nonblocking: bool = False,
) -> str:
    """Open a URL, returning the name of the command that was used."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("URL must be a non-empty string.")
    return _run_browser_open(
        url,
        command=command,
        kind="URL",
        needs_pane=needs_pane,
        zellij_session=zellij_session,
        direct_nonblocking=direct_nonblocking,
    )


def open_item_document(url: str, *, zellij_session: str | None) -> str:
    """Show a GitHub issue or pull request as an item document in a full-size pane."""

    return _run_browser_open(
        url,
        command=" ".join(_ITEM_DOCUMENT_COMMAND),
        kind="URL",
        needs_pane=True,
        zellij_session=zellij_session,
    )


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src http: https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
       max-width: 52em; margin: 2em auto; padding: 0 1em; line-height: 1.6; color: #1f2328; }}
pre {{ background: #f6f8fa; padding: 1em; overflow-x: auto; border-radius: 6px; }}
code {{ background: #f0f0f0; padding: 0.2em 0.4em; border-radius: 3px; font-size: 0.9em; }}
pre code {{ background: none; padding: 0; }}
blockquote {{ border-left: 4px solid #d0d7de; margin: 0; padding: 0.5em 1em; color: #656d76; }}
table {{ border-collapse: collapse; }} th, td {{ border: 1px solid #d0d7de; padding: 6px 13px; }}
@media (max-width: 1023px) {{
  body {{ margin: 1em auto; padding: 0 0.75em; overflow-wrap: anywhere; }}
  pre {{ padding: 0.75em; font-size: 0.85em; }}
  pre, table {{ max-width: 100%; overflow-x: auto; }}
  table {{ display: block; }}
  th, td {{ padding: 4px 8px; }}
  img {{ max-width: 100%; height: auto; }}
}}
</style></head>
<body>{body}</body></html>
"""


def render_analysis_html(content: str, title: str = "Analysis") -> str:
    body = markdown.markdown(content, extensions=["fenced_code", "tables", "codehilite"])
    return _HTML_TEMPLATE.format(title=html.escape(title), body=nh3.clean(body))


def open_markdown(
    path: str,
    command: str | None = None,
    *,
    needs_pane: bool = False,
    zellij_session: str | None = None,
    format: str = "html",
    focus: bool = False,
) -> None:
    """Open a local analysis in the requested format."""

    if not isinstance(path, str) or not path.strip():
        raise ValueError("Path must be a non-empty string.")
    md_path = Path(path)
    if format == "html":
        md_content = md_path.read_text(encoding="utf-8")
        html = render_analysis_html(md_content, md_path.stem)
        html_path = md_path.with_suffix(".html")
        html_path.write_text(html, encoding="utf-8")
    else:
        html_path = md_path
    _run_browser_open(
        html_path.resolve().as_uri(),
        command=command,
        kind="file",
        direct_nonblocking=True,
        focus=focus,
        needs_pane=needs_pane,
        zellij_session=zellij_session,
    )


def _run_launch_command(
    command: list[str], *, context: str = "", timeout: float | None = None
) -> subprocess.CompletedProcess:
    prefix = f"{context}: " if context else ""
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{prefix}command timed out after {error.timeout:g}s") from error
    except FileNotFoundError as error:
        raise RuntimeError(f"{prefix}required command '{command[0]}' is not on PATH.") from error
    except subprocess.CalledProcessError as error:
        message = error.stderr.strip() or error.stdout.strip() or str(error)
        raise RuntimeError(f"{prefix}{message}") from error


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
        raise ZellijBinaryUnavailableError(
            "zellij is not installed or not configured. Run 'workdash --configure' to set it up."
        )
    return zellij


def is_live_zellij_pane(pane: dict[str, object]) -> bool:
    """Whether a listed pane is a running, visible terminal pane rather than a plugin or exit."""

    return (
        pane.get("is_plugin") is not True
        and pane.get("exited") is not True
        and pane.get("is_held") is not True
    )


def is_zellij_shell_pane(pane: dict[str, object]) -> bool:
    """Whether Zellij lists a shell rather than a command launched through our wrapper."""
    command = pane.get("pane_command")
    if not isinstance(command, str) or not command.strip():
        return False
    if command.startswith("/bin/sh -c ") and any(
        f" {wrapper} " in command for wrapper in (_PANE_WRAPPER_NAME, _ITEM_VIEW_WRAPPER_NAME)
    ):
        return False
    shells = {"sh", "bash", "zsh", "fish", "dash", "ksh", "tcsh", "csh"}
    shells.add(os.path.basename(os.environ.get("SHELL", "")))
    return os.path.basename(command.split(maxsplit=1)[0]) in shells


def dashboard_is_running(session: str, *, attach_only: bool = False) -> bool:
    """Whether a dashboard already serves ``session``.

    A recorded dashboard counts only while its process lives and its address still
    reports the same session. With no usable record, the address this start would
    bind decides, which also covers dashboards started before records existed. A
    dashboard serving another session cannot be replaced, and an address that will
    not answer proves nothing, so both are reported instead of starting a dashboard
    that could not bind or that would duplicate a live one. An attach-only probe
    treats a healthy dashboard for another session as no match.
    """

    # The control API speaks to this module, so the address and probe arrive late.
    from .control import server_address

    records = DashboardRecords()
    record = records.load(session)
    if record is not None:
        if _process_alive(record.pid) and _dashboard_session_at(record.address) == session:
            return True
        records.remove(session)

    host, port = server_address()
    address = f"{host}:{port}"
    reported = _dashboard_session_at(address)
    if reported == session:
        return True
    if reported is not None and not attach_only:
        raise RuntimeError(
            f"Zellij session {reported!r} already has a Workdash dashboard at {address}. "
            "Stop it or set WORKDASH_HOST to a different address."
        )
    return False


def _dashboard_session_at(address: str) -> str | None:
    """Return the Zellij session of the dashboard serving ``address``, if any."""

    from .control import WorkdashControlError, probe_dashboard_session

    try:
        return probe_dashboard_session(address)
    except WorkdashControlError as error:
        if error.code == "version_mismatch":
            raise RuntimeError(str(error)) from error
        raise RuntimeError(
            f"Cannot tell whether a Workdash dashboard is running at {address}: {error}"
        ) from error


def _process_alive(pid: int) -> bool:
    """Whether the recorded dashboard process still exists."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _pane_input_error(
    session: str, pane_id: str, error: subprocess.CalledProcessError
) -> RuntimeError:
    details = (error.stderr or "").strip() or (error.stdout or "").strip()
    return RuntimeError(
        f"Failed to send input to Zellij pane {pane_id} in {session}: {details or error.returncode}"
    )


def _build_direct_workdash_command(original_args: Sequence[str]) -> list[str]:
    current_entrypoint = Path(sys.argv[0])
    if current_entrypoint.name in {"__main__.py", "workdash.py"} and (
        current_entrypoint.parent.name == "workdash"
    ):
        return [sys.executable, "-m", "workdash", *original_args]
    return [sys.argv[0] or "workdash", *original_args]


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
        'on_force_close "detach"\n'
        "session_serialization true\n"
        "disable_session_metadata false\n"
        "show_startup_tips false\n"
        "attach_to_session false\n"
        "layout {\n"
        "    default_tab_template {\n"
        "        pane size=1 borderless=true {\n"
        '            plugin location="zellij:tab-bar"\n'
        "        }\n"
        "        children\n"
        "        pane size=2 borderless=true {\n"
        '            plugin location="zellij:status-bar"\n'
        "        }\n"
        "    }\n"
        '    tab name="dashboard" {\n'
        '        pane split_direction="horizontal" {\n'
        f'            pane name="workdash tui" command={command} size="75%" close_on_exit=true {{\n'
        f"                args {args}\n"
        "            }\n"
        '            pane size="25%"\n'
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


def _launch_zellij_pane(
    command: list[str],
    *,
    context: str = "",
    zellij_session: str | None = None,
    cwd: str | None = None,
    name: str | None = None,
    tab_id: object = None,
    floating: bool = False,
    blocking: bool = False,
    pane_callback: Callable[[str | None], None] | None = None,
    checked_executable: str | None = None,
) -> str | None:
    """Launch one Zellij pane and return its ID once the pane proves it started.

    Zellij can accept a pane request, exit zero and never run the command, so the
    pane writes its own ``$ZELLIJ_PANE_ID`` to a private marker before exec.
    Workdash retries only while it has seen no marker, and returns ``None`` if
    no pane proved it started.
    """
    zellij = _resolve_zellij_binary()
    session_args = ["--session", zellij_session] if zellij_session is not None else []
    with tempfile.TemporaryDirectory(prefix="workdash-pane-") as directory:
        claim_dir = Path(directory) / "launch"
        claim_dir.mkdir()
        # The pane may have a different PATH or cwd from the Zellij client.
        preflight = ""
        if checked_executable is not None:
            preflight = "cmd=$2; "
            if "/" not in checked_executable:
                # Search files, not shell builtins; empty entries mean the pane cwd.
                preflight = (
                    'path=$PATH:; while [ -n "$path" ]; do '
                    'dir=${path%%:*}; path=${path#*:}; cmd="${dir:-.}/$2"; '
                    '[ -f "$cmd" ] && [ -x "$cmd" ] && break; done; '
                )
            preflight += '[ -f "$cmd" ] && [ -x "$cmd" ] || claim=missing; '
        argv = [
            zellij,
            *session_args,
            "action",
            "new-pane",
            *(
                ["-f", "-x", "0", "-y", "0", "--width", "100%", "--height", "100%"]
                if floating
                else []
            ),
            *(["--tab-id", str(tab_id)] if tab_id is not None else []),
            *(["--name", name] if name is not None else []),
            *(["--cwd", cwd] if cwd is not None else []),
            "--close-on-exit",
            *(["--block-until-exit"] if blocking else []),
            "--",
            "/bin/sh",
            "-c",
            'claim="$ZELLIJ_PANE_ID"; '
            + preflight
            # Find ln even under a restricted PATH, without changing the command's PATH.
            + '[ -n "$ZELLIJ_PANE_ID" ] && command -p ln -s "$claim" "$1" 2>/dev/null '
            '&& [ "$claim" != missing ] || exit 1; shift 2; exec "$@"',
            _PANE_WRAPPER_NAME,
        ]
        last_timeout = None
        prefix = f"{context}: " if context else ""
        try:
            for attempt in range(_ZELLIJ_PANE_LAUNCH_ATTEMPTS):
                marker = claim_dir / f"pane-{attempt}"
                launch_argv = [*argv, str(marker), checked_executable or "", *command]
                error = None
                stderr = ""
                with ExitStack() as clients:
                    client = None
                    try:
                        if blocking:
                            client = clients.enter_context(
                                subprocess.Popen(
                                    launch_argv,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True,
                                )
                            )
                            deadline = time.monotonic() + _ZELLIJ_PANE_MARKER_TIMEOUT_SECONDS
                            while not marker.is_symlink():
                                remaining = deadline - time.monotonic()
                                try:
                                    _, stderr = client.communicate(
                                        timeout=max(
                                            0, min(remaining, _ZELLIJ_PANE_MARKER_POLL_SECONDS)
                                        )
                                    )
                                    break
                                except subprocess.TimeoutExpired:
                                    if remaining <= 0:
                                        raise
                        else:
                            stderr = _run_launch_command(
                                launch_argv,
                                context=context,
                                timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                            ).stderr
                            deadline = time.monotonic() + _ZELLIJ_PANE_MARKER_TIMEOUT_SECONDS
                            while not marker.is_symlink() and time.monotonic() < deadline:
                                time.sleep(_ZELLIJ_PANE_MARKER_POLL_SECONDS)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as launch_error:
                        error = launch_error

                    if pane_id := _cancel_or_find_pane(marker):
                        if pane_id == "missing":
                            raise RuntimeError(
                                f"{prefix}Executable {checked_executable!r} is not available in the Zellij pane."
                            )
                        pane_id = _format_zellij_pane_id(pane_id)
                        if client is not None:
                            try:
                                if pane_callback is not None:
                                    pane_callback(pane_id)
                                client.communicate()
                            finally:
                                if pane_callback is not None:
                                    pane_callback(None)
                        return pane_id
                    if client is not None and client.returncode is None:
                        client.kill()

                if isinstance(error, subprocess.TimeoutExpired) or (
                    error is not None and isinstance(error.__cause__, subprocess.TimeoutExpired)
                ):
                    last_timeout = error
                elif isinstance(error, OSError):
                    raise RuntimeError(f"{prefix}{error}") from error
                elif error is not None:
                    raise error
                elif details := stderr.strip():
                    # Zellij can exit zero even when it reports a launch error.
                    raise RuntimeError(f"{prefix}{details}")

            if isinstance(last_timeout, subprocess.TimeoutExpired):
                raise RuntimeError(
                    f"{prefix}the Zellij pane never ran (startup timed out)"
                ) from last_timeout
            if last_timeout is not None:
                raise last_timeout
            return None
        finally:
            # Remove the claim path atomically before cleanup unlinks its contents.
            claim_dir.rename(Path(directory) / "finished")


def _cancel_or_find_pane(marker: Path) -> str:
    # Cancellation and wrapper startup compete for the same atomic claim.
    try:
        marker.symlink_to("cancelled")
    except FileExistsError:
        return os.readlink(marker)
    return ""


def _zellij_pane_name_for_work_action(work_action: str, repo_path: str) -> str:
    return f"{work_action}_{Path(repo_path).name}"


def _format_zellij_pane_id(pane_id: object) -> str | None:
    if pane_id is None or pane_id == "":
        return None
    pane_id_text = str(pane_id)
    if pane_id_text.startswith("terminal_"):
        return pane_id_text
    return f"terminal_{pane_id_text}"


def _terminal_zellij_panes(panes: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    return [pane for pane in panes if pane.get("is_plugin") is not True]


def find_zellij_pane(
    panes: Sequence[dict[str, object]], pane_id: object
) -> dict[str, object] | None:
    """Find a terminal pane by its Zellij ID."""
    formatted = _format_zellij_pane_id(pane_id)
    if formatted is None:
        return None
    return next(
        (
            pane
            for pane in panes
            if pane.get("is_plugin") is not True
            and _format_zellij_pane_id(pane.get("id")) == formatted
        ),
        None,
    )


def collect_launch_github_context(item: WorkItem) -> dict[str, Any]:
    """Collect key GitHub context for launching an interactive Codex session."""

    return GitHubClient().fetch_launch_context(item)


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

    # A coding session either implements the user's own work or reviews somebody
    # else's pull request, so the work kind picks between two briefings.
    template_name = "launch_review.txt" if item.kind in REVIEW_KINDS else "launch_issue.txt"

    partial_work_section = ""
    if item.kind == WorkItemKind.AUTHORED_PR:
        partial_work_section = (
            "\nWORK ALREADY IN PROGRESS:\n"
            f"This work already has an open pull request of yours ({item.url}), and the\n"
            "checkout holds its partial implementation. Review those existing changes\n"
            "first and build on them instead of starting the work over.\n"
        )

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
        # The checkout holds the code being worked on, which for a targeted todo
        # is the target and not the repository the todo issue itself lives in.
        code_repo=display_repo(item),
        number=item.number,
        title=item.title,
        url=item.url,
        repo_path=repo_path,
        partial_work_section=partial_work_section,
        analysis_section=analysis_section,
        merge_base_section=merge_base_line,
        github_context=render_github_context(github_context, item.item_type),
    )


def prepare_launch_agent_prompt(
    item: WorkItem,
    repo_path: str,
    analysis_path: str | None = None,
    merge_base: str | None = None,
    github_client=None,
) -> str:
    """Collect GitHub context and return the launch prompt text for a coding agent."""

    return build_launch_agent_prompt(
        item=item,
        github_context=(
            github_client.fetch_launch_context(item)
            if github_client is not None
            else collect_launch_github_context(item)
        ),
        repo_path=repo_path,
        analysis_path=analysis_path,
        merge_base=merge_base,
    )


def launch_terminal_context(
    repo_path: str, *, zellij_session: str | None = None
) -> ZellijPaneLaunch:
    """Open a terminal in zellij rooted at the given repository path."""

    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")
    user_shell = os.environ.get("SHELL", "/bin/sh")
    shell_command = [user_shell, "-i"]
    return zellij.launch_pane(
        repo_path,
        shell_command,
        context="Failed to launch terminal in zellij",
        work_action="terminal",
        zellij_session=zellij_session,
    )


def launch_branchdiff_context(
    repo_path: str, item: WorkItem | None = None, *, github_client=None
) -> ZellijPaneLaunch:
    """Open branchdiff TUI in zellij rooted at the given repository path.

    This launches the `workdash branchdiff` command as a standalone subprocess
    in a new zellij pane. The command will display a side-by-side diff viewer
    for the git repository at repo_path.
    """
    if not isinstance(repo_path, str) or not repo_path.strip():
        raise ValueError("Repository path must be a non-empty string.")

    branchdiff_command = ["workdash", "branchdiff"]
    if item is not None and item.item_type == WorkItemType.PR:
        branchdiff_command.append(
            _resolve_branchdiff_pr_base_ref(item, github_client=github_client)
        )

    user_shell = os.environ.get("SHELL", "/bin/sh")
    command = [user_shell, "-ic", shlex.join(branchdiff_command)]
    return zellij.launch_pane(
        repo_path,
        command,
        context="Failed to launch branchdiff in zellij",
        work_action="diff",
    )


def _resolve_branchdiff_pr_base_ref(item: WorkItem, *, github_client=None) -> str:
    base_ref_name, head_repo = (
        github_client if github_client is not None else GitHubClient()
    ).fetch_branchdiff_base(item)
    remote = "origin" if head_repo == item.repo else "upstream"
    return f"{remote}/{base_ref_name}"


def _read_zellij_panes(session: str) -> list[dict[str, object]]:
    """Read one pane listing, reporting every unusable answer as ``RuntimeError``.

    Zellij exits 0 and prints its session list when the session is not available, so a
    listing that is not pane JSON is a failure like a failed or hung command.
    """

    command = [
        _resolve_zellij_binary(),
        "--session",
        session,
        "action",
        "list-panes",
        "--json",
        "--all",
        "--command",
        "--state",
        "--tab",
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or (error.stdout or "").strip()
        raise RuntimeError(
            f"Failed to list Zellij panes for {session}: {details or error.returncode}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Failed to list Zellij panes for {session}: no answer within "
            f"{_ZELLIJ_COMMAND_TIMEOUT_SECONDS:.0f}s"
        ) from error
    try:
        panes = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to parse Zellij pane JSON: {error.msg}") from error
    if not isinstance(panes, list):
        raise RuntimeError("Zellij pane JSON must be a list.")
    return [pane for pane in panes if isinstance(pane, dict)]


class Zellij:
    """All Zellij interactions must go through this object."""

    def open_viewer(self, session: str, command: list[str], *, executable: str) -> str:
        pane_id = _launch_zellij_pane(
            command,
            zellij_session=session,
            name=Path(command[0]).name,
            floating=True,
            checked_executable=executable,
        )
        if pane_id is None:
            raise RuntimeError("Zellij did not return the viewer pane ID.")
        return pane_id

    @staticmethod
    def exec_zellij_wrapped_workdash(
        argv: Sequence[str] | None,
        preflight: Callable[[], bool] = lambda: True,
        *,
        attach_only: bool = False,
    ) -> None:
        """Attach to a live dashboard, or start one unless setup is still needed."""
        zellij = _resolve_zellij_binary()

        original_args = list(argv) if argv is not None else sys.argv[1:]
        session_name = "workdash"
        for index, arg in enumerate(original_args):
            if arg == "--session":
                session_name = original_args[index + 1]
            elif arg.startswith("--session="):
                session_name = arg.partition("=")[2]
        workdash_command = _build_direct_workdash_command(["--persistent-pane", *original_args])
        try:
            result = subprocess.run(
                [zellij, "list-sessions", "--no-formatting"],
                capture_output=True,
                text=True,
                check=True,
            )
            sessions = {
                line.split(" [Created ", 1)[0]: "(EXITED" in line
                for line in result.stdout.splitlines()
            }
        except OSError as error:
            raise RuntimeError(f"Failed to list Zellij sessions: {error}") from error
        except subprocess.CalledProcessError as error:
            details = (error.stderr or "").strip() or (error.stdout or "").strip()
            if "no active zellij sessions found" not in details.lower():
                raise RuntimeError(
                    f"Failed to list Zellij sessions: {details or error.returncode}"
                ) from error
            sessions = {}

        if attach_only and sessions.get(session_name, True):
            return
        if dashboard_is_running(session_name, attach_only=attach_only):
            print("Workdash is already running; new options are ignored. Attaching...", flush=True)
            time.sleep(1)
            command = [zellij, "attach", session_name]
            Zellij._attach_and_focus_dashboard(zellij, session_name, command)
        elif attach_only:
            return
        elif session_name in sessions:
            if sessions[session_name]:
                try:
                    # Attach's boolean options can toggle twice; layout options override config.
                    subprocess.run(
                        [
                            zellij,
                            "--layout-string",
                            "session_serialization true\ndisable_session_metadata false\nlayout {}",
                            "attach",
                            "-b",
                            session_name,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except OSError as error:
                    raise RuntimeError(f"Failed to recover Zellij session: {error}") from error
                except subprocess.CalledProcessError as error:
                    details = (error.stderr or "").strip() or (error.stdout or "").strip()
                    raise RuntimeError(
                        f"Failed to recover Zellij session: {details or error.returncode}"
                    ) from error
            if not preflight():
                return
            panes = Zellij.load_zellij_panes(session_name)
            prefix = [zellij, "--session", session_name, "action"]
            try:
                tab = next(
                    (pane.get("tab_id") for pane in panes if pane.get("tab_name") == "dashboard"),
                    None,
                )
                if tab is None:
                    tab = subprocess.run(
                        [*prefix, "new-tab", "--name", "dashboard"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                pane_id = _launch_zellij_pane(
                    [
                        "env",
                        *(
                            [f"WORKDASH_HOST={os.environ['WORKDASH_HOST']}"]
                            if "WORKDASH_HOST" in os.environ
                            else ["-u", "WORKDASH_HOST"]
                        ),
                        *workdash_command,
                    ],
                    context="Failed to start dashboard in Zellij",
                    zellij_session=session_name,
                    tab_id=tab,
                    name="workdash tui",
                )
                if pane_id is None:
                    raise RuntimeError("Failed to start dashboard in Zellij: the pane never ran")
            except OSError as error:
                raise RuntimeError(f"Failed to start dashboard in Zellij: {error}") from error
            except subprocess.CalledProcessError as error:
                details = (error.stderr or "").strip() or (error.stdout or "").strip()
                raise RuntimeError(
                    f"Failed to start dashboard in Zellij: {details or error.returncode}"
                ) from error
            command = [zellij, "attach", session_name]
        else:
            if not preflight():
                return
            layout_path = _write_zellij_startup_layout(workdash_command, session_name=session_name)
            command = [zellij, "--layout", layout_path]
        try:
            os.execvp(zellij, command)
        except OSError as error:
            raise RuntimeError(f"failed to start zellij: {error}") from error
        raise AssertionError("os.execvp returned unexpectedly")

    def name_dashboard_pane(self, session: str, pane_id: str) -> None:
        """Name the dashboard's own pane so the session shows what it runs."""

        _run_launch_command(
            [
                _resolve_zellij_binary(),
                "--session",
                session,
                "action",
                "rename-pane",
                "-p",
                f"terminal_{pane_id}",
                "workdash tui",
            ],
            context="Failed to name dashboard pane",
        )

    @staticmethod
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

    @staticmethod
    def load_zellij_panes(session: str) -> list[dict[str, object]]:
        """List a session's panes, retrying briefly while Zellij answers unusably."""

        for attempt in range(1, _ZELLIJ_PANE_READ_ATTEMPTS + 1):
            try:
                return _read_zellij_panes(session)
            except RuntimeError:
                if attempt == _ZELLIJ_PANE_READ_ATTEMPTS:
                    raise
                time.sleep(_ZELLIJ_PANE_READ_RETRY_SECONDS)
        raise AssertionError("unreachable: retry loop exited without return or raise")

    def list_workdash_panes(self, session: str) -> list[dict[str, object]]:
        """List terminal panes, including exited and held panes."""
        return _terminal_zellij_panes(self.load_zellij_panes(session))

    @contextmanager
    def zellij_fullscreen_pane(self) -> Iterator[None]:
        """Fill the surrounding Zellij pane while the block runs and restore its size afterwards.

        Restores the pane only while it still fills the tab, so a pane the user sized
        back during the block is left alone. Leaves the pane untouched when the caller
        is not inside a Zellij pane, is already fullscreen, cannot have its pane state
        read, or when filling the pane fails.
        """
        session = os.environ.get("ZELLIJ_SESSION_NAME", "").strip()
        pane_id = os.environ.get("ZELLIJ_PANE_ID", "").strip()
        pane = self._find_zellij_pane(session, pane_id) if session and pane_id else None
        if pane is None or pane.get("is_fullscreen"):
            yield
            return

        def toggle_fullscreen() -> None:
            zellij = _resolve_zellij_binary()
            try:
                subprocess.run(
                    [zellij, "--session", session, "action", "toggle-fullscreen", "-p", pane_id],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as error:
                details = (error.stderr or "").strip() or (error.stdout or "").strip()
                raise RuntimeError(
                    f"Failed to toggle fullscreen for Zellij pane {pane_id} in {session}: "
                    f"{details or error.returncode}"
                ) from error

        try:
            toggle_fullscreen()
        except RuntimeError:
            yield
            return

        try:
            yield
        finally:
            with suppress(RuntimeError):
                pane = self._find_zellij_pane(session, pane_id)
                if pane is not None and pane.get("is_fullscreen"):
                    toggle_fullscreen()

    @staticmethod
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

    def send_zellij_pane_input(
        self, session: str, pane_id: str, data: str, *, raw: bool = False
    ) -> None:
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
                self.press_zellij_pane_enter(session, pane_id)
        except subprocess.CalledProcessError as error:
            raise _pane_input_error(session, pane_id, error) from error

    @staticmethod
    def press_zellij_pane_enter(session: str, pane_id: str) -> None:
        """Submit whatever the pane's input line already holds."""

        try:
            subprocess.run(
                [
                    _resolve_zellij_binary(),
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
            raise _pane_input_error(session, pane_id, error) from error

    def launch_pane(
        self,
        repo_path: str,
        command: list[str],
        *,
        context: str,
        work_action: str,
        zellij_session: str | None = None,
        group: str | None = None,
    ) -> ZellijPaneLaunch:
        target_session = (zellij_session or "").strip()
        if not target_session and not os.getenv("ZELLIJ", "").strip():
            raise RuntimeError(
                f"{context}: terminal-backed work actions require an active Zellij session. "
                "Start workdash normally, or run it inside Zellij."
            )
        pane_title = _zellij_pane_name_for_work_action(work_action, repo_path)
        pane_id = _launch_zellij_pane(
            command,
            context=context,
            zellij_session=target_session or None,
            cwd=repo_path,
            name=pane_title,
            checked_executable=command[0] if work_action in {"terminal", "diff"} else None,
            tab_id=self._group_tab_id(
                _resolve_zellij_binary(), target_session, group, context=context
            ),
        )
        if pane_id is None:
            raise RuntimeError(f"{context}: the Zellij pane never ran")
        return ZellijPaneLaunch(
            session=target_session or None,
            pane_id=pane_id,
            pane_title=pane_title,
            cwd=repo_path,
        )

    def launch_pane_and_wait(
        self,
        repo_path: str,
        command: list[str],
        *,
        context: str,
        work_action: str,
        zellij_session: str | None,
        group: str | None,
        pane_callback: Callable[[str | None], None] | None = None,
    ) -> None:
        """Run a command in a new Zellij pane and wait for it to exit."""
        target_session = (zellij_session or "").strip()
        if not target_session:
            raise RuntimeError(f"{context}: running an agent in a pane requires a Zellij session.")
        pane_id = _launch_zellij_pane(
            command,
            context=context,
            zellij_session=target_session,
            cwd=repo_path,
            name=_zellij_pane_name_for_work_action(work_action, repo_path),
            tab_id=self._group_tab_id(
                _resolve_zellij_binary(), target_session, group, context=context
            ),
            blocking=True,
            pane_callback=pane_callback,
        )
        if pane_id is None:
            raise RuntimeError(f"{context}: the Zellij pane never ran")

    @staticmethod
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

    @staticmethod
    def close_zellij_pane(session: str, pane_id: str) -> None:
        """Close a Zellij pane by its ID using zellij action close-pane.

        :param session: Zellij session name
        :param pane_id: Pane ID to close
        :raises RuntimeError: If the close command fails
        """
        zellij = _resolve_zellij_binary()
        try:
            subprocess.run(
                [
                    zellij,
                    "--session",
                    session,
                    "action",
                    "close-pane",
                    "-p",
                    pane_id,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as error:
            details = (error.stderr or "").strip() or (error.stdout or "").strip()
            raise RuntimeError(
                f"Failed to close Zellij pane {pane_id} in {session}: {details or error.returncode}"
            ) from error

    @staticmethod
    def _attach_and_focus_dashboard(zellij: str, session_name: str, command: list[str]) -> None:
        record = DashboardRecords().load(session_name)
        if record and record.pane_id:
            try:
                before = subprocess.run(
                    [zellij, "--session", session_name, "action", "list-clients"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                )
                existing_clients = {
                    line.split()[0] for line in before.stdout.splitlines()[1:] if line.split()
                }
            except (OSError, subprocess.SubprocessError):
                return
            try:
                client = subprocess.Popen(command)
            except OSError:
                pass
            else:
                previous = {
                    sig: signal.signal(sig, signal.SIG_IGN)
                    for sig in (signal.SIGINT, signal.SIGQUIT)
                }
                try:
                    with suppress(OSError, RuntimeError, subprocess.SubprocessError):
                        panes = _read_zellij_panes(session_name)
                        pane = next(
                            (
                                pane
                                for pane in panes
                                if str(pane.get("id")) == record.pane_id
                                and is_live_zellij_pane(pane)
                                and pane.get("tab_id") is not None
                            ),
                            None,
                        )
                        if pane:
                            for _ in range(20):
                                clients = subprocess.run(
                                    [zellij, "--session", session_name, "action", "list-clients"],
                                    check=True,
                                    capture_output=True,
                                    text=True,
                                    timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                                )
                                if any(
                                    line.split() and line.split()[0] not in existing_clients
                                    for line in clients.stdout.splitlines()[1:]
                                ):
                                    break
                                time.sleep(0.1)
                            else:
                                raise RuntimeError("Timed out waiting for Zellij client")
                            subprocess.run(
                                [
                                    zellij,
                                    "--session",
                                    session_name,
                                    "action",
                                    "go-to-tab-by-id",
                                    str(pane["tab_id"]),
                                ],
                                check=True,
                                capture_output=True,
                                timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                            )
                            subprocess.run(
                                [
                                    zellij,
                                    "--session",
                                    session_name,
                                    "action",
                                    "focus-pane-id",
                                    f"terminal_{record.pane_id}",
                                ],
                                check=True,
                                capture_output=True,
                                timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                            )
                            # Zellij may ignore pane-ID focus until a reattached client sends input.
                            if not existing_clients:
                                tab_panes = {
                                    f"{'plugin' if p.get('is_plugin') else 'terminal'}_{p['id']}"
                                    for p in panes
                                    if p.get("tab_id") == pane["tab_id"] and p.get("id") is not None
                                }
                                for _ in range(len(tab_panes)):
                                    clients = subprocess.run(
                                        [
                                            zellij,
                                            "--session",
                                            session_name,
                                            "action",
                                            "list-clients",
                                        ],
                                        check=True,
                                        capture_output=True,
                                        text=True,
                                        timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                                    )
                                    lines = [
                                        line.split() for line in clients.stdout.splitlines()[1:]
                                    ]
                                    if len(lines) != 1 or len(lines[0]) < 2:
                                        break
                                    focused = lines[0][1]
                                    if (
                                        focused == f"terminal_{record.pane_id}"
                                        or focused not in tab_panes
                                    ):
                                        break
                                    subprocess.run(
                                        [
                                            zellij,
                                            "--session",
                                            session_name,
                                            "action",
                                            "focus-next-pane",
                                        ],
                                        check=True,
                                        capture_output=True,
                                        timeout=_ZELLIJ_COMMAND_TIMEOUT_SECONDS,
                                    )
                    raise SystemExit(client.wait())
                finally:
                    for sig, handler in previous.items():
                        signal.signal(sig, handler)

    def _group_tab_id(
        self, zellij: str, session: str, group: str | None, *, context: str
    ) -> str | None:
        """Return the tab ID named ``group``, creating that tab when it does not exist."""

        if group is None:
            return None
        if not session:
            raise RuntimeError(f"{context}: grouping requires a selected Zellij session.")
        with _GROUP_TAB_LOCK:
            tab_id = next(
                (
                    pane.get("tab_id")
                    for pane in self.load_zellij_panes(session)
                    if pane.get("tab_name") == group
                ),
                None,
            )
            if tab_id is not None:
                return str(tab_id)
            try:
                result = subprocess.run(
                    [
                        zellij,
                        "--session",
                        session,
                        "action",
                        "new-tab",
                        f"--name={group}",
                        "--no-focus",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    f"{context}: {error.stderr.strip() or error.stdout.strip()}"
                ) from error
            tab_id = result.stdout.strip()
            if not tab_id.isdigit():
                raise RuntimeError(f"{context}: Zellij did not report the new tab ID.")
            return tab_id

    def _find_zellij_pane(self, session: str, pane_id: str) -> dict[str, object] | None:
        """Return the identified pane's state, or None when it cannot be read."""

        try:
            return find_zellij_pane(self.list_workdash_panes(session), pane_id)
        except RuntimeError:
            return None


zellij = Zellij()
