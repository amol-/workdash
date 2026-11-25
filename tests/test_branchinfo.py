"""Unit tests for `workdash branchinfo` gh/git helpers and closing-issue selection."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workdash import branchinfo
from workdash.github_client import GitHubClient
from workdash.models import ci_status_symbol
from workdash.workdash import main as workdash_main


def test_ci_status_symbol_gives_independent_ci_and_review_symbols() -> None:
    assert ci_status_symbol("SUCCESS", "APPROVED", False) == (("✓", "green"), ("✓", "green"))
    assert ci_status_symbol("SUCCESS", "CHANGES_REQUESTED", False) == (
        ("✓", "green"),
        ("✗", "red"),
    )
    assert ci_status_symbol("SUCCESS", None, True) == (("✓", "green"), ("?", "yellow"))


def test_ci_status_symbol_colors_the_question_mark_by_whether_reviewers_left_comments() -> None:
    assert ci_status_symbol(None, None, True) == ((" ", None), ("?", "yellow"))
    assert ci_status_symbol(None, None, False, True) == ((" ", None), ("?", "dodger_blue1"))
    # Unresolved comments mean a reviewer already looked, even if the request was cleared.
    assert ci_status_symbol(None, None, True, True) == ((" ", None), ("?", "dodger_blue1"))


def test_ci_status_symbol_lets_a_verdict_win_over_unresolved_comments() -> None:
    assert ci_status_symbol(None, "APPROVED", False, True)[1] == ("✓", "green")
    assert ci_status_symbol(None, "CHANGES_REQUESTED", False, True)[1] == ("✗", "red")


def test_ci_status_symbol_falls_back_to_a_blank_glyph_for_unknown_states() -> None:
    assert ci_status_symbol(None, None, False) == ((" ", None), (" ", None))
    assert ci_status_symbol("SOMETHING_NEW", None, False) == ((" ", None), (" ", None))


def test_current_repo_reads_the_origin_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["git", "config", "--local", "--get", "remote.origin.url"]
        return subprocess.CompletedProcess(
            command, returncode=0, stdout="https://github.com/owner/repo.git\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert branchinfo._current_repo(tmp_path) == "owner/repo"


def test_current_repo_rejects_a_missing_origin_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode=1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="No origin remote"):
        branchinfo._current_repo(tmp_path)


def test_fetch_pull_request_returns_none_when_gh_reports_no_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1, command, stderr='no pull requests found for branch "feature"'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GitHubClient().branch_pull_request("owner/repo", "feature") is None


@pytest.mark.parametrize("state", ["OPEN", "MERGED", "CLOSED"])
def test_fetch_pull_request_reports_the_branch_pull_request_whatever_its_state(
    monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {"number": 1, "title": "t", "url": "u", "state": state}
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    pull_request = GitHubClient().branch_pull_request("owner/repo", "feature")

    assert pull_request is not None
    assert pull_request.number == 1
    assert pull_request.state == state


def test_fetch_pull_request_raises_on_a_genuine_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="authentication required")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="authentication required"):
        GitHubClient().branch_pull_request("owner/repo", "feature")


@pytest.mark.parametrize("review_requests", [None, 1])
def test_fetch_ci_and_closing_issues_combines_both_selections_in_one_gh_call(
    monkeypatch: pytest.MonkeyPatch,
    review_requests: int | None,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {
                            "nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]
                        },
                        "reviewDecision": "APPROVED",
                        "closingIssuesReferences": {
                            "nodes": [{"number": 3, "repository": {"nameWithOwner": "owner/repo"}}]
                        },
                    }
                }
            }
        }
        if review_requests is not None:
            payload["data"]["repository"]["pullRequest"]["reviewRequests"] = {
                "totalCount": review_requests
            }
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    ci_state, review_decision, review_requested, closing_issues = (
        GitHubClient().branch_ci_and_closing_issues("owner/repo", 5)
    )

    assert len(calls) == 1
    assert ci_state == "SUCCESS"
    assert review_decision == "APPROVED"
    assert review_requested is bool(review_requests)
    assert closing_issues == [("owner/repo", 3)]


def test_fetch_ci_and_closing_issues_raises_on_a_genuine_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="authentication required")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="authentication required"):
        GitHubClient().branch_ci_and_closing_issues("owner/repo", 5)


def test_fetch_ci_and_closing_issues_raises_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to parse gh"):
        GitHubClient().branch_ci_and_closing_issues("owner/repo", 5)


def test_fetch_ci_and_closing_issues_guards_against_a_response_missing_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {"data": {"repository": {}}}
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="missing pullRequest"):
        GitHubClient().branch_ci_and_closing_issues("owner/repo", 5)


def test_fetch_ci_and_closing_issues_guards_against_malformed_closing_issues_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
                        "reviewDecision": None,
                    }
                }
            }
        }
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="missing closingIssuesReferences"):
        GitHubClient().branch_ci_and_closing_issues("owner/repo", 5)


def test_fetch_issue_reads_title_url_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == [
            "gh",
            "issue",
            "view",
            "4",
            "--repo",
            "owner/repo",
            "--json",
            "title,url,state",
        ]
        payload = {
            "title": "Bug",
            "url": "https://github.com/owner/repo/issues/4",
            "state": "CLOSED",
        }
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    issue = GitHubClient().branch_issue("owner/repo", 4)

    assert issue.title == "Bug"
    assert issue.url == "https://github.com/owner/repo/issues/4"
    assert issue.state == "CLOSED"


def test_branchinfo_rejects_non_git_directory(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    assert workdash_main(["branchinfo"]) != 0
    assert capsys.readouterr().err == "Error: Not a git repository.\n"


def test_branchinfo_reports_unknown_pr_and_issue_on_ordinary_branch(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q", "-b", "main"], check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], check=True)
    subprocess.run(["git", "commit", "-qm", "Initial commit", "--allow-empty"], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"], check=True
    )

    real_run = subprocess.run

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] != "gh":
            return real_run(command, **kwargs)
        assert command[:6] == ["gh", "pr", "view", "main", "--repo", "owner/repo"]
        raise subprocess.CalledProcessError(
            1, command, stderr='no pull requests found for branch "main"'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert workdash_main(["branchinfo"]) == 0
    assert capsys.readouterr() == ("PR: unknown\nISSUE: unknown\n", "")


@pytest.mark.parametrize(
    ("closing_issues", "expected_issues"),
    [
        ([("owner/other", 9)], ["ISSUE: unknown"]),
        (
            [("owner/repo", 42), ("owner/other", 9), ("owner/repo", 7)],
            [
                "ISSUE: Fix theme https://github.com/owner/repo/issues/7 [CLOSED]",
                "ISSUE: Fix renderer https://github.com/owner/repo/issues/42",
            ],
        ),
    ],
    ids=["other-repository-only", "mixed-repositories"],
)
def test_branchinfo_reports_pending_review_and_same_repo_issues(
    tmp_path, monkeypatch, capsys, closing_issues, expected_issues
) -> None:
    monkeypatch.setattr(branchinfo.GitHelper, "get_repo_root", lambda self: tmp_path)
    monkeypatch.setattr(branchinfo.GitHelper, "get_current_branch", lambda self, repo: "feature")
    monkeypatch.setattr(
        branchinfo.GitHelper,
        "remote_url",
        lambda self, path, remote: "https://github.com/owner/repo",
    )
    issues = {
        "42": {
            "title": "Fix renderer",
            "url": "https://github.com/owner/repo/issues/42",
            "state": "OPEN",
        },
        "7": {
            "title": "Fix theme",
            "url": "https://github.com/owner/repo/issues/7",
            "state": "CLOSED",
        },
    }

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[:4] == ["gh", "pr", "view", "feature"]:
            payload = {
                "number": 5,
                "title": "Implement renderer",
                "url": "https://github.com/owner/repo/pull/5",
                "state": "OPEN",
            }
        elif command[:3] == ["gh", "api", "graphql"]:
            pull_request = {
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviewDecision": None,
                "reviewRequests": {"totalCount": 1},
                "closingIssuesReferences": {
                    "nodes": [
                        {"number": number, "repository": {"nameWithOwner": repo}}
                        for repo, number in closing_issues
                    ]
                },
            }
            payload = {"data": {"repository": {"pullRequest": pull_request}}}
        elif command[:3] == ["gh", "issue", "view"]:
            assert command[4:6] == ["--repo", "owner/repo"]
            payload = issues[command[3]]
        else:
            pytest.fail(f"Unexpected command: {command}")
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert workdash_main(["branchinfo"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "PR: Implement renderer https://github.com/owner/repo/pull/5 ✓?",
        *expected_issues,
    ]
