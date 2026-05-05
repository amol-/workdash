"""Backend orchestration for loading and ranking work items."""

from __future__ import annotations

import shlex
from collections.abc import Callable
from pathlib import Path

from .analysis_cache import AnalysisCache
from .analyzer import Analyzer
from .config import WorkdashConfig
from .github_client import (
    GitHubClient,
    merge_normalized_work_items,
    normalize_assigned_issues,
    normalize_authored_pull_requests,
    normalize_recent_tracked_items,
    normalize_review_requested_pull_requests,
)
from .models import WorkItem, WorkItemType
from .repo_resolver import resolve_repositories

SuggestionMarkers = dict[tuple[WorkItemType, str, int], str]

_SUGGESTION_MARKER = "*"
_DEFAULT_CACHE_ROOT = Path.home() / ".config" / "workdash" / "cache"


def _noop_progress_callback(_message: str) -> None:
    """Ignore progress updates when no reporting target is configured."""


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
    ) -> None:
        self.github_client = github_client if github_client is not None else GitHubClient()
        self.analysis_cache = (
            analysis_cache if analysis_cache is not None else AnalysisCache(cache_root)
        )
        self.analyzer = analyzer if analyzer is not None else Analyzer()
        self.config = config if config is not None else WorkdashConfig()

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
        report_progress("Fetching open authored pull requests...")
        authored_pull_requests = self.github_client.list_open_authored_prs(github_username)
        report_progress(f"Fetched {len(authored_pull_requests)} open authored pull request(s).")
        report_progress("Fetching open review-requested pull requests...")
        review_requested_pull_requests = self.github_client.list_open_review_requested_prs(
            github_username
        )
        report_progress(
            f"Fetched {len(review_requested_pull_requests)} open review-requested pull request(s)."
        )
        report_progress("Fetching open reviewed pull requests...")
        reviewed_pull_requests = self.github_client.list_open_reviewed_prs(github_username)
        report_progress(f"Fetched {len(reviewed_pull_requests)} open reviewed pull request(s).")
        report_progress("Fetching open assigned issues...")
        assigned_issues = self.github_client.list_open_assigned_issues(github_username)
        report_progress(f"Fetched {len(assigned_issues)} open assigned issue(s).")
        report_progress("Fetching tracked issues and pull requests...")
        recent_tracked_items = self.github_client.list_recent_tracked_items(
            repositories,
            progress_callback=report_progress,
        )
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
        report_progress(
            f"Merged {len(merged_items)} unique work item(s); loading cached analyses..."
        )
        for item in merged_items:
            item.analysis = self.analysis_cache.load_analysis(item)
            item.analyzed_at = self.analysis_cache.load_analysis_date(item)
        report_progress("Done loading work items.")
        return merged_items, compute_suggestion_markers(merged_items)

    def _resolve_command_tokens(self, tool: str) -> list[str]:
        if tool == "claude":
            return shlex.split(self.config.claude.analyze)
        return shlex.split(self.config.codex.analyze)

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
        command_tokens = self._resolve_command_tokens(tool)
        analysis_content = self.analyzer.analyze(item, command_tokens=command_tokens)
        if analysis_content is None:
            return None
        self.analysis_cache.save(item, analysis_content)
        item.analysis = analysis_content
        item.analyzed_at = self.analysis_cache.load_analysis_date(item)
        return str(self.analysis_cache.build_analysis_path(item))
