import json
import subprocess
from unittest.mock import MagicMock

import pytest

from workdash.github_client import (
    GitHubClient,
    ParsedGitHubItemURL,
    TransientFetchError,
    parse_github_item_url,
)
from workdash.models import WorkItemKind, WorkItemType


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/owner/repo/pull/123",
            ParsedGitHubItemURL(
                repo="owner/repo",
                number=123,
                item_type=WorkItemType.PR,
                canonical_url="https://github.com/owner/repo/pull/123",
            ),
        ),
        (
            "https://github.com/owner/repo/issues/7",
            ParsedGitHubItemURL(
                repo="owner/repo",
                number=7,
                item_type=WorkItemType.ISSUE,
                canonical_url="https://github.com/owner/repo/issues/7",
            ),
        ),
        (
            "http://github.com/owner/repo/pull/8/files",
            ParsedGitHubItemURL(
                repo="owner/repo",
                number=8,
                item_type=WorkItemType.PR,
                canonical_url="https://github.com/owner/repo/pull/8",
            ),
        ),
        (
            "https://www.github.com/owner/repo/issues/9?foo=bar#diff",
            ParsedGitHubItemURL(
                repo="owner/repo",
                number=9,
                item_type=WorkItemType.ISSUE,
                canonical_url="https://github.com/owner/repo/issues/9",
            ),
        ),
    ],
)
def test_parse_github_item_url_accepts_pr_and_issue_variants(
    url: str, expected: ParsedGitHubItemURL
) -> None:
    assert parse_github_item_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "not a url",
        "ftp://github.com/owner/repo/pull/1",
        "https://gitlab.com/owner/repo/pull/1",
        "https://github.com/owner/pull/1",  # missing repo segment
        "https://github.com/owner/repo/discussions/1",
        "https://github.com/owner/repo/pull/",
        "https://github.com/owner/repo/pull/abc",
    ],
)
def test_parse_github_item_url_rejects_invalid_inputs(url: str) -> None:
    assert parse_github_item_url(url) is None


def _authored_search_stdout(nodes: list[dict[str, object]] | None = None) -> str:
    """Return a ``gh search prs`` response for authored pull requests."""

    return json.dumps(nodes or [])


