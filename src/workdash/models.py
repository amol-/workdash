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
    linked_issue: tuple[str, int] | None = None


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
    """Return the item number naming ``item``'s worktree directory.

    A pull request the user authored is the implementation of the issue it
    closes, so both share one checkout instead of splitting the same work
    across two worktrees. Only an issue in the pull request's own repository
    qualifies, because a worktree directory is named after the repository the
    checkout belongs to and an issue elsewhere would name a foreign checkout.
    """

    if (
        item.kind is WorkItemKind.AUTHORED_PR
        and item.linked_issue is not None
        and item.linked_issue[0] == item.repo
    ):
        return item.linked_issue[1]
    return item.number


def accepted_worktree_numbers(item: WorkItem) -> set[int]:
    """Return every item number a worktree of ``item`` may be named after.

    New worktrees are created under :func:`worktree_item_number`, but a pull
    request whose checkout was opened before it linked to an issue still lives
    under its own number, and that directory must keep resolving to it.
    """

    return {item.number, worktree_item_number(item)}


def display_repo(item: WorkItem) -> str:
    """Return the repository column value for ``item``.

    A targeted todo is work on its target even though the issue itself lives
    in the todo repository, so the target is what the user should see.
    """

    return item.todo_target or item.repo
