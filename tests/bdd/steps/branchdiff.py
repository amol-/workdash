"""BDD step definitions for branchdiff feature.

Tests the standalone `workdash branchdiff` CLI command that displays
a side-by-side diff viewer for git repositories.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.branchdiff as branchdiff_module
import workdash.launcher as launcher
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.workdash import main as workdash_main
from zellij_pane_launch import mark_pane_started, pane_launch

from .common import make_work_item, modal_screen_names, run_app


@given("the default branch has the old renderer and my checkout has the new renderer")
def _renderer_branch(scenario_state, tmp_path, monkeypatch):
    repo = _create_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    renderer = repo / "renderer.txt"
    renderer.write_text("old renderer\n")
    subprocess.run(["git", "add", renderer.name], check=True)
    subprocess.run(["git", "commit", "-qm", "Old renderer"], check=True)
    subprocess.run(["git", "switch", "-qc", "feature"], check=True)
    renderer.write_text("new renderer\n")
    scenario_state["renderer"] = renderer


@given("my work is based on the release branch, which has a different renderer")
def _release_renderer(scenario_state):
    renderer = scenario_state["renderer"]
    subprocess.run(["git", "switch", "-qc", "release"], check=True)
    renderer.write_text("release renderer\n")
    subprocess.run(["git", "commit", "-qam", "Release renderer"], check=True)
    subprocess.run(["git", "switch", "-qC", "feature"], check=True)
    renderer.write_text("new renderer\n")


@given("my shell is in a tiled Zellij pane")
def _tiled_shell(scenario_state, monkeypatch):
    pane = {"id": 3, "is_plugin": False, "is_fullscreen": False}
    scenario_state["zellij_pane"] = pane
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/test/zellij")
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[0] != "/test/zellij":
            return real_run(command, *args, **kwargs)
        if "list-panes" in command:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps([pane]))
        if "toggle-fullscreen" in command:
            assert command[-2:] == ["-p", "3"]
            pane["is_fullscreen"] = not pane["is_fullscreen"]
            return subprocess.CompletedProcess(command, 0, stdout="")
        pytest.fail(f"Unexpected Zellij command: {command}")

    monkeypatch.setattr(subprocess, "run", run)


@when("I run `workdash branchdiff`")
@when("I run `workdash branchdiff` and press q to return to the shell")
@when(parsers.parse("I run `workdash branchdiff {target}`"))
def _run_branchdiff(scenario_state, monkeypatch, target=None):
    def run(app):
        async def review():
            async with app.run_test(size=(100, 20)) as pilot:
                await pilot.pause()
                scenario_state["opening_screen"] = _screen_text(app)
                if pane := scenario_state.get("zellij_pane"):
                    scenario_state["fullscreen_while_reviewing"] = pane["is_fullscreen"]
                await pilot.press("q")
                assert not app.is_running

        asyncio.run(review())

    monkeypatch.setattr(branchdiff_module.BranchDiffApp, "run", run)
    monkeypatch.setenv("COLORTERM", "truecolor")
    assert workdash_main(["branchdiff", *([target] if target else [])]) == 0


@then("the viewer opens on the renderer file with the old and new lines side by side")
def _default_diff(scenario_state):
    screen = scenario_state["opening_screen"]
    assert "renderer.txt" in screen
    assert any("old renderer" in row and "new renderer" in row for row in screen.splitlines())


@then("the viewer compares the release renderer with my new renderer")
def _release_diff(scenario_state):
    screen = scenario_state["opening_screen"]
    assert any("release renderer" in row and "new renderer" in row for row in screen.splitlines())
    assert "old renderer" not in screen


@then("the diff viewer fills the pane while I review")
def _review_fullscreen(scenario_state):
    assert "new renderer" in scenario_state["opening_screen"]
    assert scenario_state["fullscreen_while_reviewing"] is True


@then("my shell returns to its original tiled layout")
def _restore_tiled_layout(scenario_state):
    assert scenario_state["zellij_pane"]["is_fullscreen"] is False


@given("the diff viewer is open on the first of two changed files")
def _two_changed_files(scenario_state):
    scenario_state["diffs"] = {
        "a.txt": ("old renderer\n", "new renderer\n"),
        "b.txt": ("old theme\n", "new theme\n"),
    }


@given("the diff viewer is open on a changed line whose end is off screen")
def _wide_changed_line(scenario_state):
    scenario_state["diffs"] = {"wide.txt": ("short\n", "BEGIN" + "." * 18 + "END\n")}


@when("I press k, press j, and then click the second file")
def _navigate_diff_files(scenario_state, monkeypatch, tmp_path):
    async def navigate(app, pilot):
        assert "new renderer" in _screen_text(app)
        await pilot.press("k")
        second = _screen_text(app)
        await pilot.press("j")
        first = _screen_text(app)
        await pilot.click("#file-list", offset=(2, 2))
        scenario_state["navigation_screens"] = (second, first, _screen_text(app))

    _review_diff(scenario_state["diffs"], monkeypatch, tmp_path, (100, 20), navigate)


@when("I press Right to read farther along the line and Left to return")
def _scroll_wide_diff(scenario_state, monkeypatch, tmp_path):
    async def scroll(app, pilot):
        opening = _screen_text(app)
        await pilot.press(*(["right"] * 12))
        right = _screen_text(app)
        await pilot.press(*(["left"] * 12))
        scenario_state["scroll_screens"] = (opening, right, _screen_text(app))

    _review_diff(scenario_state["diffs"], monkeypatch, tmp_path, (50, 20), scroll)


@then(
    "I see the second file's changes, the first file's changes, and the second file's changes again"
)
def _see_each_file_diff(scenario_state):
    for screen, current, other in zip(
        scenario_state["navigation_screens"],
        ("new theme", "new renderer", "new theme"),
        ("new renderer", "new theme", "new renderer"),
        strict=True,
    ):
        assert current in screen and other not in screen


@then("I see the end of the changed line before returning to its beginning")
def _see_wide_line_end(scenario_state):
    opening, right, left = scenario_state["scroll_screens"]
    assert "BEGIN" in opening and "END" not in opening
    assert "END" in right and "BEGIN" not in right
    assert "BEGIN" in left and "END" not in left


def _review_diff(diffs, monkeypatch, tmp_path, size, interact):
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setattr(
        branchdiff_module.GitHelper, "get_file_diff", lambda self, file, *_: diffs[file]
    )
    app = branchdiff_module.BranchDiffApp(list(diffs), tmp_path, "main")

    async def run():
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await interact(app, pilot)

    asyncio.run(run())


def _screen_text(app):
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


@given("a git repository with a known upstream branch")
def _git_repo_with_upstream(
    scenario_state: dict[str, Any],
    tmp_path: Path,
) -> None:
    """Create a git repo with upstream branch."""
    repo_path = _create_git_repo(tmp_path)

    # Create feature branch
    subprocess.run(
        ["git", "checkout", "-b", "feature"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "push", "-u", "origin", "feature"],
        cwd=repo_path,
        check=False,
        capture_output=True,
    )  # May fail without remote, but that's ok

    subprocess.run(
        ["git", "checkout", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    scenario_state["repo_path"] = repo_path


@given("the TUI has a pull request work item selected")
def _tui_has_pull_request_selected(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
) -> None:
    """Select a pull request row for branchdiff launch scenarios."""
    if not work_items:
        work_items.append(
            make_work_item(
                item_type=WorkItemType.PR,
                kind=WorkItemKind.AUTHORED_PR,
                number=9,
                title="Branchdiff PR",
                url="https://github.com/owner/repo/pull/9",
            )
        )
    scenario_state.setdefault("selected_item", work_items[0])


@given(parsers.parse('GitHub reports the PR base branch "{base_branch}"'))
def _github_reports_pr_base_branch(base_branch: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["branchdiff_pr_base_branch"] = base_branch


@given("the worktree for that item exists")
def _worktree_for_item_exists(scenario_state: dict[str, Any]) -> None:
    scenario_state["worktree_exists"] = True


@then("Workdash has prepared the worktree for the selected item")
def _workdash_prepared_worktree_for_selected_item(scenario_state: dict[str, Any]) -> None:
    selected_item = scenario_state["selected_item"]
    assert scenario_state.get("branchdiff_worktree_calls") == [selected_item]


@then("Workdash asks GitHub for that PR's base branch")
def _workdash_asks_github_for_pr_base_branch(scenario_state: dict[str, Any]) -> None:
    selected_item = scenario_state["selected_item"]
    assert scenario_state.get("branchdiff_gh_commands") == [
        [
            "gh",
            "pr",
            "view",
            str(selected_item.number),
            "--repo",
            selected_item.repo,
            "--json",
            "baseRefName,headRepository,headRepositoryOwner",
        ]
    ]


@then("GitHub is not queried for a PR base branch")
def _github_is_not_queried_for_pr_base_branch(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("branchdiff_gh_commands") == []


@then("a new zellij pane opens")
def _new_zellij_pane_opens(scenario_state: dict[str, Any]) -> None:
    commands = scenario_state.get("branchdiff_zellij_commands", [])
    assert len(commands) == 1, commands
    command = commands[0]
    assert Path(command[0]).name == "zellij"
    assert command[1:3] == ["action", "new-pane"]


@then('the pane runs "workdash branchdiff" in the worktree directory')
def _pane_runs_branchdiff_in_worktree(scenario_state: dict[str, Any]) -> None:
    _assert_branchdiff_pane_command(scenario_state, "workdash branchdiff")


@then(
    parsers.parse('the pane runs "workdash branchdiff {target_branch}" in the worktree directory')
)
def _pane_runs_branchdiff_with_target_in_worktree(
    target_branch: str,
    scenario_state: dict[str, Any],
) -> None:
    _assert_branchdiff_pane_command(scenario_state, f"workdash branchdiff {target_branch}")


def _assert_branchdiff_pane_command(scenario_state: dict[str, Any], shell_text: str) -> None:
    zellij_arguments, pane_command = pane_launch(scenario_state["branchdiff_zellij_commands"][0])
    cwd_index = zellij_arguments.index("--cwd")
    assert zellij_arguments[cwd_index : cwd_index + 2] == [
        "--cwd",
        scenario_state["branchdiff_worktree_path"],
    ]
    assert pane_command == ["/bin/bash", "-ic", shell_text]


@then("the diff viewer displays the PR changes")
def _diff_viewer_displays_pr_changes(scenario_state: dict[str, Any]) -> None:
    command_text = " ".join(scenario_state.get("branchdiff_zellij_commands", [[]])[0])
    assert "workdash branchdiff" in command_text


@then("the TUI reports that the diff viewer was opened")
def _tui_reports_diff_viewer_opened(scenario_state: dict[str, Any]) -> None:
    assert "Opened diff for pr owner/repo#9." in scenario_state["branchdiff_status"]


@then("no diff viewer pane is opened")
def _no_diff_viewer_pane_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("branchdiff_zellij_commands") == []


@then("no progress overlay remains")
def _no_progress_overlay_remains(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("modal_screen_names") == []


def run_branchdiff_tui_scenario(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Drive pressing 'd' in the TUI and record worktree + zellij calls."""
    import workdash.launcher as launcher_module

    zellij_commands: list[list[str]] = []
    gh_commands: list[list[str]] = []
    worktree_calls: list[WorkItem] = []
    captured: dict[str, Any] = {}
    worktree_path = tmp_path / "wt"
    worktree_path.mkdir(exist_ok=True)

    def fake_which(name: str) -> str | None:
        if name == "zellij":
            return f"/usr/bin/{name}"
        return None

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "pr", "view"]:
            gh_commands.append(command)
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "baseRefName": scenario_state.get("branchdiff_pr_base_branch", "main"),
                        "headRepository": {"name": "repo"},
                        "headRepositoryOwner": {"login": "owner"},
                    }
                ),
                stderr="",
            )
        zellij_commands.append(command)
        mark_pane_started(command, "23")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    def worktree_callback(item: WorkItem) -> str:
        if scenario_state.get("worktree_fails"):
            raise RuntimeError("worktree failed")
        worktree_calls.append(item)
        return str(worktree_path)

    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(launcher_module.shutil, "which", fake_which)
    monkeypatch.setattr(launcher_module.subprocess, "run", fake_run)

    async def interactions(app, pilot) -> None:
        await pilot.press("c", "d")
        for _ in range(40):
            await pilot.pause()
            status = app.query_one("#status-footer").render().plain
            if (
                zellij_commands
                or "Worktree setup failed" in status
                or "Failed to open diff" in status
            ):
                break
        captured["status"] = app.query_one("#status-footer").render().plain
        captured["modal_screen_names"] = modal_screen_names(app)

    run_app(
        work_items=list(work_items),
        worktree_callback=worktree_callback,
        interactions=interactions,
    )
    scenario_state["branchdiff_worktree_calls"] = worktree_calls
    scenario_state["branchdiff_worktree_path"] = str(worktree_path)
    scenario_state["branchdiff_gh_commands"] = gh_commands
    scenario_state["branchdiff_zellij_commands"] = zellij_commands
    scenario_state["branchdiff_status"] = captured["status"]
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def scenario_state() -> dict[str, Any]:
    """Generic bucket for per-scenario state across step functions."""
    return {}


# -- Git repository helpers -------------------------------------------------


def _create_git_repo(tmp_path: Path, initial_commit: bool = True) -> Path:
    """Create a git repository at the given path."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir(parents=True, exist_ok=True)

    # Set up minimal repository for diff testing
    subprocess.run(["git", "init", "-b", "main"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    if initial_commit:
        # Establish initial commit as baseline for comparison
        (repo_path / "README.md").write_text("# Initial content\n")
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

    return repo_path
