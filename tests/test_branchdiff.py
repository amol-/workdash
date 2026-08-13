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


def test_branchdiff_from_subdirectory_uses_repo_root_for_working_tree_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)

    nested_dir = tmp_path / "pkg" / "module"
    nested_dir.mkdir(parents=True)
    changed_file = nested_dir / "command.py"
    changed_file.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    changed_file.write_text("base\nchanged\n", encoding="utf-8")

    captured: dict[str, object] = {}

    class FakeBranchDiffApp:
        def __init__(self, files: list[str], repo_path: Path, base_branch: str) -> None:
            captured["files"] = files
            captured["repo_path"] = repo_path
            captured["base_branch"] = base_branch
            captured["diff"] = branchdiff.get_file_diff(files[0], base_branch, repo_path)

        def run(self) -> None:
            captured["ran"] = True

    monkeypatch.chdir(nested_dir)
    monkeypatch.setattr(branchdiff, "BranchDiffApp", FakeBranchDiffApp)

    assert branchdiff.run_branchdiff("main") == 0

    assert captured["files"] == ["pkg/module/command.py"]
    assert captured["repo_path"] == tmp_path
    assert captured["base_branch"] == "main"
    assert captured["diff"] == ("base\n", "base\nchanged\n")
    assert captured["ran"] is True


def test_branchdiff_app_preserves_existing_colorterm(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("COLORTERM", "24bit")
    branchdiff.BranchDiffApp([], tmp_path, "main")

    assert os.environ["COLORTERM"] == "24bit"


def test_branchdiff_groups_scroll_arrow_bindings_in_footer() -> None:
    bindings_by_action = {
        binding.action: binding
        for binding in branchdiff.BranchDiffScreen.BINDINGS
        if hasattr(binding, "action")
    }

    scroll_bindings = [
        bindings_by_action["scroll_diff_up"],
        bindings_by_action["scroll_diff_down"],
        bindings_by_action["scroll_diff_left"],
        bindings_by_action["scroll_diff_right"],
    ]

    assert [binding.key_display for binding in scroll_bindings] == ["↑", "↓", "←", "→"]
    assert {binding.description for binding in scroll_bindings} == {"Scroll diff"}
    assert {binding.group.description for binding in scroll_bindings} == {"Scroll diff"}
    assert {binding.group.compact for binding in scroll_bindings} == {True}


def test_branchdiff_left_and_right_arrows_scroll_wide_diff(
    monkeypatch,
    tmp_path: Path,
) -> None:
    contents = {
        "wide.py": (
            "old = 'short'\n",
            f"new = '{'x' * 200}'\n",
        ),
    }

    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return contents[filepath]

    monkeypatch.setattr(branchdiff, "get_file_diff", get_file_diff)
    app = branchdiff.BranchDiffApp(["wide.py"], tmp_path, "main")

    async def run_smoke() -> None:
        async with app.run_test(size=(50, 20)) as pilot:
            await pilot.pause()
            diff_view = app.screen.query_one("#diff-view", DiffView)
            scroll_container = diff_view.query("DiffScrollContainer").first()
            assert scroll_container.max_scroll_x > 0
            assert scroll_container.scroll_x == 0

            await pilot.press("right")
            await pilot.pause()

            assert scroll_container.scroll_x > 0

            await pilot.press("left")
            await pilot.pause()

            assert scroll_container.scroll_x == 0

    asyncio.run(run_smoke())


def test_branchdiff_file_list_keeps_horizontal_scroll_when_clicking_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    long_a = "src/workdash/very/long/path/that/needs/horizontal/scroll/a.py"
    long_b = "src/workdash/very/long/path/that/needs/horizontal/scroll/b.py"
    contents = {
        long_a: ("old a\n", "new a\n"),
        long_b: ("old b\n", "new b\n"),
    }

    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return contents[filepath]

    monkeypatch.setattr(branchdiff, "get_file_diff", get_file_diff)
    app = branchdiff.BranchDiffApp([long_a, long_b], tmp_path, "main")

    async def run_smoke() -> None:
        async with app.run_test(size=(50, 20)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#file-list", DataTable)
            table.scroll_x = 12
            await pilot.pause()

            previous_scroll_x = table.scroll_x
            assert previous_scroll_x > 0

            await pilot.click("#file-list", offset=(2, 2))
            await pilot.pause()

            assert table.cursor_row == 1
            assert table.scroll_x == previous_scroll_x
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == "new b\n"

    asyncio.run(run_smoke())


def test_branchdiff_refresh_keeps_one_diff_view_when_selected_file_is_replaced(
    monkeypatch,
    tmp_path: Path,
) -> None:
    files = ["a.py", "b.py"]
    contents = {
        "a.py": ("old a\n", "new a\n"),
        "b.py": ("old b\n", "new b\n"),
    }

    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return contents[filepath]

    monkeypatch.setattr(branchdiff, "get_file_diff", get_file_diff)
    monkeypatch.setattr(branchdiff, "get_changed_files", lambda base_branch, repo_path: files)
    app = branchdiff.BranchDiffApp(files, tmp_path, "main")

    async def run_smoke() -> None:
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            await pilot.press("k")
            await pilot.pause()

            await pilot.press("r")
            await pilot.pause()

            diff_views = list(app.screen.query("#diff-view"))
            assert len(diff_views) == 1
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == "new b\n"

    asyncio.run(run_smoke())


def test_branchdiff_file_list_keeps_horizontal_scroll_when_switching_files(
    monkeypatch,
    tmp_path: Path,
) -> None:
    long_a = "src/workdash/very/long/path/that/needs/horizontal/scroll/a.py"
    long_b = "src/workdash/very/long/path/that/needs/horizontal/scroll/b.py"
    contents = {
        long_a: ("old a\n", "new a\n"),
        long_b: ("old b\n", "new b\n"),
    }

    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return contents[filepath]

    monkeypatch.setattr(branchdiff, "get_file_diff", get_file_diff)
    app = branchdiff.BranchDiffApp([long_a, long_b], tmp_path, "main")

    async def run_smoke() -> None:
        async with app.run_test(size=(50, 20)) as pilot:
            await pilot.pause()
            table = app.screen.query_one("#file-list", DataTable)
            table.scroll_x = 12
            await pilot.pause()

            previous_scroll_x = table.scroll_x
            assert previous_scroll_x > 0

            await pilot.press("k")
            await pilot.pause()

            assert table.cursor_row == 1
            assert table.scroll_x == previous_scroll_x

    asyncio.run(run_smoke())


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


def test_branchdiff_erases_the_display_when_the_pane_is_resized(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return ("old\n", "new\n")

    monkeypatch.setattr(branchdiff, "get_file_diff", get_file_diff)
    app = branchdiff.BranchDiffApp(["a.py", "b.py"], tmp_path, "main")

    async def run_smoke() -> None:
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            written: list[str] = []
            monkeypatch.setattr(app._driver, "write", written.append)

            await pilot.resize_terminal(120, 40)
            await pilot.pause()

            assert "\x1b[2J" in written

    asyncio.run(run_smoke())
