"""Standalone CLI command for viewing branch diffs.

This module provides the `workdash branchdiff` CLI command that displays a
side-by-side diff viewer for the current git repository. It compares the current
branch against its upstream (or a specified target) and shows both committed
and uncommitted changes.

The command is spawned by workdash's TUI when the user presses 'd'.
Workdash only needs to ensure the worktree exists and spawn this command.

Uses textual-diff-view for the actual side-by-side diff rendering.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

# Textual UI components
try:
    from textual.app import App, ComposeResult
    from textual.containers import Grid
    from textual.screen import Screen
    from textual.widgets import DataTable, Footer
    from textual_diff_view import DiffView

    HAS_TEXTUAL = True
    HAS_DIFF_VIEW = True
except ModuleNotFoundError as error:
    HAS_TEXTUAL = False
    HAS_DIFF_VIEW = False


def get_upstream_branch(repo_path: Path | None = None) -> str:
    """Get the upstream branch for the current branch."""
    if repo_path is None:
        repo_path = Path.cwd()

    original_cwd = Path.cwd()
    try:
        import os

        os.chdir(repo_path)

        # Try to get upstream branch
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{u}"],
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout.strip().split("/")[-1]
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

        raise ValueError("Cannot determine upstream branch")

    finally:
        os.chdir(original_cwd)


def get_merge_base(base_branch: str, repo_path: Path | None = None) -> str:
    """Get the merge base between HEAD and the base branch."""
    if repo_path is None:
        repo_path = Path.cwd()

    original_cwd = Path.cwd()
    try:
        import os

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
    """Get list of changed files (both committed and uncommitted)."""
    if repo_path is None:
        repo_path = Path.cwd()

    if base_branch is None:
        base_branch = get_upstream_branch(repo_path)

    original_cwd = Path.cwd()
    try:
        import os

        os.chdir(repo_path)

        # Get diff against merge base (only our changes, not upstream changes)
        merge_base = get_merge_base(base_branch, repo_path)
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", f"{merge_base}..."],
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
        import os

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


if HAS_TEXTUAL:

    class BranchDiffScreen(Screen[None]):
        """Textual screen for displaying branch diff with file navigation.

        Two-panel layout:
        - Left: List of changed files
        - Right: textual-diff-view.DiffView showing side-by-side diff
        """

        BINDINGS = [
            ("j", "next_file", "Next file"),
            ("k", "previous_file", "Previous file"),
            ("q", "dismiss", "Close"),
            ("enter", "view_file", "View file"),
        ]

        def __init__(self, files: list[str], repo_path: Path, base_branch: str) -> None:
            super().__init__()
            self._files = files
            self._repo_path = repo_path
            self._base_branch = base_branch
            self._selected_index = 0
            self._cache: dict[str, tuple[str, str]] = {}
            self._temp_files: list[str] = []

        def on_unmount(self) -> None:
            """Clean up temporary files."""
            for temp_path in self._temp_files:
                try:
                    Path(temp_path).unlink()
                except FileNotFoundError:
                    pass
            self._temp_files.clear()

        def compose(self) -> ComposeResult:
            with Grid(columns="1fr 3fr", rows="1fr", id="diff-grid"):
                yield DataTable(id="file-list")
                if HAS_DIFF_VIEW:
                    # Use textual-diff-view for proper side-by-side rendering
                    yield DiffView(id="diff-view")
                else:
                    # Fallback if textual-diff-view is not available
                    from textual.widgets import Static

                    yield Static(id="diff-view")
                yield Footer()

        def on_mount(self) -> None:
            file_list = self.query_one("#file-list", DataTable)
            file_list.add_column("Changed Files", key="filename")
            for filepath in self._files:
                file_list.add_row(filepath)
            if self._files:
                file_list.cursor_row = 0
                self._selected_index = 0
                self._update_diff_view()

        def action_next_file(self) -> None:
            file_list = self.query_one("#file-list", DataTable)
            if file_list.cursor_row is not None and file_list.cursor_row < len(self._files) - 1:
                file_list.cursor_row += 1
                self._selected_index = file_list.cursor_row
                self._update_diff_view()

        def action_previous_file(self) -> None:
            file_list = self.query_one("#file-list", DataTable)
            if file_list.cursor_row is not None and file_list.cursor_row > 0:
                file_list.cursor_row -= 1
                self._selected_index = file_list.cursor_row
                self._update_diff_view()

        def action_view_file(self) -> None:
            self._update_diff_view()

        # TODO(EVO-002): Handle binary files and encoding errors gracefully
        # Why: Some files cannot be displayed as text (binary, encoding issues).
        # Done: get_file_diff handles FileNotFoundError and UnicodeDecodeError by returning empty strings.
        # Non-Goals: Do not add file type detection or conversion - just skip unreadable files.

        def _get_file_diff_cached(self, filepath: str) -> tuple[str, str]:
            if filepath not in self._cache:
                self._cache[filepath] = get_file_diff(filepath, self._base_branch, self._repo_path)
            return self._cache[filepath]

        def _update_diff_view(self) -> None:
            """Update the diff view for the currently selected file."""
            if 0 <= self._selected_index < len(self._files):
                filepath = self._files[self._selected_index]
                old_content, new_content = self._get_file_diff_cached(filepath)

                if HAS_DIFF_VIEW:
                    # Use textual-diff-view.DiffView with file paths
                    # textual-diff-view expects to read from file paths
                    diff_view = self.query_one("#diff-view", DiffView)
                    
                    # Create temp files for old and new content
                    # so DiffView can read from paths as it expects
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".old", delete=False
                    ) as old_file:
                        old_file.write(old_content)
                        old_path = old_file.name
                    
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".new", delete=False
                    ) as new_file:
                        new_file.write(new_content)
                        new_path = new_file.name
                    
                    # Store temp file paths for cleanup
                    if not hasattr(self, "_temp_files"):
                        self._temp_files: list[str] = []
                    self._temp_files.extend([old_path, new_path])
                    
                    # Load diff from temp file paths
                    diff_view.path_original = old_path
                    diff_view.path_modified = new_path
                    # Clear any cached code to force reload from paths
                    diff_view.code_original = ""
                    diff_view.code_modified = ""
                    # Refresh to load from the new paths
                    diff_view.refresh()
                else:
                    # Fallback to Static widget
                    from textual.widgets import Static

                    diff_view = self.query_one("#diff-view", Static)
                    old_lines = old_content.splitlines()
                    new_lines = new_content.splitlines()

                    max_lines = 50
                    old_display = "\n".join(old_lines[:max_lines])
                    new_display = "\n".join(new_lines[:max_lines])

                    diff_view.update(
                        f"[bold]Diff: {filepath}[/bold]\n\n"
                        f"[dim]OLD[/dim]                          [dim]NEW[/dim]\n"
                        f"{'-' * 38}    {'-' * 38}\n"
                        f"{old_display[:2000]}\n"
                        f"{new_display[:2000]}"
                    )


    class BranchDiffApp(App[None]):
        """Standalone Textual app for branch diff viewing."""

        TITLE = "workdash branchdiff"

        def __init__(self, files: list[str], repo_path: Path, base_branch: str) -> None:
            super().__init__()
            self._files = files
            self._repo_path = repo_path
            self._base_branch = base_branch

        def on_mount(self) -> None:
            self.push_screen(BranchDiffScreen(self._files, self._repo_path, self._base_branch))


else:
    BranchDiffScreen = None  # type: ignore[misc, assignment]


def main() -> int:
    """CLI entrypoint. Parses sys.argv directly."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="workdash branchdiff",
        description="Show side-by-side diff of current branch vs upstream (or target).",
    )
    parser.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Branch to compare against (default: upstream).",
    )

    args = parser.parse_args()

    repo_path = Path.cwd()

    # Verify this is a git repository
    if not (repo_path / ".git").is_dir():
        try:
            subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            print("Error: Not a git repository.", file=sys.stderr)
            return 1

    if args.target is None:
        target = get_upstream_branch(repo_path)
    else:
        target = args.target

    files = get_changed_files(target, repo_path)

    if not files:
        print("No changes found.")
        return 0

    if not HAS_TEXTUAL:
        print("Error: textual is required. Install with: pip install textual", file=sys.stderr)
        return 1

    app = BranchDiffApp(files, repo_path, target)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