def test_list_open_authored_prs_returns_open_prs_including_drafts_and_forks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        captured_commands.append(command)
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=_authored_search_stdout(
                    [
                        {
                            "id": "A",
                            "number": 10,
                            "title": "draft pr",
                            "url": "https://example.com/pull/10",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "updatedAt": "2026-02-02T00:00:00Z",
                            "isDraft": True,
                            "repository": {"nameWithOwner": "someone/fork-repo"},
                        },
                        {
                            "id": "B",
                            "number": 11,
                            "title": "ready pr",
                            "url": "https://example.com/pull/11",
                            "createdAt": "2026-02-03T00:00:00Z",
                            "updatedAt": "2026-02-04T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "upstream/main-repo"},
                        },
                    ]
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "p0": {
                                "pullRequest": {
                                    "commits": {
                                        "nodes": [
                                            {"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}
                                        ]
                                    },
                                    "reviewDecision": "APPROVED",
                                    "reviewRequests": {"totalCount": 0},
                                    "reviewThreads": {"nodes": [{"isResolved": True}]},
                                }
                            },
                            "p1": {
                                "pullRequest": {
                                    "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
                                    "reviewDecision": None,
                                    "reviewRequests": {"totalCount": 1},
                                    "reviewThreads": {
                                        "nodes": [{"isResolved": True}, {"isResolved": False}]
                                    },
                                }
                            },
                        }
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_authored_prs("testuser")

    search_command, ci_command = captured_commands
    assert search_command == [
        "gh",
        "search",
        "prs",
        "--author",
        "testuser",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "id,number,title,url,createdAt,updatedAt,isDraft,repository",
    ]
    assert "number: 10" in ci_command[4]
    assert "number: 11" in ci_command[4]
    assert "statusCheckRollup { state }" in ci_command[4]
    assert "reviewThreads(first: 100) { nodes { isResolved } }" in ci_command[4]
    assert ci_command[4].count("{") == ci_command[4].count("}")
    assert result == [
        {
            "id": "A",
            "repo": "someone/fork-repo",
            "number": 10,
            "title": "draft pr",
            "url": "https://example.com/pull/10",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-02T00:00:00Z",
            "is_draft": True,
            "ci_state": "SUCCESS",
            "review_decision": "APPROVED",
            "review_requested": False,
            "has_unresolved_comments": False,
        },
        {
            "id": "B",
            "repo": "upstream/main-repo",
            "number": 11,
            "title": "ready pr",
            "url": "https://example.com/pull/11",
            "created_at": "2026-02-03T00:00:00Z",
            "updated_at": "2026-02-04T00:00:00Z",
            "is_draft": False,
            "ci_state": None,
            "review_decision": None,
            "review_requested": True,
            "has_unresolved_comments": True,
        },
    ]


@pytest.mark.parametrize(
    "error_message",
    [
        "Resource protected by organization SAML enforcement.",
        "Could not resolve to a Repository with the name 'protected-org/private'.",
    ],
)
def test_list_open_authored_prs_skips_only_an_attributed_inaccessible_pr(
    monkeypatch: pytest.MonkeyPatch, error_message: str
) -> None:
    progress_messages: list[str] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=_authored_search_stdout(
                    [
                        {
                            "id": "PRIVATE",
                            "number": 10,
                            "title": "private",
                            "url": "https://github.com/protected-org/private/pull/10",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "updatedAt": "2026-02-02T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "protected-org/private"},
                        },
                        {
                            "id": "PUBLIC",
                            "number": 11,
                            "title": "public",
                            "url": "https://github.com/owner/repo/pull/11",
                            "createdAt": "2026-02-03T00:00:00Z",
                            "updatedAt": "2026-02-04T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "owner/repo"},
                        },
                    ]
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            payload = {
                "data": {
                    "p0": None,
                    "p1": {
                        "pullRequest": {
                            "commits": {
                                "nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]
                            }
                        }
                    },
                },
                "errors": [{"path": ["p0"], "message": error_message}],
            }
            raise subprocess.CalledProcessError(
                1, command, output=json.dumps(payload), stderr=error_message
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_authored_prs(
        "testuser", progress_callback=progress_messages.append
    )

    assert [(item["repo"], item["number"], item["ci_state"]) for item in result] == [
        ("owner/repo", 11, "SUCCESS")
    ]
    assert progress_messages == [
        f"Warning: skipped authored pull request protected-org/private#10: {error_message}"
    ]


def test_list_open_authored_prs_surfaces_unrelated_ci_lookup_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=_authored_search_stdout(
                    [
                        {
                            "id": "A",
                            "number": 10,
                            "title": "authored",
                            "url": "https://github.com/owner/repo/pull/10",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "updatedAt": "2026-02-02T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "owner/repo"},
                        }
                    ]
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            payload = {
                "data": {"p0": None},
                "errors": [{"path": ["p0"], "message": "Something went wrong."}],
            }
            raise subprocess.CalledProcessError(
                1, command, output=json.dumps(payload), stderr="Something went wrong."
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=r"\(owner/repo#10\): Something went wrong"):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_authored_prs_rejects_partial_ci_responses_without_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=_authored_search_stdout(
                    [
                        {
                            "id": "A",
                            "number": 10,
                            "title": "authored",
                            "url": "https://github.com/owner/repo/pull/10",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "updatedAt": "2026-02-02T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "owner/repo"},
                        }
                    ]
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            raise subprocess.CalledProcessError(
                1,
                command,
                output='{"message":"Bad credentials","status":"401"}',
                stderr="Bad credentials",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Bad credentials"):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_authored_prs_rejects_ci_response_missing_authored_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress_messages: list[str] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=_authored_search_stdout(
                    [
                        {
                            "id": "A",
                            "number": 10,
                            "title": "authored",
                            "url": "https://github.com/owner/repo/pull/10",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "updatedAt": "2026-02-02T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "owner/repo"},
                        }
                    ]
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps({"data": {}}),
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError,
        match="Invalid gh authored PR CI payload for owner/repo#10: expected an object",
    ):
        GitHubClient().list_open_authored_prs(
            "testuser", progress_callback=progress_messages.append
        )

    assert progress_messages == []


def test_list_open_authored_prs_accepts_custom_author(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=_authored_search_stdout(),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = GitHubClient().list_open_authored_prs(author_login="octocat")

    assert result == []
    assert captured["command"] == [
        "gh",
        "search",
        "prs",
        "--author",
        "octocat",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "id,number,title,url,createdAt,updatedAt,isDraft,repository",
    ]


def test_list_open_authored_prs_raises_clear_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_authored_prs_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="authentication failed",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError, match="Failed to list open authored PRs for 'testuser' via gh"
    ):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_authored_prs_surfaces_transient_ci_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ci_attempts = 0

    def fake_run(*args, **kwargs):
        nonlocal ci_attempts
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=_authored_search_stdout(
                    [
                        {
                            "id": "A",
                            "number": 10,
                            "title": "authored",
                            "url": "https://github.com/owner/repo/pull/10",
                            "createdAt": "2026-02-01T00:00:00Z",
                            "updatedAt": "2026-02-02T00:00:00Z",
                            "isDraft": False,
                            "repository": {"nameWithOwner": "owner/repo"},
                        }
                    ]
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            ci_attempts += 1
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr="non-200 OK status code: 504 Gateway Timeout",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    with pytest.raises(TransientFetchError, match="504 Gateway Timeout"):
        GitHubClient().list_open_authored_prs("testuser")
    assert ci_attempts == 3


def test_list_open_authored_prs_raises_clear_error_for_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="{not valid json",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to parse gh authored PR JSON"):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_authored_prs_raises_error_for_invalid_payload_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=_authored_search_stdout([{"id": "A", "number": "not-an-int"}]),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="entry 0 has missing or invalid repository"):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_review_requested_prs_searches_direct_requests_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        captured_commands.append(command)
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '[{"id":"R1","number":42,"title":"needs review","url":"https://example.com/pull/42",'
                    '"createdAt":"2026-02-05T00:00:00Z","updatedAt":"2026-02-06T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
                ),
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_review_requested_prs("testuser")

    assert captured_commands == [
        [
            "gh",
            "search",
            "prs",
            "user-review-requested:testuser",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "id,number,title,url,createdAt,updatedAt,isDraft,repository",
        ]
    ]
    assert result == [
        {
            "id": "R1",
            "repo": "owner/repo",
            "number": 42,
            "title": "needs review",
            "url": "https://example.com/pull/42",
            "created_at": "2026-02-05T00:00:00Z",
            "updated_at": "2026-02-06T00:00:00Z",
            "is_draft": False,
            # The review selectors do not ask for CI state, review decision, or
            # pending review requests.
            "ci_state": None,
            "review_decision": None,
            "review_requested": False,
            "has_unresolved_comments": False,
        }
    ]


def test_list_open_review_requested_prs_accepts_custom_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="[]",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_review_requested_prs(reviewer_login="octocat")

    assert result == []
    assert captured["command"][3] == "user-review-requested:octocat"


def test_list_open_review_requested_prs_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="authentication failed",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError,
        match="Failed to list open review-requested PRs for 'testuser' via gh",
    ):
        GitHubClient().list_open_review_requested_prs("testuser")


def test_fetch_linked_issues_reads_every_pull_request_in_one_graphql_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        captured_commands.append(command)
        if command[:3] == ["gh", "api", "graphql"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "p0": {
                                "pullRequest": {
                                    "closingIssuesReferences": {
                                        "nodes": [
                                            {
                                                "number": 41999,
                                                "repository": {
                                                    "nameWithOwner": "posit-dev/connect"
                                                },
                                            },
                                            # Every closing issue is reported,
                                            # including one in another repository.
                                            {
                                                "number": 41830,
                                                "repository": {
                                                    "nameWithOwner": "posit-dev/planning"
                                                },
                                            },
                                        ]
                                    }
                                }
                            },
                            "p1": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}},
                        }
                    }
                ),
                stderr="",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().fetch_linked_issues([("posit-dev/connect", 42149), ("owner/repo", 7)])

    assert result == {
        ("posit-dev/connect", 42149): [
            ("posit-dev/connect", 41999),
            ("posit-dev/planning", 41830),
        ]
    }
    # Every pull request is asked for in one aliased document.
    assert len(captured_commands) == 1
    query = captured_commands[0][4]
    assert query.startswith("query={ p0: ")
    assert 'p0: repository(owner: "posit-dev", name: "connect")' in query
    assert "pullRequest(number: 42149)" in query
    assert 'p1: repository(owner: "owner", name: "repo")' in query
    assert "pullRequest(number: 7)" in query
    assert (
        "closingIssuesReferences(first: 5) { nodes { number repository { nameWithOwner } } }"
        in query
    )


