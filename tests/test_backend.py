import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import workdash.backend as backend_module
import workdash.github_client as github_client_module
from workdash.backend import (
    _MAX_WORK_ITEMS,
    IncludeResult,
    WorkdashBackend,
    compute_suggestion_markers,
)
from workdash.config import AgentConfig, WorkdashConfig, WorkdashConfigValidationError
from workdash.github_client import GitHubClient, TransientFetchError
from workdash.included_items import IncludedItemsStore
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def make_work_item(
    *,
    item_type: WorkItemType,
    kind: WorkItemKind,
    number: int,
    created_at: datetime,
) -> WorkItem:
    return WorkItem(
        kind=kind,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title=f"Item {number}",
        created_at=created_at,
        updated_at=created_at,
        url=f"https://example.com/{number}",
    )


def test_load_items_parses_selectors_fetches_merges_and_applies_cached_analyses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve_repositories(selectors):
        captured["repositories_selectors"] = selectors
        return ["owner/repo", "owner/other-repo"]

    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            captured["authored_called"] = True
            return [
                {
                    "id": "PR-1",
                    "repo": "owner/repo",
                    "number": 10,
                    "title": "Authored PR",
                    "url": "https://example.com/pull/10",
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-01T01:00:00Z",
                    "is_draft": False,
                    "ci_state": None,
                    "review_decision": None,
                }
            ]

        def list_open_review_requested_prs(self, login, progress_callback=None):
            captured["review_requested_called"] = True
            captured["review_requested_progress_callback"] = progress_callback is not None
            return [
                {
                    "id": "REVIEW-DUPLICATE-PR",
                    "repo": "owner/repo",
                    "number": 10,
                    "title": "Duplicate review requested PR row",
                    "url": "https://example.com/pull/10",
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-01T01:00:00Z",
                    "is_draft": False,
                },
                {
                    "id": "REVIEW-2",
                    "repo": "owner/repo",
                    "number": 12,
                    "title": "Review requested PR",
                    "url": "https://example.com/pull/12",
                    "created_at": "2026-02-03T00:00:00Z",
                    "updated_at": "2026-02-03T01:00:00Z",
                    "is_draft": False,
                },
            ]

        def list_open_reviewed_prs(self, login):
            captured["reviewed_called"] = True
            return [
                {
                    "id": "REVIEWED-DUPLICATE-PR",
                    "repo": "owner/repo",
                    "number": 12,
                    "title": "Duplicate reviewed PR row",
                    "url": "https://example.com/pull/12",
                    "created_at": "2026-02-03T00:00:00Z",
                    "updated_at": "2026-02-03T01:00:00Z",
                    "is_draft": False,
                },
                {
                    "id": "REVIEWED-3",
                    "repo": "owner/repo",
                    "number": 13,
                    "title": "Open reviewed PR",
                    "url": "https://example.com/pull/13",
                    "created_at": "2026-02-04T00:00:00Z",
                    "updated_at": "2026-02-04T01:00:00Z",
                    "is_draft": False,
                },
            ]

        def list_open_assigned_issues(self, login):
            captured["assigned_called"] = True
            return [
                {
                    "id": "ASSIGNED-ISSUE-1",
                    "repo": "owner/repo",
                    "number": 11,
                    "title": "Assigned issue",
                    "url": "https://example.com/issues/11",
                    "created_at": "2026-02-02T00:00:00Z",
                    "updated_at": "2026-02-02T01:00:00Z",
                }
            ]

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            captured["tracked_repositories"] = repositories
            captured["tracked_progress_callback"] = progress_callback is not None
            return [
                {
                    "id": "TRACKED-DUPLICATE-PR",
                    "repo": "owner/repo",
                    "number": 10,
                    "title": "Duplicate tracked PR row",
                    "url": "https://example.com/pull/10",
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-01T01:00:00Z",
                    "is_pull_request": True,
                },
                {
                    "id": "TRACKED-DUPLICATE-REVIEWED-PR",
                    "repo": "owner/repo",
                    "number": 12,
                    "title": "Duplicate tracked reviewed PR row",
                    "url": "https://example.com/pull/12",
                    "created_at": "2026-02-03T00:00:00Z",
                    "updated_at": "2026-02-03T01:00:00Z",
                    "is_pull_request": True,
                },
                {
                    "id": "TRACKED-ISSUE-2",
                    "repo": "owner/repo",
                    "number": 11,
                    "title": "Tracked issue",
                    "url": "https://example.com/issues/11",
                    "created_at": "2026-02-02T00:00:00Z",
                    "updated_at": "2026-02-02T01:00:00Z",
                    "is_pull_request": False,
                },
            ]

        def list_open_todo_issues(self, todo_repository, progress_callback=None):
            captured["todo_repository"] = todo_repository
            return []

        def fetch_linked_issues(self, pull_requests, progress_callback=None):
            return {}

    class FakeAnalysisCache:
        def is_valid(self, item: WorkItem) -> bool:
            return item.number == 11

        def load_analysis(self, item: WorkItem) -> str | None:
            if item.number == 11:
                return "cached analysis"
            return None

        def load_analysis_date(self, item: WorkItem) -> datetime | None:
            return None

    class FakeAnalyzer:
        def analyze(
            self, item: WorkItem, command_tokens: list[str] | None = None
        ) -> str | None:  # pragma: no cover - unused here
            raise AssertionError("analyze should not be called while loading items")

    monkeypatch.setattr(backend_module, "resolve_repositories", fake_resolve_repositories)
    config = WorkdashConfig(repositories=("owner/*",))
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        analysis_cache=FakeAnalysisCache(),
        analyzer=FakeAnalyzer(),
        config=config,
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    work_items, suggestion_markers = backend.load_items(progress_callback=lambda _: None)

    assert captured["repositories_selectors"] == ["owner/*"]
    assert captured["authored_called"] is True
    assert captured["review_requested_called"] is True
    assert captured["review_requested_progress_callback"] is True
    assert captured["reviewed_called"] is True
    assert captured["assigned_called"] is True
    assert captured["tracked_repositories"] == ["owner/repo", "owner/other-repo"]
    assert captured["tracked_progress_callback"] is True
    assert [(item.kind, item.number) for item in work_items] == [
        (WorkItemKind.AUTHORED_PR, 10),
        (WorkItemKind.REVIEW_REQUESTED_PR, 12),
        (WorkItemKind.REVIEW_REQUESTED_PR, 13),
        (WorkItemKind.ASSIGNED_ISSUE, 11),
    ]
    assert work_items[0].analysis is None
    assert work_items[1].analysis is None
    assert work_items[2].analysis is None
    assert work_items[3].analysis == "cached analysis"
    assert work_items[3].analyzed_at is None
    assert suggestion_markers == {(WorkItemType.PR, "owner/repo", 10): "*"}


