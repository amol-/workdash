import json
import subprocess
from datetime import UTC, datetime

import pytest

from workdash.github import GithubHelper
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def make_work_item(item_type: WorkItemType) -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.AUTHORED_PR
        if item_type == WorkItemType.PR
        else WorkItemKind.TRACKED_ISSUE,
        item_type=item_type,
        repo="owner/repo",
        number=42,
        title="Test item",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/42",
    )


def graphql_payload(*, issues: list | None = None, threads: list | None = None) -> str:
    pull_request = {
        "closingIssuesReferences": {"nodes": issues or []},
        "reviewThreads": {"nodes": threads or []},
    }
    return json.dumps({"data": {"repository": {"pullRequest": pull_request}}})


def fake_gh(calls: list[list[str]], *, graphql_stdout: str):
    def fake_run(*args, **kwargs):
        calls.append(args[0])
        stdout = graphql_stdout if "graphql" in args[0] else '{"state":"OPEN"}'
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    return fake_run


def test_fetch_launch_context_for_pr_adds_linked_issues_and_unresolved_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    comment = {"author": {"login": "bob"}, "body": "fix this", "createdAt": "2026-02-01T00:00:00Z"}
    graphql_stdout = graphql_payload(
        issues=[
            {
                "number": 7,
                "title": "Goal",
                "body": "Do the thing",
                "author": {"login": "alice"},
                "repository": {"nameWithOwner": "other/repo"},
                "comments": {"nodes": [comment]},
            }
        ],
        threads=[
            {"isResolved": False, "path": "a.py", "line": 3, "comments": {"nodes": [comment]}},
            {"isResolved": True, "path": "b.py", "line": 9, "comments": {"nodes": [comment]}},
        ],
    )
    monkeypatch.setattr(subprocess, "run", fake_gh(calls, graphql_stdout=graphql_stdout))

    context = GithubHelper().fetch_launch_context(make_work_item(WorkItemType.PR))

    assert context == {
        "state": "OPEN",
        "linkedIssues": [
            {
                "number": 7,
                "title": "Goal",
                "body": "Do the thing",
                "author": {"login": "alice"},
                "repository": "other/repo",
                "comments": [comment],
            }
        ],
        "unresolvedReviewThreads": [{"path": "a.py", "line": 3, "comments": [comment]}],
    }
    view_command, graphql_command = calls
    assert view_command[:7] == ["gh", "pr", "view", "42", "--repo", "owner/repo", "--json"]
    assert {"comments", "reviews"} <= set(view_command[7].split(","))
    assert "latestReviews" not in view_command[7]
    assert graphql_command[:4] == ["gh", "api", "graphql", "-f"]
    assert 'repository(owner: "owner", name: "repo")' in graphql_command[4]
    assert "pullRequest(number: 42)" in graphql_command[4]
    assert "repository { nameWithOwner }" in graphql_command[4]
    query = graphql_command[4]
    closing_issues = query.split("closingIssuesReferences")[1].split("reviewThreads")[0]
    review_threads = query.split("reviewThreads")[1]
    assert "number title body author { login } repository" in closing_issues
    assert "isMinimized minimizedReason" in closing_issues
    assert "isMinimized minimizedReason" in review_threads


def test_fetch_launch_context_for_issue_makes_no_graphql_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", fake_gh(calls, graphql_stdout=graphql_payload()))

    context = GithubHelper().fetch_launch_context(make_work_item(WorkItemType.ISSUE))

    assert context == {"state": "OPEN"}
    assert [command[1] for command in calls] == ["issue"]


def test_fetch_analysis_context_for_issue_includes_discussion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(*args, **kwargs):
        captured.append(args[0][-1])
        return subprocess.CompletedProcess(args[0], 0, stdout='{"comments":[]}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GithubHelper().fetch_analysis_context(make_work_item(WorkItemType.ISSUE))

    assert captured == [
        "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,comments"
    ]


def test_fetch_launch_context_rejects_non_object_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="expected a JSON object"):
        GithubHelper().fetch_launch_context(make_work_item(WorkItemType.PR))


def test_fetch_diff_reports_missing_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        GithubHelper().fetch_diff(make_work_item(WorkItemType.PR))
