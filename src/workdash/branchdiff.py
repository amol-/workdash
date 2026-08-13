"""Standalone CLI command for viewing branch diffs.

This module provides the `workdash branchdiff` CLI command that displays a
side-by-side diff viewer for the current git repository. It compares the current
branch against the repository default branch (or a specified target) and shows
committed, working-tree, and untracked changes.

The command is spawned by workdash's TUI when the user presses 'd'.
Workdash only needs to ensure the worktree exists and spawn this command.

Uses textual-diff-view for the actual side-by-side diff rendering.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.color import Color
from textual.containers import Container, Horizontal
from textual.screen import Screen
from textual.style import Style
from textual.widgets import DataTable, Footer, Static
from textual_diff_view import DiffView

_SCROLL_DIFF_BINDING_GROUP = Binding.Group("Scroll diff", compact=True)


def get_repo_root(repo_path: Path | None = None) -> Path:
    """Get the repository root for any path inside a git worktree."""
    if repo_path is None:
        repo_path = Path.cwd()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError("Not a git repository.") from error
    return Path(result.stdout.strip())


def get_base_branch(repo_path: Path | None = None) -> str:
    """Get the branch the current branch is compared against."""
    if repo_path is None:
        repo_path = Path.cwd()

    original_cwd = Path.cwd()
    try:
        os.chdir(repo_path)

        # The remote default branch, not this branch's upstream: once a branch is
        # pushed its upstream is that same branch, which would diff it against
        # itself. Prefer the remote-tracking ref so a stale local default branch
        # cannot drag the merge base back and mix other people's commits in.
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
                capture_output=True,
                text=True,
                check=True,
            )
            default_branch = result.stdout.strip()
            if default_branch:
                return default_branch
        except subprocess.CalledProcessError:
            pass

        # Fallback: try main/master
        for candidate in ["main", "master"]:
            try:
                subprocess.run(
                    ["git", "show-ref", "--verify", f"refs/heads/{candidate}"],
                    capture_output=True,
                    check=True,
                )
                return candidate
            except subprocess.CalledProcessError:
                continue

        raise ValueError("Cannot determine base branch")

    finally:
        os.chdir(original_cwd)


def get_merge_base(base_branch: str, repo_path: Path | None = None) -> str:
    """Get the merge base between HEAD and the base branch."""
    if repo_path is None:
        repo_path = Path.cwd()

    original_cwd = Path.cwd()
    try:
        os.chdir(repo_path)
        result = subprocess.run(
            ["git", "merge-base", "HEAD", base_branch],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as error:
        raise RuntimeError(f"Failed to get merge base: {error.stderr}") from error
    finally:
        os.chdir(original_cwd)


def get_changed_files(
    base_branch: str | None,
    repo_path: Path | None = None,
) -> list[str]:
    """Get list of committed, working-tree, and untracked changed files."""
    if repo_path is None:
        repo_path = Path.cwd()

    if base_branch is None:
        base_branch = get_base_branch(repo_path)

    original_cwd = Path.cwd()
    try:
        os.chdir(repo_path)

        # Get diff against merge base (only our changes, not upstream changes)
        merge_base = get_merge_base(base_branch, repo_path)
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", merge_base],
                capture_output=True,
                text=True,
                check=True,
            )
            files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"Failed to get diff: {error.stderr}") from error

        # Include untracked files
        try:
            result = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True,
                text=True,
                check=True,
            )
            untracked = result.stdout.strip().split("\n") if result.stdout.strip() else []
            files.extend(untracked)
        except subprocess.CalledProcessError:
            pass

        return [f for f in files if f and f.strip()]

    finally:
        os.chdir(original_cwd)


def get_file_content_at_ref(filepath: str, ref: str, repo_path: Path) -> str:
    """Get file content at a specific git reference."""
    original_cwd = Path.cwd()
    try:
        os.chdir(repo_path)

        try:
            result = subprocess.run(
                ["git", "show", f"{ref}:{filepath}"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout
        except subprocess.CalledProcessError:
            return ""

    finally:
        os.chdir(original_cwd)


def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
    """Get old and new content for a specific file."""
    # Get merge base to compare against branching point, not upstream tip
    merge_base = get_merge_base(base_branch, repo_path)
    old_content = get_file_content_at_ref(filepath, merge_base, repo_path)

    file_path = repo_path / filepath
    try:
        new_content = file_path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError):
        try:
            result = subprocess.run(
                ["git", "show", f":{filepath}"],
                capture_output=True,
                text=True,
                check=True,
            )
            new_content = result.stdout
        except subprocess.CalledProcessError:
            new_content = ""

    return old_content, new_content


class WorkdashDiffView(DiffView):
    """DiffView with stronger added/removed line highlighting."""

    def _update_styles(self) -> None:
        super()._update_styles()
        self._line_styles["+"] = Style.parse("white on #143f24")
        self._line_styles["-"] = Style.parse("white on #680000")
        self._annotation_styles["+"] = Style(
            foreground=Color.parse("#7ee787"),
            bold=True,
        )
        self._annotation_styles["-"] = Style(
            foreground=Color.parse("#ff7b72"),
            bold=True,
        )


class FileListTable(DataTable):
    """File list table that keeps long path scroll while changing rows."""

    def _scroll_cursor_into_view(self, animate: bool = False) -> None:
        scroll_x = self.scroll_x
        super()._scroll_cursor_into_view(animate=False)
        self.scroll_x = scroll_x
        self.call_after_refresh(setattr, self, "scroll_x", scroll_x)


class BranchDiffScreen(Screen[None]):
    """Textual screen for displaying branch diff with file navigation.

    Two-panel layout:
    - Left: List of changed files
    - Right: textual-diff-view.DiffView showing side-by-side diff
    """

    DEFAULT_CSS = """
    #diff-layout {
        width: 1fr;
        height: 1fr;
    }

    #file-list {
        width: 1fr;
        height: 1fr;
    }

    #diff-pane {
        width: 4fr;
        height: 1fr;
        overflow-y: auto;
    }

    #diff-view {
        width: 1fr;
        height: 1fr;
    }
    """

    BINDINGS = [
        ("k", "next_file", "Next file"),
        ("j", "previous_file", "Previous file"),
        Binding(
            "up",
            "scroll_diff_up",
            "Scroll diff",
            key_display="↑",
            priority=True,
            group=_SCROLL_DIFF_BINDING_GROUP,
        ),
        Binding(
            "down",
            "scroll_diff_down",
            "Scroll diff",
            key_display="↓",
            priority=True,
            group=_SCROLL_DIFF_BINDING_GROUP,
        ),
        Binding(
            "left",
            "scroll_diff_left",
            "Scroll diff",
            key_display="←",
            priority=True,
            group=_SCROLL_DIFF_BINDING_GROUP,
        ),
        Binding(
            "right",
            "scroll_diff_right",
            "Scroll diff",
            key_display="→",
            priority=True,
            group=_SCROLL_DIFF_BINDING_GROUP,
        ),
        Binding("space", "scroll_diff_page_down", "Scroll diff page", priority=True),
        Binding("ctrl+c", "screen.copy_text", "Copy selected text"),
        ("r", "refresh_diff", "Refresh"),
        ("q", "quit", "Close"),
        ("enter", "view_file", "View file"),
    ]

    def __init__(self, files: list[str], repo_path: Path, base_branch: str) -> None:
        super().__init__()
        self._files = files
        self._repo_path = repo_path
        self._base_branch = base_branch
        self._selected_index = 0
        self._cache: dict[str, tuple[str, str]] = {}
        self._replace_diff_view_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        with Horizontal(id="diff-layout"):
            yield FileListTable(id="file-list")
            with Container(id="diff-pane"):
                yield self._make_diff_view(0)
        yield Footer()

    def on_mount(self) -> None:
        file_list = self.query_one("#file-list", DataTable)
        file_list.add_column("Changed Files", key="filename")
        for filepath in self._files:
            file_list.add_row(filepath)
        file_list.focus()
        if self._files:
            file_list.move_cursor(row=0)

    async def action_next_file(self) -> None:
        file_list = self.query_one("#file-list", DataTable)
        if file_list.cursor_row is not None and file_list.cursor_row < len(self._files) - 1:
            next_row = file_list.cursor_row + 1
            scroll_x = file_list.scroll_x
            file_list.move_cursor(row=next_row)
            self._restore_file_list_scroll_x(file_list, scroll_x)
            await self._select_file(next_row)

    async def action_previous_file(self) -> None:
        file_list = self.query_one("#file-list", DataTable)
        if file_list.cursor_row is not None and file_list.cursor_row > 0:
            previous_row = file_list.cursor_row - 1
            scroll_x = file_list.scroll_x
            file_list.move_cursor(row=previous_row)
            self._restore_file_list_scroll_x(file_list, scroll_x)
            await self._select_file(previous_row)

    async def action_view_file(self) -> None:
        file_list = self.query_one("#file-list", DataTable)
        if file_list.cursor_row is not None:
            await self._select_file(file_list.cursor_row)

    def action_scroll_diff_down(self) -> None:
        diff_view = self._diff_view()
        if diff_view is not None:
            diff_view.scroll_down(animate=False, force=True)

    def action_scroll_diff_up(self) -> None:
        diff_view = self._diff_view()
        if diff_view is not None:
            diff_view.scroll_up(animate=False, force=True)

    def action_scroll_diff_right(self) -> None:
        self._scroll_diff_horizontally(1)

    def action_scroll_diff_left(self) -> None:
        self._scroll_diff_horizontally(-1)

    def action_scroll_diff_page_down(self) -> None:
        diff_view = self._diff_view()
        if diff_view is not None:
            diff_view.scroll_page_down(animate=False, force=True)

    async def action_refresh_diff(self) -> None:
        previous_file = self._files[self._selected_index] if self._files else None
        self._files = get_changed_files(self._base_branch, self._repo_path)
        self._cache.clear()

        file_list = self.query_one("#file-list", DataTable)
        scroll_x = file_list.scroll_x
        file_list.clear(columns=False)
        for filepath in self._files:
            file_list.add_row(filepath)

        if previous_file in self._files:
            next_index = self._files.index(previous_file)
        else:
            next_index = min(self._selected_index, len(self._files) - 1)
        self._selected_index = max(next_index, 0)

        if self._files:
            file_list.move_cursor(row=self._selected_index)
            self._restore_file_list_scroll_x(file_list, scroll_x)
        file_list.focus()
        await self._replace_diff_view(self._selected_index)

    async def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "file-list":
            await self._select_file(event.cursor_row)

    async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "file-list":
            await self._select_file(event.cursor_row)

    async def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        if event.data_table.id == "file-list":
            await self._select_file(event.coordinate.row)

    async def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        if event.data_table.id == "file-list":
            await self._select_file(event.coordinate.row)

    def action_quit(self) -> None:
        self.app.exit()

    def on_resize(self, event: events.Resize) -> None:
        # Textual only repaints the cells its compositor believes changed. A terminal
        # multiplexer shifts the pane contents when the pane is resized, which Textual
        # cannot see, so those rows are never rewritten and the file list is left
        # showing duplicated and stale rows. Erase the display so the repaint that
        # follows this resize has to draw every cell, the way vim redraws on SIGWINCH.
        self.app._driver.write("\x1b[2J")

    # TODO(EVO-002): Handle binary files and encoding errors gracefully
    # Why: Some files cannot be displayed as text (binary, encoding issues).
    # Done: get_file_diff handles FileNotFoundError and UnicodeDecodeError by returning empty strings.
    # Non-Goals: Do not add file type detection or conversion - just skip unreadable files.

    def _scroll_diff_horizontally(self, delta: int) -> None:
        diff_view = self._diff_view()
        if diff_view is None:
            return

        scroll_containers = list(diff_view.query("DiffScrollContainer"))
        if not scroll_containers:
            return

        scroll_x = max(container.scroll_x for container in scroll_containers) + delta
        for container in scroll_containers:
            container.scroll_to(x=scroll_x, animate=False, force=True)

    def _restore_file_list_scroll_x(self, file_list: DataTable, scroll_x: float) -> None:
        file_list.scroll_x = scroll_x
        file_list.call_after_refresh(setattr, file_list, "scroll_x", scroll_x)

    def _get_file_diff_cached(self, filepath: str) -> tuple[str, str]:
        if filepath not in self._cache:
            self._cache[filepath] = get_file_diff(filepath, self._base_branch, self._repo_path)
        return self._cache[filepath]

    def _diff_view(self) -> DiffView | None:
        widget = self.query_one("#diff-view")
        return widget if isinstance(widget, DiffView) else None

    def _make_diff_view(self, index: int) -> DiffView | Static:
        if not self._files:
            return Static(id="diff-view")

        filepath = self._files[index]
        old_content, new_content = self._get_file_diff_cached(filepath)

        return WorkdashDiffView(
            filepath,
            filepath,
            old_content,
            new_content,
            annotations=True,
            id="diff-view",
        )

    async def _select_file(self, index: int) -> None:
        """Display the diff for a file-list row."""
        if not 0 <= index < len(self._files):
            return
        if index == self._selected_index:
            return

        self._selected_index = index
        await self._replace_diff_view(index)

    async def _replace_diff_view(self, index: int) -> None:
        async with self._replace_diff_view_lock:
            diff_pane = self.query_one("#diff-pane", Container)
            await diff_pane.remove_children()
            await diff_pane.mount(self._make_diff_view(index))


class BranchDiffApp(App[None]):
    """Standalone Textual app for branch diff viewing."""

    TITLE = "workdash branchdiff"

    def __init__(self, files: list[str], repo_path: Path, base_branch: str) -> None:
        os.environ.setdefault("COLORTERM", "truecolor")
        super().__init__()
        self._files = files
        self._repo_path = repo_path
        self._base_branch = base_branch

    def on_mount(self) -> None:
        self.push_screen(BranchDiffScreen(self._files, self._repo_path, self._base_branch))


def run_branchdiff(target: str | None = None) -> int:
    """Run the branchdiff TUI for the current git repository."""
    try:
        repo_path = get_repo_root()
    except RuntimeError:
        print("Error: Not a git repository.", file=sys.stderr)
        return 1

    if target is None:
        target = get_base_branch(repo_path)

    files = get_changed_files(target, repo_path)

    if not files:
        print("No changes found.")
        return 0

    app = BranchDiffApp(files, repo_path, target)
    app.run()
    return 0
