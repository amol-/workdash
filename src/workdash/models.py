"""Shared data models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class WorkItemType(StrEnum):
    """GitHub object types shown in workdash."""

    ISSUE = "issue"
    PR = "pr"


class WorkItemKind(StrEnum):
    """Work categories shown in workdash."""

    AUTHORED_PR = "authored_pr"
    REVIEW_REQUESTED_PR = "review_requested_pr"
    ASSIGNED_ISSUE = "assigned_issue"
    TRACKED_PR = "tracked_pr"
    TRACKED_ISSUE = "tracked_issue"


@dataclass(slots=True)
class WorkItem:
    """Normalized dashboard row model."""

    kind: WorkItemKind
    item_type: WorkItemType
    repo: str
    number: int
    title: str
    created_at: datetime
    updated_at: datetime
    url: str
    analysis: str | None = None
    analyzed_at: datetime | None = None
    included: bool = False
    todo_target: str | None = None
    ci_state: str | None = None
    review_decision: str | None = None
    closing_issue_numbers: tuple[int, ...] = ()


_TYPE_LABELS = {
    WorkItemKind.ASSIGNED_ISSUE: "ISSUE",
    WorkItemKind.TRACKED_ISSUE: "ISSUE",
    WorkItemKind.AUTHORED_PR: "PR",
    WorkItemKind.REVIEW_REQUESTED_PR: "REVIEW",
    # A pull request nobody asked the user about is still waiting to be looked
    # at, which is different work from the user's own PRs and reviews.
    WorkItemKind.TRACKED_PR: "CHECK",
}


def format_type_label(item: WorkItem) -> str:
    """Return the type column label for ``item`` including the ``+`` include suffix."""

    base = _TYPE_LABELS[item.kind]
    return f"{base}+" if item.included else base


def worktree_item_number(item: WorkItem) -> int:
    """Return the item number naming a freshly created worktree for ``item``."""

    return accepted_worktree_numbers(item)[0]


def accepted_worktree_numbers(item: WorkItem) -> tuple[int, ...]:
    """Return every item number a worktree of ``item`` may be named after, most preferred first.

    A pull request the user authored is the implementation of every issue it
    closes in its own repository, so all of them share one checkout instead of
    splitting the same work across several worktrees; only a same-repository
    issue qualifies, because a worktree directory is named after the
    repository the checkout belongs to and an issue elsewhere would name a
    foreign checkout. The lowest-numbered one comes first so a freshly created
    worktree keeps a stable name regardless of the order GitHub reports
    closing issues in, but a checkout already opened under the pull request's
    own number, or under any other closing issue, keeps resolving to it too.
    """

    if item.kind is WorkItemKind.AUTHORED_PR and item.closing_issue_numbers:
        lowest = min(item.closing_issue_numbers)
        others = tuple(number for number in item.closing_issue_numbers if number != lowest)
        return (lowest, item.number, *others)
    return (item.number,)


# One-character CI symbols, keyed by the GraphQL status check rollup states.
_CI_SYMBOLS = {
    "SUCCESS": ("\u2713", "green"),
    "FAILURE": ("\u2717", "red"),
    "ERROR": ("\u2717", "red"),
    "PENDING": ("\u25cf", "yellow"),
    "EXPECTED": ("\u25cf", "yellow"),
}


def ci_status_symbol(ci_state: str | None, review_decision: str | None) -> tuple[str, str | None]:
    """Return the (symbol, color) pair representing a CI/review state pair."""

    if ci_state == "SUCCESS" and review_decision == "APPROVED":
        return "\u2713\u2713", "green"
    return _CI_SYMBOLS.get(ci_state or "", (" ", None))


def display_repo(item: WorkItem) -> str:
    """Return the repository column value for ``item``.

    A targeted todo is work on its target even though the issue itself lives
    in the todo repository, so the target is what the user should see.
    """

    return item.todo_target or item.repo
