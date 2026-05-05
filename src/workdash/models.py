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
