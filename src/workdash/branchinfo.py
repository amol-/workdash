"""Standalone CLI command reporting the current branch's pull request and issues.

This module provides the `workdash branchinfo` CLI command, which reports the
pull request for the currently checked-out branch and every issue it closes in
the same repository, resolved from GitHub metadata alone. Merged and closed
items are reported too, flagged with their state. It works in any git
repository directory with no server dependency, the same as `workdash
branchdiff`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git import GitHelper, get_current_branch, get_repo_root
from .github_client import _AUTHORED_CI_SELECTION, _CLOSING_ISSUES_SELECTION, _extract_ci_state
from .models import ci_status_symbol


@dataclass(frozen=True, slots=True)
class _PullRequestInfo:
    """The pull request resolved for the current branch."""

    number: int
    title: str
    url: str
    state: str


@dataclass(frozen=True, slots=True)
class _IssueInfo:
    """An issue a pull request closes."""

    title: str
    url: str
    state: str


def run_branchinfo() -> int:
    """Report the pull request and the issues it closes for the current branch."""
    try:
        repo_path = get_repo_root()
    except RuntimeError:
        print("Error: Not a git repository.", file=sys.stderr)
        return 1

    try:
        repo = _current_repo(repo_path)
        branch = get_current_branch(repo_path)
        pull_request = _fetch_pull_request(repo, branch)
        symbol: str | None = None
        issues: list[_IssueInfo] = []
        if pull_request is not None:
            ci_state, review_decision, closing_issues = _fetch_ci_and_closing_issues(
                repo, pull_request.number
            )
            symbol, _color = ci_status_symbol(ci_state, review_decision)
            issues = [_fetch_issue(*issue) for issue in _linked_issues(repo, closing_issues)]
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


def _fetch_pull_request(repo: str, branch: str) -> _PullRequestInfo | None:
    """Return the pull request for ``branch``, whatever its state, or ``None`` if there is none."""
    command = ["gh", "pr", "view", branch, "--repo", repo, "--json", "number,title,url,state"]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError("gh CLI is not installed or not on PATH.") from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        if "no pull requests found" in stderr.lower():
            return None
        raise RuntimeError(
            f"Failed to look up the pull request for branch {branch!r}: "
            f"{stderr or f'gh exited with code {error.returncode}'}"
        ) from error
    payload = _parse_json_object(completed.stdout, context="pull request lookup")
    number, title, url, state = (
        payload.get("number"),
        payload.get("title"),
        payload.get("url"),
        payload.get("state"),
    )
    if (
        not isinstance(number, int)
        or not isinstance(title, str)
        or not isinstance(url, str)
        or not isinstance(state, str)
    ):
        raise RuntimeError("Invalid gh pull request payload: missing number, title, url, or state.")
    return _PullRequestInfo(number=number, title=title, url=url, state=state)


def _fetch_ci_and_closing_issues(
    repo: str, number: int
) -> tuple[str | None, str | None, list[tuple[str, int]]]:
    """Return CI state, review decision, and closing issues for one pull request."""
    owner, _, name = repo.partition("/")
    query = (
        f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
        f"{{ pullRequest(number: {number}) "
        f"{{ {_AUTHORED_CI_SELECTION} {_CLOSING_ISSUES_SELECTION} }} }} }}"
    )
    command = ["gh", "api", "graphql", "-f", f"query={query}"]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError("gh CLI is not installed or not on PATH.") from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        raise RuntimeError(
            f"Failed to fetch CI status and closing issues for {repo}#{number}: "
            f"{stderr or f'gh exited with code {error.returncode}'}"
        ) from error
    payload = _parse_json_object(completed.stdout, context="CI status and closing issues lookup")
    data = payload.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(pull_request, dict):
        raise RuntimeError(f"Invalid gh GraphQL payload for {repo}#{number}: missing pullRequest.")
    return (
        _extract_ci_state(pull_request),
        pull_request.get("reviewDecision"),
        _closing_issues(pull_request),
    )


def _closing_issues(pull_request: dict[str, Any]) -> list[tuple[str, int]]:
    """Collect the ``(repo, number)`` issues one pull request closes."""
    references = pull_request.get("closingIssuesReferences")
    nodes = references.get("nodes") if isinstance(references, dict) else None
    if not isinstance(nodes, list):
        raise RuntimeError("Invalid gh GraphQL payload: missing closingIssuesReferences.")
    issues: list[tuple[str, int]] = []
    for node in nodes:
        node_fields = node if isinstance(node, dict) else {}
        number = node_fields.get("number")
        repository = node_fields.get("repository")
        repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
        if not isinstance(number, int) or not isinstance(repo, str) or not repo:
            raise RuntimeError("Invalid gh GraphQL payload: missing closing issue reference.")
        issues.append((repo, number))
    return issues


def _linked_issues(repo: str, closing_issues: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """Return every closing issue that lives in ``repo``, lowest number first."""
    return sorted(issue for issue in closing_issues if issue[0] == repo)


def _fetch_issue(repo: str, number: int) -> _IssueInfo:
    """Return the title and url of one issue."""
    command = ["gh", "issue", "view", str(number), "--repo", repo, "--json", "title,url,state"]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError("gh CLI is not installed or not on PATH.") from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        raise RuntimeError(
            f"Failed to look up issue {repo}#{number}: "
            f"{stderr or f'gh exited with code {error.returncode}'}"
        ) from error
    payload = _parse_json_object(completed.stdout, context=f"issue lookup for {repo}#{number}")
    title, url, state = payload.get("title"), payload.get("url"), payload.get("state")
    if not isinstance(title, str) or not isinstance(url, str) or not isinstance(state, str):
        raise RuntimeError(
            f"Invalid gh issue payload for {repo}#{number}: missing title, url, or state."
        )
    return _IssueInfo(title=title, url=url, state=state)


def _parse_json_object(raw: str, *, context: str) -> dict[str, Any]:
    """Parse ``raw`` as a gh JSON object, translating malformed output into ``RuntimeError``."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to parse gh {context} JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid gh {context} payload: expected a JSON object.")
    return payload
