"""Backend orchestration for loading and ranking work items."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .analysis_cache import AnalysisCache
from .analyzer import Analyzer
from .config import WorkdashConfig
from .github_client import (
    GitHubClient,
    TransientFetchError,
    _noop_progress_callback,
    merge_normalized_work_items,
    normalize_assigned_issues,
    normalize_authored_pull_requests,
    normalize_recent_tracked_items,
    normalize_review_requested_pull_requests,
    normalize_todo_issues,
    parse_github_item_url,
)
from .included_items import IncludedItemsStore
from .models import WorkItem, WorkItemType
from .repo_resolver import resolve_repositories

SuggestionMarkers = dict[tuple[WorkItemType, str, int], str]
ItemIdentity = tuple[WorkItemType, str, int]


@dataclass(frozen=True, slots=True)
class IncludeResult:
    """Outcome of including a URL into the dashboard.

    Exactly one of ``invalid``, ``transient_failure``, ``duplicate_identity``,
    or ``fetched_item`` is truthy.
    """

    invalid: bool = False
    transient_failure: bool = False
    duplicate_identity: ItemIdentity | None = None
    fetched_item: WorkItem | None = None


_SUGGESTION_MARKER = "*"
# Rows the dashboard shows; the oldest discovered work beyond this is dropped.
_MAX_WORK_ITEMS = 100
_DEFAULT_CACHE_ROOT = Path.home() / ".config" / "workdash" / "cache"
_DEFAULT_INCLUDED_STORE_PATH = Path.home() / ".config" / "workdash" / "included.json"


def _suggestion_sort_key(item: WorkItem) -> tuple[object, int, str, int]:
    return (
        item.created_at,
        0 if item.item_type == WorkItemType.PR else 1,
        item.repo,
        item.number,
    )


def compute_suggestion_markers(work_items: list[WorkItem]) -> SuggestionMarkers:
    """Return a single suggestion marker using the v1 ranking heuristic."""

    if not work_items:
        return {}
    suggested_item = min(work_items, key=_suggestion_sort_key)
    return {
        (suggested_item.item_type, suggested_item.repo, suggested_item.number): _SUGGESTION_MARKER
    }


class WorkdashBackend:
    """Backend service coordinating repository resolution, retrieval, and cache use."""

    def __init__(
        self,
        *,
        cache_root: Path = _DEFAULT_CACHE_ROOT,
        github_client: GitHubClient | None = None,
        analysis_cache: AnalysisCache | None = None,
        analyzer: Analyzer | None = None,
        config: WorkdashConfig | None = None,
        included_items_store: IncludedItemsStore | None = None,
    ) -> None:
        self.github_client = github_client if github_client is not None else GitHubClient()
        self.analysis_cache = (
            analysis_cache if analysis_cache is not None else AnalysisCache(cache_root)
        )
        self.analyzer = analyzer if analyzer is not None else Analyzer()
        self.config = config if config is not None else WorkdashConfig()
        self.included_items_store = (
            included_items_store
            if included_items_store is not None
            else IncludedItemsStore(_DEFAULT_INCLUDED_STORE_PATH)
        )

    def load_items(
        self, progress_callback: Callable[[str], None] | None = None
    ) -> tuple[list[WorkItem], SuggestionMarkers]:
        """Load work items from GitHub sources and attach cached analysis state."""

        report_progress = (
            progress_callback if progress_callback is not None else _noop_progress_callback
        )
        repositories = resolve_repositories(list(self.config.repositories))
        report_progress(f"Resolved {len(repositories)} repository target(s):")
        for repository in repositories:
            report_progress(f"  - {repository}")
        github_username = self.config.github_username
        with ThreadPoolExecutor(max_workers=6) as executor:
            report_progress("Fetching open authored pull requests...")
            authored_future = executor.submit(
                self.github_client.list_open_authored_prs,
                github_username,
                progress_callback=report_progress,
            )
            report_progress("Fetching open review-requested pull requests...")
            review_requested_future = executor.submit(
                self.github_client.list_open_review_requested_prs,
                github_username,
                progress_callback=report_progress,
            )
            report_progress("Fetching open reviewed pull requests...")
            reviewed_future = executor.submit(
                self.github_client.list_open_reviewed_prs, github_username
            )
            report_progress("Fetching open assigned issues...")
            assigned_future = executor.submit(
                self.github_client.list_open_assigned_issues, github_username
            )
            report_progress("Fetching tracked issues and pull requests...")
            tracked_future = executor.submit(
                self.github_client.list_recent_tracked_items,
                repositories,
                progress_callback=report_progress,
            )
            report_progress("Fetching open todo issues...")
            todo_future = executor.submit(
                self.github_client.list_open_todo_issues,
                self.config.todo_repository,
                progress_callback=report_progress,
            )

            authored_pull_requests = authored_future.result()
            report_progress(f"Fetched {len(authored_pull_requests)} open authored pull request(s).")
            review_requested_pull_requests = review_requested_future.result()
            report_progress(
                f"Fetched {len(review_requested_pull_requests)} open review-requested pull request(s)."
            )
            reviewed_pull_requests = reviewed_future.result()
            report_progress(f"Fetched {len(reviewed_pull_requests)} open reviewed pull request(s).")
            assigned_issues = assigned_future.result()
            report_progress(f"Fetched {len(assigned_issues)} open assigned issue(s).")
            recent_tracked_items = tracked_future.result()
            todo_issues = todo_future.result()
            report_progress(f"Fetched {len(todo_issues)} open todo issue(s).")
        merged_review_items = merge_normalized_work_items(
            normalize_review_requested_pull_requests(review_requested_pull_requests),
            normalize_review_requested_pull_requests(reviewed_pull_requests),
        )
        merged_items = merge_normalized_work_items(
            normalize_authored_pull_requests(authored_pull_requests),
            merged_review_items,
        )
        merged_items = merge_normalized_work_items(
            merged_items,
            normalize_assigned_issues(assigned_issues),
        )
        merged_items = merge_normalized_work_items(
            merged_items,
            normalize_recent_tracked_items(recent_tracked_items),
        )
        # Tracked repositories can hold years of open work, so only the most
        # recently updated discovered items are kept. Todo and included items
        # are merged after the cap because the user asked for those by hand and
        # must not lose them to a busy week elsewhere.
        if len(merged_items) > _MAX_WORK_ITEMS:
            report_progress(
                f"Keeping the {_MAX_WORK_ITEMS} most recently updated "
                f"of {len(merged_items)} discovered work item(s)."
            )
            merged_items = sorted(merged_items, key=lambda item: item.updated_at, reverse=True)[
                :_MAX_WORK_ITEMS
            ]
        # Todo records go first so the target they carry survives dedupe against
        # the same issue found through the assigned-issue source.
        merged_items = merge_normalized_work_items(
            normalize_todo_issues(todo_issues),
            merged_items,
        )
        included_work_items = self._load_included_items(report_progress)
        if included_work_items:
            merged_items = merge_normalized_work_items(merged_items, included_work_items)
        pull_request_items = [item for item in merged_items if item.item_type == WorkItemType.PR]
        if pull_request_items:
            report_progress(
                f"Fetching linked issues for {len(pull_request_items)} pull request(s)..."
            )
            linked_issues = self.github_client.fetch_linked_issues(
                [(item.repo, item.number) for item in pull_request_items],
                progress_callback=report_progress,
            )
            for item in pull_request_items:
                # Only an issue in the pull request's own repository can name the
                # pull request's worktree, so a foreign closing issue is not the
                # item's linked issue even though it is still hidden below.
                own_repo_issues = [
                    issue
                    for issue in linked_issues.get((item.repo, item.number), [])
                    if issue[0] == item.repo
                ]
                item.linked_issue = min(own_repo_issues, key=lambda issue: issue[1], default=None)
            # A pull request already carries the work of the issue it closes, so
            # listing that issue as well would show the same work twice.
            hidden_issues = {issue for issues in linked_issues.values() for issue in issues}
            merged_items = [
                item
                for item in merged_items
                if item.item_type != WorkItemType.ISSUE
                or (item.repo, item.number) not in hidden_issues
            ]
        report_progress(
            f"Merged {len(merged_items)} unique work item(s); loading cached analyses..."
        )
        for item in merged_items:
            item.analysis = self.analysis_cache.load_analysis(item)
            item.analyzed_at = self.analysis_cache.load_analysis_date(item)
        report_progress("Done loading work items.")
        return merged_items, compute_suggestion_markers(merged_items)

    def _load_included_items(self, report_progress: Callable[[str], None]) -> list[WorkItem]:
        """Fetch each persisted included URL, pruning gone or invalid entries.

        Transient failures keep the URL in the store so the next refresh
        can retry. Only permanent drops (state != OPEN, 404-style errors)
        remove the URL.
        """

        stored_urls = self.included_items_store.load()
        if not stored_urls:
            return []
        report_progress(f"Fetching {len(stored_urls)} included item(s)...")
        survivors: list[str] = []
        fetched: list[WorkItem] = []
        for url in stored_urls:
            parsed = parse_github_item_url(url)
            if parsed is None:
                continue
            try:
                item = self.github_client.fetch_item_by_url(parsed, self.config.github_username)
            except TransientFetchError:
                survivors.append(parsed.canonical_url)
                continue
            if item is None:
                continue
            survivors.append(parsed.canonical_url)
            fetched.append(item)
        if survivors != stored_urls:
            self.included_items_store.save(survivors)
        return fetched

    def include_item_by_url(
        self, url: str, existing_identities: set[ItemIdentity]
    ) -> IncludeResult:
        """Resolve a pasted URL to an ``IncludeResult`` for the TUI.

        The backend owns URL parsing, duplicate detection, fetching, and
        persistence so the TUI only has to consume the result. ``url`` is
        parsed; a known identity short-circuits with ``duplicate_identity``
        after idempotently persisting the canonical URL; an unknown
        identity is fetched and persisted on success. Transient fetch
        failures surface via ``transient_failure`` and do NOT persist so
        the URL is not retained for a URL that may never resolve.
        """

        parsed = parse_github_item_url(url)
        if parsed is None:
            return IncludeResult(invalid=True)
        identity: ItemIdentity = (parsed.item_type, parsed.repo, parsed.number)
        if identity in existing_identities:
            # Persist canonical URL idempotently so a URL pasted in a
            # non-canonical form (e.g. ``/pull/7/files``) is still saved
            # under its canonical shape for future sessions.
            self.included_items_store.add(parsed.canonical_url)
            return IncludeResult(duplicate_identity=identity)
        try:
            item = self.github_client.fetch_item_by_url(parsed, self.config.github_username)
        except TransientFetchError:
            return IncludeResult(transient_failure=True)
        if item is None:
            return IncludeResult(invalid=True)
        self.included_items_store.add(parsed.canonical_url)
        return IncludeResult(fetched_item=item)

    def resolve_analyze_command_tokens(self, tool: str) -> list[str]:
        return self.config.analyze_agent_command_tokens(tool)

    def analyze_item(self, item: WorkItem, tool: str = "codex") -> str | None:
        """Generate or retrieve analysis, returning the markdown file path.

        :param str tool: ``"cached"`` to return existing analysis,
            ``"claude"`` or ``"codex"`` to force a fresh run with that backend.
        """

        if tool == "cached":
            cached_analysis = self.analysis_cache.load_analysis(item)
            if cached_analysis is not None:
                item.analysis = cached_analysis
                return str(self.analysis_cache.build_analysis_path(item))
            return None
        command_tokens = self.resolve_analyze_command_tokens(tool)
        analysis_content = self.analyzer.analyze(item, command_tokens=command_tokens)
        if analysis_content is None:
            return None
        self.analysis_cache.save(item, analysis_content)
        item.analysis = analysis_content
        item.analyzed_at = self.analysis_cache.load_analysis_date(item)
        return str(self.analysis_cache.build_analysis_path(item))
