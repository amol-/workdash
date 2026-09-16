"""BDD step definitions for the branchinfo feature.

Tests the standalone `workdash branchinfo` CLI command that reports the pull
request and closing issues for the current branch.
"""

from __future__ import annotations

import io
import json
import subprocess
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

from workdash.workdash import main as workdash_main

# -- Step Definitions -------------------------------------------------------


@given("the current directory is a git repository on a branch with an open pull request")
def _repo_on_branch_with_open_pull_request(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = _create_git_repo(tmp_path)
    monkeypatch.chdir(repo_path)
    scenario_state["repo_path"] = repo_path
    scenario_state["gh_pr"] = {
        "number": 5,
        "title": "Implement renderer",
        "url": "https://github.com/owner/repo/pull/5",
        "state": "OPEN",
    }
    scenario_state["gh_ci_state"] = None
    scenario_state["gh_review_decision"] = None
    scenario_state["gh_closing_issues"] = []


@given("the current directory is a git repository on a branch with no pull request")
def _repo_on_branch_with_no_pull_request(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = _create_git_repo(tmp_path)
    monkeypatch.chdir(repo_path)
    scenario_state["repo_path"] = repo_path
    scenario_state["gh_pr"] = None


@given("the current directory is a git repository on a branch whose pull request is merged")
def _repo_on_branch_with_merged_pull_request(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_on_branch_with_open_pull_request(scenario_state, tmp_path, monkeypatch)
    scenario_state["gh_pr"]["state"] = "MERGED"


@given("that pull request is passing CI and approved, and closes an issue in the same repository")
def _pull_request_passing_and_approved_with_same_repo_issue(
    scenario_state: dict[str, Any],
) -> None:
    scenario_state["gh_ci_state"] = "SUCCESS"
    scenario_state["gh_review_decision"] = "APPROVED"
    scenario_state["gh_closing_issues"] = [{"number": 42, "repository": "owner/repo"}]
    scenario_state["gh_issues"] = {
        42: {
            "title": "Renderer crashes on empty input",
            "url": "https://github.com/owner/repo/issues/42",
            "state": "OPEN",
        }
    }


@given("that pull request closes an already closed issue in the same repository")
def _pull_request_closes_closed_issue_in_same_repository(
    scenario_state: dict[str, Any],
) -> None:
    scenario_state["gh_closing_issues"] = [{"number": 42, "repository": "owner/repo"}]
    scenario_state["gh_issues"] = {
        42: {
            "title": "Renderer crashes on empty input",
            "url": "https://github.com/owner/repo/issues/42",
            "state": "CLOSED",
        }
    }


@given("that pull request closes two issues in the same repository")
def _pull_request_closes_two_issues_in_same_repository(
    scenario_state: dict[str, Any],
) -> None:
    scenario_state["gh_closing_issues"] = [
        {"number": 42, "repository": "owner/repo"},
        {"number": 7, "repository": "owner/repo"},
    ]
    scenario_state["gh_issues"] = {
        42: {
            "title": "Renderer crashes on empty input",
            "url": "https://github.com/owner/repo/issues/42",
            "state": "OPEN",
        },
        7: {
            "title": "Renderer ignores the theme",
            "url": "https://github.com/owner/repo/issues/7",
            "state": "CLOSED",
        },
    }


@given("that pull request closes an issue in a different repository")
def _pull_request_closes_issue_in_different_repository(
    scenario_state: dict[str, Any],
) -> None:
    scenario_state["gh_closing_issues"] = [{"number": 9, "repository": "owner/other"}]


@given("the current directory is not a git repository")
def _current_dir_not_git_repo(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_git_dir = tmp_path / "non_git_dir"
    non_git_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(non_git_dir)


@when('the user runs "workdash branchinfo"')
def _user_runs_branchinfo(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_branchinfo_command(scenario_state, monkeypatch)


@when('the user runs "workdash branchinfo" from a subdirectory of the repository')
def _user_runs_branchinfo_from_subdirectory(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subdir = scenario_state["repo_path"] / "pkg" / "module"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)
    _run_branchinfo_command(scenario_state, monkeypatch)


@then("the command reports the pull request's title, url, and a passing, approved symbol")
def _command_reports_pull_request(scenario_state: dict[str, Any]) -> None:
    stdout = scenario_state["stdout"]
    pull_request = scenario_state["gh_pr"]
    assert pull_request["title"] in stdout
    assert pull_request["url"] in stdout
    assert "✓✓" in stdout


@then("the command reports the closed issue's title and url")
def _command_reports_closed_issue(scenario_state: dict[str, Any]) -> None:
    stdout = scenario_state["stdout"]
    issue = scenario_state["gh_issues"][42]
    assert issue["title"] in stdout
    assert issue["url"] in stdout


@then("the command reports the pull request marked as merged")
def _command_reports_pull_request_marked_merged(scenario_state: dict[str, Any]) -> None:
    pull_request = scenario_state["gh_pr"]
    expected = f"PR: {pull_request['title']} {pull_request['url']}"
    assert expected in scenario_state["stdout"]
    assert "[MERGED]" in scenario_state["stdout"]


@then("the command reports the issue marked as closed")
def _command_reports_issue_marked_closed(scenario_state: dict[str, Any]) -> None:
    issue = scenario_state["gh_issues"][42]
    assert f"ISSUE: {issue['title']} {issue['url']} [CLOSED]" in scenario_state["stdout"]


@then("the command reports both issues' titles and urls")
def _command_reports_both_issues(scenario_state: dict[str, Any]) -> None:
    stdout = scenario_state["stdout"]
    for issue in scenario_state["gh_issues"].values():
        assert f"{issue['title']} {issue['url']}" in stdout
    assert stdout.count("ISSUE: ") == 2


@then("the command reports the pull request as unknown")
def _command_reports_pull_request_unknown(scenario_state: dict[str, Any]) -> None:
    assert "PR: unknown" in scenario_state["stdout"]


@then("the command reports the issue as unknown")
def _command_reports_issue_unknown(scenario_state: dict[str, Any]) -> None:
    assert "ISSUE: unknown" in scenario_state["stdout"]


@then("the command reports an error")
def _command_reports_error(scenario_state: dict[str, Any]) -> None:
    assert "Error" in scenario_state["stderr"]


@then("exits with non-zero status")
def _exits_nonzero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def scenario_state() -> dict[str, Any]:
    """Generic bucket for per-scenario state across step functions."""
    return {}


# -- Helpers ------------------------------------------------------------


def _create_git_repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=repo_path,
        check=True,
    )
    (repo_path / "README.md").write_text("# Initial content\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "Initial commit"], cwd=repo_path, check=True)
    return repo_path


def _run_branchinfo_command(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run the CLI path with `gh` calls faked via a subprocess.run monkeypatch."""
    real_run = subprocess.run

    def fake_run(command: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] != "gh":
            return real_run(command, *args, **kwargs)
        if command[:3] == ["gh", "pr", "view"]:
            pull_request = scenario_state.get("gh_pr")
            if pull_request is None:
                raise subprocess.CalledProcessError(
                    1, command, stderr=f'no pull requests found for branch "{command[3]}"'
                )
            return subprocess.CompletedProcess(
                command, returncode=0, stdout=json.dumps(pull_request), stderr=""
            )
        if command[:2] == ["gh", "api"]:
            rollup = (
                {"state": scenario_state["gh_ci_state"]} if scenario_state["gh_ci_state"] else None
            )
            payload = {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
                            "reviewDecision": scenario_state["gh_review_decision"],
                            "closingIssuesReferences": {
                                "nodes": [
                                    {
                                        "number": issue["number"],
                                        "repository": {"nameWithOwner": issue["repository"]},
                                    }
                                    for issue in scenario_state["gh_closing_issues"]
                                ]
                            },
                        }
                    }
                }
            }
            return subprocess.CompletedProcess(
                command, returncode=0, stdout=json.dumps(payload), stderr=""
            )
        if command[:3] == ["gh", "issue", "view"]:
            issue = scenario_state["gh_issues"][int(command[3])]
            return subprocess.CompletedProcess(
                command, returncode=0, stdout=json.dumps(issue), stderr=""
            )
        raise AssertionError(f"Unexpected gh command in branchinfo test: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = workdash_main(["branchinfo"])

    scenario_state["exit_code"] = exit_code
    scenario_state["stdout"] = stdout.getvalue()
    scenario_state["stderr"] = stderr.getvalue()
