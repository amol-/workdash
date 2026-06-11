from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

from textual.widgets import DataTable, Static
from textual_diff_view import DiffView

import workdash.branchdiff as branchdiff


def test_changed_files_include_committed_modified_and_untracked_files(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)

    (tmp_path / "committed.txt").write_text("base committed\n")
    (tmp_path / "modified.txt").write_text("base modified\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "committed.txt").write_text("branch committed\n")
    subprocess.run(["git", "add", "committed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "branch change"], cwd=tmp_path, check=True)

    (tmp_path / "modified.txt").write_text("working tree modified\n")
    (tmp_path / "untracked.txt").write_text("untracked\n")

    assert set(branchdiff.get_changed_files("main", tmp_path)) == {
        "committed.txt",
        "modified.txt",
        "untracked.txt",
    }


def test_branchdiff_app_preserves_existing_colorterm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COLORTERM", "24bit")
    branchdiff.BranchDiffApp([], tmp_path, "main")

    assert os.environ["COLORTERM"] == "24bit"


def test_branchdiff_app_mounts_and_navigates_between_files(monkeypatch, tmp_path: Path) -> None:
    long_old = "".join(f"old a {index}\n" for index in range(100))
    long_new = "".join(f"new a {index}\n" for index in range(100))
    contents = {
        "a.py": (long_old, long_new),
        "b.py": ("old b\n", "new b\n"),
    }

    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return contents[filepath]

    changed_files = ["a.py", "b.py"]
    monkeypatch.setattr(branchdiff, "get_file_diff", get_file_diff)
    monkeypatch.setattr(
        branchdiff,
        "get_changed_files",
        lambda base_branch, repo_path: list(changed_files),
    )
    monkeypatch.delenv("COLORTERM", raising=False)
    app = branchdiff.BranchDiffApp(["a.py", "b.py"], tmp_path, "main")

    assert os.environ["COLORTERM"] == "truecolor"

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.screen.query_one("#file-list", DataTable)
            diff_view = app.screen.query_one("#diff-view", DiffView)

            assert table.cursor_row == 0
            assert diff_view.code_modified == long_new
            assert diff_view.annotations is True
            assert str(diff_view.line_styles["+"]) == "rgb(255,255,255) on rgb(20,63,36)"
            assert str(diff_view.line_styles["-"]) == "rgb(255,255,255) on rgb(104,0,0)"

            await pilot.press("down")
            await pilot.pause()

            assert table.cursor_row == 0
            assert diff_view.scroll_y > 0

            await pilot.press("up")
            await pilot.pause()

            assert table.cursor_row == 0
            assert diff_view.scroll_y == 0

            await pilot.press("k")
            await pilot.pause()

            assert table.cursor_row == 1
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == "new b\n"
            title = diff_view.query_one(".title", Static)
            assert "b.py" in str(title.content)

            await pilot.press("j")
            await pilot.pause()

            assert table.cursor_row == 0
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == long_new
            title = diff_view.query_one(".title", Static)
            assert "a.py" in str(title.content)

            await pilot.press("space")
            await pilot.pause()

            assert table.cursor_row == 0
            assert diff_view.scroll_y > 1

            contents["a.py"] = (long_old, "refreshed a\n")
            await pilot.press("r")
            await pilot.pause()

            assert table.cursor_row == 0
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == "refreshed a\n"

            await pilot.press("k")
            await pilot.pause()

            assert table.cursor_row == 1
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == "new b\n"

            changed_files.clear()
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("up")
            await pilot.pause()

            assert app.is_running is True

            await pilot.press("q")
            await pilot.pause()

            assert app.is_running is False
            assert app.return_code == 0

    asyncio.run(run_smoke())
