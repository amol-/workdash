"""Top-level app entrypoint."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from . import __version__
from .backend import SuggestionMarkers, WorkdashBackend
from .config import WorkdashConfig, WorkdashConfigValidationError, configure, load_config
from .control import (
    WorkdashControlClient,
    WorkdashControlError,
    WorkdashControlServer,
    WorkdashSession,
    format_work_item_id,
    show_config_payload,
)
from .launcher import (
    exec_zellij_wrapped_workdash,
    inject_workdash_local_bin_into_path,
    launch_terminal_context,
    list_workdash_sessions,
    open_in_browser,
)
from .models import WorkItem, format_type_label
from .repo_worktree import ensure_worktree
from .tui import WorkdashApp


def main(argv: Sequence[str] | None = None) -> int:
    """Run the app entrypoint with backend data orchestration."""

    options = _parse_args(argv)
    if options.debug:
        logging.basicConfig(level=logging.DEBUG)

    if options.configure:
        configure()
        return 0

    commands = WorkdashCommands()
    if options.command in {"list", "info", "analyze", "code", "read", "write"}:
        return _run_server_backed_command(commands, options)
    if options.command == "branchdiff":
        from .branchdiff import run_branchdiff

        return run_branchdiff(target=options.target)
    if options.command == "show-config":
        return commands.show_config(json_output=options.json_output)

    gh_error = _check_gh_preflight()
    if gh_error:
        print(f"Error: {gh_error}", file=sys.stderr, flush=True)
        return 1

    if not commands.preload_config():
        return 1

    if _should_wrap_interactive_start(options):
        try:
            exec_zellij_wrapped_workdash(argv)
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr, flush=True)
            return 1

    return commands.interactive(server=options.server)


@dataclass(frozen=True)
class CLIOptions:
    """CLI configuration available during plumbing phases."""

    debug: bool
    refresh: bool
    configure: bool
    direct: bool
    server: bool
    json_output: bool
    command: str | None = None
    session: str | None = None
    target: str | None = None
    agent: str | None = None
    include_all_panes: bool = False
    pane_id: str | None = None
    text: str | None = None
    full: bool = False
    raw: bool = False


class WorkdashCommands:
    """Commands that share Workdash config, backend, and server-backed clients."""

    def __init__(self) -> None:
        self._config: WorkdashConfig | None = None
        self._backend: WorkdashBackend | None = None
        self._client = WorkdashControlClient()

    def list_items(self, *, json_output: bool, refresh: bool = False) -> int:
        """List work items through the local Workdash server."""

        try:
            result = self._client.request("list", {"refresh": refresh})
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            _print_work_items_result(result)
        return 0

    def info(self, *, json_output: bool, include_all_panes: bool) -> int:
        """Report live panes through the local Workdash server."""

        try:
            result = self._client.request("info", {"include_all_panes": include_all_panes})
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            _print_pane_info(result)
        return 0

    def analyze_cli(self, *, target: str, agent: str | None, json_output: bool) -> int:
        """Analyze a current work item through the local Workdash server."""

        try:
            result = self._client.request("analyze", {"target": target, "agent": agent})
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        try:
            result = _with_local_analysis_path(result)
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            _print_analysis_result(result)
        return 0

    def code_cli(self, *, target: str, agent: str | None, json_output: bool) -> int:
        """Launch a coding agent through the local Workdash server."""

        try:
            result = self._client.request("code", {"target": target, "agent": agent})
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            _print_code_result(result)
        return 0

    def read_pane(self, *, pane_id: str, full: bool, json_output: bool) -> int:
        """Read pane content through the local Workdash server."""

        try:
            result = self._client.request("pane/content", {"pane_id": pane_id, "full": full})
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            content = str(result.get("content") or "")
            print(content, end="" if content.endswith("\n") else "\n")
        return 0

    def write_pane(self, *, pane_id: str, text: str, raw: bool, json_output: bool) -> int:
        """Send pane input through the local Workdash server."""

        try:
            result = self._client.request(
                "pane/send", {"pane_id": pane_id, "data": text, "raw": raw}
            )
        except WorkdashControlError as error:
            _print_control_error(error)
            return 1
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            print(
                f"Accepted input for {result['pane_id']} (raw: {'yes' if result['raw'] else 'no'})."
            )
        return 0

    def show_config(self, *, json_output: bool) -> int:
        """Show configured automation choices without requiring the server."""

        try:
            config = load_config().require_valid()
        except WorkdashConfigValidationError as error:
            _print_config_validation_error(error)
            return 1
        result = show_config_payload(config)
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            _print_show_config(result)
        return 0

    def preload_config(self) -> bool:
        """Validate startup config before any Zellij wrapping."""

        if self._config is not None:
            return True
        try:
            self._config = load_config().require_valid()
        except WorkdashConfigValidationError as error:
            _print_config_validation_error(error)
            return False
        return True

    def interactive(self, *, server: bool = False) -> int:
        """Start the interactive dashboard, optionally with the JSON server."""

        loaded = self._load_config_and_backend()
        if loaded is None:
            return 1
        config, backend = loaded
        print("Loading work items from GitHub...", flush=True)
        work_items, suggestion_markers = backend.load_items(
            progress_callback=lambda message: print(message, flush=True)
        )
        try:
            zellij_session = _select_workdash_session(None) if server else None
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr, flush=True)
            return 1
        session = WorkdashSession(
            config=config,
            backend=backend,
            work_items=work_items,
            suggestion_markers=suggestion_markers,
            zellij_session=zellij_session,
        )
        app = WorkdashApp(
            work_items=session.work_items,
            suggestion_markers=session.suggestion_markers,
            open_callback=lambda item: open_in_browser(item.url),
            refresh_callback=lambda: (
                session.list_items(refresh=True)
                and (session.work_items, session.suggestion_markers)
            ),
            worktree_callback=lambda item: ensure_worktree(config.workdir, item),
            analyze_callback=lambda item, tool="codex": session.analyze(
                target=format_work_item_id(item), agent=tool, prefer_cache=(tool == "cached")
            )["path"],
            launch_callback=lambda item, tool="codex": session.code(
                target=format_work_item_id(item), agent=tool, allow_vscode=True
            ),
            analyze_choices=config.tui_analyze_choices(),
            code_choices=config.tui_code_choices(),
            terminal_callback=lambda item: launch_terminal_context(
                ensure_worktree(config.workdir, item)
            ),
            include_callback=lambda url, _identities: session.include_item_by_url(url),
            session=session,
        )
        if server:

            def refresh_tui_from_session() -> None:
                if not app.is_running:
                    return
                try:
                    app.call_from_thread(app.refresh_from_session)
                except RuntimeError as error:
                    if str(error) != "App is not running":
                        raise

            session.items_changed_callback = refresh_tui_from_session
        control_server = WorkdashControlServer(session) if server else None
        if control_server is not None:
            try:
                control_server.start()
            except RuntimeError as error:
                print(f"Error: {error}", file=sys.stderr, flush=True)
                return 1
        try:
            app.run()
        finally:
            if control_server is not None:
                control_server.stop()
        return 0

    def _load_config_and_backend(self) -> tuple[WorkdashConfig, WorkdashBackend] | None:
        if self._config is not None and self._backend is not None:
            return self._config, self._backend
        if self._config is None and not self.preload_config():
            return None
        config = self._config
        assert config is not None
        backend = WorkdashBackend(config=config)
        self._backend = backend
        return config, backend


def _run_server_backed_command(commands: WorkdashCommands, options: CLIOptions) -> int:
    if options.command == "list":
        return commands.list_items(json_output=options.json_output, refresh=options.refresh)
    if options.command == "info":
        return commands.info(
            json_output=options.json_output,
            include_all_panes=options.include_all_panes,
        )
    if options.command == "analyze":
        return commands.analyze_cli(
            target=options.target or "",
            agent=options.agent,
            json_output=options.json_output,
        )
    if options.command == "code":
        return commands.code_cli(
            target=options.target or "",
            agent=options.agent,
            json_output=options.json_output,
        )
    if options.command == "read":
        return commands.read_pane(
            pane_id=options.pane_id or "",
            full=options.full,
            json_output=options.json_output,
        )
    if options.command == "write":
        return commands.write_pane(
            pane_id=options.pane_id or "",
            text=options.text or "",
            raw=options.raw,
            json_output=options.json_output,
        )
    raise AssertionError(f"Unsupported server-backed command: {options.command}")


def _print_config_validation_error(error: WorkdashConfigValidationError) -> None:
    print(f"Error: {error}", file=sys.stderr, flush=True)
    print(
        "Run 'workdash --configure' to set up your configuration.",
        file=sys.stderr,
        flush=True,
    )


def _parse_args(argv: Sequence[str] | None = None) -> CLIOptions:
    """Parse CLI flags introduced in Phase 1 task 2."""

    parser = argparse.ArgumentParser(
        prog="workdash",
        description="GitHub work triage dashboard.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose logging.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force a data refresh from GitHub sources.",
    )
    parser.add_argument(
        "--configure",
        action="store_true",
        help="Interactively set up or update the configuration file.",
    )
    parser.add_argument(
        "--direct",
        action="store_true",
        help="Start directly without wrapping the interactive dashboard in Zellij.",
    )
    parser.add_argument(
        "--server",
        action="store_true",
        help="Start the TUI with the localhost JSON control API.",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        help="Emit machine-readable JSON for commands that return information.",
    )
    subparsers = parser.add_subparsers(dest="command")
    list_parser = subparsers.add_parser(
        "list",
        help="List current Workdash items without launching the TUI.",
    )
    list_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    list_parser.add_argument(
        "--refresh",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Ask the running Workdash server to refresh items before listing.",
    )
    info_parser = subparsers.add_parser(
        "info",
        help="Report live Workdash-owned Zellij panes.",
    )
    info_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    info_parser.add_argument(
        "--all",
        dest="include_all_panes",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Include live non-plugin panes whose titles do not prove Workdash launched them.",
    )
    analyze_parser = subparsers.add_parser(
        "analyze",
        help="Analyze a current Workdash item.",
    )
    analyze_parser.add_argument("target", metavar="ITEM", help="Workdash item ID or GitHub URL.")
    analyze_parser.add_argument(
        "--agent",
        help="Configured analysis agent to run when no fresh cache exists.",
    )
    analyze_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    code_parser = subparsers.add_parser(
        "code",
        help="Launch a terminal-backed coding agent for a current Workdash item.",
    )
    code_parser.add_argument("target", metavar="ITEM", help="Workdash item ID or GitHub URL.")
    code_parser.add_argument(
        "--agent",
        help="Configured terminal-backed coding agent to launch.",
    )
    code_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    read_parser = subparsers.add_parser(
        "read",
        help="Read text from a live pane.",
    )
    read_parser.add_argument("pane_id", metavar="PANE_ID", help="Pane ID from workdash info.")
    read_parser.add_argument(
        "--full",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Read full scrollback instead of the visible viewport.",
    )
    read_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    write_parser = subparsers.add_parser(
        "write",
        help="Send text to a live pane.",
    )
    write_parser.add_argument("pane_id", metavar="PANE_ID", help="Pane ID from workdash info.")
    write_parser.add_argument("text", metavar="TEXT", help="Text to send to the pane.")
    write_parser.add_argument(
        "--raw",
        "--no-enter",
        dest="raw",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Send raw text without the default trailing Enter.",
    )
    write_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    show_config_parser = subparsers.add_parser(
        "show-config",
        help="Show configured agents and the fixed server address.",
    )
    show_config_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    branchdiff_parser = subparsers.add_parser(
        "branchdiff",
        help="Show side-by-side diff of current branch vs upstream.",
    )
    branchdiff_parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Branch to compare against (default: upstream).",
    )
    namespace = parser.parse_args(argv) if argv is not None else parser.parse_args()
    return CLIOptions(
        debug=namespace.debug,
        refresh=namespace.refresh,
        configure=namespace.configure,
        direct=namespace.direct,
        server=namespace.server,
        json_output=namespace.json_output,
        command=namespace.command,
        session=getattr(namespace, "session", None),
        target=getattr(namespace, "target", None),
        agent=getattr(namespace, "agent", None),
        include_all_panes=getattr(namespace, "include_all_panes", False),
        pane_id=getattr(namespace, "pane_id", None),
        text=getattr(namespace, "text", None),
        full=getattr(namespace, "full", False),
        raw=getattr(namespace, "raw", False),
    )


def _should_wrap_interactive_start(options: CLIOptions) -> bool:
    return (
        not options.direct
        and not options.configure
        and options.command is None
        and not os.getenv("ZELLIJ")
    )


def _print_work_items(
    work_items: Sequence[WorkItem], suggestion_markers: SuggestionMarkers
) -> None:
    if not work_items:
        print("No work items found.")
        return
    for item in sorted(work_items, key=lambda entry: entry.updated_at, reverse=True):
        suggestion_marker = suggestion_markers.get((item.item_type, item.repo, item.number), "")
        print(
            f"{format_type_label(item):7} "
            f"{format_work_item_id(item):24} "
            f"{item.created_at.date().isoformat()} "
            f"{f'* {item.title}' if suggestion_marker else item.title}"
        )


def _print_work_items_json(
    work_items: Sequence[WorkItem], suggestion_markers: SuggestionMarkers
) -> None:
    items = []
    for item in sorted(work_items, key=lambda entry: entry.updated_at, reverse=True):
        items.append(
            {
                "id": format_work_item_id(item),
                "type": item.item_type.value,
                "kind": item.kind.value,
                "repo": item.repo,
                "number": item.number,
                "title": item.title,
                "url": item.url,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
                "suggested": bool(
                    suggestion_markers.get((item.item_type, item.repo, item.number), "")
                ),
            }
        )
    print(json.dumps({"items": items}, ensure_ascii=True, indent=2))


def _print_work_items_result(result: dict[str, object]) -> None:
    items = result.get("items")
    if not items:
        print("No work items found.")
        return
    for item in items:
        if not isinstance(item, dict):
            continue
        title = f"* {item['title']}" if item.get("suggested") else item["title"]
        display_type = item.get("display_type")
        if display_type is None:
            display_type = str(item["id"]).split("#", maxsplit=1)[1].split("-", maxsplit=1)[0]
        print(f"{display_type:7} {item['id']:24} {str(item['updated_at'])[:10]} {title}")


def _with_local_analysis_path(result: dict[str, object]) -> dict[str, object]:
    cli_result = dict(result)
    file_content = cli_result.pop("file_content", None)
    file_name = cli_result.pop("file_name", None)
    cli_result.pop("content_type", None)
    if not isinstance(file_content, str):
        raise WorkdashControlError(
            "invalid_response",
            "Workdash server analysis response is missing base64 file_content.",
        )

    base_name = (
        os.path.basename(file_name) if isinstance(file_name, str) and file_name else "analysis.md"
    )
    stem, suffix = os.path.splitext(base_name)
    stem = "".join(character if character.isalnum() else "-" for character in stem).strip("-")
    prefix = f"workdash-{(stem or 'analysis')[:32]}-"
    analysis_path: str | None = None
    try:
        content = base64.b64decode(file_content, validate=True)
        with tempfile.NamedTemporaryFile(
            delete=False,
            prefix=prefix,
            suffix=suffix or ".md",
        ) as analysis_file:
            analysis_path = analysis_file.name
            analysis_file.write(content)
    except (binascii.Error, ValueError, OSError) as error:
        if analysis_path is not None:
            with suppress(OSError):
                os.unlink(analysis_path)
        raise WorkdashControlError(
            "invalid_response", "Workdash server returned invalid analysis content."
        ) from error
    cli_result["analysis_path"] = analysis_path
    cli_result.pop("path", None)
    return cli_result


def _print_analysis_result(result: dict[str, object]) -> None:
    print(f"Item: {result['item_id']}")
    print(f"Agent: {result['agent']}")
    print(f"Status: {result['status']}")
    print(f"Analysis path: {result['analysis_path']}")


def _print_code_result(result: dict[str, object]) -> None:
    print(f"Item: {result['item_id']}")
    print(f"Agent: {result['agent']}")
    print(f"Session: {result['session']}")
    print(f"Cwd: {result['cwd']}")
    print(f"Pane title: {result['pane_title'] or '-'}")
    print(f"Pane id: {result['pane_id'] or '-'}")



def _print_pane_info(info: dict[str, object]) -> None:
    print(f"Session: {info['session']}")
    panes = info["panes"]
    if not panes:
        print("No Workdash-owned panes found.")
        return
    for pane in panes:
        print(
            f"{pane['kind']:8} {pane['pane_id']} {pane['title']} "
            f"cwd={pane.get('cwd') or '-'} command={pane.get('command') or '-'} "
            f"tab={pane.get('tab_name') or pane.get('tab_id') or '-'} "
            f"state={pane.get('state') or '-'} item={pane['item']}"
        )


def _print_show_config(result: dict[str, object]) -> None:
    agents = result["agents"]
    server = result["server"]
    print("Analysis agents: " + ", ".join(agents["analyze"] or ["-"]))
    print("Code agents: " + ", ".join(agents["code"] or ["-"]))
    print(f"Server: {server['host']}:{server['port']}")


def _print_control_error(error: WorkdashControlError) -> None:
    print(f"Error: {error}", file=sys.stderr, flush=True)


def _select_workdash_session(requested_session: str | None) -> str:
    sessions = list_workdash_sessions()
    if requested_session is not None:
        if requested_session in sessions:
            return requested_session
        raise RuntimeError(
            f"Workdash-owned Zellij session {requested_session!r} is not active. "
            f"Candidates: {', '.join(sessions) if sessions else '(none)'}"
        )
    if not sessions:
        raise RuntimeError("An active Workdash-owned Zellij session is required.")
    if len(sessions) > 1:
        raise RuntimeError(
            "Multiple Workdash-owned Zellij sessions found. Close the extras before "
            "starting `workdash --server`: " + ", ".join(sessions)
        )
    return sessions[0]


def _check_gh_preflight() -> str | None:
    inject_workdash_local_bin_into_path()
    gh = shutil.which("gh")
    if gh is None:
        return (
            "gh CLI is not installed or not configured. Run 'workdash --configure' "
            "to install a local copy, or install gh globally from https://cli.github.com/."
        )
    try:
        subprocess.run(
            [gh, "auth", "status"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        auth_command = shlex.join([gh, "auth", "login"])
        return (
            "gh CLI is not authenticated.\n"
            "Run this command to authenticate the GitHub CLI used by workdash:\n"
            f"  {auth_command}"
        )
    except OSError as error:
        return f"failed to run gh auth status: {error}"
    return None
