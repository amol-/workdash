"""GitHub data access wrappers."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast
from urllib.parse import urlsplit

from .git import is_todo_target
from .models import WorkItem, WorkItemKind, WorkItemType
from .work_state import (
    IssueWorkState,
    PullRequestWorkState,
    ReadyWork,
    _actionable_feedback,
    parse_github_datetime,
    select_ready_work,
)

# GitHub computes mergeability in the background after it is first asked for,
# so an UNKNOWN answer is asked once more after this delay.
_MERGEABILITY_RETRY_DELAY_SECONDS = 3.0
# Keeps one aliased pull request query well within GitHub's 10 second query timeout:
# a pull request with a hundred checks takes about half a second.
_PULL_REQUESTS_PER_QUERY = 10
_PUSH_PERMISSIONS = {"WRITE", "MAINTAIN", "ADMIN"}
_FAILED_RESULTS = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}
_PENDING_RESULTS = {"PENDING", "EXPECTED"}
_SUCCESSFUL_RESULTS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
# Nobody closes an issue with more than a handful of pull requests.
_ISSUE_FRAGMENT = """
fragment ReadyIssue on Issue {
    number title url createdAt updatedAt state body
    repository { nameWithOwner }
    subIssuesSummary { total }
    issueDependenciesSummary { blockedBy }
    closedByPullRequestsReferences(first: 10) {
        nodes { number state repository { nameWithOwner } }
    }
}
"""
_ACTOR = "author { __typename login }"
_CHECK_CONTEXTS = f"""
    {_PAGE_INFO}
    nodes {{
        __typename
        ... on CheckRun {{
            name status conclusion startedAt
            checkSuite {{ app {{ slug }} workflowRun {{ workflow {{ databaseId }} }} }}
        }}
        ... on StatusContext {{ context state }}
    }}
"""
# Per pull request feedback lists stop at GitHub's page size of 100, keeping the
# newest entries: only feedback after the latest push can be actionable.
_PULL_REQUEST_FRAGMENT = f"""
fragment ReadyPullRequest on PullRequest {{
    number title url createdAt updatedAt state
    {_ACTOR}
    repository {{ nameWithOwner }}
    headRefOid mergedAt maintainerCanModify
    headRef {{ refUpdateRule {{ viewerCanPush }} }}
    headRepository {{ viewerPermission }}
    baseRepository {{ viewerPermission }}
    baseRefName baseRef {{ target {{ oid }} }} mergeable reviewDecision
    closingIssuesReferences(first: 10) {{
        nodes {{ number title url createdAt updatedAt body repository {{ nameWithOwner }} }}
    }}
    commits(last: 1) {{
        nodes {{ commit {{
            oid
            statusCheckRollup {{ contexts(first: 100) {{ {_CHECK_CONTEXTS} }} }}
        }} }}
    }}
    reviewRequests(first: 100) {{
        nodes {{ requestedReviewer {{ __typename ... on User {{ login }} }} }}
    }}
    latestOpinionatedReviews(first: 100) {{
        nodes {{ state url submittedAt isMinimized commit {{ oid }} {_ACTOR} }}
    }}
    reviews(last: 100) {{ nodes {{ state body url submittedAt isMinimized {_ACTOR} }} }}
    reviewThreads(last: 100) {{
        nodes {{ isResolved isOutdated
            comments(last: 10) {{ nodes {{ url publishedAt isMinimized {_ACTOR} }} }}
        }}
    }}
    comments(last: 100) {{ nodes {{ url createdAt isMinimized {_ACTOR} }} }}
}}
"""


_LAUNCH_ISSUE_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,comments"
)
_LAUNCH_PR_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
    "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName,"
    "comments,reviews"
)
_ANALYSIS_ISSUE_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,comments"
)
_ANALYSIS_PR_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
    "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName,"
    "comments,reviews"
)
_HEAD_METADATA_JSON_FIELDS = "headRefName,headRepository,headRepositoryOwner"
_BASE_METADATA_JSON_FIELDS = "baseRefName,headRepository,headRepositoryOwner"


TODO_LABEL = "workdash-todo"

_TODO_METADATA_VERSION = 1
_METADATA_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_ISSUE_NUMBER_RE = re.compile(r"/issues/(\d+)$")


def todo_target_from_body(body: object) -> str | None:
    """Read the todo target recorded in a todo issue body.

    Absent, unparseable, or unexpected metadata means the todo has no target;
    a hand-edited issue body must never break a dashboard refresh.
    """

    if not isinstance(body, str):
        return None
    match = _METADATA_BLOCK_RE.search(body)
    if match is None:
        return None
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    target = metadata.get("target")
    if isinstance(target, str) and is_todo_target(target):
        return target
    return None


def is_missing_repository_error(gh_message: str) -> bool:
    """Report whether a gh failure message says the repository does not exist.

    The todo repository is allowed to not exist until the first capture, so
    callers need to tell this failure apart from every other gh failure. gh
    names the same missing repository differently per endpoint: GraphQL-backed
    issue commands fail to resolve it, while REST calls such as label creation
    report an HTTP 404. Only those two shapes count, so an unrelated failure
    that merely mentions 404 or a not-found reference, and authentication or
    permission failures, are not mistaken for a missing repository.
    """

    message = gh_message.lower()
    return any(marker in message for marker in ("could not resolve to a repository", "http 404"))


_GITHUB_HOSTS = {"github.com", "www.github.com"}
_ITEM_SEGMENT_TYPES = {"pull": WorkItemType.PR, "issues": WorkItemType.ISSUE}
_NUMBER_RE = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class ParsedGitHubItemURL:
    """A parsed GitHub issue or pull request URL."""

    repo: str
    number: int
    item_type: WorkItemType
    canonical_url: str


def parse_github_item_url(url: str) -> ParsedGitHubItemURL | None:
    """Parse a GitHub issue/PR URL, ignoring trailing path/query/fragment.

    Returns ``None`` for any input that does not name a GitHub issue or
    pull request: wrong host, wrong path shape, or a non-numeric item
    number.
    """

    if not isinstance(url, str):
        return None
    stripped = url.strip()
    if not stripped:
        return None
    parts = urlsplit(stripped)
    if parts.scheme not in {"http", "https"}:
        return None
    if parts.hostname is None or parts.hostname.lower() not in _GITHUB_HOSTS:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if len(segments) < 4:
        return None
    owner, repository, kind_segment, number_segment = segments[:4]
    if not owner or not repository:
        return None
    item_type = _ITEM_SEGMENT_TYPES.get(kind_segment)
    if item_type is None:
        return None
    if not _NUMBER_RE.match(number_segment):
        return None
    number = int(number_segment)
    canonical_url = f"https://github.com/{owner}/{repository}/{kind_segment}/{number}"
    return ParsedGitHubItemURL(
        repo=f"{owner}/{repository}",
        number=number,
        item_type=item_type,
        canonical_url=canonical_url,
    )


# GitHub returns at most 100 results per page, which matches the 100 most
# recently updated discovered items the backend keeps.
_ITEMS_FETCH_LIMIT = 100
_PR_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,isDraft,repository"
# `gh search prs` cannot report the CI result, review decision, or pending
# review requests shown next to authored PRs.
_AUTHORED_CI_SELECTION = (
    "commits(last: 1) { nodes { commit { statusCheckRollup { state } } } } "
    "reviewDecision reviewRequests { totalCount }"
)
# GraphQL selection for the review threads of an authored pull request, so a
# reviewer's comment nobody resolved yet can be told apart from no review at
# all. Nobody leaves more than 100 threads on one pull request.
_UNRESOLVED_THREADS_SELECTION = "reviewThreads(first: 100) { nodes { isResolved } }"
# GraphQL selection naming the issues a pull request closes, so the pull request
# can take the place of that issue on the dashboard. Nobody links a pull request
# to more than a handful of issues.
_CLOSING_ISSUES_SELECTION = (
    "closingIssuesReferences(first: 5) { nodes { number repository { nameWithOwner } } }"
)
# `search` returns a union type, so PullRequest and Issue each need their own
# inline fragment even though they ask for the same fields.
_ISSUE_FIELDS = "id number title url createdAt updatedAt state repository { nameWithOwner } subIssuesSummary { total }"
_RECENT_TRACKED_ITEM_FRAGMENT = (
    "__typename "
    "... on PullRequest { id number title url createdAt updatedAt state repository { nameWithOwner } } "
    f"... on Issue {{ {_ISSUE_FIELDS} }}"
)
_TRANSIENT_RETRIES = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0
# gh never surfaces the `Retry-After` header on rate-limit errors (it is
# dropped before the CLI formats its error text), so this is a fixed stand-in
# for GitHub's own guidance to wait about a minute before retrying.
_RATE_LIMIT_RETRY_DELAY_SECONDS = 70.0
# gh surfaces transient API failures in several shapes we've seen in the wild:
#   - "HTTP 5xx: ..." / "non-200 OK status code: 5xx ..." (5xx server errors)
#   - "HTTP 429" / "API rate limit exceeded" / "abuse detection" (rate limiting)
#   - "dial tcp ... no such host" / "connection reset" / "i/o timeout" ... (network)
#   - "result of a timeout" (GraphQL execution timeout, sometimes with HTTP 200)
#   - "unexpected end of JSON input" (GraphQL timeout cutting off an HTTP 200 body)
# Each of these can succeed on a later retry, so the caller should retain the
# URL in the included-items store and retry on the next refresh.
_TRANSIENT_HTTP_STATUS_RE = re.compile(r"(?:HTTP |status code:?\s*)5\d{2}", re.IGNORECASE)
# Rate-limit shapes get the longer _RATE_LIMIT_RETRY_DELAY_SECONDS instead of
# _TRANSIENT_RETRY_DELAY_SECONDS: retrying a rate limit after 2s just digs the
# penalty deeper, unlike a 5xx or network blip.
_RATE_LIMIT_SUBSTRINGS = (
    "http 429",
    "rate limit",
    "abuse detection",
)
_TRANSIENT_SUBSTRINGS = (
    *_RATE_LIMIT_SUBSTRINGS,
    "dial tcp",
    "connection refused",
    "connection reset",
    "no such host",
    "i/o timeout",
    "result of a timeout",
    "context deadline exceeded",
    "unexpected end of json input",
    "eof",
)
_REPOSITORY_AUTHORIZATION_SUBSTRINGS = (
    "resource protected by organization saml enforcement",
    "saml enforcement",
    "must grant your oauth token access",
    "organization has enabled oauth app access restrictions",
)


class TransientFetchError(RuntimeError):
    """Raised when a gh command failed with a retryable network/5xx/429 error.

    Callers that maintain a persistent store of URLs should retain the
    entry so the next refresh can try again.
    """


def _is_transient_gh_error(error: subprocess.CalledProcessError) -> bool:
    stderr = (error.stderr or "").strip()
    if not stderr:
        return False
    if _TRANSIENT_HTTP_STATUS_RE.search(stderr):
        return True
    stderr_lower = stderr.lower()
    return any(needle in stderr_lower for needle in _TRANSIENT_SUBSTRINGS)


def _is_rate_limit_error(stderr: str) -> bool:
    stderr_lower = stderr.lower()
    return any(needle in stderr_lower for needle in _RATE_LIMIT_SUBSTRINGS)


def _is_repository_authorization_error(message: str) -> bool:
    message_lower = message.lower()
    return any(needle in message_lower for needle in _REPOSITORY_AUTHORIZATION_SUBSTRINGS)


def _extract_ci_state(entry: dict[str, object]) -> str | None:
    """Return the CI result GitHub rolled up for a pull request's head commit.

    Only the GraphQL authored search asks for it, so every other payload yields
    no result at all, as does a pull request whose checks GitHub has not rolled
    up or that has no checks configured.

    :param dict entry: One raw pull request payload.
    """

    commits = entry.get("commits")
    if commits is None:
        return None
    # A pull request always has a head commit, but GitHub reports no rollup at
    # all when it has no checks configured or has not rolled them up yet.
    rollup = commits["nodes"][0]["commit"]["statusCheckRollup"]
    return rollup["state"] if rollup else None


def _extract_pending_review_request(entry: dict[str, object]) -> bool:
    """Report whether a pull request still has an unresolved review request.

    A requested reviewer stays pending until they approve or request changes;
    a comment-only review does not clear the request.

    :param dict entry: One raw pull request payload.
    """

    review_requests = entry.get("reviewRequests")
    if not isinstance(review_requests, dict):
        return False
    total_count = review_requests.get("totalCount")
    return isinstance(total_count, int) and total_count > 0


def _extract_unresolved_comments(entry: dict[str, object]) -> bool:
    """Report whether a pull request has a review thread nobody resolved yet.

    Only the GraphQL authored lookup asks for review threads, so every other
    payload reports none.

    :param dict entry: One raw pull request payload.
    """

    review_threads = entry.get("reviewThreads")
    if review_threads is None:
        return False
    return any(not thread["isResolved"] for thread in review_threads["nodes"])


def _is_unresolvable_item_error(message: str) -> bool:
    """Report whether a GraphQL error says GitHub cannot resolve the queried item.

    A repository that was renamed, deleted, or is no longer visible, and a pull
    request that is gone, are all as inaccessible as an authorization denial:
    the work item cannot be loaded, but every other item still can.
    """

    return "could not resolve to a" in message.lower()


def _accept_summary_error(error: object, data: object, prefix: list[object]) -> bool:
    """Accept only an error attached to an existing issue's summary field."""
    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
        return False
    path = error.get("path")
    if not isinstance(path, list) or path[: len(prefix)] != prefix:
        return False
    if path[len(prefix) :] not in (["subIssuesSummary"], ["subIssuesSummary", "total"]):
        return False
    node = data
    try:
        for part in prefix:
            node = node[part]
    except (KeyError, IndexError, TypeError):
        return False
    if not isinstance(node, dict) or node.get("__typename", "Issue") != "Issue":
        return False
    summary = node.get("subIssuesSummary")
    if summary is None or (isinstance(summary, dict) and summary.get("total") is None):
        node["_summary_error"] = True
    return True


