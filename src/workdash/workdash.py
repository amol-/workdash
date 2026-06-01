"""Top-level app entrypoint."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from . import __version__
from .backend import SuggestionMarkers, WorkdashBackend
from .config import WorkdashConfig, configure, load_config, validate_config
from .github_client import parse_github_item_url
from .launcher import (
    exec_zellij_wrapped_workdash,
    inject_workdash_local_bin_into_path,
    launch_agent_context,
    launch_terminal_context,
    launch_vscode_context,
    list_workdash_sessions,
    load_zellij_panes,
    open_in_browser,
    prepare_launch_agent_prompt,
)
from .models import WorkItem, format_type_label
from .repo_worktree import ensure_worktree, existing_worktree_path, get_merge_base
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
    if options.command == "info":
        return commands.info(
            session=options.session,
            json_output=options.json_output,
            include_all_panes=options.include_all_panes,
        )
    if options.command == "analyze":
        return commands.analyze_cli(
            target=options.target or "",
            agent=options.agent,
            session=options.session,
            json_output=options.json_output,
        )

    # TODO(EVO-030): Add CLI code through the shared launch action.
    #                  Why: The shared launch action exists so TUI Code and CLI
    #                  code do not drift, but the probe does not yet expose the
    #                  CLI command that external automation will call.
    #                  Done: `workdash code ITEM [--agent NAME] [--session NAME]
    #                  [--json]` resolves the item, requires/selects a
    #                  Workdash-owned Zellij session, launches a configured
    #                  terminal-backed agent, and renders the session/pane result.
    #                  Non-Goals: Do not support non-terminal editors, create a
    #                  Workdash Zellij session automatically, or persist pane ids.

    gh_error = _check_gh_preflight()
    if gh_error:
        print(f"Error: {gh_error}", file=sys.stderr, flush=True)
        return 1

    if _should_wrap_interactive_start(options):
        try:
            exec_zellij_wrapped_workdash(argv)
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr, flush=True)
            return 1

    if options.command == "list":
        return commands.list_items(json_output=options.json_output)
    return commands.interactive()


@dataclass(frozen=True)
class CLIOptions:
    """CLI configuration available during plumbing phases."""

    debug: bool
    refresh: bool
    configure: bool
    direct: bool
    json_output: bool
    command: str | None = None
    session: str | None = None
    target: str | None = None
    agent: str | None = None
    include_all_panes: bool = False


class WorkdashCommands:
    """Commands that share Workdash config, backend, and item actions."""

    def __init__(self) -> None:
        self._config: WorkdashConfig | None = None
        self._backend: WorkdashBackend | None = None

    def info(self, *, session: str | None, json_output: bool, include_all_panes: bool) -> int:
        """Report live Workdash-owned Zellij panes."""

        try:
            selected_session = _select_workdash_session(session)
            gh_error = _check_gh_preflight()
            if gh_error:
                print(f"Error: {gh_error}", file=sys.stderr, flush=True)
                return 1
            loaded = self._load_config_and_backend()
            if loaded is None:
                return 1
            config, backend = loaded
            work_items, _suggestion_markers = backend.load_items()
            pane_info = _workdash_pane_info(
                selected_session,
                config.workdir,
                work_items,
                include_all_panes=include_all_panes,
            )
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr, flush=True)
            return 1
        if json_output:
            print(json.dumps(pane_info, ensure_ascii=True, indent=2))
        else:
            _print_pane_info(pane_info)
        return 0

    def list_items(self, *, json_output: bool) -> int:
        """List work items without starting the TUI."""

        loaded = self._load_config_and_backend()
        if loaded is None:
            return 1
        _config, backend = loaded
        work_items, suggestion_markers = backend.load_items()
        if json_output:
            _print_work_items_json(work_items, suggestion_markers)
        else:
            _print_work_items(work_items, suggestion_markers)
        return 0

    def analyze_cli(
        self,
        *,
        target: str,
        agent: str | None,
        session: str | None,
        json_output: bool,
    ) -> int:
        """Analyze a current work item from the CLI."""

        try:
            _select_workdash_session(session)
            gh_error = _check_gh_preflight()
            if gh_error:
                print(f"Error: {gh_error}", file=sys.stderr, flush=True)
                return 1
            loaded = self._load_config_and_backend(validate=False)
            if loaded is None:
                return 1
            config, backend = loaded
            missing = _missing_analyze_load_fields(config)
            if missing:
                _print_missing_config(missing)
                return 1
            work_items, _suggestion_markers = backend.load_items()
            item = _resolve_work_item_target(target, work_items)
            if item is None:
                raise RuntimeError(
                    f"{target!r} is not a current Workdash item ID or GitHub issue/PR URL."
                )
            item_id = format_work_item_id(item)
            cache_used = item.analysis is not None
            agents = _configured_analyze_agents(config)
            selected_agent = agent or (agents[0] if agents else None)
            if not cache_used:
                if selected_agent is None:
                    raise RuntimeError("No analysis agents are configured.")
                if selected_agent not in agents:
                    raise RuntimeError(
                        f"Analysis agent {selected_agent!r} is not configured. "
                        f"Configured agents: {', '.join(agents) if agents else '(none)'}"
                    )
                if not config.workdir:
                    _print_missing_config(["workdir"])
                    return 1
            path = self.analyze(item, tool=selected_agent or "cached", prefer_cache=True)
            if path is None:
                raise RuntimeError(f"Analysis failed for {item_id} with agent {selected_agent}.")
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr, flush=True)
            return 1

        result = {
            "item_id": item_id,
            "path": path,
            "agent": selected_agent,
            "cache_used": cache_used,
            "status": "cached" if cache_used else "generated",
        }
        if json_output:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            _print_analysis_result(result)
        return 0

    def interactive(self) -> int:
        """Start the interactive dashboard."""

        loaded = self._load_config_and_backend()
        if loaded is None:
            return 1
        config, backend = loaded
        print("Loading work items from GitHub...", flush=True)
        work_items, suggestion_markers = backend.load_items(
            progress_callback=lambda message: print(message, flush=True)
        )
        app = WorkdashApp(
            work_items=work_items,
            suggestion_markers=suggestion_markers,
            open_callback=lambda item: open_in_browser(item.url),
            refresh_callback=backend.load_items,
            worktree_callback=lambda item: ensure_worktree(config.workdir, item),
            analyze_callback=lambda item, tool="codex": self.analyze(item, tool=tool),
            launch_callback=lambda item, tool="codex": self.code(item, tool=tool),
            terminal_callback=lambda item: launch_terminal_context(
                ensure_worktree(config.workdir, item)
            ),
            include_callback=backend.include_item_by_url,
        )
        app.run()
        return 0

    def analyze(
        self,
        item: WorkItem,
        *,
        tool: str = "codex",
        prefer_cache: bool = False,
    ) -> str | None:
        """Produce an analysis file for a work item through the shared action path."""

        loaded = self._load_config_and_backend()
        if loaded is None:
            raise RuntimeError("Workdash configuration is incomplete.")
        config, backend = loaded
        if prefer_cache:
            cached_path = backend.analyze_item(item, tool="cached")
            if cached_path is not None:
                return cached_path
        if tool != "cached":
            backend.resolve_analyze_command_tokens(tool)
            ensure_worktree(config.workdir, item)
        return backend.analyze_item(item, tool=tool)

    def code(self, item: WorkItem, *, tool: str) -> dict[str, str | None]:
        """Launch a terminal-backed coding session through the shared action path."""

        loaded = self._load_config_and_backend()
        if loaded is None:
            raise RuntimeError("Workdash configuration is incomplete.")
        config, backend = loaded
        repo_path = ensure_worktree(config.workdir, item)
        prompt = prepare_launch_agent_prompt(
            item,
            repo_path,
            analysis_path=str(backend.analysis_cache.build_analysis_path(item))
            if item.analysis is not None
            else None,
            merge_base=get_merge_base(repo_path),
        )
        if tool == "vscode":
            # TODO(EVO-040): Exclude non-terminal editor launches from CLI code.
            #                  Why: The probe preserves the existing TUI behavior, but
            #                  the CLI code command must only launch panes that external
            #                  automation can control through Zellij.
            #                  Done: CLI agent selection ignores editor commands that do
            #                  not create terminal panes, while the TUI can still offer
            #                  configured non-terminal editors through its own dialog.
            #                  Non-Goals: Do not design the future configurable-editor
            #                  system or add per-editor capability metadata here.
            launch_vscode_context(repo_path, prompt)
            pane_title = None
        else:
            launch_commands = {
                "claude": config.claude.launch,
                "codex": config.codex.launch,
                "pi": config.pi.launch,
            }
            if tool not in launch_commands:
                raise ValueError(f"Unsupported coding agent: {tool!r}")
            pane_title = f"code_{os.path.basename(repo_path)}"
            launch_agent_context(
                repo_path,
                prompt,
                agent_command_tokens=shlex.split(launch_commands[tool]),
            )
        # TODO(EVO-050): Return targeted Zellij pane identifiers.
        #                  Why: The shared launch path now exposes one place for TUI and
        #                  CLI code behavior, but automation needs the session and pane
        #                  id produced by Zellij when launching from the CLI.
        #                  Done: terminal-backed launch helpers can target a selected
        #                  Workdash-owned session, capture Zellij's created pane id, and
        #                  this result includes session, pane id, pane title, cwd, agent,
        #                  and Workdash item id for JSON and human CLI output.
        #                  Non-Goals: Do not persist pane ids, add a pane registry, or
        #                  change how closed panes are discovered by `workdash info`.
        return {"agent": tool, "cwd": repo_path, "pane_title": pane_title, "pane_id": None}

    def _load_config_and_backend(
        self, *, validate: bool = True
    ) -> tuple[WorkdashConfig, WorkdashBackend] | None:
        if self._config is not None and self._backend is not None:
            return self._config, self._backend
        config = load_config()
        if validate:
            missing = validate_config(config)
            if missing:
                _print_missing_config(missing)
                return None
        backend = WorkdashBackend(config=config)
        self._config = config
        self._backend = backend
        return config, backend


def format_work_item_id(item: WorkItem) -> str:
    """Return the copy/paste identifier accepted by CLI work-item commands."""

    item_type = format_type_label(item).removesuffix("+")
    return f"{item.repo}#{item_type}-{item.number}"


def _resolve_work_item_target(target: str, work_items: Sequence[WorkItem]) -> WorkItem | None:
    for item in work_items:
        if target == format_work_item_id(item):
            return item
    parsed = parse_github_item_url(target)
    if parsed is None:
        return None
    for item in work_items:
        if (
            item.repo == parsed.repo
            and item.item_type == parsed.item_type
            and item.number == parsed.number
        ):
            return item
    return None


def _configured_analyze_agents(config: WorkdashConfig) -> list[str]:
    agents = []
    if config.codex.analyze:
        agents.append("codex")
    if config.claude.analyze:
        agents.append("claude")
    return agents


def _missing_analyze_load_fields(config: WorkdashConfig) -> list[str]:
    missing = []
    if not config.github_username:
        missing.append("github_username")
    if not config.repositories:
        missing.append("repositories")
    return missing


def _print_missing_config(missing: Sequence[str]) -> None:
    print(
        f"Error: missing configuration fields: {', '.join(missing)}",
        file=sys.stderr,
        flush=True,
    )
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
    info_parser = subparsers.add_parser(
        "info",
        help="Report live Workdash-owned Zellij panes.",
    )
    info_parser.add_argument(
        "--session",
        help="Inspect a specific Workdash-owned Zellij session.",
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
        "--session",
        help="Validate against a specific Workdash-owned Zellij session.",
    )
    analyze_parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit machine-readable JSON.",
    )
    namespace = parser.parse_args(argv) if argv is not None else parser.parse_args()
    return CLIOptions(
        debug=namespace.debug,
        refresh=namespace.refresh,
        configure=namespace.configure,
        direct=namespace.direct,
        json_output=namespace.json_output,
        command=namespace.command,
        session=getattr(namespace, "session", None),
        target=getattr(namespace, "target", None),
        agent=getattr(namespace, "agent", None),
        include_all_panes=getattr(namespace, "include_all_panes", False),
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


def _print_analysis_result(result: dict[str, object]) -> None:
    print(f"Item: {result['item_id']}")
    print(f"Agent: {result['agent']}")
    print(f"Status: {result['status']}")
    print(f"Path: {result['path']}")


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
            "Multiple Workdash-owned Zellij sessions found. Pass --session with one of: "
            + ", ".join(sessions)
        )
    return sessions[0]


def _is_live_non_plugin_pane(pane: dict[str, object]) -> bool:
    return (
        pane.get("is_plugin") is not True
        and pane.get("exited") is not True
        and pane.get("is_held") is not True
    )


def _is_workdash_work_pane(pane: dict[str, object]) -> bool:
    if not _is_live_non_plugin_pane(pane):
        return False
    title = pane.get("title")
    return isinstance(title, str) and (title.startswith("code_") or title.startswith("terminal_"))


def _workdash_pane_info(
    session: str,
    workdir: str | None,
    work_items: Sequence[WorkItem],
    *,
    include_all_panes: bool = False,
) -> dict[str, object]:
    item_by_cwd = {}
    if workdir is not None:
        for item in work_items:
            item_path = existing_worktree_path(workdir, item)
            if item_path is not None:
                item_by_cwd[_normalized_path(item_path)] = format_work_item_id(item)
    panes = []
    for pane in load_zellij_panes(session):
        if _is_workdash_work_pane(pane):
            panes.append(_pane_info(selected_session=session, pane=pane, item_by_cwd=item_by_cwd))
        elif include_all_panes and _is_live_non_plugin_pane(pane):
            panes.append(
                _pane_info(
                    selected_session=session,
                    pane=pane,
                    item_by_cwd={},
                    kind="unknown",
                )
            )
    return {"session": session, "panes": panes}


def _pane_info(
    selected_session: str,
    pane: dict[str, object],
    item_by_cwd: dict[str, str],
    *,
    kind: str | None = None,
) -> dict[str, object]:
    title = str(pane.get("title") or "")
    pane_id = pane.get("id")
    kind = kind or ("agent" if title.startswith("code_") else "terminal")
    cwd = pane.get("pane_cwd")
    mapped_item = None
    if isinstance(cwd, str) and cwd:
        normalized_cwd = _normalized_path(cwd)
        matches = [
            (root, item_id)
            for root, item_id in item_by_cwd.items()
            if normalized_cwd == root or normalized_cwd.startswith(root + os.sep)
        ]
        if matches:
            mapped_item = max(matches, key=lambda match: len(match[0]))[1]
    state = pane.get("state")
    if not isinstance(state, str) or not state:
        state = "exited" if pane.get("exited") is True else "running"
    return {
        "session": selected_session,
        "tab_id": pane.get("tab_id"),
        "tab_name": pane.get("tab_name"),
        "pane_id": f"terminal_{pane_id}",
        "title": title,
        "cwd": cwd,
        "command": pane.get("pane_command") or pane.get("terminal_command"),
        "kind": kind,
        "state": state,
        "exited": pane.get("exited"),
        "focused": pane.get("is_focused"),
        "floating": pane.get("is_floating"),
        "item": mapped_item or "unknown",
    }


def _normalized_path(path: os.PathLike[str] | str) -> str:
    return os.path.realpath(os.path.expanduser(os.fspath(path)))


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