def test_fetch_linked_issues_asks_gh_nothing_when_there_are_no_pull_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise AssertionError(f"Unexpected command: {args[0]}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GitHubClient().fetch_linked_issues([]) == {}


@pytest.mark.parametrize(
    ("error_type", "skip_error", "expected_fragment"),
    [
        (
            "FORBIDDEN",
            "GraphQL: Resource protected by organization SAML enforcement. "
            "You must grant your OAuth token access to this organization.",
            "SAML enforcement",
        ),
        (
            "NOT_FOUND",
            "Could not resolve to a Repository with the name 'protected-org/protected-repo'.",
            "Could not resolve to a Repository",
        ),
    ],
)
def test_fetch_linked_issues_warns_and_skips_an_unreachable_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
    skip_error: str,
    expected_fragment: str,
) -> None:
    warnings: list[str] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "api", "graphql"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                output=json.dumps(
                    {
                        "data": {
                            "p0": None,
                            "p1": {
                                "pullRequest": {
                                    "closingIssuesReferences": {
                                        "nodes": [
                                            {
                                                "number": 12,
                                                "repository": {"nameWithOwner": "owner/repo"},
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                        "errors": [{"type": error_type, "path": ["p0"], "message": skip_error}],
                    }
                ),
                stderr=skip_error,
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().fetch_linked_issues(
        [("protected-org/protected-repo", 860), ("owner/repo", 42)],
        progress_callback=warnings.append,
    )

    # The denied pull request has no linked issue, so it never redirects a
    # worktree or hides an issue row.
    assert result == {("owner/repo", 42): [("owner/repo", 12)]}
    assert any(
        "Warning: skipped linked issue lookup for protected-org/protected-repo#860" in warning
        and expected_fragment in warning
        for warning in warnings
    )


def test_fetch_linked_issues_reraises_a_non_authorization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="permission denied",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="permission denied"):
        GitHubClient().fetch_linked_issues(
            [("owner/repo", 42)],
            progress_callback=warnings.append,
        )

    assert not any("Warning: skipped linked issue lookup" in warning for warning in warnings)


def test_fetch_linked_issues_for_pull_request_lists_the_issues_it_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        assert command[:3] == ["gh", "api", "graphql"]
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "p0": {
                            "pullRequest": {
                                "closingIssuesReferences": {
                                    "nodes": [
                                        {
                                            "number": 41830,
                                            "repository": {"nameWithOwner": "other/repo"},
                                        }
                                    ]
                                }
                            }
                        }
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GitHubClient().fetch_linked_issues_for_pull_request("owner/repo", 42) == [
        ("other/repo", 41830)
    ]


def test_fetch_linked_issues_for_pull_request_returns_nothing_when_it_closes_no_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {"data": {"p0": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}}}}
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GitHubClient().fetch_linked_issues_for_pull_request("owner/repo", 42) == []


@pytest.mark.parametrize(
    "skip_error",
    [
        "GraphQL: Resource protected by organization SAML enforcement. "
        "You must grant your OAuth token access to this organization.",
        "Could not resolve to a Repository with the name 'owner/repo'.",
    ],
)
def test_fetch_linked_issues_for_pull_request_raises_the_reason_github_denied_it(
    monkeypatch: pytest.MonkeyPatch, skip_error: str
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            output=json.dumps(
                {
                    "data": {"p0": None},
                    "errors": [{"path": ["p0"], "message": skip_error}],
                }
            ),
            stderr=skip_error,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as raised:
        GitHubClient().fetch_linked_issues_for_pull_request("owner/repo", 42)

    assert str(raised.value) == skip_error


def test_list_open_reviewed_prs_returns_open_prs_reviewed_by_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        command = args[0]
        captured["command"] = command
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                '[{"id":"RV1","number":73,"title":"already reviewed","url":"https://example.com/pull/73",'
                '"createdAt":"2026-03-01T00:00:00Z","updatedAt":"2026-03-02T00:00:00Z","isDraft":false,'
                '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_reviewed_prs("testuser")

    assert captured["command"] == [
        "gh",
        "search",
        "prs",
        "--reviewed-by",
        "testuser",
        "--state",
        "open",
        "--limit",
        "100",
        "--json",
        "id,number,title,url,createdAt,updatedAt,isDraft,repository",
    ]
    assert result == [
        {
            "id": "RV1",
            "repo": "owner/repo",
            "number": 73,
            "title": "already reviewed",
            "url": "https://example.com/pull/73",
            "created_at": "2026-03-01T00:00:00Z",
            "updated_at": "2026-03-02T00:00:00Z",
            "is_draft": False,
            # The review selectors do not ask for CI state, review decision, or
            # pending review requests.
            "ci_state": None,
            "review_decision": None,
            "review_requested": False,
            "has_unresolved_comments": False,
        }
    ]


def test_list_open_reviewed_prs_accepts_custom_reviewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="[]",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_reviewed_prs(reviewer_login="octocat")

    assert result == []
    assert captured["command"][4] == "octocat"


def test_list_open_reviewed_prs_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="authentication failed",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError,
        match="Failed to list open reviewed PRs for 'testuser' via gh",
    ):
        GitHubClient().list_open_reviewed_prs("testuser")


def _tracked_pr_node(
    number: int, *, repo: str = "owner/repo-one", state: str = "OPEN", **overrides: object
) -> dict[str, object]:
    node: dict[str, object] = {
        "__typename": "PullRequest",
        "id": f"PR-{number}",
        "number": number,
        "title": f"pr {number}",
        "url": f"https://example.com/pull/{number}",
        "createdAt": "2026-02-20T00:00:00Z",
        "updatedAt": "2026-02-21T00:00:00Z",
        "state": state,
        "repository": {"nameWithOwner": repo},
    }
    node.update(overrides)
    return node


def _tracked_issue_node(
    number: int, *, repo: str = "owner/repo-one", state: str = "OPEN", **overrides: object
) -> dict[str, object]:
    node: dict[str, object] = {
        "__typename": "Issue",
        "subIssuesSummary": {"total": 0},
        "id": f"ISSUE-{number}",
        "number": number,
        "title": f"issue {number}",
        "url": f"https://example.com/issues/{number}",
        "createdAt": "2026-02-20T00:00:00Z",
        "updatedAt": "2026-02-21T00:00:00Z",
        "state": state,
        "repository": {"nameWithOwner": repo},
    }
    node.update(overrides)
    return node


def _tracked_search_response(nodes: list[dict[str, object]]) -> dict[str, object]:
    return {"nodes": nodes}


def test_list_recent_tracked_items_returns_issue_and_pr_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        captured_commands.append(command)
        assert command[:3] == ["gh", "api", "graphql"]
        assert command[4] == (
            "query={ tracked: search(type: ISSUE, first: 100, "
            'query: "is:open sort:updated-desc user:owner") '
            "{ nodes { __typename "
            "... on PullRequest { id number title url createdAt updatedAt state "
            "repository { nameWithOwner } } "
            "... on Issue { id number title url createdAt updatedAt state "
            "repository { nameWithOwner } subIssuesSummary { total } } } } }"
        )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "tracked": _tracked_search_response(
                            [
                                _tracked_issue_node(1, subIssuesSummary={"total": 3}),
                                _tracked_pr_node(2),
                            ]
                        )
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items("user:owner")

    assert len(captured_commands) == 1
    assert result == [
        {
            "id": "ISSUE-1",
            "repo": "owner/repo-one",
            "number": 1,
            "title": "issue 1",
            "url": "https://example.com/issues/1",
            "created_at": "2026-02-20T00:00:00Z",
            "updated_at": "2026-02-21T00:00:00Z",
            "is_pull_request": False,
            "sub_issue_total": 3,
        },
        {
            "id": "PR-2",
            "repo": "owner/repo-one",
            "number": 2,
            "title": "pr 2",
            "url": "https://example.com/pull/2",
            "created_at": "2026-02-20T00:00:00Z",
            "updated_at": "2026-02-21T00:00:00Z",
            "is_pull_request": True,
        },
    ]


def test_list_recent_tracked_items_returns_empty_list_for_an_empty_search_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise AssertionError("gh should not be called when nothing is tracked")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GitHubClient().list_recent_tracked_items("") == []


def test_list_recent_tracked_items_filters_out_closed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "tracked": _tracked_search_response(
                            [
                                _tracked_pr_node(1, state="CLOSED"),
                                _tracked_issue_node(2, state="CLOSED"),
                                _tracked_issue_node(3, state="OPEN"),
                            ]
                        )
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items("user:owner")

    assert [item["number"] for item in result] == [3]


def test_list_recent_tracked_items_raises_clear_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        GitHubClient().list_recent_tracked_items("user:owner")


def test_list_recent_tracked_items_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="permission denied",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to list recent tracked items via gh"):
        GitHubClient().list_recent_tracked_items("user:owner")


@pytest.mark.parametrize(
    "error_message",
    [
        "Resource protected by organization SAML enforcement.",
        "Could not resolve to a PullRequest with the number of 999.",
    ],
    ids=["denied_repository", "unresolvable_item"],
)
def test_list_recent_tracked_items_keeps_the_resolved_items_when_scope_entry_is_denied(
    error_message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A denied repo or unresolvable item still answers for the rest of the scope."""
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        error_stdout = json.dumps(
            {
                "data": {"tracked": _tracked_search_response([_tracked_issue_node(1)])},
                "errors": [{"message": error_message}],
            }
        )
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            output=error_stdout,
            stderr=error_message,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items("user:owner")

    assert len(calls) == 1
    assert [item["number"] for item in result] == [1]


def test_list_recent_tracked_items_raises_when_an_error_does_not_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated, unclassified error must raise, even alongside a well-formed response."""

    def fake_run(*args, **kwargs):
        command = args[0]
        error_stdout = json.dumps(
            {
                "data": {"tracked": _tracked_search_response([_tracked_issue_node(2)])},
                "errors": [
                    {"message": "Resource protected by organization SAML enforcement."},
                    {"message": "Something else entirely went wrong."},
                ],
            }
        )
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            output=error_stdout,
            stderr="Something else entirely went wrong.",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Something else entirely went wrong."):
        GitHubClient().list_recent_tracked_items("user:owner")


def test_list_recent_tracked_items_retries_on_transient_http_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def fake_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=args[0],
                stderr="HTTP 502: Server Error (https://api.github.com/graphql)",
            )
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({"data": {"tracked": _tracked_search_response([])}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    result = GitHubClient().list_recent_tracked_items("user:owner")
    assert result == []
    assert call_count == 2


def test_list_recent_tracked_items_waits_longer_for_a_secondary_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrying a rate limit after the usual 2s just digs the penalty deeper."""
    call_count = 0

    def fake_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=args[0],
                stderr=(
                    "HTTP 403: You have exceeded a secondary rate limit. "
                    "(https://api.github.com/graphql)"
                ),
            )
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({"data": {"tracked": _tracked_search_response([])}}),
            stderr="",
        )

    sleeps: list[float] = []
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", sleeps.append)

    result = GitHubClient().list_recent_tracked_items("user:owner")

    assert result == []
    assert sleeps == [70.0]


def test_list_recent_tracked_items_raises_clear_error_for_bad_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="{not valid json",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to parse gh recent tracked item JSON"):
        GitHubClient().list_recent_tracked_items("user:owner")


def test_list_recent_tracked_items_raises_clear_error_for_missing_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({"message": "Bad credentials"}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="response contained no data"):
        GitHubClient().list_recent_tracked_items("user:owner")


def test_list_recent_tracked_items_raises_error_for_invalid_payload_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "tracked": _tracked_search_response(
                            [{"__typename": "Issue", "id": "A", "number": 1, "title": "x"}]
                        )
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="entry 0 has missing or invalid url"):
        GitHubClient().list_recent_tracked_items("user:owner")


def test_list_open_assigned_issues_returns_assigned_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        command = args[0]
        captured["command"] = command
        node = _tracked_issue_node(5)
        node["subIssuesSummary"] = {"total": 2}
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps({"data": {"assigned": {"nodes": [node]}}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = GitHubClient().list_open_assigned_issues("testuser")
    command = captured["command"]
    assert command[:3] == ["gh", "api", "graphql"]
    assert "is:issue is:open assignee:testuser sort:updated-desc" in command[4]
    assert "subIssuesSummary { total }" in command[4]
    assert result == [
        {
            "id": "ISSUE-5",
            "repo": "owner/repo-one",
            "number": 5,
            "title": "issue 5",
            "url": "https://example.com/issues/5",
            "created_at": "2026-02-20T00:00:00Z",
            "updated_at": "2026-02-21T00:00:00Z",
            "sub_issue_total": 2,
        }
    ]


def test_list_open_assigned_issues_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="authentication failed",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError, match="Failed to list open assigned issues for 'testuser' via gh"
    ):
        GitHubClient().list_open_assigned_issues("testuser")


@pytest.mark.parametrize(
    ("author_login", "expected_kind"),
    [
        ("testuser", WorkItemKind.AUTHORED_PR),
        # GitHub logins are case-insensitive, so a differently-cased login is
        # still the user's own pull request.
        ("TestUser", WorkItemKind.AUTHORED_PR),
        ("somebody-else", WorkItemKind.TRACKED_PR),
    ],
)
def test_fetch_item_by_url_classifies_a_pull_request_by_its_author(
    monkeypatch: pytest.MonkeyPatch, author_login: str, expected_kind: WorkItemKind
) -> None:
    # An included pull request of the user's own is their work to implement, not
    # somebody else's work to review.
    parsed = parse_github_item_url("https://github.com/owner/repo/pull/1")
    assert parsed is not None
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        captured_commands.append(args[0])
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 1,
                    "title": "Included pull request",
                    "url": "https://github.com/owner/repo/pull/1",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "updatedAt": "2026-01-02T00:00:00Z",
                    "state": "OPEN",
                    "author": {"login": author_login},
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    item = GitHubClient().fetch_item_by_url(parsed, "testuser")

    assert item is not None
    assert item.kind is expected_kind
    assert item.included is True
    assert "author" in captured_commands[0][-1]


def test_fetch_item_by_url_raises_transient_for_malformed_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present-but-unparseable timestamp must surface as TransientFetchError.

    Otherwise the plain RuntimeError from parse_github_datetime escapes the
    included-items refresh loop and the offending URL is silently re-queued
    without the store being re-saved.
    """

    parsed = parse_github_item_url("https://github.com/owner/repo/pull/1")
    assert parsed is not None
    payload = {
        "number": 1,
        "title": "t",
        "url": "https://github.com/owner/repo/pull/1",
        "createdAt": "not-an-iso-ts",
        "updatedAt": "2026-01-01T00:00:00Z",
        "state": "OPEN",
        "isDraft": False,
    }

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TransientFetchError, match="invalid timestamp"):
        GitHubClient().fetch_item_by_url(parsed, "testuser")


@pytest.mark.parametrize(
    ("simulated", "expected"),
    [
        # Transient bucket: upstream 5xx, rate limit, network DNS failures, and
        # a missing gh binary must all be classified as TransientFetchError so
        # the included-items store retains the URL for a later refresh.
        ("stderr:HTTP 503: Service Unavailable", "transient"),
        ("stderr:API rate limit exceeded", "transient"),
        ("stderr:dial tcp: lookup api.github.com: no such host", "transient"),
        ("missing_gh", "transient"),
        # Permanent-drop bucket: state != OPEN and permanent gh errors (e.g.
        # "could not resolve to a PullRequest" = 404) must return None so the
        # URL is dropped from the store.
        ("state:CLOSED", "none"),
        (
            "stderr:could not resolve to a PullRequest with the number of 999",
            "none",
        ),
    ],
)
def test_fetch_item_by_url_classifies_errors_and_states(
    monkeypatch: pytest.MonkeyPatch, simulated: str, expected: str
) -> None:
    parsed = parse_github_item_url("https://github.com/owner/repo/pull/1")
    assert parsed is not None
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    if simulated == "missing_gh":

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("gh")
    elif simulated.startswith("state:"):
        state = simulated.split(":", 1)[1]
        payload = {
            "number": 1,
            "title": "t",
            "url": "https://github.com/owner/repo/pull/1",
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-02T00:00:00Z",
            "state": state,
        }

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args=args[0], returncode=0, stdout=json.dumps(payload), stderr=""
            )
    else:
        stderr_value = simulated.split(":", 1)[1]

        def fake_run(*args, **kwargs):
            raise subprocess.CalledProcessError(returncode=1, cmd=args[0], stderr=stderr_value)

    monkeypatch.setattr(subprocess, "run", fake_run)

    if expected == "transient":
        with pytest.raises(TransientFetchError):
            GitHubClient().fetch_item_by_url(parsed, "testuser")
    else:
        assert GitHubClient().fetch_item_by_url(parsed, "testuser") is None


def test_list_open_todo_issues_reads_targets_from_issue_body_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    payload = [
        {
            "subIssuesSummary": {"total": 0},
            "id": "I110",
            "number": 110,
            "title": "Fix the flaky test",
            "url": "https://github.com/testuser/todos/issues/110",
            "createdAt": "2026-02-01T00:00:00Z",
            "updatedAt": "2026-02-02T00:00:00Z",
            "body": '```json\n{"workdash_todo_version": 1, "target": "owner/repo"}\n```\n',
        },
        {
            "subIssuesSummary": {"total": 0},
            "id": "I111",
            "number": 111,
            "title": "Buy milk",
            "url": "https://github.com/testuser/todos/issues/111",
            "createdAt": "2026-02-03T00:00:00Z",
            "updatedAt": "2026-02-03T00:00:00Z",
            "body": "someone replaced the metadata by hand",
        },
    ]

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps({"data": {"repository": {"issues": {"nodes": payload}}}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = GitHubClient().list_open_todo_issues("testuser/todos")

    assert captured["command"][:3] == ["gh", "api", "graphql"]
    query = captured["command"][-1]
    assert 'labels: ["workdash-todo"]' in query
    assert "states: OPEN" in query
    assert "field: CREATED_AT, direction: DESC" in query
    assert "subIssuesSummary { total } body" in query
    assert [(issue["repo"], issue["number"], issue["target"]) for issue in result] == [
        ("testuser/todos", 110, "owner/repo"),
        ("testuser/todos", 111, None),
    ]


def test_list_open_todo_issues_rejects_a_malformed_gh_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            stdout='{"data":{"repository":{"issues":{"nodes":[{"number":"110"}]}}}}',
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="entry 0 has missing or invalid id"):
        GitHubClient().list_open_todo_issues("testuser/todos")


@pytest.mark.parametrize(
    ("stderr", "tolerated"),
    [
        ("GraphQL: Could not resolve to a Repository with the name 'testuser/todos'.", True),
        # `gh issue list` resolves the repository over GraphQL, so the REST 404 shape
        # is deliberate defensive coverage of the classifier shared with label creation.
        ("HTTP 404: Not Found (https://api.github.com/repos/testuser/todos/issues)", True),
        ("HTTP 401: Bad credentials", False),
        ("HTTP 403: Resource not accessible by integration", False),
        # Incidental mentions of 404 or a not-found reference are other failures.
        ("HTTP 422: Validation Failed (label not found), see 404 handling docs", False),
    ],
)
def test_list_open_todo_issues_tolerates_only_a_todo_repository_that_is_missing(
    stderr: str, tolerated: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The todo repository may not exist yet, but every other gh failure is real."""

    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fake_run)
    progress: list[str] = []

    if not tolerated:
        with pytest.raises(RuntimeError, match="Failed to list open todo issues"):
            GitHubClient().list_open_todo_issues("testuser/todos")
        return
    assert (
        GitHubClient().list_open_todo_issues("testuser/todos", progress_callback=progress.append)
        == []
    )
    assert progress == ["Todo repository testuser/todos does not exist yet."]


def test_list_open_todo_issues_keeps_a_transient_failure_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry-exhausted GitHub outage must not be read as a missing repository."""

    def fake_run(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, stderr="HTTP 503: Service Unavailable (not found in cache)"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    with pytest.raises(TransientFetchError, match="Failed to list open todo issues"):
        GitHubClient().list_open_todo_issues("testuser/todos")


def _item_lookup_run(payload: dict[str, object], *, failed: bool = False):
    def fake_run(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        if failed:
            raise subprocess.CalledProcessError(
                1, command, output=json.dumps(payload), stderr="GraphQL: error"
            )
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    return fake_run


def _item_lookup_payload(repo: str, node: dict[str, object]) -> dict[str, object]:
    fields = {
        "title": "Work",
        "url": f"https://github.com/{repo}/issues/5",
        "createdAt": "2026-02-01T00:00:00Z",
        "updatedAt": "2026-02-02T00:00:00Z",
        "author": {"login": "TestUser"},
    }
    return {"data": {"repository": {"nameWithOwner": repo, "issueOrPullRequest": fields | node}}}


def test_fetch_item_reads_an_authored_pull_request_and_its_own_repository_closing_issues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closing = [
        {"number": 9, "repository": {"nameWithOwner": "owner/repo"}},
        {"number": 3, "repository": {"nameWithOwner": "owner/repo"}},
        {"number": 1, "repository": {"nameWithOwner": "other/repo"}},
    ]
    payload = _item_lookup_payload(
        "owner/repo",
        {"__typename": "PullRequest", "closingIssuesReferences": {"nodes": closing}},
    )
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload))

    item = GitHubClient().fetch_item(
        "Owner/Repo", 5, github_username="testuser", todo_repository="testuser/todos"
    )

    assert item is not None
    assert (item.kind, item.item_type, item.repo, item.number) == (
        WorkItemKind.AUTHORED_PR,
        WorkItemType.PR,
        "owner/repo",
        5,
    )
    assert item.closing_issue_numbers == (3, 9)


@pytest.mark.parametrize(
    ("repo", "expected_target"), [("testuser/todos", "owner/repo"), ("owner/repo", None)]
)
def test_fetch_item_reads_the_todo_target_only_for_todo_repository_issues(
    monkeypatch: pytest.MonkeyPatch, repo: str, expected_target: str | None
) -> None:
    body = '```json\n{"workdash_todo_version": 1, "target": "owner/repo"}\n```\n'
    payload = _item_lookup_payload(
        repo, {"__typename": "Issue", "body": body, "subIssuesSummary": {"total": 0}}
    )
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload))

    item = GitHubClient().fetch_item(
        repo, 5, github_username="testuser", todo_repository="testuser/todos"
    )

    assert item is not None
    assert (item.item_type, item.todo_target) == (WorkItemType.ISSUE, expected_target)


@pytest.mark.parametrize(
    "message",
    [
        "Could not resolve to an issue or pull request with the number of 5.",
        "Could not resolve to a Repository with the name 'owner/gone'.",
        "Resource protected by organization SAML enforcement.",
    ],
)
def test_fetch_item_reports_missing_or_inaccessible_items_as_none(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    payload = {"data": {"repository": None}, "errors": [{"message": message}]}
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload, failed=True))

    assert (
        GitHubClient().fetch_item(
            "owner/repo", 5, github_username="testuser", todo_repository="testuser/todos"
        )
        is None
    )


def test_fetch_item_raises_on_other_github_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"data": None, "errors": [{"message": "Something went wrong while executing"}]}
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload, failed=True))

    with pytest.raises(RuntimeError, match="Something went wrong while executing"):
        GitHubClient().fetch_item(
            "owner/repo", 5, github_username="testuser", todo_repository="testuser/todos"
        )


