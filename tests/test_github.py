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


def test_fetch_launch_context_builds_pr_view_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout='{"state":"OPEN"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GithubHelper().fetch_launch_context(make_work_item(WorkItemType.PR)) == {"state": "OPEN"}
    assert calls == [
        (
            [
                "gh",
                "pr",
                "view",
                "42",
                "--repo",
                "owner/repo",
                "--json",
                (
                    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
                    "isDraft,reviewDecision,additions,deletions,changedFiles,"
                    "headRefName,baseRefName"
                ),
            ],
            {"check": True, "capture_output": True, "text": True},
        )
    ]


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
