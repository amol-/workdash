import json
import subprocess

import pytest

from workdash.github_client import (
    GitHubClient,
    ParsedGitHubItemURL,
    RepositoryAuthorizationError,
    TransientFetchError,
    parse_github_item_url,
)
from workdash.models import WorkItemType


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
    """Return a GraphQL authored-search response wrapping ``nodes``."""

    return json.dumps({"data": {"search": {"nodes": nodes or []}}})


def test_list_open_authored_prs_returns_open_prs_including_drafts_and_forks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        command = args[0]
        captured["command"] = command
        captured["check"] = kwargs.get("check")
        captured["capture_output"] = kwargs.get("capture_output")
        captured["text"] = kwargs.get("text")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=json.dumps(
                {
                    "data": {
                        "search": {
                            "nodes": [
                                {
                                    "id": "A",
                                    "number": 10,
                                    "title": "draft pr",
                                    "url": "https://example.com/pull/10",
                                    "createdAt": "2026-02-01T00:00:00Z",
                                    "updatedAt": "2026-02-02T00:00:00Z",
                                    "isDraft": True,
                                    "repository": {"nameWithOwner": "someone/fork-repo"},
                                    "commits": {
                                        "nodes": [
                                            {"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}
                                        ]
                                    },
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
                                    # A pull request with no checks configured.
                                    "commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]},
                                },
                            ]
                        }
                    }
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_authored_prs("testuser")

    assert captured["check"] is True
    assert captured["capture_output"] is True
    assert captured["text"] is True
    assert captured["command"][:4] == ["gh", "api", "graphql", "-f"]
    query = captured["command"][4]
    assert 'search(query: "author:testuser is:pr is:open", type: ISSUE, first: 100)' in query
    assert "statusCheckRollup { state }" in query
    # Drafts and fork pull requests are deliberately not filtered out.
    assert "draft:" not in query
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
        },
    ]


def test_list_open_authored_prs_accepts_custom_author_and_limit(
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
    result = GitHubClient().list_open_authored_prs(author_login="octocat", limit=25)

    assert result == []
    assert (
        'search(query: "author:octocat is:pr is:open", type: ISSUE, first: 25)'
        in (captured["command"][4])
    )


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


def test_list_open_authored_prs_retries_on_transient_http_5xx(
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
                stderr=(
                    "non-200 OK status code: 504 Gateway Timeout "
                    "(https://api.github.com/search/issues?...)"
                ),
            )
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=_authored_search_stdout(),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    result = GitHubClient().list_open_authored_prs("testuser")
    assert result == []
    assert call_count == 2


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


def test_list_open_authored_prs_raises_when_the_response_carries_no_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Expired credentials answer with a message instead of data, which must not
    # be mistaken for "this user authored no open pull requests".
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='{"message":"Bad credentials","status":"401"}',
            stderr="Bad credentials",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Bad credentials"):
        GitHubClient().list_open_authored_prs("testuser")


def test_list_open_review_requested_prs_returns_directly_requested_prs_in_one_graphql_call(
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
                    '"repository":{"name":"repo","nameWithOwner":"owner/repo"}},'
                    '{"id":"R2","number":43,"title":"team review","url":"https://example.com/pull/43",'
                    '"createdAt":"2026-02-05T00:00:00Z","updatedAt":"2026-02-06T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"other","nameWithOwner":"owner/other"}}]'
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            assert 'repository(owner: "owner", name: "repo")' in command[4]
            assert "pullRequest(number: 42)" in command[4]
            assert 'repository(owner: "owner", name: "other")' in command[4]
            assert "pullRequest(number: 43)" in command[4]
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=json.dumps(
                    {
                        "data": {
                            "p0": {
                                "pullRequest": {
                                    "reviewRequests": {
                                        "nodes": [
                                            # GitHub emits a null reviewer for a
                                            # request it can no longer resolve.
                                            {"requestedReviewer": None},
                                            {
                                                "requestedReviewer": {
                                                    "__typename": "User",
                                                    "login": "testuser",
                                                }
                                            },
                                        ]
                                    },
                                }
                            },
                            "p1": {
                                "pullRequest": {
                                    "reviewRequests": {
                                        "nodes": [
                                            {
                                                "requestedReviewer": {
                                                    "__typename": "Team",
                                                    "slug": "owner/reviewers",
                                                }
                                            }
                                        ]
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

    result = GitHubClient().list_open_review_requested_prs("testuser")

    assert captured_commands[0] == [
        "gh",
        "search",
        "prs",
        "--review-requested",
        "testuser",
        "--state",
        "open",
        "--limit",
        "1000",
        "--json",
        "id,number,title,url,createdAt,updatedAt,isDraft,repository",
    ]
    # Every pull request's reviewers come from one aliased query, no matter how
    # many pull requests the search returned.
    assert [
        command for command in captured_commands if command[:3] == ["gh", "api", "graphql"]
    ] == [captured_commands[1]]
    assert captured_commands[1][3] == "-f"
    # Only the pull request that named the reviewer survives; the team-only one
    # is dropped.
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
            # The review selectors do not ask for CI state.
            "ci_state": None,
        }
    ]


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
def test_list_open_review_requested_prs_warns_and_skips_an_unreachable_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    error_type: str,
    skip_error: str,
    expected_fragment: str,
) -> None:
    warnings: list[str] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '[{"id":"PRIVATE","number":860,"title":"private review",'
                    '"url":"https://example.com/pull/860",'
                    '"createdAt":"2026-02-05T00:00:00Z",'
                    '"updatedAt":"2026-02-06T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"protected-repo","nameWithOwner":"protected-org/protected-repo"}},'
                    '{"id":"PUBLIC","number":42,"title":"public review",'
                    '"url":"https://example.com/pull/42",'
                    '"createdAt":"2026-02-07T00:00:00Z",'
                    '"updatedAt":"2026-02-08T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            # GitHub answers the readable aliases and reports the denied one as
            # an error, which gh surfaces as a non-zero exit.
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                output=json.dumps(
                    {
                        "data": {
                            "p0": None,
                            "p1": {
                                "pullRequest": {
                                    "reviewRequests": {
                                        "nodes": [
                                            {
                                                "requestedReviewer": {
                                                    "__typename": "User",
                                                    "login": "testuser",
                                                }
                                            }
                                        ]
                                    }
                                }
                            },
                        },
                        "errors": [
                            {
                                "type": error_type,
                                "path": ["p0"],
                                "message": skip_error,
                            }
                        ],
                    }
                ),
                stderr=skip_error,
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_open_review_requested_prs(
        "testuser",
        progress_callback=warnings.append,
    )

    assert [item["number"] for item in result] == [42]
    assert any(
        "Warning: skipped review-requested pull request protected-org/protected-repo#860" in warning
        and expected_fragment in warning
        for warning in warnings
    )


def test_list_open_review_requested_prs_reraises_non_auth_metadata_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warnings: list[str] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '[{"id":"BROKEN","number":42,"title":"broken review",'
                    '"url":"https://example.com/pull/42",'
                    '"createdAt":"2026-02-07T00:00:00Z",'
                    '"updatedAt":"2026-02-08T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr="permission denied",
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="permission denied"):
        GitHubClient().list_open_review_requested_prs(
            "testuser",
            progress_callback=warnings.append,
        )

    assert not any(
        "Warning: skipped review-requested pull request" in warning for warning in warnings
    )


def test_list_open_review_requested_prs_retries_a_graphql_execution_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A batched query that times out comes back as a partial answer plus this
    # error, which one retry usually cures; the dashboard must keep the previous
    # REVIEW list instead of failing the whole refresh.
    timeout_error = (
        "Something went wrong while executing your query. This may be the result "
        "of a timeout, or it could be a GitHub bug. Please include "
        "`AB12:CD34:5678` when reporting this issue."
    )
    graphql_attempts = 0

    def fake_run(*args, **kwargs):
        nonlocal graphql_attempts
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '[{"id":"R1","number":42,"title":"needs review",'
                    '"url":"https://example.com/pull/42",'
                    '"createdAt":"2026-02-05T00:00:00Z",'
                    '"updatedAt":"2026-02-06T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            graphql_attempts += 1
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                output=json.dumps({"data": {"p0": None}, "errors": [{"message": timeout_error}]}),
                stderr=timeout_error,
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    with pytest.raises(TransientFetchError, match="result of a timeout"):
        GitHubClient().list_open_review_requested_prs("testuser")

    # One attempt plus the two retries the retry budget allows.
    assert graphql_attempts == 3


@pytest.mark.parametrize(
    ("metadata_stdout", "metadata_stderr", "expected_message"),
    [
        # A whole-query failure must not be read as "nobody requested a review".
        ('{"message":"Bad credentials","status":"401"}', "Bad credentials", "Bad credentials"),
        # The payload gate fires on its own, even with nothing on stderr to echo.
        ('{"message":"Bad credentials","status":"401"}', "", "response contained no data"),
        # An error that leaves the pull request reachable is not a reason to
        # skip it, so it stays a hard failure that names the pull request.
        (
            '{"data":{"p0":null},"errors":[{"type":"INTERNAL","path":["p0"],'
            '"message":"Something went wrong."}]}',
            "Something went wrong",
            r"\(owner/repo#42\): Something went wrong",
        ),
        # A skip with no path, or with an alias this query never asked for,
        # cannot be attributed to one pull request: dropping it silently would
        # hide the pull request from the REVIEW section.
        (
            '{"data":{"p0":null},"errors":[{"type":"FORBIDDEN",'
            '"message":"Resource protected by organization SAML enforcement."}]}',
            "SAML enforcement",
            "SAML enforcement",
        ),
        (
            '{"data":{"p0":null},"errors":[{"type":"FORBIDDEN","path":["p7"],'
            '"message":"Resource protected by organization SAML enforcement."}]}',
            "SAML enforcement",
            "SAML enforcement",
        ),
        # Output gh printed but we cannot parse is not "no direct request"
        # either.
        ("not json", "not json", "Failed to parse gh review-request metadata JSON"),
        # A nulled alias with no error to explain it, and a reviewRequests object
        # missing its nodes, are both unreadable metadata rather than "no direct
        # request".
        ('{"data":{"p0":null}}', "", "owner/repo#42: expected an object"),
        (
            '{"data":{"p0":{"pullRequest":{"reviewRequests":{}}}}}',
            "",
            "owner/repo#42: missing or invalid reviewRequests",
        ),
    ],
)
def test_list_open_review_requested_prs_raises_on_unusable_metadata_response(
    monkeypatch: pytest.MonkeyPatch,
    metadata_stdout: str,
    metadata_stderr: str,
    expected_message: str,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "search", "prs"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '[{"id":"R1","number":42,"title":"needs review",'
                    '"url":"https://example.com/pull/42",'
                    '"createdAt":"2026-02-07T00:00:00Z",'
                    '"updatedAt":"2026-02-08T00:00:00Z","isDraft":false,'
                    '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
                ),
                stderr="",
            )
        if command[:3] == ["gh", "api", "graphql"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                output=metadata_stdout,
                stderr=metadata_stderr,
            )
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match=expected_message):
        GitHubClient().list_open_review_requested_prs("testuser")


def test_list_open_review_requested_prs_accepts_custom_reviewer_and_limit(
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

    result = GitHubClient().list_open_review_requested_prs(
        reviewer_login="octocat",
        limit=25,
    )

    assert result == []
    assert captured["command"][4] == "octocat"
    assert captured["command"][8] == "25"


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
        "1000",
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
            # The review selectors do not ask for CI state.
            "ci_state": None,
        }
    ]


def test_list_open_reviewed_prs_accepts_custom_reviewer_and_limit(
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

    result = GitHubClient().list_open_reviewed_prs(reviewer_login="octocat", limit=25)

    assert result == []
    assert captured["command"][4] == "octocat"
    assert captured["command"][8] == "25"


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


def test_list_recent_tracked_items_returns_issue_and_pr_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        captured_commands.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                '[{"id":"ISSUE-1","number":1,"title":"issue title","url":"https://example.com/issues/1",'
                '"createdAt":"2026-02-20T00:00:00Z","updatedAt":"2026-02-21T00:00:00Z","state":"OPEN","isPullRequest":false,'
                '"repository":{"name":"repo-one","nameWithOwner":"owner/repo-one"}},'
                '{"id":"PR-2","number":2,"title":"pr title","url":"https://example.com/pull/2",'
                '"createdAt":"2026-02-22T00:00:00Z","updatedAt":"2026-02-23T00:00:00Z","state":"OPEN","isPullRequest":true,'
                '"repository":{"name":"repo-one","nameWithOwner":"owner/repo-one"}}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items(
        repositories=["owner/repo-one", "owner/repo-two"]
    )

    assert captured_commands == [
        [
            "gh",
            "search",
            "issues",
            "--include-prs",
            "--state",
            "open",
            "--sort",
            "updated",
            "--order",
            "desc",
            "--limit",
            "1000",
            "--json",
            "id,number,title,url,createdAt,updatedAt,state,isPullRequest,repository",
            "--repo",
            "owner/repo-one",
            "--repo",
            "owner/repo-two",
        ]
    ]
    assert result == [
        {
            "id": "ISSUE-1",
            "repo": "owner/repo-one",
            "number": 1,
            "title": "issue title",
            "url": "https://example.com/issues/1",
            "created_at": "2026-02-20T00:00:00Z",
            "updated_at": "2026-02-21T00:00:00Z",
            "is_pull_request": False,
        },
        {
            "id": "PR-2",
            "repo": "owner/repo-one",
            "number": 2,
            "title": "pr title",
            "url": "https://example.com/pull/2",
            "created_at": "2026-02-22T00:00:00Z",
            "updated_at": "2026-02-23T00:00:00Z",
            "is_pull_request": True,
        },
    ]


def test_list_recent_tracked_items_deduplicates_by_repo_number_and_item_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "id": "ISSUE-1",
                        "number": 1,
                        "title": "first issue",
                        "url": "https://example.com/issues/1",
                        "createdAt": "2026-02-20T00:00:00Z",
                        "updatedAt": "2026-02-21T00:00:00Z",
                        "state": "OPEN",
                        "isPullRequest": False,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                    {
                        "id": "ISSUE-1-DUPLICATE",
                        "number": 1,
                        "title": "duplicate issue",
                        "url": "https://example.com/issues/1-duplicate",
                        "createdAt": "2026-02-22T00:00:00Z",
                        "updatedAt": "2026-02-23T00:00:00Z",
                        "state": "OPEN",
                        "isPullRequest": False,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                    {
                        "id": "PR-1",
                        "number": 1,
                        "title": "same-number pr",
                        "url": "https://example.com/pull/1",
                        "createdAt": "2026-02-24T00:00:00Z",
                        "updatedAt": "2026-02-25T00:00:00Z",
                        "state": "OPEN",
                        "isPullRequest": True,
                        "repository": {"nameWithOwner": "owner/repo"},
                    },
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items(["owner/repo"])

    assert [(item["id"], item["number"], item["is_pull_request"]) for item in result] == [
        ("ISSUE-1", 1, False),
        ("PR-1", 1, True),
    ]


def test_list_recent_tracked_items_raises_clear_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        GitHubClient().list_recent_tracked_items(["owner/repo"])


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

    with pytest.raises(
        RuntimeError,
        match="Failed to list recent tracked items for repository batch",
    ):
        GitHubClient().list_recent_tracked_items(["owner/repo"])


def test_list_recent_tracked_items_warns_and_skips_saml_protected_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    warnings: list[str] = []
    saml_error = (
        "GraphQL: Resource protected by organization SAML enforcement. "
        "You must grant your OAuth token access to this organization."
    )

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        repositories = [
            command[index + 1] for index, token in enumerate(command) if token == "--repo"
        ]
        if repositories == ["owner/private", "owner/public"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr=saml_error,
            )
        if repositories == ["owner/private"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr=saml_error,
            )
        if repositories == ["owner/public"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '[{"id":"ISSUE-1","number":1,"title":"issue title",'
                    '"url":"https://example.com/issues/1",'
                    '"createdAt":"2026-02-20T00:00:00Z",'
                    '"updatedAt":"2026-02-21T00:00:00Z",'
                    '"state":"OPEN","isPullRequest":false,'
                    '"repository":{"name":"public","nameWithOwner":"owner/public"}}]'
                ),
                stderr="",
            )
        raise AssertionError(f"Unexpected repositories: {repositories}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items(
        ["owner/private", "owner/public"],
        progress_callback=warnings.append,
    )

    assert [item["repo"] for item in result] == ["owner/public"]
    assert any(
        "Warning: skipped repository owner/private" in warning and "SAML enforcement" in warning
        for warning in warnings
    )
    assert [
        [command[index + 1] for index, token in enumerate(command) if token == "--repo"]
        for command in calls
    ] == [
        ["owner/private", "owner/public"],
        ["owner/private"],
        ["owner/public"],
    ]


def test_recent_tracked_items_search_raises_typed_repository_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    saml_error = (
        "GraphQL: Resource protected by organization SAML enforcement. "
        "You must grant your OAuth token access to this organization."
    )

    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr=saml_error,
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RepositoryAuthorizationError, match="SAML enforcement"):
        GitHubClient()._run_recent_tracked_items_search(
            ["owner/private"],
            limit=1000,
            report_progress=lambda _: None,
            retry_label="Repository owner/private",
        )


def test_list_recent_tracked_items_reraises_permanent_error_after_auth_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    warnings: list[str] = []
    saml_error = (
        "GraphQL: Resource protected by organization SAML enforcement. "
        "You must grant your OAuth token access to this organization."
    )

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        repositories = [
            command[index + 1] for index, token in enumerate(command) if token == "--repo"
        ]
        if repositories == ["owner/private", "owner/broken"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr=saml_error,
            )
        if repositories == ["owner/private"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr=saml_error,
            )
        if repositories == ["owner/broken"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr="permission denied",
            )
        raise AssertionError(f"Unexpected repositories: {repositories}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="permission denied"):
        GitHubClient().list_recent_tracked_items(
            ["owner/private", "owner/broken"],
            progress_callback=warnings.append,
        )

    assert any("Warning: skipped repository owner/private" in warning for warning in warnings)
    assert [
        [command[index + 1] for index, token in enumerate(command) if token == "--repo"]
        for command in calls
    ] == [
        ["owner/private", "owner/broken"],
        ["owner/private"],
        ["owner/broken"],
    ]


def test_list_recent_tracked_items_does_not_skip_generic_auth_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    warnings: list[str] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=command,
            stderr="This operation requires additional authorization from security.",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="requires additional authorization"):
        GitHubClient().list_recent_tracked_items(
            ["owner/private", "owner/public"],
            progress_callback=warnings.append,
        )

    assert not any("Warning: skipped repository" in warning for warning in warnings)
    assert [
        [command[index + 1] for index, token in enumerate(command) if token == "--repo"]
        for command in calls
    ] == [["owner/private", "owner/public"]]


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
                stderr="HTTP 502: Server Error (https://api.github.com/search/issues?...)",
            )
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="[]",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("workdash.github_client.time.sleep", lambda _: None)

    result = GitHubClient().list_recent_tracked_items(["owner/repo"])
    assert result == []
    assert call_count == 2


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
        GitHubClient().list_recent_tracked_items(["owner/repo"])


