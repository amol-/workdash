from datetime import UTC, datetime, timedelta

import pytest

from workdash.github_client import (
    merge_normalized_work_items,
    normalize_authored_pull_request,
    normalize_authored_pull_requests,
    normalize_recent_tracked_item,
    normalize_recent_tracked_items,
    normalize_review_requested_pull_request,
    normalize_review_requested_pull_requests,
    parse_github_datetime,
)
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def _make_item(
    *,
    kind: WorkItemKind,
    item_type: WorkItemType,
    number: int,
    included: bool,
) -> WorkItem:
    return WorkItem(
        kind=kind,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title=f"Item {number}",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url=f"https://github.com/owner/repo/pull/{number}",
        included=included,
    )


def test_normalize_authored_pull_request_maps_to_work_item() -> None:
    item = normalize_authored_pull_request(
        {
            "id": "PR-1",
            "repo": "owner/repo",
            "number": 10,
            "title": "Improve docs",
            "url": "https://example.com/pull/10",
            "created_at": "2026-02-20T12:00:00Z",
            "updated_at": "2026-02-21T12:00:00Z",
            "is_draft": True,
            "ci_state": "SUCCESS",
        }
    )

    assert item.kind is WorkItemKind.AUTHORED_PR
    assert item.item_type is WorkItemType.PR
    assert item.repo == "owner/repo"
    assert item.number == 10
    assert item.title == "Improve docs"
    assert item.url == "https://example.com/pull/10"
    assert item.created_at == datetime(2026, 2, 20, 12, 0, 0, tzinfo=UTC)
    assert item.updated_at == datetime(2026, 2, 21, 12, 0, 0, tzinfo=UTC)
    assert item.ci_state == "SUCCESS"


def test_normalize_recent_tracked_item_maps_issue_and_pr() -> None:
    issue = normalize_recent_tracked_item(
        {
            "id": "ISSUE-1",
            "repo": "owner/repo",
            "number": 11,
            "title": "Issue",
            "url": "https://example.com/issues/11",
            "created_at": "2026-02-18T09:00:00Z",
            "updated_at": "2026-02-19T09:00:00Z",
            "is_pull_request": False,
        }
    )
    pull_request = normalize_recent_tracked_item(
        {
            "id": "PR-2",
            "repo": "owner/repo",
            "number": 12,
            "title": "Tracked PR",
            "url": "https://example.com/pull/12",
            "created_at": "2026-02-18T10:00:00Z",
            "updated_at": "2026-02-19T10:00:00Z",
            "is_pull_request": True,
        }
    )

    assert issue.kind is WorkItemKind.TRACKED_ISSUE
    assert issue.item_type is WorkItemType.ISSUE
    assert pull_request.kind is WorkItemKind.TRACKED_PR
    assert pull_request.item_type is WorkItemType.PR


def test_normalize_review_requested_pull_request_maps_to_work_item() -> None:
    item = normalize_review_requested_pull_request(
        {
            "id": "PR-2",
            "repo": "owner/repo",
            "number": 12,
            "title": "Needs review",
            "url": "https://example.com/pull/12",
            "created_at": "2026-02-18T10:00:00Z",
            "updated_at": "2026-02-19T10:00:00Z",
            "is_draft": False,
        }
    )

    assert item.kind is WorkItemKind.REVIEW_REQUESTED_PR
    assert item.item_type is WorkItemType.PR


def test_parse_github_datetime_returns_timezone_aware_value() -> None:
    parsed = parse_github_datetime("2026-02-20T12:34:56+05:30")

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_github_datetime_rejects_timestamp_without_timezone() -> None:
    with pytest.raises(RuntimeError, match="timezone offset is required"):
        parse_github_datetime("2026-02-20T12:34:56")