def _sub_issue_total(node: dict, error: Exception) -> int | None:
    summary = node.get("subIssuesSummary")
    total = summary.get("total") if isinstance(summary, dict) else None
    if total is None and node.get("_summary_error"):
        return None
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise error
    return total


def _recent_tracked_items_selection(query: str) -> str:
    """Build the single-page ``search`` selection for the tracked-items GraphQL query."""

    return (
        f"tracked: search(type: ISSUE, first: {_ITEMS_FETCH_LIMIT}, query: {json.dumps(query)}) "
        f"{{ nodes {{ {_RECENT_TRACKED_ITEM_FRAGMENT} }} }}"
    )


def _recent_tracked_item_node_entry(node: object) -> object:
    """Add the ``isPullRequest`` flag ``_parse_recent_tracked_item_payload`` expects.

    GraphQL reports a node's type via ``__typename`` instead of the REST
    search API's ``isPullRequest`` boolean.
    """

    if not isinstance(node, dict):
        return node
    return {**node, "isPullRequest": node.get("__typename") == "PullRequest"}


def _noop_progress_callback(_message: str) -> None:
    """Ignore progress updates when no reporting target is configured."""


def _run_gh_command_with_retry(
    command: list[str],
    *,
    not_found_message: str,
    failure_message_prefix: str,
    report_progress: Callable[[str], None] = _noop_progress_callback,
    retry_label: str | None = None,
    tolerate_partial_response: bool = False,
    retry_delay_seconds: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` subprocess with bounded retry on transient failures.

    Raises ``TransientFetchError`` when retries are exhausted and the
    final failure was classified transient (5xx, 429, rate-limit, common
    network errors). Raises a plain ``RuntimeError`` for permanent errors
    (bad auth, 404, malformed command) so existing callers continue to
    surface the same descriptive message they did before.

    :param bool tolerate_partial_response: Return the output of a failed gh
        command instead of raising when it still printed something. GraphQL
        answers a partially denied query with both data and errors, and gh
        reports that as a failure.
    :param float | None retry_delay_seconds: Replace the delay chosen for the
        failure shape. Waiting out a rate limit is too slow for a read that
        holds the session lock.
    """

    for attempt in range(_TRANSIENT_RETRIES + 1):
        try:
            return subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            # Missing gh is treated as transient: the user can install it and
            # refresh. Raising a TransientFetchError (itself a RuntimeError)
            # keeps the existing "gh CLI is not installed..." message so
            # pytest.raises(RuntimeError, match=...) still matches, while
            # letting fetch_item_by_url retain the URL for a later retry.
            raise TransientFetchError(not_found_message) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            transient = _is_transient_gh_error(error)
            if transient and tolerate_partial_response and error.stdout:
                try:
                    payload = json.loads(error.stdout)
                except json.JSONDecodeError:
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("errors"), list):
                    errors = payload["errors"]
                    summary_errors = 0
                    for entry in errors:
                        path = entry.get("path") if isinstance(entry, dict) else None
                        message = entry.get("message") if isinstance(entry, dict) else None
                        if (
                            isinstance(path, list)
                            and "subIssuesSummary" in path
                            and _accept_summary_error(
                                entry, payload.get("data"), path[: path.index("subIssuesSummary")]
                            )
                        ):
                            summary_errors += 1
                        elif not isinstance(message, str) or not (
                            _is_repository_authorization_error(message)
                            or _is_unresolvable_item_error(message)
                        ):
                            break
                    else:
                        if summary_errors:
                            return subprocess.CompletedProcess(
                                command, error.returncode, stdout=error.stdout, stderr=stderr
                            )
            if attempt < _TRANSIENT_RETRIES and transient:
                if retry_delay_seconds is not None:
                    delay = retry_delay_seconds
                elif _is_rate_limit_error(stderr):
                    delay = _RATE_LIMIT_RETRY_DELAY_SECONDS
                else:
                    delay = _TRANSIENT_RETRY_DELAY_SECONDS
                if retry_label is not None:
                    report_progress(
                        f"{retry_label} got transient error, "
                        f"retrying in {delay:.0f}s ({attempt + 1}/{_TRANSIENT_RETRIES})..."
                    )
                time.sleep(delay)
                continue
            message = (
                f"{failure_message_prefix}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            )
            if transient:
                raise TransientFetchError(message) from error
            if tolerate_partial_response and error.stdout:
                return subprocess.CompletedProcess(
                    command, error.returncode, stdout=error.stdout, stderr=stderr
                )
            raise RuntimeError(message) from error
    raise AssertionError("unreachable: retry loop exited without return or raise")


class AuthoredPullRequest(TypedDict):
    """Raw authored PR record from gh search, normalized for later conversion."""

    id: str | int
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    is_draft: bool
    ci_state: str | None
    review_decision: str | None
    review_requested: bool
    has_unresolved_comments: bool


class ReviewRequestedPullRequest(TypedDict):
    """Raw review-requested PR record from gh search, normalized for conversion."""

    id: str | int
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    is_draft: bool
    ci_state: str | None
    review_decision: str | None
    review_requested: bool
    has_unresolved_comments: bool


class RecentTrackedItem(TypedDict):
    """Raw issue/PR record from gh search, normalized for later conversion."""

    id: str | int
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    is_pull_request: bool
    sub_issue_total: NotRequired[int | None]


class AssignedIssue(TypedDict):
    """Raw assigned issue record from gh search, normalized for conversion."""

    id: str | int
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    sub_issue_total: NotRequired[int | None]


class TodoIssue(TypedDict):
    """Raw todo issue record from gh, with its target already decoded."""

    id: str | int
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str
    target: str | None
    sub_issue_total: NotRequired[int | None]


def normalize_authored_pull_request(item: AuthoredPullRequest) -> WorkItem:
    """Convert one raw authored PR payload into the internal WorkItem model."""

    return WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo=item["repo"],
        number=item["number"],
        title=item["title"],
        created_at=parse_github_datetime(item["created_at"]),
        updated_at=parse_github_datetime(item["updated_at"]),
        url=item["url"],
        ci_state=item["ci_state"],
        review_decision=item["review_decision"],
        review_requested=item["review_requested"],
        has_unresolved_comments=item["has_unresolved_comments"],
    )


def normalize_review_requested_pull_request(item: ReviewRequestedPullRequest) -> WorkItem:
    """Convert one raw review-requested PR payload into the internal WorkItem model."""

    return WorkItem(
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
        item_type=WorkItemType.PR,
        repo=item["repo"],
        number=item["number"],
        title=item["title"],
        created_at=parse_github_datetime(item["created_at"]),
        updated_at=parse_github_datetime(item["updated_at"]),
        url=item["url"],
    )


def normalize_recent_tracked_item(item: RecentTrackedItem) -> WorkItem:
    """Convert one raw tracked issue/PR payload into the internal WorkItem model."""

    return WorkItem(
        kind=WorkItemKind.TRACKED_PR if item["is_pull_request"] else WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.PR if item["is_pull_request"] else WorkItemType.ISSUE,
        repo=item["repo"],
        number=item["number"],
        title=item["title"],
        created_at=parse_github_datetime(item["created_at"]),
        updated_at=parse_github_datetime(item["updated_at"]),
        url=item["url"],
        sub_issue_total=item.get("sub_issue_total"),
    )


def normalize_assigned_issue(item: AssignedIssue) -> WorkItem:
    """Convert one raw assigned issue payload into the internal WorkItem model."""

    return WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo=item["repo"],
        number=item["number"],
        title=item["title"],
        created_at=parse_github_datetime(item["created_at"]),
        updated_at=parse_github_datetime(item["updated_at"]),
        url=item["url"],
        sub_issue_total=item.get("sub_issue_total"),
    )


def normalize_todo_issue(item: TodoIssue) -> WorkItem:
    """Convert one raw todo issue payload into the internal WorkItem model.

    A todo issue is assigned to the user, so it belongs to the same work
    category as any other assigned issue; only its target sets it apart.
    """

    return WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo=item["repo"],
        number=item["number"],
        title=item["title"],
        created_at=parse_github_datetime(item["created_at"]),
        updated_at=parse_github_datetime(item["updated_at"]),
        url=item["url"],
        todo_target=item["target"],
        sub_issue_total=item.get("sub_issue_total"),
    )


def normalize_todo_issues(items: list[TodoIssue]) -> list[WorkItem]:
    """Convert todo issue records into internal WorkItems."""

    return [normalize_todo_issue(item) for item in items]


def normalize_assigned_issues(items: list[AssignedIssue]) -> list[WorkItem]:
    """Convert assigned issue records into internal WorkItems."""

    return [normalize_assigned_issue(item) for item in items]


def normalize_authored_pull_requests(items: list[AuthoredPullRequest]) -> list[WorkItem]:
    """Convert authored PR records into internal WorkItems."""

    return [normalize_authored_pull_request(item) for item in items]


def normalize_review_requested_pull_requests(
    items: list[ReviewRequestedPullRequest],
) -> list[WorkItem]:
    """Convert review-requested PR records into internal WorkItems."""

    return [normalize_review_requested_pull_request(item) for item in items]


def normalize_recent_tracked_items(items: list[RecentTrackedItem]) -> list[WorkItem]:
    """Convert tracked issue/PR records into internal WorkItems."""

    return [normalize_recent_tracked_item(item) for item in items]


def merge_normalized_work_items(
    primary_items: list[WorkItem],
    secondary_items: list[WorkItem],
) -> list[WorkItem]:
    """Merge normalized WorkItems with deterministic dedupe semantics.

    Identity is ``(item_type, repo, number)`` so issues and PRs remain independent.
    Output order is stable: primary input order first, then remaining secondary order.
    The ``included`` flag is sticky: if either side of a collision was
    included the surviving record keeps ``included=True``.
    """

    merged_items: list[WorkItem] = []
    indexes_by_identity: dict[tuple[WorkItemType, str, int], int] = {}
    for item in primary_items:
        identity = (item.item_type, item.repo, item.number)
        if identity in indexes_by_identity:
            existing = merged_items[indexes_by_identity[identity]]
            if existing.sub_issue_total is None:
                existing.sub_issue_total = item.sub_issue_total
            continue
        indexes_by_identity[identity] = len(merged_items)
        merged_items.append(item)
    for item in secondary_items:
        identity = (item.item_type, item.repo, item.number)
        if identity in indexes_by_identity:
            existing = merged_items[indexes_by_identity[identity]]
            if item.included:
                existing.included = True
            if existing.sub_issue_total is None:
                existing.sub_issue_total = item.sub_issue_total
            continue
        indexes_by_identity[identity] = len(merged_items)
        merged_items.append(item)
    return merged_items


class GitHubClient:
    """All GitHub interactions must go through this object."""

    def create_todo(
        self, *, todo_repository: str, text: str, target: str | None = None, body: str | None = None
    ) -> WorkItem:
        """Create a labeled, self-assigned todo issue and return it as a dashboard item.

        :param str todo_repository: Repository that hosts todos, as ``owner/repo``.
        :param str text: Todo text, used as the issue title.
        :param str | None target: Optional repository the todo is about.
        :param str | None body: Optional content appended after the metadata block.
        """

        # gh exits non-zero when the label is already there, which is the steady
        # state after the first capture and not a failure for us.
        _run_gh(
            [
                "gh",
                "label",
                "create",
                TODO_LABEL,
                "--repo",
                todo_repository,
                "--description",
                "Captured with workdash",
            ],
            operation=f"create the {TODO_LABEL} label in {todo_repository}",
            todo_repository=todo_repository,
            tolerate_stderr="already exists",
        )
        output = _run_gh(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                todo_repository,
                "--title",
                text,
                "--body",
                _encode_todo_metadata(target) + ("\n" + body if body is not None else ""),
                "--assignee",
                "@me",
                "--label",
                TODO_LABEL,
            ],
            operation=f"create the todo issue in {todo_repository}",
            todo_repository=todo_repository,
        )
        url = output.strip().splitlines()[-1].strip() if output.strip() else ""
        match = _ISSUE_NUMBER_RE.search(url)
        if match is None:
            raise RuntimeError(
                f"Failed to read the new todo issue URL from gh output: {output.strip()!r}"
            )
        captured_at = datetime.now(UTC)
        # A todo is assigned to the user, so it joins the dashboard in the same work
        # category as any other assigned issue; only its target sets it apart.
        return WorkItem(
            kind=WorkItemKind.ASSIGNED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo=todo_repository,
            number=int(match.group(1)),
            title=text,
            created_at=captured_at,
            updated_at=captured_at,
            url=url,
            todo_target=target,
        )

    def branch_pull_request(self, repo: str, branch: str) -> _PullRequestInfo | None:
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
            raise RuntimeError(
                "Invalid gh pull request payload: missing number, title, url, or state."
            )
        return _PullRequestInfo(number=number, title=title, url=url, state=state)

    def branch_ci_and_closing_issues(
        self, repo: str, number: int
    ) -> tuple[str | None, str | None, bool, list[tuple[str, int]]]:
        """Return CI state, review decision, pending review request, and closing issues."""
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
        payload = _parse_json_object(
            completed.stdout, context="CI status and closing issues lookup"
        )
        data = payload.get("data")
        repository = data.get("repository") if isinstance(data, dict) else None
        pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
        if not isinstance(pull_request, dict):
            raise RuntimeError(
                f"Invalid gh GraphQL payload for {repo}#{number}: missing pullRequest."
            )
        return (
            _extract_ci_state(pull_request),
            pull_request.get("reviewDecision"),
            _extract_pending_review_request(pull_request),
            _closing_issues(pull_request),
        )

    def branch_issue(self, repo: str, number: int) -> _IssueInfo:
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

    def check_authentication(self) -> str | None:
        gh = shutil.which("gh")
        if gh is None:
            return (
                "gh CLI is not installed or not configured. Run 'workdash --configure' "
                "to install a local copy, or install gh globally from https://cli.github.com/."
            )
        try:
            subprocess.run(
                [gh, "auth", "status"],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError:
            auth_command = shlex.join([gh, "auth", "login"])
            return (
                "gh CLI is not authenticated.\n"
                "Run this command to authenticate the GitHub CLI used by workdash:\n"
                f"  {auth_command}"
            )
        except OSError as error:
            return f"failed to run gh auth status: {error}"
        return None

    def download_release(self, url: str, *, urlopen_fn: Callable[[str], object]) -> bytes:
        try:
            with urlopen_fn(url) as response:
                return response.read()
        except OSError as error:
            raise RuntimeError(f"Failed to download {url}: {error}") from error

    def release_asset_url(
        self, url: str, archive_suffix: str, *, urlopen_fn: Callable[[str], object]
    ) -> str:
        release_bytes = self.download_release(url, urlopen_fn=urlopen_fn)
        try:
            release = json.loads(release_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Failed to parse GitHub CLI release metadata: {error}") from error
        assets = release.get("assets") if isinstance(release, dict) else None
        if not isinstance(assets, list):
            raise RuntimeError("GitHub CLI release metadata did not contain an assets list.")
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            download_url = asset.get("browser_download_url")
            if (
                isinstance(name, str)
                and name.endswith(archive_suffix)
                and isinstance(download_url, str)
            ):
                return download_url
        raise RuntimeError(
            f"GitHub CLI latest release did not contain an asset ending with {archive_suffix!r}."
        )

    def find_ready_work(
        self,
        *,
        repositories: Sequence[str] = (),
        issue: tuple[str, int] | None = None,
        github_username: str,
        todo_repository: str,
        include_shared_pr_issues: bool = False,
    ) -> list[ReadyWork]:
        """Return ready issues, actionable pull request work first, oldest first in each group.

        :param Sequence repositories: ``owner/repo`` or ``owner/*`` selectors.
        :param tuple issue: ``(repo, number)`` of the scope issue, instead of repositories.
        """

        states: dict[tuple[str, int], PullRequestWorkState] = {}
        if issue is not None:
            issues = self.resolve_issue_scope(*issue, todo_repository=todo_repository)
        else:
            names: dict[str, str] = {}
            for selector in repositories:
                owner, _, name = selector.partition("/")
                for repo in self._owner_repositories(owner) if name == "*" else [selector]:
                    names.setdefault(repo.lower(), repo)
            issues = []
            for repo in names.values():
                try:
                    issues += self._repository_issues(repo, todo_repository)
                except RuntimeError as error:
                    if repo.lower() != todo_repository.lower() or not is_missing_repository_error(
                        str(error)
                    ):
                        raise
            issue_states: dict[tuple[str, int], IssueWorkState] = {}
            repository_prs = []
            for repo in names.values():
                try:
                    owner, _, name = repo.partition("/")
                    nodes = self._all_nodes(
                        repo,
                        lambda after, owner=owner, name=name: (
                            f"""query ReadyRepositoryPullRequests {{
                                repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
                                    pullRequests(states: OPEN, first: 100, after: {after}) {{
                                        {_PAGE_INFO}
                                        nodes {{ number }}
                                    }}
                                }}
                            }}"""
                        ),
                        ("repository", "pullRequests"),
                    )
                    repository_prs.extend((repo, node["number"]) for node in nodes)
                except RuntimeError as error:
                    if repo.lower() != todo_repository.lower() or not is_missing_repository_error(
                        str(error)
                    ):
                        raise
            for state in self.evaluate_pull_requests(
                repository_prs, github_username=github_username, todo_repository=todo_repository
            ):
                states[(state.item.repo.lower(), state.item.number)] = state
                if state.ready and state.author and state.author.lower() == github_username.lower():
                    for linked in state.linked_issues:
                        if linked.repo.lower() not in names:
                            issues.append(
                                self.resolve_issue_scope(
                                    linked.repo,
                                    linked.number,
                                    todo_repository=todo_repository,
                                    _states=issue_states,
                                )[0]
                            )
            issues = [
                self.resolve_issue_scope(
                    entry.item.repo,
                    entry.item.number,
                    todo_repository=todo_repository,
                    _states=issue_states,
                )[0]
                if entry.has_sub_issues
                else entry
                for entry in issues
            ]

        issues = list(
            {(entry.item.repo.lower(), entry.item.number): entry for entry in issues}.values()
        )
        pull_requests = {
            (repo.lower(), number): (repo, number)
            for entry in issues
            if entry.is_open and not entry.has_open_descendants and not entry.blocked
            for repo, number in entry.open_pull_requests
        }
        states.update(
            {
                (state.item.repo.lower(), state.item.number): state
                for state in self.evaluate_pull_requests(
                    [ref for key, ref in pull_requests.items() if key not in states],
                    github_username=github_username,
                    todo_repository=todo_repository,
                )
            }
        )
        return select_ready_work(issues, states, github_username, include_shared_pr_issues)

    def resolve_issue_scope(
        self,
        repo: str,
        number: int,
        *,
        todo_repository: str,
        _states: dict[tuple[str, int], IssueWorkState] | None = None,
    ) -> list[IssueWorkState]:
        """Return the issue and its native descendants with open-descendant eligibility."""

        with _github_payload(f"{repo}#{number}"):
            if _states is not None and (repo.lower(), number) in _states:
                return [_states[(repo.lower(), number)]]
            root = self._issue_tree(repo, number)
            issues = []

            def visit(node: dict) -> bool:
                state = self._issue_state(node, todo_repository)
                index = len(issues)
                issues.append(state)
                open_descendants = False
                for child in node["subIssues"]["nodes"]:
                    child_state = self._issue_state(child, todo_repository)
                    key = (child_state.item.repo.lower(), child_state.item.number)
                    if _states is not None and key in _states:
                        child_state = _states[key]
                        issues.append(child_state)
                        descendant_open = child_state.has_open_descendants
                    elif child_state.has_sub_issues:
                        descendant_open = visit(
                            self._issue_tree(child_state.item.repo, child_state.item.number)
                        )
                    else:
                        issues.append(child_state)
                        descendant_open = False
                    open_descendants |= child_state.is_open or descendant_open
                issues[index] = replace(state, has_open_descendants=open_descendants)
                if _states is not None:
                    _states[(state.item.repo.lower(), state.item.number)] = issues[index]
                return open_descendants

            visit(root)
            return issues

    def evaluate_pull_requests(
        self,
        pull_requests: Sequence[tuple[str, int]],
        *,
        github_username: str,
        todo_repository: str,
    ) -> list[PullRequestWorkState]:
        """Return the work state of each ``(repo, number)`` pull request, in the same order.

        :param str github_username: The user whose pull requests are their own work.
        :param str todo_repository: Repository whose issues are todos, so a linked
            issue's target is read from its body.
        """

        nodes = self._fetch_pull_requests(pull_requests)
        # GitHub computes mergeability lazily, but reports UNKNOWN for a head it no longer
        # recomputes, so asking a merged or closed one again only costs a call.
        recomputing = [
            index
            for index, node in enumerate(nodes)
            if node["state"] == "OPEN" and node.get("mergeable") == "UNKNOWN"
        ]
        if recomputing:
            time.sleep(_MERGEABILITY_RETRY_DELAY_SECONDS)
            refreshed = self._fetch_pull_requests([pull_requests[index] for index in recomputing])
            for index, node in zip(recomputing, refreshed, strict=True):
                nodes[index] = node
        required_checks: dict[tuple[str, str], frozenset[str] | None] = {}
        return [
            self._pull_request_state(
                node, required_checks, github_username, todo_repository, subject=f"{repo}#{number}"
            )
            for (repo, number), node in zip(pull_requests, nodes, strict=True)
        ]

    def linked_open_pull_request(self, item: WorkItem) -> tuple[str, int] | None:
        """Find an issue's linked open pull request without evaluating readiness."""
        owner, name = item.repo.split("/", 1)
        subject = f"{item.repo}#{item.number}"
        after = "null"
        while True:
            query = (
                f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f"{{ issue(number: {item.number}) {{ closedByPullRequestsReferences(first: 100, after: {after}) "
                "{ nodes { number state repository { nameWithOwner } } "
                "pageInfo { hasNextPage endCursor } } } } }"
            )
            data = self._graphql(query, subject)
            with _github_payload(subject):
                connection = data["repository"]["issue"]["closedByPullRequestsReferences"]
                for node in connection["nodes"]:
                    if node["state"] == "OPEN":
                        return node["repository"]["nameWithOwner"], node["number"]
                if not connection["pageInfo"]["hasNextPage"]:
                    return None
                after = json.dumps(connection["pageInfo"]["endCursor"])

    def work_pull_request(
        self, item: WorkItem, *, github_username: str, todo_repository: str
    ) -> PullRequestWorkState | None:
        """Return the pull request whose state carries an item's work, ``None`` when it has none.

        That is the item's own pull request, or the user's closing pull request of the
        issue: the open one when there is one, otherwise one that merged before it was
        looked for.
        """

        candidates: list[tuple[str, int]] = []
        if item.item_type == WorkItemType.PR:
            candidates = [(item.repo, item.number)]
        else:
            with _github_payload(f"{item.repo}#{item.number}"):
                entry = self._issue_state(self._issue_tree(item.repo, item.number), todo_repository)
                candidates = [*entry.open_pull_requests, *entry.merged_pull_requests]
        if not candidates:
            return None
        states = self.evaluate_pull_requests(
            candidates, github_username=github_username, todo_repository=todo_repository
        )
        if item.item_type == WorkItemType.PR:
            return states[0]
        # Prefer the same actionable pull request that readiness selects for this issue.
        authored = [state for state in states if state.item.kind is WorkItemKind.AUTHORED_PR]
        actionable = [state for state in authored if state.ready]
        if actionable:
            return min(
                actionable,
                key=lambda state: (
                    state.item.created_at,
                    state.item.repo.lower(),
                    state.item.number,
                ),
            )
        return authored[0] if authored else None

    def pull_request_states(
        self, pull_requests: Sequence[tuple[str, int]]
    ) -> dict[tuple[str, int], str]:
        """Return each pull request's state: ``OPEN``, ``MERGED``, or ``CLOSED``.

        Repeated checks of pull requests that are already tracked only need their state,
        so this reads one metadata query instead of a whole work state evaluation.
        """

        states: dict[tuple[str, int], str] = {}
        for start in range(0, len(pull_requests), _PULL_REQUESTS_PER_QUERY):
            batch = pull_requests[start : start + _PULL_REQUESTS_PER_QUERY]
            subject = ", ".join(f"{repo}#{number}" for repo, number in batch)
            data = self._graphql(
                f"query PullRequestStates {{ {_pull_request_aliases(batch, 'state')} }}", subject
            )
            with _github_payload(subject):
                for index, pull_request in enumerate(batch):
                    states[pull_request] = data[f"p{index}"]["pullRequest"]["state"]
        return states

    def fetch_launch_context(self, item: WorkItem) -> dict[str, Any]:
        context = self._fetch_item_context(
            item,
            issue_fields=_LAUNCH_ISSUE_CONTEXT_JSON_FIELDS,
            pr_fields=_LAUNCH_PR_CONTEXT_JSON_FIELDS,
            context_label="launch context",
        )
        if item.item_type == WorkItemType.PR:
            context.update(self._fetch_pr_discussion(item))
        return context

    def fetch_analysis_context(self, item: WorkItem) -> dict[str, Any]:
        return self._fetch_item_context(
            item,
            issue_fields=_ANALYSIS_ISSUE_CONTEXT_JSON_FIELDS,
            pr_fields=_ANALYSIS_PR_CONTEXT_JSON_FIELDS,
            context_label="gh context",
        )

    def fetch_diff(self, item: WorkItem) -> str:
        command = ["gh", "pr", "diff", str(item.number), "--repo", item.repo]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise RuntimeError(
                "Failed to gather GitHub diff context with gh: "
                "gh CLI is not installed or not on PATH."
            ) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"Failed to gather gh diff context for {item.item_type.value} "
                f"{item.repo}#{item.number}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error
        return completed.stdout

    def fetch_worktree_head(self, item: WorkItem) -> tuple[str, str]:
        payload = self._run_json_command(
            item=item,
            command=[
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _HEAD_METADATA_JSON_FIELDS,
            ],
            context_label="PR head info",
        )
        head_ref = payload.get("headRefName")
        if not isinstance(head_ref, str):
            raise RuntimeError(
                f"Missing or invalid headRefName in gh output for {item.repo}#{item.number}"
            )
        return head_ref, self._head_repo_from_payload(payload, item.repo)

    def fetch_branchdiff_base(self, item: WorkItem) -> tuple[str, str]:
        payload = self._run_json_command(
            item=item,
            command=[
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _BASE_METADATA_JSON_FIELDS,
            ],
            context_label="branchdiff base branch",
        )
        base_ref_name = payload.get("baseRefName")
        if not isinstance(base_ref_name, str) or not base_ref_name.strip():
            raise RuntimeError(
                f"Invalid branchdiff base branch payload for {item.item_type.value} "
                f"{item.repo}#{item.number}: expected a non-empty baseRefName."
            )
        return base_ref_name.strip(), self._head_repo_from_payload(payload, item.repo)

    def clone_repository(self, repo: str, target: Path) -> None:
        try:
            subprocess.run(
                ["gh", "repo", "clone", repo, str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Failed to clone repository: gh CLI is not installed or not on PATH."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(
                f"Failed to clone {repo}: {stderr or f'exit code {exc.returncode}'}"
            ) from exc

    def list_open_authored_prs(
        self,
        author_login: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[AuthoredPullRequest]:
        """List open authored PRs and their CI results across accessible repositories."""

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        command = [
            "gh",
            "search",
            "prs",
            "--author",
            author_login,
            "--state",
            "open",
            "--limit",
            str(_ITEMS_FETCH_LIMIT),
            "--json",
            _PR_JSON_FIELDS,
        ]
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh authored PR search: gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=(
                f"Failed to list open authored PRs for {author_login!r} via gh"
            ),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh authored PR JSON for {author_login!r}: {error.msg}"
            ) from error
        parsed_items = self._parse_open_pull_request_payload(
            payload,
            context=f"authored for {author_login!r}",
        )
        if not parsed_items:
            return []

        alias_selections: list[str] = []
        for index, item in enumerate(parsed_items):
            owner, _, name = item["repo"].partition("/")
            alias_selections.append(
                f"p{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f"{{ pullRequest(number: {item['number']}) "
                f"{{ {_AUTHORED_CI_SELECTION} {_UNRESOLVED_THREADS_SELECTION} }} }}"
            )
        ci_command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={{ {' '.join(alias_selections)} }}",
        ]
        failure_message_prefix = f"Failed to inspect authored PR CI for {author_login!r} via gh"
        ci_completed = _run_gh_command_with_retry(
            ci_command,
            not_found_message=(
                "Failed to run gh authored PR CI lookup: gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=failure_message_prefix,
            tolerate_partial_response=True,
        )
        try:
            ci_payload = json.loads(ci_completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh authored PR CI JSON for {author_login!r}: {error.msg}"
            ) from error
        ci_by_alias = ci_payload.get("data") if isinstance(ci_payload, dict) else None
        if not isinstance(ci_by_alias, dict):
            raise RuntimeError(
                f"{failure_message_prefix}: "
                f"{ci_completed.stderr.strip() or 'response contained no data'}"
            )

        errors = ci_payload.get("errors")
        if errors is not None and not isinstance(errors, list):
            raise RuntimeError(f"{failure_message_prefix}: response contained invalid errors.")
        if ci_completed.returncode != 0 and not errors:
            raise RuntimeError(
                f"{failure_message_prefix}: "
                f"{ci_completed.stderr.strip() or 'response contained no errors'}"
            )
        skips_by_alias: dict[str, str] = {}
        subjects_by_alias = {
            f"p{index}": f"{item['repo']}#{item['number']}"
            for index, item in enumerate(parsed_items)
        }
        for entry in errors or []:
            entry_fields = entry if isinstance(entry, dict) else {}
            message = entry_fields.get("message")
            path = entry_fields.get("path")
            alias = path[0] if isinstance(path, list) and path else None
            subject = subjects_by_alias.get(alias)
            if subject is None:
                raise RuntimeError(f"{failure_message_prefix}: {message or 'unknown error'}")
            if not isinstance(message, str) or not (
                _is_repository_authorization_error(message) or _is_unresolvable_item_error(message)
            ):
                raise RuntimeError(
                    f"{failure_message_prefix} ({subject}): {message or 'unknown error'}"
                )
            skips_by_alias[alias] = message

        authored_pull_requests: list[AuthoredPullRequest] = []
        for index, item in enumerate(parsed_items):
            alias = f"p{index}"
            subject = subjects_by_alias[alias]
            skip_reason = skips_by_alias.get(alias)
            if skip_reason is not None:
                report_progress(f"Warning: skipped authored pull request {subject}: {skip_reason}")
                continue
            repository_node = ci_by_alias.get(alias)
            pull_request = (
                repository_node.get("pullRequest") if isinstance(repository_node, dict) else None
            )
            if not isinstance(pull_request, dict):
                raise RuntimeError(
                    f"Invalid gh authored PR CI payload for {subject}: expected an object."
                )
            item["ci_state"] = _extract_ci_state(pull_request)
            item["review_decision"] = pull_request.get("reviewDecision")
            item["review_requested"] = _extract_pending_review_request(pull_request)
            item["has_unresolved_comments"] = _extract_unresolved_comments(pull_request)
            authored_pull_requests.append(item)
        return authored_pull_requests

    def list_open_review_requested_prs(
        self,
        reviewer_login: str,
    ) -> list[ReviewRequestedPullRequest]:
        """List open PRs where the given user is directly a requested reviewer."""

        command = [
            "gh",
            "search",
            "prs",
            f"user-review-requested:{reviewer_login}",
            "--state",
            "open",
            "--limit",
            str(_ITEMS_FETCH_LIMIT),
            "--json",
            _PR_JSON_FIELDS,
        ]
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh review-requested PR search: "
                "gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=(
                f"Failed to list open review-requested PRs for {reviewer_login!r} via gh"
            ),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh review-requested PR JSON for {reviewer_login!r}: {error.msg}"
            ) from error
        return [
            ReviewRequestedPullRequest(**item)
            for item in self._parse_open_pull_request_payload(
                payload,
                context=f"review-requested for {reviewer_login!r}",
            )
        ]

    def list_open_reviewed_prs(
        self,
        reviewer_login: str,
    ) -> list[ReviewRequestedPullRequest]:
        """List open PRs that the given user has already reviewed."""

        command = [
            "gh",
            "search",
            "prs",
            "--reviewed-by",
            reviewer_login,
            "--state",
            "open",
            "--limit",
            str(_ITEMS_FETCH_LIMIT),
            "--json",
            _PR_JSON_FIELDS,
        ]
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh reviewed PR search: gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=(
                f"Failed to list open reviewed PRs for {reviewer_login!r} via gh"
            ),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh reviewed PR JSON for {reviewer_login!r}: {error.msg}"
            ) from error
        return [
            ReviewRequestedPullRequest(**item)
            for item in self._parse_open_pull_request_payload(
                payload,
                context=f"reviewed for {reviewer_login!r}",
            )
        ]

    def list_open_assigned_issues(
        self,
        assignee_login: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[AssignedIssue]:
        """List open issues assigned to the given user, most recently updated first."""

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        query = f"is:issue is:open assignee:{assignee_login} sort:updated-desc"
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            (
                f"query={{ assigned: search(type: ISSUE, first: {_ITEMS_FETCH_LIMIT}, "
                f"query: {json.dumps(query)}) {{ nodes {{ __typename ... on Issue {{ {_ISSUE_FIELDS} }} }} }} }}"
            ),
        ]
        completed = _run_gh_command_with_retry(
            command,
            not_found_message="Failed to run gh assigned issue search: gh CLI is not installed or not on PATH.",
            failure_message_prefix=f"Failed to list open assigned issues for {assignee_login!r} via gh",
            tolerate_partial_response=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh assigned issue JSON for {assignee_login!r}: {error.msg}"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        assigned = data.get("assigned") if isinstance(data, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        inaccessible_nodes = set()
        if isinstance(errors, list) and errors:
            for entry in errors:
                message = entry.get("message") if isinstance(entry, dict) else None
                path = entry.get("path") if isinstance(entry, dict) else None
                if (
                    isinstance(path, list)
                    and len(path) > 2
                    and path[:2] == ["assigned", "nodes"]
                    and type(path[2]) is int
                    and path[2] >= 0
                    and _accept_summary_error(entry, data, path[:3])
                ):
                    report_progress(f"Warning: sub-issue lookup failed: {message}")
                    continue
                if not isinstance(message, str) or not (
                    _is_repository_authorization_error(message)
                    or _is_unresolvable_item_error(message)
                ):
                    raise RuntimeError(
                        f"Failed to list open assigned issues for {assignee_login!r} via gh: {message or 'unknown error'}"
                    )
                if (
                    isinstance(path, list)
                    and len(path) > 2
                    and path[:2] == ["assigned", "nodes"]
                    and type(path[2]) is int
                    and path[2] >= 0
                ):
                    inaccessible_nodes.add(path[2])
        nodes = assigned.get("nodes") if isinstance(assigned, dict) else None
        if not isinstance(nodes, list):
            if errors and isinstance(data, dict):
                nodes = []
            else:
                raise RuntimeError(
                    f"Failed to list open assigned issues for {assignee_login!r} via gh: response contained no data"
                )
        items = self._parse_recent_tracked_item_payload(
            [
                _recent_tracked_item_node_entry(node)
                for index, node in enumerate(nodes)
                if node is not None or index not in inaccessible_nodes
            ],
            repositories=[assignee_login],
        )
        return [
            cast(AssignedIssue, {k: v for k, v in item.items() if k != "is_pull_request"})
            for item in items
            if not item["is_pull_request"]
        ]

    def list_open_todo_issues(
        self,
        todo_repository: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[TodoIssue]:
        """List the open todo issues of the todo repository with their targets.

        A todo repository only has to exist once the user captures their first
        todo, so one that is not on GitHub yet simply holds no todo issues.
        """

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        owner, _, name = todo_repository.partition("/")
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            f"{{ issues(first: {_ITEMS_FETCH_LIMIT}, states: OPEN, labels: [{json.dumps(TODO_LABEL)}], "
            f"orderBy: {{field: CREATED_AT, direction: DESC}}) {{ nodes {{ {_ISSUE_FIELDS} body }} }} }} }}",
        ]
        try:
            completed = _run_gh_command_with_retry(
                command,
                not_found_message=(
                    "Failed to run gh todo issue list: gh CLI is not installed or not on PATH."
                ),
                failure_message_prefix=(
                    f"Failed to list open todo issues in {todo_repository} via gh"
                ),
                tolerate_partial_response=True,
            )
        except TransientFetchError:
            # A retry-exhausted failure says nothing about the repository existing.
            raise
        except RuntimeError as error:
            if not is_missing_repository_error(str(error)):
                raise
            report_progress(f"Todo repository {todo_repository} does not exist yet.")
            return []
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh todo issue JSON for {todo_repository}: {error.msg}"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        for entry in errors if isinstance(errors, list) else []:
            message = entry.get("message") if isinstance(entry, dict) else None
            path = entry.get("path") if isinstance(entry, dict) else None
            if (
                isinstance(path, list)
                and len(path) > 3
                and path[:3] == ["repository", "issues", "nodes"]
                and type(path[3]) is int
                and path[3] >= 0
                and _accept_summary_error(entry, data, path[:4])
            ):
                report_progress(f"Warning: sub-issue lookup failed: {message}")
                continue
            if isinstance(message, str) and is_missing_repository_error(message):
                report_progress(f"Todo repository {todo_repository} does not exist yet.")
                return []
            raise RuntimeError(
                f"Failed to list open todo issues in {todo_repository} via gh: {message or 'unknown error'}"
            )
        data = payload.get("data") if isinstance(payload, dict) else None
        repository = data.get("repository") if isinstance(data, dict) else None
        issues = repository.get("issues") if isinstance(repository, dict) else None
        nodes = issues.get("nodes") if isinstance(issues, dict) else None
        return self._parse_todo_issue_payload(nodes, todo_repository=todo_repository)

    def list_recent_tracked_items(
        self,
        search_scope: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[RecentTrackedItem]:
        """List recently updated open issues/PRs across the tracked search scope.

        ``search_scope`` is the space-joined ``user:``/``repo:`` GitHub search
        qualifiers built from the configured repository selectors; an empty
        scope means nothing is tracked. One aggregated GraphQL ``search`` query
        replaces the old per-repository-batch REST searches. A repository or
        item the scope cannot reach is silently absent from the results,
        exactly like any other GitHub search.
        """

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        if not search_scope:
            return []

        query = f"is:open sort:updated-desc {search_scope}"
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            "query={ " + _recent_tracked_items_selection(query) + " }",
        ]
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh recent tracked item search: "
                "gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix="Failed to list recent tracked items via gh",
            report_progress=report_progress,
            # A repository or item denied mid-scan still answers for the rest
            # of the scope, and gh reports that partial answer as a failure.
            tolerate_partial_response=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh recent tracked item JSON: {error.msg}"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        alias_data = data.get("tracked") if isinstance(data, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None

        nodes: list[object] = []
        if isinstance(errors, list) and errors:
            for entry in errors:
                message = entry.get("message") if isinstance(entry, dict) else None
                path = entry.get("path") if isinstance(entry, dict) else None
                if (
                    isinstance(path, list)
                    and len(path) > 2
                    and path[:2] == ["tracked", "nodes"]
                    and type(path[2]) is int
                    and path[2] >= 0
                    and _accept_summary_error(entry, data, path[:3])
                ):
                    report_progress(f"Warning: sub-issue lookup failed: {message}")
                    continue
                if not isinstance(message, str) or not (
                    _is_repository_authorization_error(message)
                    or _is_unresolvable_item_error(message)
                ):
                    raise RuntimeError(
                        f"Failed to list recent tracked items via gh: {message or 'unknown error'}"
                    )
            # A denied or unresolvable repository/item in the scope still
            # answers with whatever it could resolve.
            resolved_nodes = alias_data.get("nodes") if isinstance(alias_data, dict) else None
            if isinstance(resolved_nodes, list):
                nodes.extend(resolved_nodes)
        else:
            if not isinstance(alias_data, dict):
                raise RuntimeError(
                    "Failed to list recent tracked items via gh: "
                    f"{completed.stderr.strip() or 'response contained no data'}"
                )
            resolved_nodes = alias_data.get("nodes")
            if not isinstance(resolved_nodes, list):
                raise RuntimeError(
                    "Invalid gh recent tracked item payload: expected a nodes array."
                )
            nodes.extend(resolved_nodes)

        return self._parse_recent_tracked_item_payload(
            [_recent_tracked_item_node_entry(node) for node in nodes],
            repositories=[search_scope],
        )

    def fetch_linked_issues(
        self,
        pull_requests: list[tuple[str, int]],
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[tuple[str, int], list[tuple[str, int]]]:
        """Map each ``(repo, number)`` pull request to the issues it closes.

        Pull requests that close no issue are absent from the result. Every
        closing issue is reported, because a caller hiding covered work and a
        caller naming a worktree do not accept the same issues. A pull request
        GitHub denies or cannot resolve is reported through the progress callback
        and left out.

        :param list pull_requests: ``(repo, number)`` pairs to look up.
        """

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        linked_issues, skips = self._lookup_closing_issues(pull_requests)
        for subject, reason in skips.items():
            report_progress(f"Warning: skipped linked issue lookup for {subject}: {reason}")
        return linked_issues

    def fetch_linked_issues_for_pull_request(self, repo: str, number: int) -> list[tuple[str, int]]:
        """List the issues one pull request closes.

        Returns an empty list when it closes no issue. Raises ``RuntimeError``
        carrying GitHub's reason when the lookup is denied or cannot be resolved,
        so an unknown answer never passes for a pull request without linked issues.

        :param str repo: Repository of the pull request.
        :param int number: Pull request number.
        """

        linked_issues, skips = self._lookup_closing_issues([(repo, number)])
        if skips:
            raise RuntimeError(skips[f"{repo}#{number}"])
        return linked_issues.get((repo, number), [])

    def fetch_item(
        self, repo: str, number: int, *, github_username: str, todo_repository: str
    ) -> WorkItem | None:
        """Look up one issue or pull request by number, whatever its state.

        Returns ``None`` when GitHub cannot resolve the repository or the item, or
        denies access to it. Every other failure raises, so a GitHub outage is never
        mistaken for a missing item.

        :param str todo_repository: Repository whose issues are todos, so their
            target is read from the issue body.
        """

        subject = f"{repo}#{number}"
        owner, _, name = repo.partition("/")
        fields = "title url createdAt updatedAt author { login }"
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            f"{{ nameWithOwner issueOrPullRequest(number: {number}) {{ __typename "
            f"... on Issue {{ {_ISSUE_FIELDS} body }} "
            f"... on PullRequest {{ {fields} {_CLOSING_ISSUES_SELECTION} }} }} }} }}",
        ]
        failure_message_prefix = f"Failed to look up {subject} via gh"
        completed = _run_gh_command_with_retry(
            command,
            not_found_message="Failed to run gh item lookup: gh CLI is not installed or not on PATH.",
            failure_message_prefix=failure_message_prefix,
            # gh reports "could not resolve" and access denials as a failed command
            # that still prints the GraphQL errors.
            tolerate_partial_response=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh item JSON for {subject}: {error.msg}"
            ) from error
        data = payload.get("data") if isinstance(payload, dict) else None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        for entry in errors if isinstance(errors, list) else []:
            message = entry.get("message") if isinstance(entry, dict) else None
            if _accept_summary_error(entry, data, ["repository", "issueOrPullRequest"]):
                continue
            if isinstance(message, str) and (
                _is_unresolvable_item_error(message) or _is_repository_authorization_error(message)
            ):
                return None
            raise RuntimeError(f"{failure_message_prefix}: {message or 'unknown error'}")
        data = payload.get("data") if isinstance(payload, dict) else None
        repository = data.get("repository") if isinstance(data, dict) else None
        node = repository.get("issueOrPullRequest") if isinstance(repository, dict) else None
        if node is None and isinstance(repository, dict):
            return None
        context = f"Invalid gh item payload for {subject}"
        if not isinstance(node, dict):
            raise RuntimeError(f"{context}: missing repository.issueOrPullRequest.")
        repo = repository.get("nameWithOwner")
        typename = node.get("__typename")
        title, url, created_at, updated_at = (
            node.get(key) for key in ("title", "url", "createdAt", "updatedAt")
        )
        if typename not in {"Issue", "PullRequest"} or not all(
            isinstance(value, str) for value in (repo, title, url, created_at, updated_at)
        ):
            raise RuntimeError(f"{context}: missing or invalid fields.")
        if typename == "Issue":
            total = _sub_issue_total(
                node, RuntimeError(f"{context}: invalid subIssuesSummary.total.")
            )
            return WorkItem(
                kind=WorkItemKind.TRACKED_ISSUE,
                item_type=WorkItemType.ISSUE,
                repo=repo,
                number=number,
                title=title,
                created_at=parse_github_datetime(created_at),
                updated_at=parse_github_datetime(updated_at),
                url=url,
                sub_issue_total=total,
                has_sub_issues=(total or 0) > 0,
                todo_target=todo_target_from_body(node.get("body"))
                if repo.lower() == todo_repository.lower()
                else None,
            )
        author = node.get("author")
        author_login = author.get("login") if isinstance(author, dict) else None
        authored_by_user = isinstance(author_login, str) and (
            author_login.strip().lower() == github_username.strip().lower()
        )
        # Only an issue in the pull request's own repository can name its worktree.
        closing_issues = self._closing_issues({"pullRequest": node}, subject=subject)
        return WorkItem(
            kind=WorkItemKind.AUTHORED_PR if authored_by_user else WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo=repo,
            number=number,
            title=title,
            created_at=parse_github_datetime(created_at),
            updated_at=parse_github_datetime(updated_at),
            url=url,
            closing_issue_numbers=tuple(
                sorted(
                    issue_number
                    for issue_repo, issue_number in closing_issues
                    if issue_repo == repo
                )
            ),
        )

    def fetch_item_by_url(
        self,
        parsed_url: ParsedGitHubItemURL,
        github_username: str,
        progress_callback: Callable[[str], None] | None = None,
    ) -> WorkItem | None:
        """Fetch a single issue or pull request described by ``parsed_url``.

        A pull request authored by ``github_username`` is the user's own work,
        so it is included as an authored pull request rather than as one waiting
        to be looked at.

        Returns a ``WorkItem`` when the item is OPEN. Returns ``None`` for
        permanent-drop cases: state != OPEN (closed/merged), an inaccessible
        item, or a permanent ``gh`` command error. Raises ``TransientFetchError``
        when the URL should be retained for a later retry.
        """

        if parsed_url.item_type == WorkItemType.ISSUE:
            owner, _, name = parsed_url.repo.partition("/")
            command = [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f"{{ issue(number: {parsed_url.number}) {{ {_ISSUE_FIELDS} }} }} }}",
            ]
            try:
                completed = _run_gh_command_with_retry(
                    command,
                    not_found_message="Failed to run gh item view: gh CLI is not installed or not on PATH.",
                    failure_message_prefix=f"Failed to view issue {parsed_url.repo}#{parsed_url.number} via gh",
                    tolerate_partial_response=True,
                )
            except TransientFetchError:
                raise
            except RuntimeError:
                return None
            try:
                payload = json.loads(completed.stdout)
                if not isinstance(payload, dict):
                    raise ValueError("expected an object")
                errors = payload.get("errors", [])
                if errors:
                    if not isinstance(errors, list) or any(
                        not isinstance(error, dict) or not isinstance(error.get("message"), str)
                        for error in errors
                    ):
                        raise ValueError("invalid errors")
                    remaining_errors = []
                    for error in errors:
                        if _accept_summary_error(
                            error, payload.get("data"), ["repository", "issue"]
                        ):
                            if progress_callback is not None:
                                progress_callback(
                                    f"Warning: sub-issue lookup failed: {error['message']}"
                                )
                        else:
                            remaining_errors.append(error)
                    errors = remaining_errors
                    if errors:
                        if all(
                            "bad credentials" in error["message"].lower()
                            or _is_repository_authorization_error(error["message"])
                            or error.get("path") in (None, ["repository"], ["repository", "issue"])
                            and (
                                error.get("type") == "NOT_FOUND"
                                or error["message"].startswith(
                                    (
                                        "Could not resolve to a Repository",
                                        "Could not resolve to an Issue",
                                    )
                                )
                            )
                            for error in errors
                        ):
                            return None
                        raise ValueError(" ".join(error["message"] for error in errors))
                repository = payload["data"]["repository"]
                if repository is None or repository["issue"] is None:
                    return None
                node = repository["issue"]
                state = node["state"]
                if not isinstance(state, str):
                    raise ValueError("invalid state")
                if state.upper() != "OPEN":
                    return None
                total = _sub_issue_total(node, ValueError("invalid sub-issue count"))
                title, url, created, updated = (
                    node[key] for key in ("title", "url", "createdAt", "updatedAt")
                )
                if not all(isinstance(value, str) for value in (title, url, created, updated)):
                    raise ValueError("missing required fields")
                return WorkItem(
                    kind=WorkItemKind.TRACKED_ISSUE,
                    item_type=WorkItemType.ISSUE,
                    repo=parsed_url.repo,
                    number=parsed_url.number,
                    title=title,
                    url=url,
                    created_at=parse_github_datetime(created),
                    updated_at=parse_github_datetime(updated),
                    included=True,
                    sub_issue_total=total,
                    has_sub_issues=(total or 0) > 0,
                )
            except (ValueError, KeyError, TypeError, RuntimeError) as error:
                raise TransientFetchError(
                    f"Malformed gh view payload for {parsed_url.repo}#{parsed_url.number}: {error}"
                ) from error

        kind_subcommand = "pr" if parsed_url.item_type == WorkItemType.PR else "issue"
        command = [
            "gh",
            kind_subcommand,
            "view",
            str(parsed_url.number),
            "--repo",
            parsed_url.repo,
            "--json",
            "number,title,url,createdAt,updatedAt,state,author",
        ]
        try:
            completed = _run_gh_command_with_retry(
                command,
                not_found_message=(
                    "Failed to run gh item view: gh CLI is not installed or not on PATH."
                ),
                failure_message_prefix=(
                    f"Failed to view {parsed_url.item_type.value} "
                    f"{parsed_url.repo}#{parsed_url.number} via gh"
                ),
            )
        except TransientFetchError:
            raise
        except RuntimeError:
            # Permanent error (e.g. 404 "not found", bad auth). Treat as gone
            # so the caller drops the URL from the store.
            return None
        # Malformed responses from gh are treated as transient so a botched
        # upstream response does not silently prune a valid URL from the store.
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise TransientFetchError(
                f"Malformed gh view JSON for {parsed_url.repo}#{parsed_url.number}: {error.msg}"
            ) from error
        if not isinstance(payload, dict):
            raise TransientFetchError(
                f"Malformed gh view payload for {parsed_url.repo}#{parsed_url.number}: "
                "expected an object."
            )
        state = payload.get("state")
        if not isinstance(state, str):
            raise TransientFetchError(
                f"Malformed gh view payload for {parsed_url.repo}#{parsed_url.number}: "
                "missing state."
            )
        if state.upper() != "OPEN":
            return None
        title = payload.get("title")
        url = payload.get("url")
        created_at = payload.get("createdAt")
        updated_at = payload.get("updatedAt")
        if not (
            isinstance(title, str)
            and isinstance(url, str)
            and isinstance(created_at, str)
            and isinstance(updated_at, str)
        ):
            raise TransientFetchError(
                f"Malformed gh view payload for {parsed_url.repo}#{parsed_url.number}: "
                "missing required fields."
            )
        # Convert parse errors into TransientFetchError so the included-items
        # store keeps the URL for a later retry instead of aborting the refresh
        # (other callers of parse_github_datetime still want the plain RuntimeError).
        try:
            created_at_parsed = parse_github_datetime(created_at)
            updated_at_parsed = parse_github_datetime(updated_at)
        except RuntimeError as error:
            raise TransientFetchError(
                f"gh payload contains invalid timestamp for "
                f"{parsed_url.repo}#{parsed_url.number}: {error}"
            ) from error
        author = payload.get("author")
        author_login = author.get("login") if isinstance(author, dict) else None
        # GitHub logins are case-insensitive and the configured username is free
        # text, so both sides are compared normalized.
        authored_by_user = isinstance(author_login, str) and (
            author_login.strip().lower() == github_username.strip().lower()
        )
        if parsed_url.item_type != WorkItemType.PR:
            kind = WorkItemKind.TRACKED_ISSUE
        elif authored_by_user:
            kind = WorkItemKind.AUTHORED_PR
        else:
            kind = WorkItemKind.TRACKED_PR
        return WorkItem(
            kind=kind,
            item_type=parsed_url.item_type,
            repo=parsed_url.repo,
            number=parsed_url.number,
            title=title,
            created_at=created_at_parsed,
            updated_at=updated_at_parsed,
            url=url,
            included=True,
        )

    def _issue_tree(self, repo: str, number: int) -> dict:
        owner, _, name = repo.partition("/")
        # GitHub allows at most 100 sub-issues per issue.
        data = self._graphql(
            f"query ReadyIssueTree {{ repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ issue(number: {number}) {{ ...ReadyIssue "
            f"subIssues(first: 100) {{ nodes {{ ...ReadyIssue }} }} }} }} }} {_ISSUE_FRAGMENT}",
            f"{repo}#{number}",
        )
        return data["repository"]["issue"]

    def _owner_repositories(self, owner: str) -> list[str]:
        nodes = self._all_nodes(
            f"{owner}/*",
            lambda after: (
                f"query ReadyOwnerRepositories {{ repositoryOwner(login: {json.dumps(owner)}) "
                f"{{ repositories(first: 100, after: {after}, isArchived: false, "
                f"ownerAffiliations: OWNER) {{ {_PAGE_INFO} nodes {{ nameWithOwner "
                "issues(states: OPEN) { totalCount } pullRequests(states: OPEN) { totalCount } "
                "} } } }"
            ),
            ("repositoryOwner", "repositories"),
        )
        # Most repositories of an owner have no open work; skipping them saves two lookups each.
        with _github_payload(f"{owner}/*"):
            return [
                node["nameWithOwner"]
                for node in nodes
                if node["issues"]["totalCount"] or node["pullRequests"]["totalCount"]
            ]

    def _repository_issues(self, repo: str, todo_repository: str) -> list[IssueWorkState]:
        owner, _, name = repo.partition("/")
        nodes = self._all_nodes(
            repo,
            lambda after: (
                f"""query ReadyRepositoryIssues {{
                    repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
                        issues(states: OPEN, first: 100, after: {after}) {{
                            {_PAGE_INFO}
                            nodes {{ ...ReadyIssue }}
                        }}
                    }}
                }}
                {_ISSUE_FRAGMENT}"""
            ),
            ("repository", "issues"),
        )
        with _github_payload(repo):
            return [self._issue_state(node, todo_repository) for node in nodes]

    def _fetch_pull_requests(self, pull_requests: Sequence[tuple[str, int]]) -> list[dict]:
        nodes = []
        for start in range(0, len(pull_requests), _PULL_REQUESTS_PER_QUERY):
            batch = pull_requests[start : start + _PULL_REQUESTS_PER_QUERY]
            subject = ", ".join(f"{repo}#{number}" for repo, number in batch)
            data = self._graphql(
                f"query ReadyPullRequests {{ {_pull_request_aliases(batch, '...ReadyPullRequest')} }} "
                f"{_PULL_REQUEST_FRAGMENT}",
                subject,
            )
            with _github_payload(subject):
                nodes += [data[f"p{index}"]["pullRequest"] for index in range(len(batch))]
        return nodes

    def _issue_state(self, node: dict, todo_repository: str) -> IssueWorkState:
        return IssueWorkState(
            item=self._issue_item(node, todo_repository),
            is_open=node["state"] == "OPEN",
            has_sub_issues=node["subIssuesSummary"]["total"] > 0,
            has_open_descendants=node["subIssuesSummary"]["total"] > 0,
            blocked=node["issueDependenciesSummary"]["blockedBy"] > 0,
            open_pull_requests=tuple(
                (pr["repository"]["nameWithOwner"], pr["number"])
                for pr in node["closedByPullRequestsReferences"]["nodes"]
                if pr["state"] == "OPEN"
            ),
            merged_pull_requests=tuple(
                (pr["repository"]["nameWithOwner"], pr["number"])
                for pr in node["closedByPullRequestsReferences"]["nodes"]
                if pr["state"] == "MERGED"
            ),
        )

    def _issue_item(self, node: dict, todo_repository: str) -> WorkItem:
        repo = node["repository"]["nameWithOwner"]
        return WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo=repo,
            number=node["number"],
            title=node["title"],
            created_at=parse_github_datetime(node["createdAt"]),
            updated_at=parse_github_datetime(node["updatedAt"]),
            url=node["url"],
            todo_target=todo_target_from_body(node["body"])
            if repo.lower() == todo_repository.lower()
            else None,
        )

    def _pull_request_state(
        self,
        node: dict,
        required_checks: dict[tuple[str, str], frozenset[str] | None],
        github_username: str,
        todo_repository: str,
        *,
        subject: str,
    ) -> PullRequestWorkState:
        with _github_payload(subject):
            repo = node["repository"]["nameWithOwner"]
            author = (node["author"] or {}).get("login")
            linked_issues = tuple(
                self._issue_item(issue, todo_repository)
                for issue in node["closingIssuesReferences"]["nodes"]
            )
            head_sha = node["headRefOid"]
            head_repository = node["headRepository"]
            commit = node["commits"]["nodes"][0]["commit"]
            base_key = (repo, node["baseRefName"])
            if base_key not in required_checks:
                required_checks[base_key] = self._required_checks(*base_key)
            ci_status, failing_checks = self._ci_status(
                repo, commit, head_sha, required_checks[base_key]
            )
            return PullRequestWorkState(
                item=WorkItem(
                    kind=WorkItemKind.AUTHORED_PR
                    if author and author.lower() == github_username.strip().lower()
                    else WorkItemKind.TRACKED_PR,
                    item_type=WorkItemType.PR,
                    repo=repo,
                    number=node["number"],
                    title=node["title"],
                    created_at=parse_github_datetime(node["createdAt"]),
                    updated_at=parse_github_datetime(node["updatedAt"]),
                    url=node["url"],
                    # Only an issue of the pull request's own repository can name its worktree.
                    closing_issue_numbers=tuple(
                        sorted(issue.number for issue in linked_issues if issue.repo == repo)
                    ),
                ),
                state=node["state"],
                author=author,
                linked_issues=linked_issues,
                head_sha=head_sha,
                head_writable=head_repository is not None
                and node["headRef"] is not None
                # A protected head branch can refuse pushes even from a writer.
                and (node["headRef"]["refUpdateRule"] or {}).get("viewerCanPush", True)
                and (
                    head_repository["viewerPermission"] in _PUSH_PERMISSIONS
                    or (
                        node["maintainerCanModify"]
                        and node["baseRepository"]["viewerPermission"] in _PUSH_PERMISSIONS
                    )
                ),
                base_ref=node["baseRefName"],
                base_head_sha=node["baseRef"]["target"]["oid"] if node["baseRef"] else None,
                mergeability={"MERGEABLE": "mergeable", "CONFLICTING": "conflicting"}.get(
                    node["mergeable"], "unknown"
                ),
                review_decision=node["reviewDecision"],
                ci_status=ci_status,
                failing_checks=failing_checks,
                feedback=_actionable_feedback(node),
                merged_at=parse_github_datetime(node["mergedAt"]) if node["mergedAt"] else None,
            )

    def _ci_status(
        self, repo: str, commit: dict, head_sha: str, required: frozenset[str] | None
    ) -> tuple[str, tuple[str, ...]]:
        """Classify the latest run of each check of the current head.

        Incomplete metadata fails closed. Checks beyond the first page are read by
        the head commit, so a push between pages cannot mix two heads.

        :param frozenset required: Checks the base branch requires, ``None`` when unreadable.
        """

        if commit["oid"] != head_sha:
            return "unknown", ()
        rollup = commit["statusCheckRollup"]
        contexts = rollup["contexts"]["nodes"] if rollup else []
        if rollup and rollup["contexts"]["pageInfo"]["hasNextPage"]:
            owner, _, name = repo.partition("/")
            contexts = self._all_nodes(
                f"{repo}@{head_sha}",
                lambda after: (
                    f"""query ReadyCommitChecks {{
                        repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{
                            object(oid: {json.dumps(head_sha)}) {{
                                ... on Commit {{
                                    statusCheckRollup {{
                                        contexts(first: 100, after: {after}) {{ {_CHECK_CONTEXTS} }}
                                    }}
                                }}
                            }}
                        }}
                    }}"""
                ),
                ("repository", "object", "statusCheckRollup", "contexts"),
            )
        # Re-runs keep their earlier attempts on the head; a run not started yet is the latest.
        # Workflows and apps may share a check name, so each keeps its own latest attempt.
        results: dict[tuple[str | None, int | None, str], str] = {}
        for context in sorted(
            contexts,
            key=lambda context: (context.get("startedAt") is None, context.get("startedAt")),
        ):
            if context["__typename"] == "CheckRun":
                suite = context["checkSuite"]
                run = suite["workflowRun"]
                key = (
                    suite["app"] and suite["app"]["slug"],
                    run and run["workflow"]["databaseId"],
                    context["name"],
                )
                results[key] = (
                    context["conclusion"] if context["status"] == "COMPLETED" else "PENDING"
                )
            else:
                results[(None, None, context["context"])] = context["state"]
        failing = dict.fromkeys(
            key[-1] for key, result in results.items() if result in _FAILED_RESULTS
        )
        if failing:
            return "failing", tuple(failing)
        if any(result in _PENDING_RESULTS for result in results.values()):
            return "pending", ()
        if required is None:
            return "unknown", ()
        if not results and not required:
            return "no_checks", ()
        if required - {key[-1] for key in results} or any(
            result not in _SUCCESSFUL_RESULTS for result in results.values()
        ):
            return "unknown", ()
        return "passing", ()

    def _required_checks(self, repo: str, branch: str) -> frozenset[str] | None:
        """Return the checks rulesets and branch protection require, ``None`` when unreadable."""

        def rest(path: str) -> object:
            completed = _run_gh_command_with_retry(
                ["gh", "api", path],
                not_found_message="Failed to run gh required checks lookup: "
                "gh CLI is not installed or not on PATH.",
                failure_message_prefix=f"Failed to read the required checks of {repo}:{branch} via gh",
            )
            return json.loads(completed.stdout)

        quoted_branch = urllib.parse.quote(branch, safe="")
        try:
            # A branch has far fewer than 100 rules.
            rules = rest(f"repos/{repo}/rules/branches/{quoted_branch}?per_page=100")
            required = {
                check["context"]
                for rule in rules
                if rule["type"] == "required_status_checks"
                for check in rule["parameters"]["required_status_checks"]
            }
            # The branch summary shows branch protection to every reader, unlike the
            # admin-only protection endpoints.
            branch_summary = rest(f"repos/{repo}/branches/{quoted_branch}")
            if branch_summary["protected"]:
                protection = branch_summary["protection"]["required_status_checks"]
                required |= set(protection["contexts"])
                required |= {check["context"] for check in protection["checks"]}
            return frozenset(required)
        except (RuntimeError, ValueError, KeyError, TypeError):
            # Checks that may be required are never mistaken for none being required.
            return None

    def _all_nodes(
        self, subject: str, query: Callable[[str], str], connection_path: Sequence[str]
    ) -> list[dict]:
        """Collect every node of a paginated GraphQL connection.

        :param Callable query: Builds the query for an ``after`` cursor literal.
        """

        nodes: list[dict] = []
        after = "null"
        while True:
            connection = self._graphql(query(after), subject)
            with _github_payload(subject):
                for key in connection_path:
                    connection = connection[key]
                nodes += connection["nodes"]
                if not connection["pageInfo"]["hasNextPage"]:
                    return nodes
                after = json.dumps(connection["pageInfo"]["endCursor"])

    def _graphql(self, query: str, subject: str) -> dict:
        # gh exits non-zero on any GraphQL error, so a partial answer fails the lookup.
        completed = _run_gh_command_with_retry(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            not_found_message="Failed to run gh work state lookup: "
            "gh CLI is not installed or not on PATH.",
            failure_message_prefix=f"Failed to look up {subject} via gh",
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Failed to parse gh JSON for {subject}: {error.msg}") from error
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(f"Invalid gh payload for {subject}: missing data.")
        return data

    def _fetch_item_context(
        self,
        item: WorkItem,
        *,
        issue_fields: str,
        pr_fields: str,
        context_label: str,
    ) -> dict[str, Any]:
        if item.item_type == WorkItemType.ISSUE:
            command = [
                "gh",
                "issue",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                issue_fields,
            ]
        else:
            command = [
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                pr_fields,
            ]
        return self._run_json_command(item=item, command=command, context_label=context_label)

    def _fetch_pr_discussion(self, item: WorkItem) -> dict[str, Any]:
        owner, _, name = item.repo.partition("/")
        # Lists are capped at 100 entries without pagination: more is not realistic.
        comments = "comments(first: 100) { nodes { author { login } body createdAt isMinimized minimizedReason } }"
        query = (
            f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
            f"{{ pullRequest(number: {item.number}) {{ "
            f"closingIssuesReferences(first: 100) {{ nodes {{ "
            f"number title body author {{ login }} repository {{ nameWithOwner }} {comments} }} }} "
            f"reviewThreads(first: 100) {{ nodes {{ isResolved path line {comments} }} }} "
            "} } }"
        )
        payload = self._run_json_command(
            item=item,
            command=["gh", "api", "graphql", "-f", f"query={query}"],
            context_label="linked issues and review threads",
        )
        pull_request = payload["data"]["repository"]["pullRequest"]
        return {
            "linkedIssues": [
                {
                    **issue,
                    "repository": issue["repository"]["nameWithOwner"],
                    "comments": issue["comments"]["nodes"],
                }
                for issue in pull_request["closingIssuesReferences"]["nodes"]
            ],
            "unresolvedReviewThreads": [
                {
                    "path": thread["path"],
                    "line": thread["line"],
                    "comments": thread["comments"]["nodes"],
                }
                for thread in pull_request["reviewThreads"]["nodes"]
                if not thread["isResolved"]
            ],
        }

    def _run_json_command(
        self,
        *,
        item: WorkItem,
        command: list[str],
        context_label: str,
    ) -> dict[str, Any]:
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                f"Failed to gather {context_label} with gh: gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=(
                f"Failed to gather {context_label} for {item.item_type.value} "
                f"{item.repo}#{item.number}"
            ),
            # Callers read this context while holding the session lock.
            retry_delay_seconds=_TRANSIENT_RETRY_DELAY_SECONDS,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse {context_label} JSON for {item.item_type.value} "
                f"{item.repo}#{item.number}: {error.msg}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid {context_label} payload for {item.item_type.value} "
                f"{item.repo}#{item.number}: expected a JSON object."
            )
        return payload

    @staticmethod
    def _head_repo_from_payload(payload: dict[str, Any], fallback_repo: str) -> str:
        head_repo_owner = payload.get("headRepositoryOwner", {})
        head_repo_info = payload.get("headRepository", {})
        owner_login = head_repo_owner.get("login", "") if isinstance(head_repo_owner, dict) else ""
        repo_name = head_repo_info.get("name", "") if isinstance(head_repo_info, dict) else ""
        return f"{owner_login}/{repo_name}" if owner_login and repo_name else fallback_repo

    @staticmethod
    def _parse_open_pull_request_payload(
        payload: object,
        *,
        context: str,
    ) -> list[AuthoredPullRequest]:
        if not isinstance(payload, list):
            raise RuntimeError(f"Invalid gh {context} PR payload: expected a JSON array.")

        pull_requests: list[AuthoredPullRequest] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} is not an object."
                )
            repository = entry.get("repository")
            if not isinstance(repository, dict):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid repository."
                )
            repository_name_with_owner = repository.get("nameWithOwner")
            if not isinstance(repository_name_with_owner, str) or not repository_name_with_owner:
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid repository.nameWithOwner."
                )
            pr_id = entry.get("id")
            if not isinstance(pr_id, (str, int)):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid id."
                )
            number = entry.get("number")
            if not isinstance(number, int):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid number."
                )
            title = entry.get("title")
            if not isinstance(title, str):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid title."
                )
            url = entry.get("url")
            if not isinstance(url, str):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid url."
                )
            created_at = entry.get("createdAt")
            if not isinstance(created_at, str):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid createdAt."
                )
            updated_at = entry.get("updatedAt")
            if not isinstance(updated_at, str):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid updatedAt."
                )
            is_draft = entry.get("isDraft")
            if not isinstance(is_draft, bool):
                raise RuntimeError(
                    f"Invalid gh {context} PR payload: entry {index} has missing or invalid isDraft."
                )
            pull_requests.append(
                AuthoredPullRequest(
                    id=pr_id,
                    repo=repository_name_with_owner,
                    number=number,
                    title=title,
                    url=url,
                    created_at=created_at,
                    updated_at=updated_at,
                    is_draft=is_draft,
                    ci_state=_extract_ci_state(entry),
                    review_decision=entry.get("reviewDecision"),
                    review_requested=_extract_pending_review_request(entry),
                    has_unresolved_comments=_extract_unresolved_comments(entry),
                )
            )
        return pull_requests

    @staticmethod
    def _parse_todo_issue_payload(
        payload: object,
        *,
        todo_repository: str,
    ) -> list[TodoIssue]:
        context = f"Invalid gh todo issue payload for {todo_repository}"
        if not isinstance(payload, list):
            raise RuntimeError(f"{context}: expected a JSON array.")
        issues: list[TodoIssue] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise RuntimeError(f"{context}: entry {index} is not an object.")
            values: dict[str, object] = {}
            for key, expected_types in (
                ("id", (str, int)),
                ("number", (int,)),
                ("title", (str,)),
                ("url", (str,)),
                ("createdAt", (str,)),
                ("updatedAt", (str,)),
            ):
                value = entry.get(key)
                if not isinstance(value, expected_types):
                    raise RuntimeError(f"{context}: entry {index} has missing or invalid {key}.")
                values[key] = value
            total = _sub_issue_total(
                entry, RuntimeError(f"{context}: entry {index} has invalid subIssuesSummary.total.")
            )
            issues.append(
                TodoIssue(
                    id=cast(str | int, values["id"]),
                    repo=todo_repository,
                    number=cast(int, values["number"]),
                    title=cast(str, values["title"]),
                    url=cast(str, values["url"]),
                    created_at=cast(str, values["createdAt"]),
                    updated_at=cast(str, values["updatedAt"]),
                    target=todo_target_from_body(entry.get("body")),
                    sub_issue_total=total,
                )
            )
        return issues

    @staticmethod
    def _parse_recent_tracked_item_payload(
        payload: object,
        *,
        repositories: list[str],
    ) -> list[RecentTrackedItem]:
        context = f"Invalid gh recent tracked item payload for repository batch {repositories!r}"
        if not isinstance(payload, list):
            raise RuntimeError(f"{context}: expected a JSON array.")

        required_fields = (
            ("id", (str, int)),
            ("number", (int,)),
            ("title", (str,)),
            ("url", (str,)),
            ("createdAt", (str,)),
            ("updatedAt", (str,)),
            ("state", (str,)),
            ("isPullRequest", (bool,)),
        )
        items: list[RecentTrackedItem] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise RuntimeError(f"{context}: entry {index} is not an object.")

            values: dict[str, object] = {}
            for key, expected_types in required_fields:
                value = entry.get(key)
                if not isinstance(value, expected_types):
                    raise RuntimeError(f"{context}: entry {index} has missing or invalid {key}.")
                values[key] = value

            repository = entry.get("repository")
            if not isinstance(repository, dict):
                raise RuntimeError(f"{context}: entry {index} has missing or invalid repository.")
            repository_name_with_owner = repository.get("nameWithOwner")
            if not isinstance(repository_name_with_owner, str) or not repository_name_with_owner:
                raise RuntimeError(
                    f"{context}: entry {index} has missing or invalid repository.nameWithOwner."
                )
            if cast(str, values["state"]).upper() != "OPEN":
                continue
            if values["isPullRequest"]:
                summary = entry.get("subIssuesSummary")
                total = summary.get("total") if isinstance(summary, dict) else None
            else:
                total = _sub_issue_total(
                    entry,
                    RuntimeError(f"{context}: entry {index} has invalid subIssuesSummary.total."),
                )
            item = RecentTrackedItem(
                id=cast(str | int, values["id"]),
                repo=repository_name_with_owner,
                number=cast(int, values["number"]),
                title=cast(str, values["title"]),
                url=cast(str, values["url"]),
                created_at=cast(str, values["createdAt"]),
                updated_at=cast(str, values["updatedAt"]),
                is_pull_request=cast(bool, values["isPullRequest"]),
            )
            if total is not None:
                item["sub_issue_total"] = total
            items.append(item)
        return items

    def _lookup_closing_issues(
        self, pull_requests: list[tuple[str, int]]
    ) -> tuple[dict[tuple[str, int], list[tuple[str, int]]], dict[str, str]]:
        """Read the issues each pull request closes in one GraphQL call.

        Returns the linked issues by pull request, plus GitHub's reason for every
        pull request it denied or could not resolve.
        """

        if not pull_requests:
            return {}, {}

        # One aliased GraphQL document for every pull request: `gh pr view` per
        # pull request would cost a round trip each.
        alias_selections = [
            f"p{index}: repository(owner: {json.dumps(repo.partition('/')[0])}, "
            f"name: {json.dumps(repo.partition('/')[2])}) "
            f"{{ pullRequest(number: {number}) {{ {_CLOSING_ISSUES_SELECTION} }} }}"
            for index, (repo, number) in enumerate(pull_requests)
        ]
        command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={{ {' '.join(alias_selections)} }}",
        ]
        failure_message_prefix = "Failed to inspect linked issues via gh"
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh linked issue lookup: gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=failure_message_prefix,
            # A query where some aliases are denied still answers for the
            # others, and gh reports that partial answer as a failure.
            tolerate_partial_response=True,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Failed to parse gh linked issue JSON: {error.msg}") from error
        # A response without `data` is a whole-query failure such as expired
        # credentials, which must not be mistaken for "no pull request closes an issue".
        repositories_by_alias = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(repositories_by_alias, dict):
            raise RuntimeError(
                f"{failure_message_prefix}: "
                f"{completed.stderr.strip() or 'response contained no data'}"
            )
        # Only the alias in an error's path attributes a failure to one pull
        # request; the merged stderr concatenates every alias's message.
        errors = payload.get("errors")
        skips_by_alias: dict[str, str] = {}
        subjects_by_alias = {
            f"p{index}": f"{repo}#{number}" for index, (repo, number) in enumerate(pull_requests)
        }
        for entry in errors if isinstance(errors, list) else []:
            entry_fields = entry if isinstance(entry, dict) else {}
            message = entry_fields.get("message")
            path = entry_fields.get("path")
            alias = path[0] if isinstance(path, list) and path else None
            subject = subjects_by_alias.get(alias)
            if subject is None:
                raise RuntimeError(f"{failure_message_prefix}: {message or 'unknown error'}")
            if not isinstance(message, str) or not (
                _is_repository_authorization_error(message) or _is_unresolvable_item_error(message)
            ):
                raise RuntimeError(
                    f"{failure_message_prefix} ({subject}): {message or 'unknown error'}"
                )
            skips_by_alias[alias] = message

        linked_issues: dict[tuple[str, int], list[tuple[str, int]]] = {}
        for index, (repo, number) in enumerate(pull_requests):
            alias = f"p{index}"
            if alias in skips_by_alias:
                continue
            closing_issues = self._closing_issues(
                repositories_by_alias.get(alias), subject=subjects_by_alias[alias]
            )
            if closing_issues:
                linked_issues[(repo, number)] = closing_issues
        return linked_issues, {
            subjects_by_alias[alias]: reason for alias, reason in skips_by_alias.items()
        }

    @staticmethod
    def _closing_issues(repository_node: object, *, subject: str) -> list[tuple[str, int]]:
        """Collect the ``(repo, number)`` issues one pull request closes.

        :param object repository_node: One aliased ``repository`` GraphQL node.
        :param str subject: ``owner/repo#number`` used in error messages.
        """

        context = f"Invalid gh linked issue payload for {subject}"
        pull_request = (
            repository_node.get("pullRequest") if isinstance(repository_node, dict) else None
        )
        if not isinstance(pull_request, dict):
            raise RuntimeError(f"{context}: expected an object.")
        references = pull_request.get("closingIssuesReferences")
        nodes = references.get("nodes") if isinstance(references, dict) else None
        if not isinstance(nodes, list):
            raise RuntimeError(f"{context}: missing or invalid closingIssuesReferences.")
        issues: list[tuple[str, int]] = []
        for node in nodes:
            node_fields = node if isinstance(node, dict) else {}
            number = node_fields.get("number")
            repository = node_fields.get("repository")
            repo = repository.get("nameWithOwner") if isinstance(repository, dict) else None
            if not isinstance(number, int) or not isinstance(repo, str) or not repo:
                raise RuntimeError(f"{context}: missing or invalid closing issue reference.")
            issues.append((repo, number))
        return issues


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


def _parse_json_object(raw: str, *, context: str) -> dict[str, Any]:
    """Parse ``raw`` as a gh JSON object, translating malformed output into ``RuntimeError``."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to parse gh {context} JSON: {error.msg}") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid gh {context} payload: expected a JSON object.")
    return payload


@contextmanager
def _github_payload(subject: str) -> Iterator[None]:
    """Report a GitHub answer lacking an expected field as a failed lookup."""

    try:
        yield
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise RuntimeError(
            f"Invalid gh payload for {subject}: missing or invalid {error}."
        ) from error


def _pull_request_aliases(batch: Sequence[tuple[str, int]], fields: str) -> str:
    """Name each pull request of a batch by one alias, selecting ``fields`` of it."""

    return " ".join(
        f"p{index}: repository(owner: {json.dumps(repo.partition('/')[0])}, "
        f"name: {json.dumps(repo.partition('/')[2])}) "
        f"{{ pullRequest(number: {number}) {{ {fields} }} }}"
        for index, (repo, number) in enumerate(batch)
    )


def _encode_todo_metadata(target: str | None) -> str:
    metadata: dict[str, object] = {"workdash_todo_version": _TODO_METADATA_VERSION}
    if target is not None:
        metadata["target"] = target
    return f"```json\n{json.dumps(metadata, indent=2, ensure_ascii=True)}\n```\n"


def _run_gh(
    command: list[str],
    *,
    operation: str,
    todo_repository: str,
    tolerate_stderr: str | None = None,
) -> str:
    """Run a gh command, translating its failures into user-facing advice.

    :param str | None tolerate_stderr: Marker that makes a gh failure a success.
    """

    try:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Failed to {operation}: gh CLI is not installed or not on PATH."
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        if tolerate_stderr is not None and tolerate_stderr in stderr.lower():
            return ""
        message = f"Failed to {operation}: {stderr or f'exit code {error.returncode}'}"
        if is_missing_repository_error(stderr):
            message = (
                f"{message.rstrip('.')}. Create the todo repository {todo_repository} "
                "on GitHub if it does not exist yet."
            )
        raise RuntimeError(message) from error