def test_assigned_graphql_search_keeps_resolved_open_issues_and_rejects_other_errors(monkeypatch):
    node = _tracked_issue_node(1, subIssuesSummary={"total": 0})
    payload = {
        "data": {"assigned": {"nodes": [node, _tracked_issue_node(2, state="CLOSED")]}},
        "errors": [{"message": "Could not resolve to a Repository"}],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    result = GitHubClient().list_open_assigned_issues("user")
    assert [(item["number"], item["sub_issue_total"]) for item in result] == [(1, 0)]
    payload["errors"] = [{"message": "API rate limit exceeded"}]
    with pytest.raises(RuntimeError, match="API rate limit exceeded"):
        GitHubClient().list_open_assigned_issues("user")


@pytest.mark.parametrize(
    "message",
    ["Could not resolve to a Repository", "Resource protected by organization SAML enforcement."],
)
def test_assigned_search_skips_only_null_nodes_with_inaccessible_errors(monkeypatch, message):
    payload = {
        "data": {"assigned": {"nodes": [_tracked_issue_node(1), None]}},
        "errors": [{"message": message, "path": ["assigned", "nodes", 1]}],
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps(payload), stderr=""
        ),
    )
    assert [item["number"] for item in GitHubClient().list_open_assigned_issues("user")] == [1]
    payload["errors"][0]["path"] = ["assigned", "nodes", 0]
    with pytest.raises(RuntimeError):
        GitHubClient().list_open_assigned_issues("user")
    payload["errors"] = []
    with pytest.raises(RuntimeError):
        GitHubClient().list_open_assigned_issues("user")