def test_load_items_keeps_only_the_most_recently_updated_items(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # One more tracked issue than the dashboard shows, oldest last, so the cap
    # has to drop the oldest one rather than the tail of the fetch order.
    overflow = _MAX_WORK_ITEMS + 1
    tracked_payload = [
        {
            "id": f"ISSUE-{number}",
            "repo": "owner/repo",
            "number": number,
            "title": f"Tracked {number}",
            "url": f"https://example.com/{number}",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": (datetime(2026, 2, 1, tzinfo=UTC) + timedelta(minutes=number)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "is_pull_request": False,
            "state": "OPEN",
        }
        for number in range(overflow)
    ]

    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            return []

        def list_open_review_requested_prs(self, login, progress_callback=None):
            return []

        def list_open_reviewed_prs(self, login):
            return []

        def list_open_assigned_issues(self, login):
            return []

        def list_open_todo_issues(self, repository, progress_callback=None):
            return []

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            return tracked_payload

    monkeypatch.setattr(backend_module, "resolve_repositories", lambda selectors: ["owner/repo"])
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        config=WorkdashConfig(repositories=("owner/repo",)),
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    work_items, _markers = backend.load_items(progress_callback=lambda _: None)

    assert len(work_items) == _MAX_WORK_ITEMS
    # Most recently updated first, and the single oldest item is the one dropped.
    assert [item.number for item in work_items] == list(range(overflow - 1, 0, -1))


def test_load_items_keeps_a_hand_picked_item_older_than_every_discovered_item(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The user asked for these two by hand, so a full dashboard must not hide
    # them even though they are older than everything the selectors found.
    stale_timestamp = "2020-01-01T00:00:00Z"
    todo_payload = [
        {
            "id": "ISSUE-777",
            "repo": "testuser/todos",
            "number": 777,
            "title": "An ancient todo",
            "url": "https://github.com/testuser/todos/issues/777",
            "created_at": stale_timestamp,
            "updated_at": stale_timestamp,
            "target": None,
        }
    ]
    tracked_payload = [
        {
            "id": f"ISSUE-{number}",
            "repo": "owner/repo",
            "number": number,
            "title": f"Tracked {number}",
            "url": f"https://example.com/{number}",
            "created_at": "2026-02-01T00:00:00Z",
            "updated_at": (datetime(2026, 2, 1, tzinfo=UTC) + timedelta(minutes=number)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "is_pull_request": False,
            "state": "OPEN",
        }
        for number in range(_MAX_WORK_ITEMS + 10)
    ]
    included_item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=888,
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    included_item.included = True

    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            return []

        def list_open_review_requested_prs(self, login, progress_callback=None):
            return []

        def list_open_reviewed_prs(self, login):
            return []

        def list_open_assigned_issues(self, login):
            return []

        def list_open_todo_issues(self, repository, progress_callback=None):
            return todo_payload

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            return tracked_payload

    monkeypatch.setattr(backend_module, "resolve_repositories", lambda selectors: ["owner/repo"])
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        config=WorkdashConfig(repositories=("owner/repo",), todo_repository="testuser/todos"),
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )
    monkeypatch.setattr(backend, "_load_included_items", lambda report_progress: [included_item])

    work_items, _markers = backend.load_items(progress_callback=lambda _: None)

    numbers = [item.number for item in work_items]
    assert 777 in numbers, "the todo item was dropped by the row cap"
    assert 888 in numbers, "the included item was dropped by the row cap"


def test_load_items_keeps_the_todo_target_when_the_same_issue_is_also_assigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    issue_payload = {
        "id": "ISSUE-110",
        "repo": "testuser/todos",
        "number": 110,
        "title": "Fix the flaky test",
        "url": "https://github.com/testuser/todos/issues/110",
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-01T01:00:00Z",
    }

    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            return []

        def list_open_review_requested_prs(self, login, progress_callback=None):
            return []

        def list_open_reviewed_prs(self, login):
            return []

        def list_open_assigned_issues(self, login):
            return [dict(issue_payload)]

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            return []

        def list_open_todo_issues(self, todo_repository, progress_callback=None):
            return [dict(issue_payload, target="owner/repo")]

    monkeypatch.setattr(backend_module, "resolve_repositories", lambda selectors: [])
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        analysis_cache=MagicMock(),
        config=WorkdashConfig(repositories=("owner/repo",), todo_repository="testuser/todos"),
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    work_items, _markers = backend.load_items(progress_callback=lambda _: None)

    assert [(item.repo, item.number, item.todo_target) for item in work_items] == [
        ("testuser/todos", 110, "owner/repo")
    ]


def test_load_items_survives_a_todo_repository_that_does_not_exist_yet(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fresh user has no todo repository yet, so the dashboard must still load."""

    assigned_issue = {
        "id": "ISSUE-11",
        "repo": "owner/repo",
        "number": 11,
        "title": "Broken renderer",
        "url": "https://github.com/owner/repo/issues/11",
        "created_at": "2026-02-01T00:00:00Z",
        "updated_at": "2026-02-01T01:00:00Z",
    }
    monkeypatch.setattr(
        GitHubClient, "list_open_authored_prs", lambda self, login, progress_callback=None: []
    )
    monkeypatch.setattr(
        GitHubClient,
        "list_open_review_requested_prs",
        lambda self, login, progress_callback=None: [],
    )
    monkeypatch.setattr(GitHubClient, "list_open_reviewed_prs", lambda self, login: [])
    monkeypatch.setattr(
        GitHubClient, "list_open_assigned_issues", lambda self, login: [assigned_issue]
    )
    monkeypatch.setattr(
        GitHubClient,
        "list_recent_tracked_items",
        lambda self, repositories, progress_callback=None: [],
    )

    def fake_run(command, **kwargs):
        assert command[:3] == ["gh", "issue", "list"], command
        raise subprocess.CalledProcessError(
            1,
            command,
            stderr="GraphQL: Could not resolve to a Repository with the name 'testuser/todos'.",
        )

    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)
    monkeypatch.setattr(backend_module, "resolve_repositories", lambda selectors: [])
    backend = WorkdashBackend(
        github_client=GitHubClient(),
        analysis_cache=MagicMock(),
        config=WorkdashConfig(repositories=("owner/repo",), todo_repository="testuser/todos"),
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    work_items, _markers = backend.load_items(progress_callback=lambda _: None)

    assert [(item.repo, item.number) for item in work_items] == [("owner/repo", 11)]


def test_load_items_submits_independent_github_fetches_before_waiting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[tuple[str, str]] = []

    class FakeFuture:
        def __init__(self, name, callback, args, kwargs) -> None:
            self.name = name
            self.callback = callback
            self.args = args
            self.kwargs = kwargs

        def result(self):
            events.append(("result", self.name))
            return self.callback(*self.args, **self.kwargs)

    class FakeThreadPoolExecutor:
        def __init__(self, max_workers) -> None:
            assert max_workers == 6

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback) -> None:
            pass

        def submit(self, callback, *args, **kwargs):
            events.append(("submit", callback.__name__))
            return FakeFuture(callback.__name__, callback, args, kwargs)

    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            return []

        def list_open_review_requested_prs(self, login, progress_callback=None):
            return []

        def list_open_reviewed_prs(self, login):
            return []

        def list_open_assigned_issues(self, login):
            return []

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            return []

        def list_open_todo_issues(self, todo_repository, progress_callback=None):
            return []

    monkeypatch.setattr(backend_module, "ThreadPoolExecutor", FakeThreadPoolExecutor)
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        analysis_cache=MagicMock(),
        config=WorkdashConfig(repositories=("owner/repo",)),
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    work_items, suggestion_markers = backend.load_items(progress_callback=lambda _: None)

    expected_fetches = [
        "list_open_authored_prs",
        "list_open_review_requested_prs",
        "list_open_reviewed_prs",
        "list_open_assigned_issues",
        "list_recent_tracked_items",
        "list_open_todo_issues",
    ]
    assert work_items == []
    assert suggestion_markers == {}
    assert events[:6] == [("submit", name) for name in expected_fetches]
    assert events[6] == ("result", "list_open_authored_prs")


@pytest.mark.parametrize(
    ("closing_issues", "expected_visible", "expected_linked_issue"),
    [
        # A closing issue in another repository cannot name this pull request's
        # worktree, so the lowest-numbered issue in its own repository wins.
        (
            [("other/repo", 12), ("owner/repo", 41999), ("owner/repo", 41830)],
            {("owner/repo", 42149), ("owner/repo", 500)},
            ("owner/repo", 41830),
        ),
        # With nothing to redirect to the pull request keeps its own number, but
        # the foreign issue is still work the pull request covers.
        (
            [("other/repo", 12)],
            {("owner/repo", 42149), ("owner/repo", 41830), ("owner/repo", 500)},
            None,
        ),
    ],
)
def test_load_items_hides_every_issue_a_pull_request_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    closing_issues: list[tuple[str, int]],
    expected_visible: set[tuple[str, int]],
    expected_linked_issue: tuple[str, int] | None,
) -> None:
    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            return [
                {
                    "id": "PR-42149",
                    "repo": "owner/repo",
                    "number": 42149,
                    "title": "Implement the tracked issue",
                    "url": "https://example.com/pull/42149",
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-05T01:00:00Z",
                    "is_draft": False,
                    "ci_state": None,
                    "review_decision": None,
                }
            ]

        def list_open_review_requested_prs(self, login, progress_callback=None):
            return []

        def list_open_reviewed_prs(self, login):
            return []

        def list_open_assigned_issues(self, login):
            return [
                {
                    "id": "ISSUE-41830",
                    "repo": "owner/repo",
                    "number": 41830,
                    "title": "The issue the pull request closes",
                    "url": "https://example.com/issues/41830",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-02-04T01:00:00Z",
                },
                {
                    "id": "ISSUE-500",
                    "repo": "owner/repo",
                    "number": 500,
                    "title": "Unrelated assigned issue",
                    "url": "https://example.com/issues/500",
                    "created_at": "2026-01-02T00:00:00Z",
                    "updated_at": "2026-02-03T01:00:00Z",
                },
            ]

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            return [
                {
                    "id": "ISSUE-12",
                    "repo": "other/repo",
                    "number": 12,
                    "title": "Planning issue in another repository",
                    "url": "https://example.com/other/issues/12",
                    "created_at": "2026-01-03T00:00:00Z",
                    "updated_at": "2026-02-02T01:00:00Z",
                    "is_pull_request": False,
                }
            ]

        def list_open_todo_issues(self, todo_repository, progress_callback=None):
            return []

        def fetch_linked_issues(self, pull_requests, progress_callback=None):
            assert pull_requests == [("owner/repo", 42149)]
            return {("owner/repo", 42149): closing_issues}

    monkeypatch.setattr(backend_module, "resolve_repositories", lambda selectors: ["owner/repo"])
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        config=WorkdashConfig(repositories=("owner/repo",)),
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    work_items, _markers = backend.load_items(progress_callback=lambda _: None)

    assert {(item.repo, item.number) for item in work_items} == expected_visible
    pull_request = next(item for item in work_items if item.item_type == WorkItemType.PR)
    assert pull_request.linked_issue == expected_linked_issue


def test_load_items_hides_an_included_issue_that_a_listed_pull_request_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Including the issue by URL brings it back for the session, but a refresh
    # must drop it again because the pull request already covers that work.
    included_issue = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=41830,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    included_issue.included = True

    class FakeGitHubClient:
        def list_open_authored_prs(self, login, progress_callback=None):
            return [
                {
                    "id": "PR-42149",
                    "repo": "owner/repo",
                    "number": 42149,
                    "title": "Implement the tracked issue",
                    "url": "https://example.com/pull/42149",
                    "created_at": "2026-02-01T00:00:00Z",
                    "updated_at": "2026-02-05T01:00:00Z",
                    "is_draft": False,
                    "ci_state": None,
                    "review_decision": None,
                }
            ]

        def list_open_review_requested_prs(self, login, progress_callback=None):
            return []

        def list_open_reviewed_prs(self, login):
            return []

        def list_open_assigned_issues(self, login):
            return []

        def list_recent_tracked_items(self, repositories, progress_callback=None):
            return []

        def list_open_todo_issues(self, todo_repository, progress_callback=None):
            return []

        def fetch_item_by_url(self, parsed_url, github_username):
            return included_issue

        def fetch_linked_issues(self, pull_requests, progress_callback=None):
            return {("owner/repo", 42149): [("owner/repo", 41830)]}

    monkeypatch.setattr(backend_module, "resolve_repositories", lambda selectors: ["owner/repo"])
    store = IncludedItemsStore(tmp_path / "included.json")
    store.save(["https://github.com/owner/repo/issues/41830"])
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        config=WorkdashConfig(repositories=("owner/repo",)),
        cache_root=tmp_path / "cache",
        included_items_store=store,
    )

    work_items, _markers = backend.load_items(progress_callback=lambda _: None)

    assert [(item.item_type, item.number) for item in work_items] == [(WorkItemType.PR, 42149)]
    # The URL stays in the store, so the issue is still there to be re-included.
    assert store.load() == ["https://github.com/owner/repo/issues/41830"]


def test_compute_suggestion_markers_prefers_pr_when_age_is_tied() -> None:
    created_at = datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC)
    issue = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=20,
        created_at=created_at,
    )
    pull_request = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.TRACKED_PR,
        number=21,
        created_at=created_at,
    )

    assert compute_suggestion_markers([issue, pull_request]) == {
        (WorkItemType.PR, "owner/repo", 21): "*"
    }


def test_compute_suggestion_markers_returns_empty_for_no_work_items() -> None:
    assert compute_suggestion_markers([]) == {}


def test_analyze_item_uses_cache_then_falls_back_to_analyzer_and_saves() -> None:
    class FakeGitHubClient:
        def list_open_authored_prs(
            self, login, progress_callback=None
        ):  # pragma: no cover - unused here
            raise AssertionError("list_open_authored_prs should not be called")

        def list_open_review_requested_prs(self, login):  # pragma: no cover - unused here
            raise AssertionError("list_open_review_requested_prs should not be called")

        def list_open_reviewed_prs(self, login):  # pragma: no cover - unused here
            raise AssertionError("list_open_reviewed_prs should not be called")

        def list_open_assigned_issues(self, login):  # pragma: no cover - unused here
            raise AssertionError("list_open_assigned_issues should not be called")

        def list_recent_tracked_items(
            self, repositories, progress_callback=None
        ):  # pragma: no cover - unused here
            raise AssertionError("list_recent_tracked_items should not be called")

    class FakeAnalysisCache:
        def __init__(self) -> None:
            self.saved: list[tuple[int, str | None]] = []

        def is_valid(self, item: WorkItem) -> bool:
            return item.number == 100

        def load_analysis(self, item: WorkItem) -> str | None:
            if item.number == 100:
                return "cached analysis"
            return None

        def load_analysis_date(self, item: WorkItem) -> datetime | None:
            return None

        def build_analysis_path(self, item: WorkItem) -> Path:
            return Path(f"/tmp/analyses/owner_repo_PR{item.number}.md")

        def save(self, item: WorkItem, analysis: str | None = None) -> None:
            self.saved.append((item.number, analysis))

    class FakeAnalyzer:
        def __init__(self) -> None:
            self.analyze_calls: list[tuple[int, list[str] | None]] = []

        def analyze(self, item: WorkItem, command_tokens: list[str] | None = None) -> str | None:
            self.analyze_calls.append((item.number, command_tokens))
            return f"## Summary\nanalysis {item.number}"

    cache = FakeAnalysisCache()
    analyzer = FakeAnalyzer()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/*",),
        workdir="~/src",
    )
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        analysis_cache=cache,
        analyzer=analyzer,
        config=config,
    )
    cached_item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        number=100,
        created_at=datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC),
    )
    uncached_item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=101,
        created_at=datetime(2026, 2, 2, 0, 0, 0, tzinfo=UTC),
    )

    cached_result = backend.analyze_item(cached_item, tool="cached")
    generated_result = backend.analyze_item(uncached_item, tool="codex")

    assert cached_result == "/tmp/analyses/owner_repo_PR100.md"
    assert generated_result == "/tmp/analyses/owner_repo_PR101.md"
    assert uncached_item.analysis == "## Summary\nanalysis 101"
    assert cache.saved == [(101, "## Summary\nanalysis 101")]
    assert analyzer.analyze_calls == [(101, ["codex", "exec"])]


def test_analyze_item_tool_claude_bypasses_cache() -> None:
    class FakeGitHubClient:
        def list_open_authored_prs(
            self, login, progress_callback=None
        ):  # pragma: no cover - unused here
            raise AssertionError("list_open_authored_prs should not be called")

        def list_open_review_requested_prs(self, login):  # pragma: no cover - unused here
            raise AssertionError("list_open_review_requested_prs should not be called")

        def list_open_reviewed_prs(self, login):  # pragma: no cover - unused here
            raise AssertionError("list_open_reviewed_prs should not be called")

        def list_open_assigned_issues(self, login):  # pragma: no cover - unused here
            raise AssertionError("list_open_assigned_issues should not be called")

        def list_recent_tracked_items(
            self, repositories, progress_callback=None
        ):  # pragma: no cover - unused here
            raise AssertionError("list_recent_tracked_items should not be called")

    class FakeAnalysisCache:
        def __init__(self) -> None:
            self.saved: list[tuple[int, str | None]] = []

        def is_valid(self, item: WorkItem) -> bool:
            return True

        def load_analysis(self, item: WorkItem) -> str | None:
            return "cached analysis"

        def load_analysis_date(self, item: WorkItem) -> datetime | None:
            return None

        def build_analysis_path(self, item: WorkItem) -> Path:
            return Path(f"/tmp/analyses/owner_repo_PR{item.number}.md")

        def save(self, item: WorkItem, analysis: str | None = None) -> None:
            self.saved.append((item.number, analysis))

    class FakeAnalyzer:
        def __init__(self) -> None:
            self.analyze_calls: list[tuple[int, list[str] | None]] = []

        def analyze(self, item: WorkItem, command_tokens: list[str] | None = None) -> str | None:
            self.analyze_calls.append((item.number, command_tokens))
            return f"## Summary\nfresh analysis {item.number}"

    cache = FakeAnalysisCache()
    analyzer = FakeAnalyzer()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/*",),
        workdir="~/src",
    )
    backend = WorkdashBackend(
        github_client=FakeGitHubClient(),
        analysis_cache=cache,
        analyzer=analyzer,
        config=config,
    )
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        number=55,
        created_at=datetime(2026, 2, 3, 0, 0, 0, tzinfo=UTC),
    )

    analysis_path = backend.analyze_item(item, tool="claude")

    assert analysis_path == "/tmp/analyses/owner_repo_PR55.md"
    assert cache.saved == [(55, "## Summary\nfresh analysis 55")]
    assert analyzer.analyze_calls == [(55, ["claude", "-p"])]


def test_analyze_item_uses_config_boundary_for_malformed_agent_command() -> None:
    analyzer = MagicMock()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex 'broken", launch="codex"),
        repositories=("owner/*",),
        workdir="~/src",
    )
    backend = WorkdashBackend(
        github_client=MagicMock(),
        analyzer=analyzer,
        config=config,
    )
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=101,
        created_at=datetime(2026, 2, 2, 0, 0, 0, tzinfo=UTC),
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        backend.analyze_item(item, tool="codex")

    assert error.value.invalid_fields == ("agents.codex.analyze: No closing quotation",)
    analyzer.analyze.assert_not_called()


def test_analyze_item_uses_config_boundary_for_non_string_agent_command() -> None:
    analyzer = MagicMock()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze=["codex", "exec"], launch="codex"),
        repositories=("owner/*",),
        workdir="~/src",
    )
    backend = WorkdashBackend(
        github_client=MagicMock(),
        analyzer=analyzer,
        config=config,
    )
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=101,
        created_at=datetime(2026, 2, 2, 0, 0, 0, tzinfo=UTC),
    )

    with pytest.raises(WorkdashConfigValidationError) as error:
        backend.analyze_item(item, tool="codex")

    assert error.value.invalid_fields == ("agents.codex.analyze: expected a non-empty string",)
    analyzer.analyze.assert_not_called()


def test_analyze_item_rejects_unsupported_agent_without_codex_fallback() -> None:
    analyzer = MagicMock()
    config = WorkdashConfig(
        github_username="testuser",
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        repositories=("owner/*",),
        workdir="~/src",
    )
    backend = WorkdashBackend(
        github_client=MagicMock(),
        analyzer=analyzer,
        config=config,
    )
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.TRACKED_ISSUE,
        number=101,
        created_at=datetime(2026, 2, 2, 0, 0, 0, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="Unsupported analyze agent: 'pi'"):
        backend.analyze_item(item, tool="pi")

    analyzer.analyze.assert_not_called()


# ---------------------------------------------------------------------------
# include entry points
# ---------------------------------------------------------------------------


def _make_include_backend(
    tmp_path: Path, github_client: GitHubClient
) -> tuple[WorkdashBackend, IncludedItemsStore]:
    store = IncludedItemsStore(tmp_path / "included.json")
    backend = WorkdashBackend(
        github_client=github_client,
        included_items_store=store,
        cache_root=tmp_path / "cache",
    )
    return backend, store


def _make_fetched_item(number: int = 1) -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=number,
        title="fetched",
        created_at=datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 4, 2, 0, 0, 0, tzinfo=UTC),
        url=f"https://github.com/owner/repo/pull/{number}",
        included=True,
    )


def test_include_item_by_url_persists_canonical_url_on_success(tmp_path: Path) -> None:
    # URL with noisy path/query/fragment must collapse to the canonical form
    # when saved so subsequent refreshes match by identity.
    github_client = MagicMock(spec=GitHubClient)
    github_client.fetch_item_by_url.return_value = _make_fetched_item(1)
    backend, store = _make_include_backend(tmp_path, github_client)

    result = backend.include_item_by_url(
        "https://github.com/owner/repo/pull/1/files?diff=1#diff-abc",
        existing_identities=set(),
    )

    assert result.fetched_item is not None
    assert store.load() == ["https://github.com/owner/repo/pull/1"]


def test_include_item_by_url_marks_invalid_and_does_not_persist_for_invalid_url(
    tmp_path: Path,
) -> None:
    github_client = MagicMock(spec=GitHubClient)
    backend, store = _make_include_backend(tmp_path, github_client)

    result = backend.include_item_by_url(
        "https://example.com/not-github", existing_identities=set()
    )

    assert result == IncludeResult(invalid=True)
    github_client.fetch_item_by_url.assert_not_called()
    assert store.load() == []


def test_include_item_by_url_reports_transient_without_persisting(tmp_path: Path) -> None:
    github_client = MagicMock(spec=GitHubClient)
    github_client.fetch_item_by_url.side_effect = TransientFetchError("HTTP 503")
    backend, store = _make_include_backend(tmp_path, github_client)

    result = backend.include_item_by_url(
        "https://github.com/owner/repo/pull/1", existing_identities=set()
    )

    assert result == IncludeResult(transient_failure=True)
    assert store.load() == []


def test_include_item_by_url_duplicate_identity_persists_canonical_without_fetch(
    tmp_path: Path,
) -> None:
    # An identity already visible on-screen must short-circuit the fetch, persist
    # the canonical URL idempotently, and surface ``duplicate_identity`` so the
    # TUI can move the cursor without mutating its item list.
    github_client = MagicMock(spec=GitHubClient)
    backend, store = _make_include_backend(tmp_path, github_client)
    identity = (WorkItemType.PR, "owner/repo", 1)

    first = backend.include_item_by_url(
        "https://github.com/owner/repo/pull/1/files",
        existing_identities={identity},
    )
    second = backend.include_item_by_url(
        "https://github.com/owner/repo/pull/1?diff=1",
        existing_identities={identity},
    )

    assert first == IncludeResult(duplicate_identity=identity)
    assert second == IncludeResult(duplicate_identity=identity)
    github_client.fetch_item_by_url.assert_not_called()
    assert store.load() == ["https://github.com/owner/repo/pull/1"]
