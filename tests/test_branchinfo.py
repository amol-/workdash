"""Unit tests for `workdash branchinfo` gh/git helpers and issue tie-break."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workdash import branchinfo
from workdash.models import ci_status_symbol


def test_ci_status_symbol_gives_a_double_checkmark_for_passing_approved_prs() -> None:
    assert ci_status_symbol("SUCCESS", "APPROVED") == ("✓✓", "green")


def test_ci_status_symbol_falls_back_to_a_blank_glyph_for_unknown_states() -> None:
    assert ci_status_symbol(None, None) == (" ", None)
    assert ci_status_symbol("SOMETHING_NEW", None) == (" ", None)


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


def test_fetch_open_pull_request_returns_none_when_gh_reports_no_pull_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(
            1, command, stderr='no pull requests found for branch "feature"'
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert branchinfo._fetch_open_pull_request("owner/repo", "feature") is None


def test_fetch_open_pull_request_returns_none_when_the_pull_request_is_not_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        payload = {"number": 1, "title": "t", "url": "u", "state": "MERGED"}
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert branchinfo._fetch_open_pull_request("owner/repo", "feature") is None


def test_fetch_open_pull_request_raises_on_a_genuine_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="authentication required")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="authentication required"):
        branchinfo._fetch_open_pull_request("owner/repo", "feature")


def test_fetch_ci_and_closing_issues_combines_both_selections_in_one_gh_call(
    monkeypatch: pytest.MonkeyPatch,
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
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    ci_state, review_decision, closing_issues = branchinfo._fetch_ci_and_closing_issues(
        "owner/repo", 5
    )

    assert len(calls) == 1
    assert ci_state == "SUCCESS"
    assert review_decision == "APPROVED"
    assert closing_issues == [("owner/repo", 3)]


def test_fetch_ci_and_closing_issues_raises_on_a_genuine_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(1, command, stderr="authentication required")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="authentication required"):
        branchinfo._fetch_ci_and_closing_issues("owner/repo", 5)


def test_fetch_ci_and_closing_issues_raises_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to parse gh"):
        branchinfo._fetch_ci_and_closing_issues("owner/repo", 5)


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
        branchinfo._fetch_ci_and_closing_issues("owner/repo", 5)


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
        branchinfo._fetch_ci_and_closing_issues("owner/repo", 5)


def test_linked_issue_picks_lowest_numbered_same_repo_issue() -> None:
    closing_issues = [("owner/other", 1), ("owner/repo", 9), ("owner/repo", 4)]

    assert branchinfo._linked_issue("owner/repo", closing_issues) == ("owner/repo", 4)


def test_linked_issue_is_none_without_a_same_repo_closing_issue() -> None:
    assert branchinfo._linked_issue("owner/repo", [("owner/other", 1)]) is None


def test_fetch_issue_reads_title_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == [
            "gh",
            "issue",
            "view",
            "4",
            "--repo",
            "owner/repo",
            "--json",
            "title,url",
        ]
        payload = {"title": "Bug", "url": "https://github.com/owner/repo/issues/4"}
        return subprocess.CompletedProcess(
            command, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    issue = branchinfo._fetch_issue("owner/repo", 4)

    assert issue.title == "Bug"
    assert issue.url == "https://github.com/owner/repo/issues/4"
