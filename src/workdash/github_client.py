"""GitHub data access wrappers."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict, cast
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


_DEFAULT_PR_SEARCH_LIMIT = 1000
_DEFAULT_ASSIGNED_ISSUE_LIMIT = 20
_PR_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,isDraft,repository"
# GraphQL selection listing the reviewers a pull request asked for, so a
# direct user request can be told apart from a team-wide one.
_REVIEW_REQUESTS_SELECTION = (
    "reviewRequests(first: 100) "
    "{ nodes { requestedReviewer { __typename ... on User { login } } } }"
)
_ISSUE_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,repository"
# `gh issue list` runs inside one repository and cannot report `repository`,
# but it can report the body that carries the todo target metadata.
_TODO_ISSUE_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,body"
_DEFAULT_TODO_ISSUE_LIMIT = 200
_DEFAULT_RECENT_SEARCH_LIMIT = 1000
_RECENT_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,state,isPullRequest,repository"
_RECENT_QUALIFIER_BATCH_SIZE = 20
_TRANSIENT_RETRIES = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0
# gh surfaces transient API failures in several shapes we've seen in the wild:
#   - "HTTP 5xx: ..." / "non-200 OK status code: 5xx ..." (5xx server errors)
#   - "HTTP 429" / "API rate limit exceeded" / "abuse detection" (rate limiting)
#   - "dial tcp ... no such host" / "connection reset" / "i/o timeout" ... (network)
#   - "result of a timeout" (GraphQL execution timeout, sometimes with HTTP 200)
# Each of these can succeed on a later retry, so the caller should retain the
# URL in the included-items store and retry on the next refresh.
_TRANSIENT_HTTP_STATUS_RE = re.compile(r"(?:HTTP |status code:?\s*)5\d{2}", re.IGNORECASE)
_TRANSIENT_SUBSTRINGS = (
    "http 429",
    "rate limit",
    "abuse detection",
    "dial tcp",
    "connection refused",
    "connection reset",
    "no such host",
    "i/o timeout",
    "result of a timeout",
    "context deadline exceeded",
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


class RepositoryAuthorizationError(RuntimeError):
    """Raised when gh reports repository-specific authorization is required."""


def _is_transient_gh_error(error: subprocess.CalledProcessError) -> bool:
    stderr = (error.stderr or "").strip()
    if not stderr:
        return False
    if _TRANSIENT_HTTP_STATUS_RE.search(stderr):
        return True
    stderr_lower = stderr.lower()
    return any(needle in stderr_lower for needle in _TRANSIENT_SUBSTRINGS)


def _is_repository_authorization_error(message: str) -> bool:
    message_lower = message.lower()
    return any(needle in message_lower for needle in _REPOSITORY_AUTHORIZATION_SUBSTRINGS)


def _is_unresolvable_item_error(message: str) -> bool:
    """Report whether a GraphQL error says GitHub cannot resolve the queried item.

    A repository that was renamed, deleted, or is no longer visible, and a pull
    request that is gone, are all as inaccessible as an authorization denial:
    the work item cannot be loaded, but every other item still can.
    """

    return "could not resolve to a" in message.lower()


def _noop_progress_callback(_message: str) -> None:
    """Ignore progress updates when no reporting target is configured."""


def _run_gh_command_with_retry(
    command: list[str],
    *,
    not_found_message: str,
    failure_message_prefix: str,
    report_progress: Callable[[str], None] = _noop_progress_callback,
    retry_label: str | None = None,
    classify_repository_authorization: bool = False,
    tolerate_partial_response: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` subprocess with bounded retry on transient failures.

    Raises ``TransientFetchError`` when retries are exhausted and the
    final failure was classified transient (5xx, 429, rate-limit, common
    network errors). Raises a plain ``RuntimeError`` for permanent errors
    (bad auth, 404, malformed command) so existing callers continue to
    surface the same descriptive message they did before. Raises
    ``RepositoryAuthorizationError`` for known repository-specific denial
    shapes when ``classify_repository_authorization`` is enabled.

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
            transient = _is_transient_gh_error(error)
            if attempt < _TRANSIENT_RETRIES and transient:
                if retry_label is not None:
                    report_progress(
                        f"{retry_label} got transient error, "
                        f"retrying ({attempt + 1}/{_TRANSIENT_RETRIES})..."
                    )
                time.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                continue
            stderr = (error.stderr or "").strip()
            message = (
                f"{failure_message_prefix}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            )
            if transient:
                raise TransientFetchError(message) from error
            if classify_repository_authorization and _is_repository_authorization_error(stderr):
                raise RepositoryAuthorizationError(message) from error
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


class AssignedIssue(TypedDict):
    """Raw assigned issue record from gh search, normalized for conversion."""

    id: str | int
    repo: str
    number: int
    title: str
    url: str
    created_at: str
    updated_at: str


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
            continue
        indexes_by_identity[identity] = len(merged_items)
        merged_items.append(item)
    for item in secondary_items:
        identity = (item.item_type, item.repo, item.number)
        if identity in indexes_by_identity:
            if item.included:
                merged_items[indexes_by_identity[identity]].included = True
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
                )
            )
        return pull_requests

    @staticmethod
    def _parse_issue_payload(
        payload: object,
        *,
        context: str,
    ) -> list[AssignedIssue]:
        if not isinstance(payload, list):
            raise RuntimeError(f"Invalid gh {context} issue payload: expected a JSON array.")

        issues: list[AssignedIssue] = []
        for index, entry in enumerate(payload):
            if not isinstance(entry, dict):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} is not an object."
                )
            repository = entry.get("repository")
            if not isinstance(repository, dict):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid repository."
                )
            repository_name_with_owner = repository.get("nameWithOwner")
            if not isinstance(repository_name_with_owner, str) or not repository_name_with_owner:
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid repository.nameWithOwner."
                )
            record_id = entry.get("id")
            if not isinstance(record_id, (str, int)):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid id."
                )
            number = entry.get("number")
            if not isinstance(number, int):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid number."
                )
            title = entry.get("title")
            if not isinstance(title, str):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid title."
                )
            url = entry.get("url")
            if not isinstance(url, str):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid url."
                )
            created_at = entry.get("createdAt")
            if not isinstance(created_at, str):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid createdAt."
                )
            updated_at = entry.get("updatedAt")
            if not isinstance(updated_at, str):
                raise RuntimeError(
                    f"Invalid gh {context} issue payload: entry {index} has missing or invalid updatedAt."
                )
            issues.append(
                AssignedIssue(
                    id=record_id,
                    repo=repository_name_with_owner,
                    number=number,
                    title=title,
                    url=url,
                    created_at=created_at,
                    updated_at=updated_at,
                )
            )
        return issues

    def list_open_authored_prs(
        self,
        author_login: str,
        limit: int = _DEFAULT_PR_SEARCH_LIMIT,
    ) -> list[AuthoredPullRequest]:
        """List open authored PRs across accessible repositories.

        This intentionally does not filter out draft PRs or fork PRs.
        """

        command = [
            "gh",
            "search",
            "prs",
            "--author",
            author_login,
            "--state",
            "open",
            "--limit",
            str(limit),
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
        return self._parse_open_pull_request_payload(
            payload,
            context=f"authored for {author_login!r}",
        )

    def list_open_review_requested_prs(
        self,
        reviewer_login: str,
        limit: int = _DEFAULT_PR_SEARCH_LIMIT,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[ReviewRequestedPullRequest]:
        """List open PRs where the given user is a requested reviewer."""

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        command = [
            "gh",
            "search",
            "prs",
            "--review-requested",
            reviewer_login,
            "--state",
            "open",
            "--limit",
            str(limit),
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
        parsed_items = [
            ReviewRequestedPullRequest(**item)
            for item in self._parse_open_pull_request_payload(
                payload,
                context=f"review-requested for {reviewer_login!r}",
            )
        ]
        if not parsed_items:
            return []

        # Ask for every pull request's reviewers in a single aliased GraphQL
        # document: one `gh pr view` per pull request cost ~10s for 15 results,
        # this costs ~1.6s.
        alias_selections: list[str] = []
        for index, item in enumerate(parsed_items):
            owner, _, name = item["repo"].partition("/")
            alias_selections.append(
                f"p{index}: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
                f"{{ pullRequest(number: {item['number']}) {{ {_REVIEW_REQUESTS_SELECTION} }} }}"
            )
        review_request_command = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={{ {' '.join(alias_selections)} }}",
        ]
        failure_message_prefix = (
            f"Failed to inspect review-request metadata for {reviewer_login!r} via gh"
        )
        review_request_completed = _run_gh_command_with_retry(
            review_request_command,
            not_found_message=(
                "Failed to run gh review-request metadata lookup: "
                "gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=failure_message_prefix,
            # A query where some aliases are denied still answers for the
            # others, and gh reports that partial answer as a failure.
            tolerate_partial_response=True,
        )
        try:
            review_request_payload = json.loads(review_request_completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Failed to parse gh review-request metadata JSON for "
                f"{reviewer_login!r}: {error.msg}"
            ) from error
        # A response without `data` is a whole-query failure such as expired
        # credentials, which must not be mistaken for "no direct requests".
        repositories_by_alias = (
            review_request_payload.get("data") if isinstance(review_request_payload, dict) else None
        )
        if not isinstance(repositories_by_alias, dict):
            raise RuntimeError(
                f"{failure_message_prefix}: "
                f"{review_request_completed.stderr.strip() or 'response contained no data'}"
            )
        # Only the alias in an error's path attributes a failure to one pull
        # request; the merged stderr concatenates every alias's message. A
        # failure that leaves the item unreachable skips just that item, while
        # anything else stays a hard failure.
        errors = review_request_payload.get("errors")
        skips_by_alias: dict[str, str] = {}
        subjects_by_alias = {
            f"p{index}": f"{item['repo']}#{item['number']}"
            for index, item in enumerate(parsed_items)
        }
        for entry in errors if isinstance(errors, list) else []:
            entry_fields = entry if isinstance(entry, dict) else {}
            message = entry_fields.get("message")
            path = entry_fields.get("path")
            alias = path[0] if isinstance(path, list) and path else None
            # A failure we cannot attribute to one alias would drop that pull
            # request without warning, so it stays a hard failure.
            subject = subjects_by_alias.get(alias)
            if subject is None:
                raise RuntimeError(f"{failure_message_prefix}: {message or 'unknown error'}")
            # One stderr covers every alias, so naming the subject is the only
            # way the operator learns which pull request broke.
            if not isinstance(message, str) or not (
                _is_repository_authorization_error(message) or _is_unresolvable_item_error(message)
            ):
                raise RuntimeError(
                    f"{failure_message_prefix} ({subject}): {message or 'unknown error'}"
                )
            skips_by_alias[alias] = message

        direct_review_requested_items: list[ReviewRequestedPullRequest] = []
        normalized_reviewer_login = reviewer_login.strip().lower()
        for index, item in enumerate(parsed_items):
            alias = f"p{index}"
            subject = subjects_by_alias[alias]
            skip_reason = skips_by_alias.get(alias)
            if skip_reason is not None:
                report_progress(
                    f"Warning: skipped review-requested pull request {subject}: {skip_reason}"
                )
                continue
            if normalized_reviewer_login in self._direct_review_requesters(
                repositories_by_alias.get(alias),
                subject=subject,
            ):
                direct_review_requested_items.append(item)
        return direct_review_requested_items

    @staticmethod
    def _direct_review_requesters(repository_node: object, *, subject: str) -> set[str]:
        """Collect the normalized logins whose review one pull request requested.

        Team requests and reviewers GitHub no longer resolves are ignored, so
        only a pull request that named the user keeps its REVIEW item.

        :param object repository_node: One aliased ``repository`` GraphQL node.
        :param str subject: ``owner/repo#number`` used in error messages.
        """

        context = f"Invalid gh review-request metadata payload for {subject}"
        if not isinstance(repository_node, dict):
            raise RuntimeError(f"{context}: expected an object.")
        pull_request = repository_node.get("pullRequest")
        if not isinstance(pull_request, dict):
            raise RuntimeError(f"{context}: expected an object.")
        review_requests = pull_request.get("reviewRequests")
        nodes = review_requests.get("nodes") if isinstance(review_requests, dict) else None
        if not isinstance(nodes, list):
            raise RuntimeError(f"{context}: missing or invalid reviewRequests.")
        logins: set[str] = set()
        for node in nodes:
            reviewer = node.get("requestedReviewer") if isinstance(node, dict) else None
            if not isinstance(reviewer, dict) or reviewer.get("__typename") != "User":
                continue
            login = reviewer.get("login")
            if isinstance(login, str):
                logins.add(login.strip().lower())
        return logins

    def list_open_reviewed_prs(
        self,
        reviewer_login: str,
        limit: int = _DEFAULT_PR_SEARCH_LIMIT,
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
            str(limit),
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
        limit: int = _DEFAULT_ASSIGNED_ISSUE_LIMIT,
    ) -> list[AssignedIssue]:
        """List open issues assigned to the given user, most recently updated first."""

        command = [
            "gh",
            "search",
            "issues",
            "--assignee",
            assignee_login,
            "--state",
            "open",
            "--sort",
            "updated",
            "--order",
            "desc",
            "--limit",
            str(limit),
            "--json",
            _ISSUE_JSON_FIELDS,
        ]
        completed = _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh assigned issue search: gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=(
                f"Failed to list open assigned issues for {assignee_login!r} via gh"
            ),
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh assigned issue JSON for {assignee_login!r}: {error.msg}"
            ) from error
        return self._parse_issue_payload(
            payload,
            context=f"assigned for {assignee_login!r}",
        )

    def list_open_todo_issues(
        self,
        todo_repository: str,
        limit: int = _DEFAULT_TODO_ISSUE_LIMIT,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[TodoIssue]:
        """List the open todo issues of the todo repository with their targets.

        A todo repository only has to exist once the user captures their first
        todo, so one that is not on GitHub yet simply holds no todo issues.
        """

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        command = [
            "gh",
            "issue",
            "list",
            "--repo",
            todo_repository,
            "--state",
            "open",
            "--label",
            TODO_LABEL,
            "--limit",
            str(limit),
            "--json",
            _TODO_ISSUE_JSON_FIELDS,
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
        return self._parse_todo_issue_payload(payload, todo_repository=todo_repository)

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
                )
            )
        return issues

    def list_recent_tracked_items(
        self,
        repositories: list[str],
        limit: int = _DEFAULT_RECENT_SEARCH_LIMIT,
        progress_callback: Callable[[str], None] | None = None,
    ) -> list[RecentTrackedItem]:
        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        if not repositories:
            return []

        deduped_repositories = list(dict.fromkeys(repositories))
        if not deduped_repositories:
            return []

        batch_count = (
            len(deduped_repositories) + _RECENT_QUALIFIER_BATCH_SIZE - 1
        ) // _RECENT_QUALIFIER_BATCH_SIZE
        report_progress(
            f"Prepared {len(deduped_repositories)} repository filter(s) across {batch_count} batch(es)."
        )
        items: list[RecentTrackedItem] = []
        seen_keys: set[tuple[str, int, bool]] = set()
        for batch_start in range(0, len(deduped_repositories), _RECENT_QUALIFIER_BATCH_SIZE):
            batch_number = (batch_start // _RECENT_QUALIFIER_BATCH_SIZE) + 1
            repository_batch = deduped_repositories[
                batch_start : batch_start + _RECENT_QUALIFIER_BATCH_SIZE
            ]
            report_progress(
                f"Querying recent items batch {batch_number}/{batch_count} for: {', '.join(repository_batch)}"
            )
            completed_batches: list[tuple[list[str], subprocess.CompletedProcess[str]]] = []
            try:
                completed_batches.append(
                    (
                        repository_batch,
                        self._run_recent_tracked_items_search(
                            repository_batch,
                            limit=limit,
                            report_progress=report_progress,
                            retry_label=f"Batch {batch_number}/{batch_count}",
                        ),
                    )
                )
            except RepositoryAuthorizationError as error:
                if len(repository_batch) == 1:
                    report_progress(
                        "Warning: skipped repository "
                        f"{repository_batch[0]} because GitHub denied access: {error}"
                    )
                    continue
                report_progress(
                    "A repository in this batch needs additional GitHub "
                    "authorization; checking repositories individually."
                )
                for repository in repository_batch:
                    try:
                        completed_batches.append(
                            (
                                [repository],
                                self._run_recent_tracked_items_search(
                                    [repository],
                                    limit=limit,
                                    report_progress=report_progress,
                                    retry_label=f"Repository {repository}",
                                ),
                            )
                        )
                    except RepositoryAuthorizationError as single_error:
                        report_progress(
                            "Warning: skipped repository "
                            f"{repository} because GitHub denied access: {single_error}"
                        )
                        continue

            for payload_repositories, completed in completed_batches:
                try:
                    payload = json.loads(completed.stdout)
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        "Failed to parse gh recent tracked item JSON for repository batch "
                        f"{payload_repositories!r}: {error.msg}"
                    ) from error

                for item in self._parse_recent_tracked_item_payload(
                    payload,
                    repositories=payload_repositories,
                ):
                    dedupe_key = (item["repo"], item["number"], item["is_pull_request"])
                    if dedupe_key in seen_keys:
                        continue
                    seen_keys.add(dedupe_key)
                    items.append(item)
            report_progress(
                f"Processed batch {batch_number}/{batch_count}; accumulated {len(items)} unique item(s)."
            )
        return items

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
            items.append(
                RecentTrackedItem(
                    id=cast(str | int, values["id"]),
                    repo=repository_name_with_owner,
                    number=cast(int, values["number"]),
                    title=cast(str, values["title"]),
                    url=cast(str, values["url"]),
                    created_at=cast(str, values["createdAt"]),
                    updated_at=cast(str, values["updatedAt"]),
                    is_pull_request=cast(bool, values["isPullRequest"]),
                )
            )
        return items

    def _run_recent_tracked_items_search(
        self,
        repositories: list[str],
        *,
        limit: int,
        report_progress: Callable[[str], None],
        retry_label: str,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            "gh",
            "search",
            "issues",
            "--include-prs",
            "--state",
            "open",
            "--limit",
            str(limit),
            "--json",
            _RECENT_JSON_FIELDS,
        ]
        for repository in repositories:
            command.extend(["--repo", repository])
        return _run_gh_command_with_retry(
            command,
            not_found_message=(
                "Failed to run gh recent tracked item search: "
                "gh CLI is not installed or not on PATH."
            ),
            failure_message_prefix=(
                f"Failed to list recent tracked items for repository batch {repositories!r} via gh"
            ),
            report_progress=report_progress,
            retry_label=retry_label,
            classify_repository_authorization=True,
        )

    def fetch_item_by_url(self, parsed_url: ParsedGitHubItemURL) -> WorkItem | None:
        """Fetch a single issue or pull request described by ``parsed_url``.

        Returns a ``WorkItem`` when the item is OPEN. Returns ``None`` for
        permanent-drop cases: state != OPEN (closed/merged) or a permanent
        ``gh`` error (e.g. 404 not-found). Raises ``TransientFetchError``
        on retryable failures (5xx, 429, common network errors, missing
        ``gh`` binary, malformed JSON) so callers can retain the URL for
        a later retry.
        """

        kind_subcommand = "pr" if parsed_url.item_type == WorkItemType.PR else "issue"
        command = [
            "gh",
            kind_subcommand,
            "view",
            str(parsed_url.number),
            "--repo",
            parsed_url.repo,
            "--json",
            "number,title,url,createdAt,updatedAt,state",
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
        return WorkItem(
            kind=WorkItemKind.TRACKED_PR
            if parsed_url.item_type == WorkItemType.PR
            else WorkItemKind.TRACKED_ISSUE,
            item_type=parsed_url.item_type,
            repo=parsed_url.repo,
            number=parsed_url.number,
            title=title,
            created_at=created_at_parsed,
            updated_at=updated_at_parsed,
            url=url,
            included=True,
        )
