"""GitHub data access wrappers."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import NotRequired, TypedDict, cast
from urllib.parse import urlsplit

from .models import WorkItem, WorkItemKind, WorkItemType
from .todo import TODO_LABEL, is_missing_repository_error, todo_target_from_body

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
                delay = (
                    _RATE_LIMIT_RETRY_DELAY_SECONDS
                    if _is_rate_limit_error(stderr)
                    else _TRANSIENT_RETRY_DELAY_SECONDS
                )
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


def parse_github_datetime(timestamp: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp into a timezone-aware datetime."""

    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"Invalid GitHub timestamp {timestamp!r}: {error}") from error
    if parsed_timestamp.tzinfo is None:
        raise RuntimeError(f"Invalid GitHub timestamp {timestamp!r}: timezone offset is required.")
    return parsed_timestamp


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
    """Thin wrapper around the ``gh`` CLI for work item discovery.

    Each method shells out to ``gh search`` or ``gh pr view`` and normalizes
    the JSON payload into TypedDict records for downstream conversion.
    """

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

    def fetch_linked_issues(
        self,
        pull_requests: list[tuple[str, int]],
        progress_callback: Callable[[str], None] | None = None,
    ) -> dict[tuple[str, int], list[tuple[str, int]]]:
        """Map each ``(repo, number)`` pull request to the issues it closes.

        Pull requests that close no issue are absent from the result. Every
        closing issue is reported, because a caller hiding covered work and a
        caller naming a worktree do not accept the same issues.

        :param list pull_requests: ``(repo, number)`` pairs to look up.
        """

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        if not pull_requests:
            return {}

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
            subject = subjects_by_alias[alias]
            skip_reason = skips_by_alias.get(alias)
            if skip_reason is not None:
                report_progress(
                    f"Warning: skipped linked issue lookup for {subject}: {skip_reason}"
                )
                continue
            closing_issues = self._closing_issues(repositories_by_alias.get(alias), subject=subject)
            if closing_issues:
                linked_issues[(repo, number)] = closing_issues
        return linked_issues

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
