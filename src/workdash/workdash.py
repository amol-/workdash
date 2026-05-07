"""Top-level app entrypoint."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from . import __version__
from .backend import SuggestionMarkers, WorkdashBackend
from .config import configure, load_config, validate_config
from .launcher import (
    exec_zellij_wrapped_workdash,
    launch_agent_context,
    launch_terminal_context,
    launch_vscode_context,
    open_in_browser,
    prepare_launch_agent_prompt,
)
from .models import WorkItem, format_type_label
from .repo_worktree import ensure_worktree, get_merge_base
from .tui import WorkdashApp


@dataclass(frozen=True)
class CLIOptions:
    """CLI configuration available during plumbing phases."""

    debug: bool
    print_mode: bool
    refresh: bool
    configure: bool
    direct: bool


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
        "--print",
        dest="print_mode",
        action="store_true",
        help="Emit flattened data listing without launching the TUI.",
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
    namespace = parser.parse_args(argv) if argv is not None else parser.parse_args()
    return CLIOptions(
        debug=namespace.debug,
        print_mode=namespace.print_mode,
        refresh=namespace.refresh,
        configure=namespace.configure,
        direct=namespace.direct,
    )


def _should_wrap_interactive_start(options: CLIOptions) -> bool:
    return (
        not options.direct
        and not options.print_mode
        and not options.configure
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
            f"{item.repo}#{item.number:<5} "
            f"{item.created_at.date().isoformat()} "
            f"{f'* {item.title}' if suggestion_marker else item.title}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the app entrypoint with backend data orchestration."""

    options = _parse_args(argv)
    if options.debug:
        logging.basicConfig(level=logging.DEBUG)

    if _should_wrap_interactive_start(options):
        try:
            exec_zellij_wrapped_workdash(argv)
        except RuntimeError as error:
            print(f"Error: {error}", file=sys.stderr, flush=True)
            return 1

    if options.configure:
        configure()
        return 0

    if shutil.which("gh") is None:
        print(
            "Error: gh CLI is not installed or not on PATH. Install it from https://cli.github.com/",
            file=sys.stderr,
            flush=True,
        )
        return 1

    config = load_config()
    missing = validate_config(config)
    if missing:
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
        return 1

    backend = WorkdashBackend(config=config)
    if not options.print_mode:
        print("Loading work items from GitHub...", flush=True)
        work_items, suggestion_markers = backend.load_items(
            progress_callback=lambda message: print(message, flush=True)
        )
    else:
        work_items, suggestion_markers = backend.load_items()

    if options.print_mode:
        _print_work_items(work_items, suggestion_markers)
        return 0

    def _analyze(item, tool="codex"):
        ensure_worktree(config.workdir, item)
        return backend.analyze_item(item, tool=tool)

    def _launch(item: WorkItem, tool: str = "codex") -> None:
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
            launch_vscode_context(repo_path, prompt)
        else:
            launch_agent_context(
                repo_path,
                prompt,
                agent_command_tokens=shlex.split(
                    config.claude.launch if tool == "claude" else config.codex.launch
                ),
            )

    app = WorkdashApp(
        work_items=work_items,
        suggestion_markers=suggestion_markers,
        open_callback=lambda item: open_in_browser(item.url),
        refresh_callback=backend.load_items,
        worktree_callback=lambda item: ensure_worktree(config.workdir, item),
        analyze_callback=_analyze,
        launch_callback=_launch,
        terminal_callback=lambda item: launch_terminal_context(
            ensure_worktree(config.workdir, item)
        ),
        include_callback=backend.include_item_by_url,
    )
    app.run()
    return 0