def test_normalize_list_helpers_convert_each_record() -> None:
    authored = normalize_authored_pull_requests(
        [
            {
                "id": "A",
                "repo": "owner/repo",
                "number": 1,
                "title": "Authored PR",
                "url": "https://example.com/pull/1",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_draft": False,
                "ci_state": None,
            }
        ]
    )
    tracked = normalize_recent_tracked_items(
        [
            {
                "id": "B",
                "repo": "owner/repo",
                "number": 2,
                "title": "Tracked Issue",
                "url": "https://example.com/issues/2",
                "created_at": "2026-02-21T00:00:00Z",
                "updated_at": "2026-02-21T01:00:00Z",
                "is_pull_request": False,
            }
        ]
    )
    review_requested = normalize_review_requested_pull_requests(
        [
            {
                "id": "C",
                "repo": "owner/repo",
                "number": 3,
                "title": "Review requested PR",
                "url": "https://example.com/pull/3",
                "created_at": "2026-02-22T00:00:00Z",
                "updated_at": "2026-02-22T01:00:00Z",
                "is_draft": False,
            }
        ]
    )

    assert [item.kind for item in authored] == [WorkItemKind.AUTHORED_PR]
    assert [item.kind for item in tracked] == [WorkItemKind.TRACKED_ISSUE]
    assert [item.kind for item in review_requested] == [WorkItemKind.REVIEW_REQUESTED_PR]


def test_merge_normalized_work_items_prefers_authored_pr_for_same_pr_identity() -> None:
    authored = normalize_authored_pull_requests(
        [
            {
                "id": "A",
                "repo": "owner/repo",
                "number": 101,
                "title": "Authored PR row",
                "url": "https://example.com/pull/101",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_draft": False,
                "ci_state": None,
            }
        ]
    )
    tracked = normalize_recent_tracked_items(
        [
            {
                "id": "B",
                "repo": "owner/repo",
                "number": 101,
                "title": "Tracked duplicate PR row",
                "url": "https://example.com/pull/101",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_pull_request": True,
            },
            {
                "id": "C",
                "repo": "owner/repo",
                "number": 102,
                "title": "Tracked issue row",
                "url": "https://example.com/issues/102",
                "created_at": "2026-02-21T00:00:00Z",
                "updated_at": "2026-02-21T01:00:00Z",
                "is_pull_request": False,
            },
        ]
    )

    merged = merge_normalized_work_items(authored, tracked)

    assert [item.kind for item in merged] == [
        WorkItemKind.AUTHORED_PR,
        WorkItemKind.TRACKED_ISSUE,
    ]
    assert [item.item_type for item in merged] == [WorkItemType.PR, WorkItemType.ISSUE]
    assert [(item.repo, item.number) for item in merged] == [
        ("owner/repo", 101),
        ("owner/repo", 102),
    ]


def test_merge_normalized_work_items_keeps_issue_and_pr_rows_independent() -> None:
    authored = normalize_authored_pull_requests(
        [
            {
                "id": "A",
                "repo": "owner/repo",
                "number": 77,
                "title": "Authored PR",
                "url": "https://example.com/pull/77",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_draft": False,
                "ci_state": None,
            }
        ]
    )
    tracked = normalize_recent_tracked_items(
        [
            {
                "id": "B",
                "repo": "owner/repo",
                "number": 77,
                "title": "Tracked issue sharing number",
                "url": "https://example.com/issues/77",
                "created_at": "2026-02-21T00:00:00Z",
                "updated_at": "2026-02-21T01:00:00Z",
                "is_pull_request": False,
            }
        ]
    )

    merged = merge_normalized_work_items(authored, tracked)

    assert [item.item_type for item in merged] == [WorkItemType.PR, WorkItemType.ISSUE]
    assert [item.kind for item in merged] == [
        WorkItemKind.AUTHORED_PR,
        WorkItemKind.TRACKED_ISSUE,
    ]