def test_list_recent_tracked_items_raises_error_for_invalid_payload_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '[{"id":"A","number":1,"title":"x","url":"u","createdAt":"c",'
                '"updatedAt":"u","state":"OPEN"}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="entry 0 has missing or invalid isPullRequest"):
        GitHubClient().list_recent_tracked_items(["owner/repo"])


def test_list_recent_tracked_items_filters_out_closed_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=(
                '[{"id":"PR-1","number":1,"title":"closed pr","url":"https://example.com/pull/1",'
                '"createdAt":"2026-02-20T00:00:00Z","updatedAt":"2026-02-21T00:00:00Z","state":"CLOSED","isPullRequest":true,'
                '"repository":{"name":"repo-one","nameWithOwner":"owner/repo-one"}},'
                '{"id":"ISSUE-2","number":2,"title":"closed issue","url":"https://example.com/issues/2",'
                '"createdAt":"2026-02-22T00:00:00Z","updatedAt":"2026-02-23T00:00:00Z","state":"CLOSED","isPullRequest":false,'
                '"repository":{"name":"repo-one","nameWithOwner":"owner/repo-one"}},'
                '{"id":"ISSUE-3","number":3,"title":"open issue","url":"https://example.com/issues/3",'
                '"createdAt":"2026-02-24T00:00:00Z","updatedAt":"2026-02-25T00:00:00Z","state":"OPEN","isPullRequest":false,'
                '"repository":{"name":"repo-one","nameWithOwner":"owner/repo-one"}}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = GitHubClient().list_recent_tracked_items(["owner/repo-one"])

    assert result == [
        {
            "id": "ISSUE-3",
            "repo": "owner/repo-one",
            "number": 3,
            "title": "open issue",
            "url": "https://example.com/issues/3",
            "created_at": "2026-02-24T00:00:00Z",
            "updated_at": "2026-02-25T00:00:00Z",
            "is_pull_request": False,
        }
    ]


def test_list_open_assigned_issues_returns_assigned_issues(
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
                '[{"id":"I1","number":5,"title":"assigned issue","url":"https://example.com/issues/5",'
                '"createdAt":"2026-02-01T00:00:00Z","updatedAt":"2026-02-02T00:00:00Z",'
                '"repository":{"name":"repo","nameWithOwner":"owner/repo"}}]'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = GitHubClient().list_open_assigned_issues("testuser")

    assert captured["command"] == [
        "gh",
        "search",
        "issues",
        "--assignee",
        "testuser",
        "--state",
        "open",
        "--sort",
        "updated",
        "--order",
        "desc",
        "--limit",
        "20",
        "--json",
        "id,number,title,url,createdAt,updatedAt,repository",
    ]
    assert result == [
        {
            "id": "I1",
            "repo": "owner/repo",
            "number": 5,
            "title": "assigned issue",
            "url": "https://example.com/issues/5",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-02T00:00:00Z",
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
        GitHubClient().fetch_item_by_url(parsed)


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
            GitHubClient().fetch_item_by_url(parsed)
    else:
        assert GitHubClient().fetch_item_by_url(parsed) is None


def test_list_open_todo_issues_reads_targets_from_issue_body_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    payload = [
        {
            "id": "I110",
            "number": 110,
            "title": "Fix the flaky test",
            "url": "https://github.com/testuser/todos/issues/110",
            "createdAt": "2026-02-01T00:00:00Z",
            "updatedAt": "2026-02-02T00:00:00Z",
            "body": '```json\n{"workdash_todo_version": 1, "target": "owner/repo"}\n```\n',
        },
        {
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
        return subprocess.CompletedProcess(args[0], 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = GitHubClient().list_open_todo_issues("testuser/todos")

    assert captured["command"] == [
        "gh",
        "issue",
        "list",
        "--repo",
        "testuser/todos",
        "--state",
        "open",
        "--label",
        "workdash-todo",
        "--limit",
        "200",
        "--json",
        "id,number,title,url,createdAt,updatedAt,body",
    ]
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
            args[0], 0, stdout='[{"number":"110"}]', stderr=""
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
