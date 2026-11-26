"""BDD step definitions for the branchinfo feature.

Tests the standalone `workdash branchinfo` CLI command that reports the pull
request and closing issues for the current branch.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

from workdash.workdash import main as workdash_main

# -- Step Definitions -------------------------------------------------------


@given("I am on a Workdash issue branch with no pull request")
def _repo_on_issue_branch_with_no_pull_request(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _create_git_repo(tmp_path)
    monkeypatch.chdir(repo)
    subprocess.run(["git", "switch", "-qc", "issue-42"], check=True)
    scenario_state["gh_pr"] = None
    scenario_state["gh_issues"] = {
        42: {
            "title": "Renderer crashes on empty input",
            "url": "https://github.com/owner/repo/issues/42",
            "state": "OPEN",
        }
    }


@given("I am in a repository subdirectory on a branch with an open pull request")
def _subdirectory_on_branch_with_open_pull_request(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _repo_on_branch_with_open_pull_request(scenario_state, tmp_path, monkeypatch)
    nested = scenario_state["repo_path"] / "pkg" / "module"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)


@given("I am still on the branch of a merged pull request")
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


@when("I run `workdash branchinfo`")
def _user_runs_branchinfo(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_github_cli(scenario_state, monkeypatch)
    assert workdash_main(["branchinfo"]) == 0
    scenario_state["stdout"] = capsys.readouterr().out


@then("I see the pull request's title, URL, and passing and approved indicators")
def _command_reports_pull_request(scenario_state: dict[str, Any]) -> None:
    stdout = scenario_state["stdout"]
    pull_request = scenario_state["gh_pr"]
    assert pull_request["title"] in stdout
    assert pull_request["url"] in stdout
    assert "✓✓" in stdout


@then("I see the title and URL of the issue it closes")
def _command_reports_linked_issue(scenario_state: dict[str, Any]) -> None:
    stdout = scenario_state["stdout"]
    issue = scenario_state["gh_issues"][42]
    assert issue["title"] in stdout
    assert issue["url"] in stdout


@then("I see the title and URL of the branch's issue")
def _command_reports_branch_issue(scenario_state: dict[str, Any]) -> None:
    issue = scenario_state["gh_issues"][42]
    assert f"ISSUE: {issue['title']} {issue['url']}\n" in scenario_state["stdout"]


@then("the output identifies the pull request as merged and the issue as closed")
def _command_reports_completed_work(scenario_state: dict[str, Any]) -> None:
    pull_request = scenario_state["gh_pr"]
    issue = scenario_state["gh_issues"][42]
    pr_line, issue_line = scenario_state["stdout"].splitlines()
    assert pr_line.startswith(f"PR: {pull_request['title']} {pull_request['url']}")
    assert pr_line.endswith("[MERGED]")
    assert issue_line == f"ISSUE: {issue['title']} {issue['url']} [CLOSED]"


@then("the pull request is reported as unknown")
def _command_reports_pull_request_unknown(scenario_state: dict[str, Any]) -> None:
    assert "PR: unknown" in scenario_state["stdout"]


# -- Fixtures ---------------------------------------------------------------


@pytest.fixture
def scenario_state() -> dict[str, Any]:
    """Generic bucket for per-scenario state across step functions."""
    return {}


# -- Helpers ------------------------------------------------------------


def _repo_on_branch_with_open_pull_request(
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_path = _create_git_repo(tmp_path)
    monkeypatch.chdir(repo_path)
    subprocess.run(["git", "switch", "-qc", "renderer"], check=True)
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


def _fake_github_cli(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake GitHub CLI responses while allowing real local Git commands."""
    real_run = subprocess.run

    def fake_run(command: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] != "gh":
            return real_run(command, *args, **kwargs)
        if command[:3] == ["gh", "pr", "view"]:
            pull_request = scenario_state.get("gh_pr")
            expected_branch = "renderer" if pull_request else "issue-42"
            assert command[3:6] == [expected_branch, "--repo", "owner/repo"]
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
