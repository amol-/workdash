from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest
from textual.widgets import DataTable
from textual_diff_view import DiffView

import workdash.branchdiff as branchdiff
from workdash.workdash import main as workdash_main


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

    assert set(branchdiff.GitHelper().get_changed_files("main", tmp_path)) == {
        "committed.txt",
        "modified.txt",
        "untracked.txt",
    }


def test_default_diff_includes_commits_on_a_branch_tracking_its_pushed_head(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "Base", "--allow-empty"], cwd=tmp_path, check=True)
    # Model the local remote-tracking refs a fetch would leave behind.
    for args in (
        ["remote", "add", "origin", "https://github.com/owner/repo.git"],
        ["update-ref", "refs/remotes/origin/main", "HEAD"],
        ["symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main"],
        ["switch", "-qc", "feature"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "pushed.txt").write_text("already pushed\n")
    for args in (
        ["add", "pushed.txt"],
        ["commit", "-qm", "Feature work"],
        ["update-ref", "refs/remotes/origin/feature", "HEAD"],
        ["branch", "--set-upstream-to=origin/feature"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)

    git = branchdiff.GitHelper()
    assert git.get_base_branch(tmp_path) == "origin/main"
    assert git.get_changed_files(None, tmp_path) == ["pushed.txt"]


@pytest.mark.parametrize("target", [None, "main"])
def test_branchdiff_from_subdirectory_uses_repo_root_for_working_tree_files(
    tmp_path: Path,
    monkeypatch,
    target: str | None,
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
        def __init__(self, files: list[str], repo_path: Path, base_branch: str, git) -> None:
            captured["files"] = files
            captured["repo_path"] = repo_path
            captured["base_branch"] = base_branch
            captured["diff"] = git.get_file_diff(files[0], base_branch, repo_path)

        def run(self) -> None:
            captured["ran"] = True

    zellij_commands: list[list[str]] = []
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if Path(command[0]).name == "zellij":
            zellij_commands.append(command)
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")
        return real_run(command, *args, **kwargs)

    monkeypatch.chdir(nested_dir)
    monkeypatch.setattr(branchdiff, "BranchDiffApp", FakeBranchDiffApp)
    monkeypatch.setattr("workdash.launcher._resolve_zellij_binary", lambda: "/test/zellij")
    monkeypatch.setattr(subprocess, "run", run)

    assert workdash_main(["branchdiff", *([target] if target else [])]) == 0

    assert zellij_commands == []
    assert captured["files"] == ["pkg/module/command.py"]
    assert captured["repo_path"] == tmp_path
    assert captured["base_branch"] == "main"
    assert captured["diff"] == ("base\n", "base\nchanged\n")
    assert captured["ran"] is True


def test_branchdiff_rejects_non_git_directory(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert workdash_main(["branchdiff"]) != 0
    assert capsys.readouterr().err == "Error: Not a git repository.\n"


def test_branchdiff_reports_no_changes_instead_of_opening_viewer(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], check=True)
    subprocess.run(["git", "commit", "-qm", "Initial commit", "--allow-empty"], check=True)
    monkeypatch.setattr(branchdiff.BranchDiffApp, "run", lambda self: pytest.fail("Viewer opened"))

    assert workdash_main(["branchdiff"]) == 0
    assert capsys.readouterr() == ("No changes found.\n", "")


def test_branchdiff_footer_advertises_copy_selected_text() -> None:
    copy_binding = next(
        binding
        for binding in branchdiff.BranchDiffScreen.BINDINGS
        if getattr(binding, "action", None) == "screen.copy_text"
    )
    assert copy_binding.key == "ctrl+c"
    assert copy_binding.description == "Copy selected text"
    assert copy_binding.show is True


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

    monkeypatch.setattr(
        branchdiff.GitHelper, "get_file_diff", lambda self, *args: get_file_diff(*args)
    )
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

    monkeypatch.setattr(
        branchdiff.GitHelper, "get_file_diff", lambda self, *args: get_file_diff(*args)
    )
    monkeypatch.setattr(
        branchdiff.GitHelper, "get_changed_files", lambda self, base_branch, repo_path: files
    )
    app = branchdiff.BranchDiffApp(files, tmp_path, "main")

    async def run_smoke() -> None:
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            await pilot.press("k")

            await pilot.press("r")

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

    monkeypatch.setattr(
        branchdiff.GitHelper, "get_file_diff", lambda self, *args: get_file_diff(*args)
    )
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

            assert table.cursor_row == 1
            assert table.scroll_x == previous_scroll_x

    asyncio.run(run_smoke())


def test_branchdiff_app_scrolls_vertically_and_refreshes(monkeypatch, tmp_path: Path) -> None:
    long_old = "".join(f"old a {index}\n" for index in range(100))
    long_new = "".join(f"new a {index}\n" for index in range(100))
    contents = {
        "a.py": (long_old, long_new),
        "b.py": ("old b\n", "new b\n"),
    }

    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return contents[filepath]

    changed_files = ["a.py", "b.py"]
    monkeypatch.setattr(
        branchdiff.GitHelper, "get_file_diff", lambda self, *args: get_file_diff(*args)
    )
    monkeypatch.setattr(
        branchdiff.GitHelper,
        "get_changed_files",
        lambda self, base_branch, repo_path: list(changed_files),
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

            assert table.cursor_row == 0
            assert diff_view.scroll_y > 0

            await pilot.press("up")

            assert table.cursor_row == 0
            assert diff_view.scroll_y == 0

            await pilot.press("space")

            assert table.cursor_row == 0
            assert diff_view.scroll_y > 1

            contents["a.py"] = (long_old, "refreshed a\n")
            await pilot.press("r")

            assert table.cursor_row == 0
            diff_view = app.screen.query_one("#diff-view", DiffView)
            assert diff_view.code_modified == "refreshed a\n"

            await pilot.press("k")
            changed_files.clear()
            await pilot.press("r")
            await pilot.press("down")
            await pilot.press("space")
            await pilot.press("up")

            assert app.is_running is True

            await pilot.press("q")

            assert app.is_running is False
            assert app.return_code == 0

    asyncio.run(run_smoke())


def test_branchdiff_erases_the_display_when_the_pane_is_resized(
    monkeypatch,
    tmp_path: Path,
) -> None:
    def get_file_diff(filepath: str, base_branch: str, repo_path: Path) -> tuple[str, str]:
        return ("old\n", "new\n")

    monkeypatch.setattr(
        branchdiff.GitHelper, "get_file_diff", lambda self, *args: get_file_diff(*args)
    )
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


def test_branchdiff_uses_one_replaceable_git_for_initial_display_and_refresh(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    git = MagicMock()
    git.get_repo_root.return_value = tmp_path
    git.get_base_branch.return_value = "main"
    git.get_changed_files.return_value = ["a.py"]
    git.get_file_diff.side_effect = [("old\n", "first\n"), ("old\n", "refreshed\n")]
    factory = MagicMock(return_value=git)
    monkeypatch.setattr(branchdiff, "GitHelper", factory)
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected subprocess")
    )

    def run(app):
        async def exercise():
            async with app.run_test() as pilot:
                await pilot.pause()
                assert app.screen.query_one("#diff-view", DiffView).code_modified == "first\n"
                await pilot.press("r")
                assert app.screen.query_one("#diff-view", DiffView).code_modified == "refreshed\n"

        asyncio.run(exercise())

    monkeypatch.setattr(branchdiff.BranchDiffApp, "run", run)
    assert branchdiff.run_branchdiff() == 0
    assert factory.call_count == 1
    assert git.get_changed_files.call_count == 2
    assert git.get_file_diff.call_count == 2
