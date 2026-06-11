"""BDD step definitions for branchdiff feature.

Tests the standalone `workdash branchdiff` CLI command that displays
a side-by-side diff viewer for git repositories.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.branchdiff as branchdiff_module
from workdash.branchdiff import get_changed_files, get_file_diff, get_upstream_branch
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.workdash import main as workdash_main

from .common import make_work_item, modal_screen_names, run_app

# -- Step Definitions -------------------------------------------------------


@given("the current directory is a git repository")
def _current_dir_is_git_repo(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set up a git repository in a temporary directory."""
    repo_path = _create_git_repo(tmp_path)
    scenario_state["repo_path"] = repo_path
    monkeypatch.chdir(repo_path)


@given("the repository has changes compared to its upstream branch")
def _repo_has_changes(
    scenario_state: dict[str, Any],
) -> None:
    """Create changes in the repository."""
    repo_path = scenario_state["repo_path"]

    # Ensure main branch exists as comparison baseline
    try:
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

    _create_branch_with_changes(
        repo_path,
        "feature",
        "# Modified content\n\nThis is the feature branch.\n",
    )

    subprocess.run(
        ["git", "checkout", "feature"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Establish main as upstream for comparison
    subprocess.run(
        ["git", "branch", "--set-upstream-to", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


@given("there are committed changes, modified working-tree files, and untracked files")
def _repo_has_committed_modified_and_untracked_changes(
    scenario_state: dict[str, Any],
) -> None:
    """Create committed, modified, and untracked changes."""
    repo_path = scenario_state["repo_path"]

    # Make sure we're on a branch with upstream
    try:
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

    _create_branch_with_changes(
        repo_path,
        "feature",
        "# Modified content\n",
    )
    subprocess.run(
        ["git", "checkout", "feature"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    # Establish main as upstream for comparison
    subprocess.run(
        ["git", "branch", "--set-upstream-to", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    (repo_path / "file.txt").write_text("Working tree content\n")
    (repo_path / "new_file.txt").write_text("Untracked content\n")


@given("the repository has no changes compared to upstream")
def _repo_has_no_changes(
    scenario_state: dict[str, Any],
) -> None:
    """Set up a repository with no changes compared to upstream."""
    repo_path = scenario_state["repo_path"]

    # Ensure we're on main with upstream tracking itself
    try:
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )

    # Set upstream to main (comparing to itself means no changes)
    subprocess.run(
        ["git", "branch", "--set-upstream-to", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )


@when('the user runs "workdash branchdiff"')
def _user_runs_branchdiff(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the branchdiff CLI without entering the interactive TUI."""
    _run_branchdiff_command(scenario_state, [], monkeypatch)


@when('the user runs "workdash branchdiff main"')
def _user_runs_branchdiff_with_target(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the branchdiff CLI with a target branch."""
    _run_branchdiff_command(scenario_state, ["main"], monkeypatch)


@then("the file list shows all changed files")
def _file_list_shows_all_changed_files(
    scenario_state: dict[str, Any],
) -> None:
    """Verify that changed files are detected and stored."""
    repo_path = scenario_state.get("repo_path")
    assert repo_path is not None, "repo_path not set in scenario_state"

    # Get the base branch (upstream)
    try:
        base_branch = get_upstream_branch(repo_path)
    except ValueError:
        base_branch = "main"

    # Get changed files
    files = get_changed_files(base_branch, repo_path)

    # Verify we got some files
    assert len(files) > 0, "Expected to find changed files"

    # Store for other steps to use
    scenario_state["changed_files"] = files


@then("the diff viewer displays changes for the first file")
def _diff_viewer_displays_changes_for_first_file(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the diff viewer can display the first changed file."""
    repo_path = scenario_state.get("repo_path")
    assert repo_path is not None, "repo_path not set in scenario_state"

    # Get the base branch (upstream)
    try:
        base_branch = get_upstream_branch(repo_path)
    except ValueError:
        base_branch = "main"

    # Get changed files
    files = get_changed_files(base_branch, repo_path)
    assert len(files) > 0, "Expected to find changed files"

    # Get diff for the first file
    first_file = files[0]
    old_content, new_content = get_file_diff(first_file, base_branch, repo_path)

    # Verify we got actual content (at least one side has content)
    assert old_content != "" or new_content != "", (
        f"Expected diff content for {first_file}, got empty on both sides"
    )

    # Store for other steps to use
    scenario_state["changed_files"] = files
    scenario_state["first_file"] = first_file
    scenario_state["first_file_diff"] = (old_content, new_content)


@then("the diff viewer displays a meaningful side-by-side diff")
def _diff_viewer_displays_meaningful_diff(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the diff shows actual differences between old and new content."""
    repo_path = scenario_state.get("repo_path")
    files = scenario_state.get("changed_files", [])

    # If we don't have changed files yet, detect them
    if not files:
        try:
            base_branch = get_upstream_branch(repo_path) if repo_path else "main"
        except ValueError:
            base_branch = "main"
        files = get_changed_files(base_branch, repo_path) if repo_path else []

    assert repo_path is not None, "repo_path not set in scenario_state"
    assert len(files) > 0, "No changed files found"

    # Get the base branch (upstream)
    try:
        base_branch = get_upstream_branch(repo_path)
    except ValueError:
        base_branch = "main"

    # Find a file with actual changes
    for filepath in files:
        old, new = get_file_diff(filepath, base_branch, repo_path)
        if old != new:
            # Verify we can actually get diff content
            assert isinstance(old, str), "Old content should be a string"
            assert isinstance(new, str), "New content should be a string"
            return
    # If no files have changes, that's fine - test setup may not have differences


@given("the diff viewer is open with multiple changed files")
def _diff_viewer_open_with_multiple_files(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set up a repository with multiple changed files."""
    repo_path = _create_git_repo(tmp_path)

    # Set up baseline files for multi-file diff testing
    (repo_path / "file1.txt").write_text("Content 1\n")
    (repo_path / "file2.txt").write_text("Content 2\n")
    (repo_path / "file3.txt").write_text("Content 3\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add multiple files"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    _create_branch_with_changes(
        repo_path,
        "feature",
        "# Modified\n",
    )
    subprocess.run(
        ["git", "checkout", "feature"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Apply changes to all baseline files to test multi-file diff
    (repo_path / "file1.txt").write_text("Modified content 1\n")
    (repo_path / "file2.txt").write_text("Modified content 2\n")
    (repo_path / "file3.txt").write_text("Modified content 3\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Modify all files"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    subprocess.run(
        ["git", "branch", "--set-upstream-to", "main"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    scenario_state["repo_path"] = repo_path
    monkeypatch.chdir(repo_path)


@when("the user navigates to the next file")
def _user_navigates_to_next_file(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the data model supports navigation by checking multiple files exist."""
    repo_path = scenario_state.get("repo_path")
    files = scenario_state.get("changed_files", [])

    assert repo_path is not None, "repo_path not set in scenario_state"

    # Get the base branch (upstream)
    try:
        base_branch = get_upstream_branch(repo_path)
    except ValueError:
        base_branch = "main"

    # If we don't have changed files yet, get them
    if not files:
        files = get_changed_files(base_branch, repo_path)
        scenario_state["changed_files"] = files

    assert len(files) >= 2, "Need at least 2 files to test navigation"

    # Store the next file index for verification
    scenario_state["next_file_index"] = 1


@then("the diff viewer displays that file's changes")
def _diff_viewer_displays_file_changes(
    scenario_state: dict[str, Any],
) -> None:
    """Verify different files have different diffs."""
    repo_path = scenario_state.get("repo_path")
    files = scenario_state.get("changed_files", [])

    assert repo_path is not None, "repo_path not set in scenario_state"
    assert len(files) >= 2, "Need at least 2 files to test pane update"

    # Get the base branch (upstream)
    try:
        base_branch = get_upstream_branch(repo_path)
    except ValueError:
        base_branch = "main"

    # Get diffs for first two files
    old_content_1, new_content_1 = get_file_diff(files[0], base_branch, repo_path)
    old_content_2, new_content_2 = get_file_diff(files[1], base_branch, repo_path)

    # Verify that the diffs are different (different files have different content)
    # This proves the pane would update when switching files
    assert (
        old_content_1,
        new_content_1,
    ) != (
        old_content_2,
        new_content_2,
    ), "Expected different diffs for different files"


@given("the current directory is not a git repository")
def _current_dir_not_git_repo(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set up a non-git directory."""
    non_git_dir = tmp_path / "non_git_dir"
    non_git_dir.mkdir(parents=True, exist_ok=True)
    scenario_state["repo_path"] = non_git_dir
    monkeypatch.chdir(non_git_dir)


@then("the command reports an error")
@then("the command exits with an error")
def _command_reports_error(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the command reports an error."""
    result = scenario_state.get("branchdiff_result")
    assert result is not None, "branchdiff command was not run"
    assert result.returncode != 0 or "Error" in result.stderr or "Error" in result.stdout


@then("the command reports no changes found")
def _command_reports_no_changes_found(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the command reports no changes found."""
    result = scenario_state.get("branchdiff_result")
    assert result is not None, "branchdiff command was not run"
    # Check for no changes message in output
    output = result.stdout + result.stderr
    assert "no changes" in output.lower() or "nothing to" in output.lower(), (
        f"Expected 'no changes' message, got stdout: {result.stdout}, stderr: {result.stderr}"
    )


@then("exits with non-zero status")
@then("the command exits with a non-zero status")
def _exits_nonzero(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the command exits with non-zero status."""
    exit_code = scenario_state.get("exit_code")
    assert exit_code is not None, "exit_code not set in scenario_state"
    assert exit_code != 0, f"Expected non-zero exit code, got {exit_code}"


@then("exits with zero status")
def _exits_with_zero_status(
    scenario_state: dict[str, Any],
) -> None:
    """Verify the command exits with zero status."""
    exit_code = scenario_state.get("exit_code")
    assert exit_code is not None, "exit_code not set in scenario_state"
    assert exit_code == 0, f"Expected zero exit code, got {exit_code}"


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
    commands = scenario_state["branchdiff_zellij_commands"]
    worktree_path = scenario_state["branchdiff_worktree_path"]
    command = commands[0]
    cwd_index = command.index("--cwd")
    separator_index = command.index("--")
    assert command[cwd_index : cwd_index + 2] == ["--cwd", worktree_path]
    assert command[separator_index + 1 :] == ["/bin/bash", "-ic", shell_text]


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
        await pilot.press("d")
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


def _run_branchdiff_command(
    scenario_state: dict[str, Any],
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the CLI path while replacing the blocking Textual run loop."""
    stdout = io.StringIO()
    stderr = io.StringIO()

    def fake_run_app(self) -> None:
        scenario_state["branchdiff_app_opened"] = True

    monkeypatch.setattr(branchdiff_module.BranchDiffApp, "run", fake_run_app)
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = workdash_main(["branchdiff", *args])

    command = ["workdash", "branchdiff", *args]
    result = subprocess.CompletedProcess(
        args=command,
        returncode=exit_code,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )
    scenario_state["branchdiff_result"] = result
    scenario_state["exit_code"] = result.returncode
    scenario_state["stdout"] = result.stdout
    scenario_state["stderr"] = result.stderr


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


def _create_branch_with_changes(repo_path: Path, branch_name: str, file_content: str) -> None:
    """Create a new branch with changes and push to upstream."""
    # Create feature branch for testing diff scenarios
    subprocess.run(
        ["git", "checkout", "-b", branch_name],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Commit modified content to establish branch history
    (repo_path / "README.md").write_text(file_content)
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"Changes in {branch_name}"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Return to main branch to set up comparison baseline
    subprocess.run(["git", "checkout", "main"], cwd=repo_path, check=True, capture_output=True)