def test_graphql_todos_include_target_and_sub_issue_count(monkeypatch):
    node = {
        "id": "I1",
        "number": 1,
        "title": "Todo",
        "url": "https://github.com/u/t/issues/1",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "state": "OPEN",
        "repository": {"nameWithOwner": "u/t"},
        "subIssuesSummary": {"total": 2},
        "body": '```json\n{"workdash_todo_version": 1, "target": "o/r"}\n```',
    }
    payload = {"data": {"repository": {"issues": {"nodes": [node]}}}}
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload))
    item = GitHubClient().list_open_todo_issues("u/t")[0]
    assert (item["target"], item["sub_issue_total"]) == ("o/r", 2)


def test_direct_issue_includes_native_sub_issue_count(monkeypatch):
    payload = _item_lookup_payload(
        "owner/repo", {"__typename": "Issue", "subIssuesSummary": {"total": 2}}
    )
    run = MagicMock(side_effect=_item_lookup_run(payload))
    monkeypatch.setattr(subprocess, "run", run)
    item = GitHubClient().fetch_item("owner/repo", 5, github_username="u", todo_repository="u/t")
    assert item is not None and item.has_sub_issues and item.sub_issue_total == 2
    query = run.call_args.args[0][4]
    issue_selection = query.split("... on Issue {", 1)[1].split("... on PullRequest", 1)[0]
    assert "subIssuesSummary { total }" in issue_selection