def test_merge_normalized_work_items_preserves_deterministic_stable_order() -> None:
    authored = normalize_authored_pull_requests(
        [
            {
                "id": "A",
                "repo": "owner/repo",
                "number": 10,
                "title": "First authored PR",
                "url": "https://example.com/pull/10",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_draft": False,
                "ci_state": None,
            },
            {
                "id": "B",
                "repo": "owner/repo",
                "number": 20,
                "title": "Second authored PR",
                "url": "https://example.com/pull/20",
                "created_at": "2026-02-21T00:00:00Z",
                "updated_at": "2026-02-21T01:00:00Z",
                "is_draft": False,
                "ci_state": None,
            },
        ]
    )
    tracked = normalize_recent_tracked_items(
        [
            {
                "id": "C",
                "repo": "owner/repo",
                "number": 20,
                "title": "Duplicate tracked PR",
                "url": "https://example.com/pull/20",
                "created_at": "2026-02-21T00:00:00Z",
                "updated_at": "2026-02-21T01:00:00Z",
                "is_pull_request": True,
            },
            {
                "id": "D",
                "repo": "owner/repo",
                "number": 30,
                "title": "Tracked issue",
                "url": "https://example.com/issues/30",
                "created_at": "2026-02-22T00:00:00Z",
                "updated_at": "2026-02-22T01:00:00Z",
                "is_pull_request": False,
            },
            {
                "id": "E",
                "repo": "owner/repo",
                "number": 40,
                "title": "Tracked PR",
                "url": "https://example.com/pull/40",
                "created_at": "2026-02-23T00:00:00Z",
                "updated_at": "2026-02-23T01:00:00Z",
                "is_pull_request": True,
            },
        ]
    )

    merged = merge_normalized_work_items(authored, tracked)

    assert [(item.kind, item.number) for item in merged] == [
        (WorkItemKind.AUTHORED_PR, 10),
        (WorkItemKind.AUTHORED_PR, 20),
        (WorkItemKind.TRACKED_ISSUE, 30),
        (WorkItemKind.TRACKED_PR, 40),
    ]


def test_merge_normalized_work_items_prefers_first_input_for_duplicate_pr_identity() -> None:
    authored = normalize_authored_pull_requests(
        [
            {
                "id": "A",
                "repo": "owner/repo",
                "number": 55,
                "title": "Authored PR",
                "url": "https://example.com/pull/55",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_draft": False,
                "ci_state": None,
            }
        ]
    )
    review_requested = normalize_review_requested_pull_requests(
        [
            {
                "id": "B",
                "repo": "owner/repo",
                "number": 55,
                "title": "Duplicate review requested PR",
                "url": "https://example.com/pull/55",
                "created_at": "2026-02-20T00:00:00Z",
                "updated_at": "2026-02-20T01:00:00Z",
                "is_draft": False,
            },
            {
                "id": "C",
                "repo": "owner/repo",
                "number": 56,
                "title": "Review requested PR",
                "url": "https://example.com/pull/56",
                "created_at": "2026-02-21T00:00:00Z",
                "updated_at": "2026-02-21T01:00:00Z",
                "is_draft": False,
            },
        ]
    )

    merged = merge_normalized_work_items(authored, review_requested)

    assert [(item.kind, item.number) for item in merged] == [
        (WorkItemKind.AUTHORED_PR, 55),
        (WorkItemKind.REVIEW_REQUESTED_PR, 56),
    ]


def test_normalize_assigned_issue_maps_to_work_item() -> None:
    from workdash.github_client import normalize_assigned_issue

    item = normalize_assigned_issue(
        {
            "id": "I1",
            "repo": "owner/repo",
            "number": 5,
            "title": "assigned issue",
            "url": "https://example.com/issues/5",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": "2026-02-02T00:00:00Z",
        }
    )

    assert item.kind == WorkItemKind.ASSIGNED_ISSUE
    assert item.item_type == WorkItemType.ISSUE
    assert item.repo == "owner/repo"
    assert item.number == 5


def test_merge_keeps_included_flag_when_primary_has_it() -> None:
    """Primary carrying ``included=True`` must not be demoted by a non-included secondary.

    Losing this property would cause an included item surfaced by a regular
    source (e.g. review-requested) to lose its "+" suffix on the next merge pass.
    """

    primary = _make_item(
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
        item_type=WorkItemType.PR,
        number=42,
        included=True,
    )
    secondary = _make_item(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        number=42,
        included=False,
    )

    merged = merge_normalized_work_items([primary], [secondary])

    assert len(merged) == 1
    assert merged[0].included is True
    assert merged[0].kind == WorkItemKind.REVIEW_REQUESTED_PR


def test_merge_keeps_included_flag_when_secondary_has_it() -> None:
    """Secondary carrying ``included=True`` must lift the flag onto the primary row.

    This is the ordering exercised by the production refresh path, where the
    included payload is merged onto existing authored/review rows.
    """

    primary = _make_item(
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
        item_type=WorkItemType.PR,
        number=42,
        included=False,
    )
    secondary = _make_item(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        number=42,
        included=True,
    )

    merged = merge_normalized_work_items([primary], [secondary])

    assert len(merged) == 1
    assert merged[0].included is True
    assert merged[0].kind == WorkItemKind.REVIEW_REQUESTED_PR
