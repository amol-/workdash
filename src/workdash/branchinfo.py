"""Standalone CLI command reporting the current branch's pull request and issues.

This module provides the `workdash branchinfo` CLI command, which reports the
pull request for the currently checked-out branch and every issue it closes in
the same repository, resolved from GitHub metadata. When GitHub links no issue,
a workdash issue branch (``issue-N``) names the issue instead. Merged and closed
items are reported too, flagged with their state. It works in any git
repository directory with no server dependency, the same as `workdash
branchdiff`.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .git import GitHelper
from .github_client import GitHubClient
from .models import ci_status_symbol
from .repo_worktree import issue_branch_number


def run_branchinfo(github_client=None) -> int:
    """Report the pull request and the issues it closes for the current branch."""
    try:
        repo_path = GitHelper().get_repo_root()
    except RuntimeError:
        print("Error: Not a git repository.", file=sys.stderr)
        return 1

    github = github_client if github_client is not None else GitHubClient()
    try:
        repo = _current_repo(repo_path)
        branch = GitHelper().get_current_branch(repo_path)
        pull_request = github.branch_pull_request(repo, branch)
        symbol: str | None = None
        issues = []
        if pull_request is not None:
            ci_state, review_decision, review_requested, closing_issues = (
                github.branch_ci_and_closing_issues(repo, pull_request.number)
            )
            (ci_symbol, _ci_color), (review_symbol, _review_color) = ci_status_symbol(
                ci_state, review_decision, review_requested
            )
            symbol = f"{ci_symbol}{review_symbol}"
            issues = [github.branch_issue(*issue) for issue in _linked_issues(repo, closing_issues)]
        branch_issue = issue_branch_number(branch)
        if not issues and branch_issue is not None:
            issues = [github.branch_issue(repo, branch_issue)]
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    if pull_request is None:
        print("PR: unknown")
    else:
        flag = _state_flag(pull_request.state)
        print(f"PR: {pull_request.title} {pull_request.url} {symbol}{flag}")
    if not issues:
        print("ISSUE: unknown")
    for issue in issues:
        print(f"ISSUE: {issue.title} {issue.url}{_state_flag(issue.state)}")
    return 0


def _state_flag(state: str) -> str:
    """Return a ``[MERGED]``/``[CLOSED]`` marker for anything no longer open."""
    return "" if state == "OPEN" else f" [{state}]"


def _current_repo(repo_path: Path) -> str:
    """Return the ``owner/repo`` the ``origin`` remote points at."""
    git_helper = GitHelper()
    remote_url = git_helper.remote_url(repo_path, "origin")
    if remote_url is None:
        raise RuntimeError("No origin remote configured for this repository.")
    repo = git_helper.repo_from_remote_url(remote_url)
    if not repo:
        raise RuntimeError(f"Could not determine owner/repo from origin remote {remote_url!r}.")
    return repo


def _linked_issues(repo: str, closing_issues: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Return every closing issue that lives in ``repo``, lowest number first."""
    return sorted(issue for issue in closing_issues if issue[0] == repo)