def test_included_issue_uses_graphql_and_retains_sub_issue_count(monkeypatch):
    payload = _item_lookup_payload(
        "owner/repo", {"__typename": "Issue", "state": "OPEN", "subIssuesSummary": {"total": 2}}
    )
    payload["data"]["repository"]["issue"] = payload["data"]["repository"].pop("issueOrPullRequest")
    run = MagicMock(side_effect=_item_lookup_run(payload))
    monkeypatch.setattr(subprocess, "run", run)
    parsed = parse_github_item_url("https://github.com/owner/repo/issues/5")
    item = GitHubClient().fetch_item_by_url(parsed, "u")
    assert item is not None and item.included and item.has_sub_issues
    query = run.call_args.args[0][4]
    issue_selection = query.split("issue(number: 5) {", 1)[1]
    assert "subIssuesSummary { total }" in issue_selection


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"data": {"repository": {"issue": {"state": "CLOSED"}}}}, "none"),
        (
            {
                "data": {"repository": None},
                "errors": [{"message": "Could not resolve to a Repository"}],
            },
            "none",
        ),
        ({"data": {"repository": {"issue": {"state": "OPEN"}}}}, "transient"),
        ({"errors": [{"message": "API rate limit exceeded"}]}, "transient"),
        ({"errors": [{"message": "Bad credentials"}]}, "none"),
        (
            {
                "errors": [
                    {"message": "missing", "type": "NOT_FOUND", "path": ["repository", "issue"]}
                ]
            },
            "none",
        ),
        (
            {
                "errors": [
                    {
                        "message": "missing",
                        "type": "NOT_FOUND",
                        "path": ["repository", "issue", "title"],
                    }
                ]
            },
            "transient",
        ),
        (
            {
                "errors": [
                    {"message": "Could not resolve to a Repository"},
                    {"message": "Bad credentials"},
                ]
            },
            "none",
        ),
    ],
)
def test_included_issue_graphql_preserves_drop_and_retry_semantics(monkeypatch, response, expected):
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(response))
    parsed = parse_github_item_url("https://github.com/owner/repo/issues/5")
    if expected == "transient":
        with pytest.raises(TransientFetchError):
            GitHubClient().fetch_item_by_url(parsed, "u")
    else:
        assert GitHubClient().fetch_item_by_url(parsed, "u") is None


