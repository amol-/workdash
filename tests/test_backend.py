from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import workdash.backend as backend_module
from workdash.backend import IncludeResult, WorkdashBackend, compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_resolve_repositories(selectors):
        captured["repositories_selectors"] = selectors
        return ["owner/repo", "owner/other-repo"]

    class FakeGitHubClient:
        def list_open_authored_prs(self, login):
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
        def list_open_authored_prs(self, login):  # pragma: no cover - unused here
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
        def list_open_authored_prs(self, login):  # pragma: no cover - unused here
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
