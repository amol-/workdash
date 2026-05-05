"""GitHub data access wrappers."""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from .models import WorkItem, WorkItemKind, WorkItemType

_DEFAULT_PR_SEARCH_LIMIT = 1000
_DEFAULT_ASSIGNED_ISSUE_LIMIT = 20
_PR_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,isDraft,repository"
_ISSUE_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,repository"
_DEFAULT_RECENT_SEARCH_LIMIT = 1000
_RECENT_JSON_FIELDS = "id,number,title,url,createdAt,updatedAt,state,isPullRequest,repository"
_RECENT_QUALIFIER_BATCH_SIZE = 20
_TRANSIENT_RETRIES = 2
_TRANSIENT_RETRY_DELAY_SECONDS = 2.0
# gh prints transient API failures in two shapes we've seen in the wild:
#   - "HTTP 502: Server Error ..." (direct API call wrappers)
#   - "non-200 OK status code: 504 Gateway Timeout ..." (GraphQL / search paths)
_TRANSIENT_HTTP_STATUS_RE = re.compile(r"(?:HTTP |status code:?\s*)5\d{2}", re.IGNORECASE)


def _is_transient_gh_error(error: subprocess.CalledProcessError) -> bool:
    stderr = (error.stderr or "").strip()
    return bool(_TRANSIENT_HTTP_STATUS_RE.search(stderr))


def _noop_progress_callback(_message: str) -> None:
    """Ignore progress updates when no reporting target is configured."""


def _run_gh_command_with_retry(
    command: list[str],
    *,
    not_found_message: str,
    failure_message_prefix: str,
    report_progress: Callable[[str], None] = _noop_progress_callback,
    retry_label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` subprocess with bounded retry on transient 5xx failures."""

    for attempt in range(_TRANSIENT_RETRIES + 1):
        try:
            return subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise RuntimeError(not_found_message) from error
        except subprocess.CalledProcessError as error:
            if attempt < _TRANSIENT_RETRIES and _is_transient_gh_error(error):
                if retry_label is not None:
                    report_progress(
                        f"{retry_label} got transient error, "
                        f"retrying ({attempt + 1}/{_TRANSIENT_RETRIES})..."
                    )
                time.sleep(_TRANSIENT_RETRY_DELAY_SECONDS)
                continue
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"{failure_message_prefix}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error
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
    """

    merged_items: list[WorkItem] = []
    seen_identities: set[tuple[WorkItemType, str, int]] = set()
    for item in primary_items:
        identity = (item.item_type, item.repo, item.number)
        if identity in seen_identities:
            continue
        merged_items.append(item)
        seen_identities.add(identity)
    for item in secondary_items:
        identity = (item.item_type, item.repo, item.number)
        if identity in seen_identities:
            continue
        merged_items.append(item)
        seen_identities.add(identity)
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
    ) -> list[ReviewRequestedPullRequest]:
        """List open PRs where the given user is a requested reviewer."""

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
        direct_review_requested_items: list[ReviewRequestedPullRequest] = []
        normalized_reviewer_login = reviewer_login.strip().lower()
        for item in parsed_items:
            review_request_command = [
                "gh",
                "pr",
                "view",
                str(item["number"]),
                "--repo",
                item["repo"],
                "--json",
                "reviewRequests",
            ]
            review_request_completed = _run_gh_command_with_retry(
                review_request_command,
                not_found_message=(
                    "Failed to run gh review-request metadata lookup: "
                    "gh CLI is not installed or not on PATH."
                ),
                failure_message_prefix=(
                    "Failed to inspect review-request metadata for "
                    f"{item['repo']}#{item['number']} via gh"
                ),
            )
            try:
                review_request_payload = json.loads(review_request_completed.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    "Failed to parse gh review-request metadata JSON for "
                    f"{item['repo']}#{item['number']}: {error.msg}"
                ) from error
            if not isinstance(review_request_payload, dict):
                raise RuntimeError(
                    "Invalid gh review-request metadata payload for "
                    f"{item['repo']}#{item['number']}: expected an object."
                )
            review_requests = review_request_payload.get("reviewRequests")
            if not isinstance(review_requests, list):
                raise RuntimeError(
                    "Invalid gh review-request metadata payload for "
                    f"{item['repo']}#{item['number']}: missing or invalid reviewRequests."
                )
            has_direct_request = False
            for request in review_requests:
                if not isinstance(request, dict):
                    continue
                if request.get("__typename") != "User":
                    continue
                login = request.get("login")
                if isinstance(login, str) and login.strip().lower() == normalized_reviewer_login:
                    has_direct_request = True
                    break
            if has_direct_request:
                direct_review_requested_items.append(item)
        return direct_review_requested_items

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
            for repository in repository_batch:
                command.extend(["--repo", repository])
            completed = _run_gh_command_with_retry(
                command,
                not_found_message=(
                    "Failed to run gh recent tracked item search: "
                    "gh CLI is not installed or not on PATH."
                ),
                failure_message_prefix=(
                    "Failed to list recent tracked items for repository batch "
                    f"{repository_batch!r} via gh"
                ),
                report_progress=report_progress,
                retry_label=f"Batch {batch_number}/{batch_count}",
            )
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    "Failed to parse gh recent tracked item JSON for repository batch "
                    f"{repository_batch!r}: {error.msg}"
                ) from error
            if not isinstance(payload, list):
                raise RuntimeError(
                    "Invalid gh recent tracked item payload for repository batch "
                    f"{repository_batch!r}: expected a JSON array."
                )

            for index, entry in enumerate(payload):
                if not isinstance(entry, dict):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} is not an object."
                    )
                record_id = entry.get("id")
                if not isinstance(record_id, (str, int)):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid id."
                    )
                number = entry.get("number")
                if not isinstance(number, int):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid number."
                    )
                title = entry.get("title")
                if not isinstance(title, str):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid title."
                    )
                url = entry.get("url")
                if not isinstance(url, str):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid url."
                    )
                created_at = entry.get("createdAt")
                if not isinstance(created_at, str):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid createdAt."
                    )
                updated_at = entry.get("updatedAt")
                if not isinstance(updated_at, str):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid updatedAt."
                    )
                state = entry.get("state")
                if not isinstance(state, str):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid state."
                    )
                is_pull_request = entry.get("isPullRequest")
                if not isinstance(is_pull_request, bool):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid isPullRequest."
                    )
                repository = entry.get("repository")
                if not isinstance(repository, dict):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid repository."
                    )
                repository_name_with_owner = repository.get("nameWithOwner")
                if (
                    not isinstance(repository_name_with_owner, str)
                    or not repository_name_with_owner
                ):
                    raise RuntimeError(
                        "Invalid gh recent tracked item payload for repository batch "
                        f"{repository_batch!r}: entry {index} has missing or invalid repository.nameWithOwner."
                    )
                if state.upper() != "OPEN":
                    continue
                dedupe_key = (repository_name_with_owner, number, is_pull_request)
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                items.append(
                    RecentTrackedItem(
                        id=record_id,
                        repo=repository_name_with_owner,
                        number=number,
                        title=title,
                        url=url,
                        created_at=created_at,
                        updated_at=updated_at,
                        is_pull_request=is_pull_request,
                    )
                )
            report_progress(
                f"Processed batch {batch_number}/{batch_count}; accumulated {len(items)} unique item(s)."
            )
        return items