def test_included_issue_permanent_command_error_is_dropped(monkeypatch):
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command, stderr="Bad credentials")

    monkeypatch.setattr(subprocess, "run", fail)
    parsed = parse_github_item_url("https://github.com/owner/repo/issues/5")
    assert GitHubClient().fetch_item_by_url(parsed, "u") is None


def test_todo_rejects_invalid_native_sub_issue_count(monkeypatch):
    node = {
        "id": "I1",
        "number": 1,
        "title": "Todo",
        "url": "https://github.com/u/t/issues/1",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "subIssuesSummary": {"total": "invalid"},
    }
    payload = {"data": {"repository": {"issues": {"nodes": [node]}}}}
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload))
    with pytest.raises(RuntimeError, match="subIssuesSummary"):
        GitHubClient().list_open_todo_issues("u/t")


def test_direct_issue_rejects_invalid_sub_issue_count(monkeypatch):
    payload = _item_lookup_payload(
        "owner/repo", {"__typename": "Issue", "subIssuesSummary": {"total": "bad"}}
    )
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload))
    with pytest.raises(RuntimeError, match="subIssuesSummary"):
        GitHubClient().fetch_item("owner/repo", 5, github_username="u", todo_repository="u/t")


@pytest.mark.parametrize("source", ["assigned", "tracked", "todo", "direct", "included"])
@pytest.mark.parametrize("message", ["summary unavailable", "API rate limit exceeded"])
def test_summary_partial_error_keeps_issue_without_highlight(monkeypatch, source, message):
    node = _tracked_issue_node(5, subIssuesSummary=None, body="", author={"login": "u"})
    if source in ("assigned", "tracked"):
        path = [source, "nodes", 0]
        payload = {"data": {source: {"nodes": [node]}}}
    elif source == "todo":
        path = ["repository", "issues", "nodes", 0]
        payload = {"data": {"repository": {"issues": {"nodes": [node]}}}}
    else:
        key = "issueOrPullRequest" if source == "direct" else "issue"
        path = ["repository", key]
        payload = {"data": {"repository": {"nameWithOwner": "owner/repo", key: node}}}
    payload["errors"] = [{"message": message, "path": path + ["subIssuesSummary"]}]
    calls = []

    def failed_gh(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=json.dumps(payload), stderr=f"GraphQL: {message}"
        )

    monkeypatch.setattr(subprocess, "run", failed_gh)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)
    client = GitHubClient()
    if source == "assigned":
        item = client.list_open_assigned_issues("u")[0]
    elif source == "tracked":
        item = client.list_recent_tracked_items("repo:owner/repo")[0]
    elif source == "todo":
        item = client.list_open_todo_issues("u/t")[0]
    elif source == "direct":
        item = client.fetch_item("owner/repo", 5, github_username="u", todo_repository="u/t")
    else:
        item = client.fetch_item_by_url(
            parse_github_item_url("https://github.com/owner/repo/issues/5"), "u"
        )
    assert item is not None
    assert len(calls) == 1
    if isinstance(item, dict):
        assert item.get("sub_issue_total") is None
    else:
        assert item.sub_issue_total is None and not item.has_sub_issues


