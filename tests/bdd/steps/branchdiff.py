"""BDD step definitions for branchdiff feature.

Tests the standalone `workdash branchdiff` CLI command that displays
a side-by-side diff viewer for git repositories.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.branchdiff as branchdiff_module
import workdash.launcher as launcher
from workdash.models import WorkItemKind, WorkItemType
from workdash.workdash import main as workdash_main
from zellij_pane_launch import mark_pane_started, pane_launch

from .common import make_work_item, run_app


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


@given("the selected pull request updates the renderer on a release branch")
def _renderer_pull_request(scenario_state, tmp_path, monkeypatch):
    # Give the default branch a different renderer so comparing against it cannot pass.
    with monkeypatch.context() as checkout:
        _renderer_branch(scenario_state, tmp_path, checkout)
        _release_renderer(scenario_state)
        subprocess.run(["git", "commit", "-qam", "Update the renderer"], check=True)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/release/2026", "release"], check=True
        )
    scenario_state["diff_item"] = make_work_item(
        item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR, number=9, title="Update renderer"
    )


@when("I press c and then d to open branchdiff")
def _open_pull_request_diff(scenario_state, monkeypatch):
    panes = []
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        if command[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "baseRefName": "release/2026",
                        "headRepository": {"name": "repo"},
                        "headRepositoryOwner": {"login": "owner"},
                    }
                ),
            )
        if command[0] == "/test/zellij":
            assert command[1:3] == ["action", "new-pane"], command
            panes.append(pane_launch(command))
            mark_pane_started(command)
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        return real_run(command, *args, **kwargs)

    item = scenario_state["diff_item"]
    checkout = scenario_state["renderer"].parent
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/test/zellij")
    monkeypatch.setattr(subprocess, "run", run)

    async def interactions(app, pilot):
        await pilot.press("c", "d")
        for _ in range(40):
            if panes:
                break
            await pilot.pause(0.05)
        assert panes, app.query_one("#status-footer").render().plain

    run_app(work_items=[item], worktree_callback=lambda _: str(checkout), interactions=interactions)

    # Stand in for Zellij executing the captured command, not for the diff viewer.
    [(options, command)] = panes
    monkeypatch.chdir(options[options.index("--cwd") + 1])
    monkeypatch.delenv("ZELLIJ_PANE_ID", raising=False)
    monkeypatch.setenv("COLORTERM", "truecolor")

    def capture_viewer(app):
        async def capture():
            async with app.run_test(size=(100, 20)) as pilot:
                for _ in range(40):
                    screen = _screen_text(app)
                    if "new renderer" in screen:
                        break
                    await pilot.pause(0.05)
                scenario_state["pr_diff_screen"] = screen

        asyncio.run(capture())

    monkeypatch.setattr(branchdiff_module.BranchDiffApp, "run", capture_viewer)
    executable, *args = shlex.split(command[-1])
    assert executable == "workdash"
    assert workdash_main(args) == 0


@then("the new diff pane shows the release renderer and the proposed renderer side by side")
def _see_pull_request_diff(scenario_state):
    screen = scenario_state["pr_diff_screen"]
    assert "renderer.txt" in screen
    assert any(
        "release renderer" in row and "new renderer" in row for row in screen.splitlines()
    ), screen
    assert "old renderer" not in screen


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