@pytest.mark.parametrize("source", ["assigned", "tracked"])
@pytest.mark.parametrize(
    "message",
    ["Resource protected by organization SAML enforcement", "Could not resolve to a Repository"],
)
def test_search_keeps_results_with_transient_summary_and_inaccessible_item(
    monkeypatch, source, message
):
    node = _tracked_issue_node(5, subIssuesSummary=None)
    payload = {
        "data": {source: {"nodes": [node]}},
        "errors": [
            {
                "message": "API rate limit exceeded",
                "path": [source, "nodes", 0, "subIssuesSummary"],
            },
            {"message": message, "path": [source, "nodes", 1]},
        ],
    }
    calls = []

    def failed_gh(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1,
            command,
            output=json.dumps(payload),
            stderr=f"GraphQL: API rate limit exceeded; {message}",
        )

    monkeypatch.setattr(subprocess, "run", failed_gh)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)
    client = GitHubClient()
    items = (
        client.list_open_assigned_issues("u")
        if source == "assigned"
        else client.list_recent_tracked_items("repo:owner/repo")
    )
    assert [(item["number"], item.get("sub_issue_total")) for item in items] == [(5, None)]
    assert len(calls) == 1


def test_search_does_not_accept_fatal_error_with_transient_summary_and_denial(monkeypatch):
    payload = {
        "data": {"tracked": {"nodes": [_tracked_issue_node(5, subIssuesSummary=None)]}},
        "errors": [
            {
                "message": "API rate limit exceeded",
                "path": ["tracked", "nodes", 0, "subIssuesSummary"],
            },
            {"message": "Resource protected by organization SAML enforcement"},
            {"message": "Unexpected query failure", "path": ["tracked"]},
        ],
    }

    def failed_gh(command, **kwargs):
        raise subprocess.CalledProcessError(
            1, command, output=json.dumps(payload), stderr="GraphQL: API rate limit exceeded"
        )

    monkeypatch.setattr(subprocess, "run", failed_gh)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)
    with pytest.raises(TransientFetchError):
        GitHubClient().list_recent_tracked_items("repo:owner/repo")


@pytest.mark.parametrize("other_error", [False, True])
def test_transient_partial_response_retries_without_usable_summary_only_data(
    monkeypatch, other_error
):
    node = _tracked_issue_node(5, subIssuesSummary=None)
    errors = [
        {"message": "API rate limit exceeded", "path": ["tracked", "nodes", 0, "subIssuesSummary"]}
    ]
    if other_error:
        errors.append(
            {"message": "API rate limit exceeded", "path": ["tracked", "nodes", 0, "title"]}
        )
    payload = {"data": {"tracked": {"nodes": [node]}} if other_error else None, "errors": errors}
    calls = []

    def failed_gh(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, output=json.dumps(payload), stderr="GraphQL: API rate limit exceeded"
        )

    monkeypatch.setattr(subprocess, "run", failed_gh)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)
    with pytest.raises(TransientFetchError):
        GitHubClient().list_recent_tracked_items("repo:owner/repo")
    assert len(calls) == 3


@pytest.mark.parametrize("source", ["assigned", "tracked"])
@pytest.mark.parametrize("summary", [None, {}, {"total": None}, {"total": True}, {"total": -1}])
def test_search_rejects_missing_or_invalid_issue_summary(monkeypatch, source, summary):
    node = _tracked_issue_node(1, subIssuesSummary=summary)
    payload = {"data": {source: {"nodes": [node]}}}
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload))
    client = GitHubClient()
    with pytest.raises(RuntimeError, match="subIssuesSummary"):
        if source == "assigned":
            client.list_open_assigned_issues("u")
        else:
            client.list_recent_tracked_items("repo:owner/repo")


def test_summary_error_does_not_hide_another_search_error_or_other_issue_failure(monkeypatch):
    nodes = [
        _tracked_issue_node(1, subIssuesSummary=None),
        _tracked_issue_node(2, subIssuesSummary=None),
    ]
    payload = {
        "data": {"tracked": {"nodes": nodes}},
        "errors": [
            {"message": "summary unavailable", "path": ["tracked", "nodes", 0, "subIssuesSummary"]}
        ],
    }
    monkeypatch.setattr(subprocess, "run", _item_lookup_run(payload, failed=True))
    with pytest.raises(RuntimeError, match="subIssuesSummary"):
        GitHubClient().list_recent_tracked_items("repo:owner/repo")
    nodes[1]["subIssuesSummary"] = {"total": 0}
    payload["errors"].append(
        {"message": "unexpected failure", "path": ["tracked", "nodes", 1, "title"]}
    )
    with pytest.raises(RuntimeError, match="unexpected failure"):
        GitHubClient().list_recent_tracked_items("repo:owner/repo")


def test_linked_open_pull_request_paginates_and_returns_cross_repository_pr(monkeypatch):
    from datetime import UTC, datetime

    from workdash.models import WorkItem

    item = WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=42,
        title="Issue",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        url="https://github.com/owner/repo/issues/42",
    )
    queries = []

    def run(command, **kwargs):
        query = command[-1]
        queries.append(query)
        if len(queries) == 1:
            assert 'repository(owner: "owner", name: "repo")' in query
            assert "issue(number: 42)" in query
            assert "after: null" in query
            nodes = [
                {"state": "MERGED", "number": 3, "repository": {"nameWithOwner": "owner/repo"}}
            ]
            page = {"hasNextPage": True, "endCursor": 'cursor"1'}
        else:
            assert 'after: "cursor\\"1"' in query
            nodes = [{"state": "OPEN", "number": 9, "repository": {"nameWithOwner": "other/fork"}}]
            page = {"hasNextPage": True, "endCursor": "unused"}
        data = {
            "repository": {
                "issue": {"closedByPullRequestsReferences": {"nodes": nodes, "pageInfo": page}}
            }
        }
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"data": data}), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert GitHubClient().linked_open_pull_request(item) == ("other/fork", 9)
    assert len(queries) == 2


def test_linked_open_pull_request_none_and_replaceable(monkeypatch):
    from datetime import UTC, datetime

    from workdash.models import WorkItem

    item = WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=42,
        title="Issue",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        url="https://github.com/owner/repo/issues/42",
    )
    data = {
        "repository": {
            "issue": {
                "closedByPullRequestsReferences": {
                    "nodes": [
                        {
                            "state": "CLOSED",
                            "number": 1,
                            "repository": {"nameWithOwner": "owner/repo"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"data": data}), stderr=""
        ),
    )
    client = GitHubClient()
    assert client.linked_open_pull_request(item) is None
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected gh"))
    client.linked_open_pull_request = MagicMock(return_value=("fork/repo", 7))
    assert client.linked_open_pull_request(item) == ("fork/repo", 7)
    client.linked_open_pull_request.assert_called_once_with(item)
