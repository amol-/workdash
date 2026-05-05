"""Analysis cache access."""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

from .models import WorkItem, WorkItemType

ANALYSIS_CACHE_SCHEMA_VERSION = 3
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class AnalysisCacheWorkItemDocument(TypedDict):
    """Serialized work item metadata stored alongside an analysis."""

    item_type: str
    repo: str
    number: int
    updated_at: str


class AnalysisCacheDocument(TypedDict):
    """Full JSON document persisted for an analysis cache entry."""

    schema_version: int
    work_item: AnalysisCacheWorkItemDocument


def sanitize_cache_filename_component(component: str) -> str:
    """Map an arbitrary text component to a filesystem-safe cache token."""

    sanitized = _UNSAFE_FILENAME_CHARS.sub("_", component.strip()).strip("._-")
    return sanitized or "unknown"


def _split_repository(repo: str) -> tuple[str, str]:
    owner, separator, repository = repo.partition("/")
    if separator != "/" or not owner or not repository:
        raise ValueError(f"Work item repo must be in 'owner/repo' format: {repo!r}")
    return owner, repository


class AnalysisCache:
    """Filesystem-backed cache for per-work-item analysis output.

    Cache entries are keyed by ``(item_type, repo, number, updated_at)`` so
    any change to the GitHub ``updated_at`` timestamp invalidates the entry
    and forces the next analysis to re-run.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def build_cache_path(self, item: WorkItem) -> Path:
        """Return the cache file path for a work item."""

        owner, repository = _split_repository(item.repo)
        item_prefix = "ISSUE" if item.item_type == WorkItemType.ISSUE else "PR"
        filename = (
            f"{sanitize_cache_filename_component(owner)}_"
            f"{sanitize_cache_filename_component(repository)}_"
            f"{item_prefix}{item.number}.json"
        )
        return self.root / "analyses" / filename

    def build_analysis_path(self, item: WorkItem) -> Path:
        """Return the markdown analysis file path for a work item."""

        return self.build_cache_path(item).with_suffix(".md")

    def build_cache_document(self, item: WorkItem) -> AnalysisCacheDocument:
        """Build the JSON payload shape for storing a cache entry."""

        return {
            "schema_version": ANALYSIS_CACHE_SCHEMA_VERSION,
            "work_item": {
                "item_type": item.item_type.value,
                "repo": item.repo,
                "number": item.number,
                "updated_at": item.updated_at.isoformat(),
            },
        }

    def _load_cached_payload(self, item: WorkItem) -> bool:
        """Return ``True`` when the cache entry is present and matches ``item``."""

        path = self.build_cache_path(item)
        if not path.exists():
            return False

        try:
            raw_document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False

        if not isinstance(raw_document, dict):
            return False
        if raw_document.get("schema_version") != ANALYSIS_CACHE_SCHEMA_VERSION:
            return False

        work_item = raw_document.get("work_item")
        if not isinstance(work_item, dict):
            return False
        cached_item_type = work_item.get("item_type")
        cached_repo = work_item.get("repo")
        cached_number = work_item.get("number")
        cached_updated_at = work_item.get("updated_at")
        if (
            cached_item_type not in {WorkItemType.ISSUE.value, WorkItemType.PR.value}
            or not isinstance(cached_repo, str)
            or not isinstance(cached_number, int)
            or not isinstance(cached_updated_at, str)
        ):
            return False
        return not (
            cached_item_type != item.item_type.value
            or cached_repo != item.repo
            or cached_number != item.number
            or cached_updated_at != item.updated_at.isoformat()
        )

    def is_valid(self, item: WorkItem) -> bool:
        """Check whether a valid cache entry exists for a work item."""

        return self._load_cached_payload(item)

    def load_analysis_date(self, item: WorkItem) -> datetime | None:
        """Return the modification time of the analysis markdown file, if valid."""

        if not self._load_cached_payload(item):
            return None
        md_path = self.build_analysis_path(item)
        if not md_path.exists():
            return None
        try:
            mtime = md_path.stat().st_mtime
        except OSError:
            return None
        return datetime.fromtimestamp(mtime, tz=UTC)

    def load_analysis(self, item: WorkItem) -> str | None:
        if not self._load_cached_payload(item):
            return None
        md_path = self.build_analysis_path(item)
        if not md_path.exists():
            return None
        try:
            content = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
        return content if content.strip() else None

    def save(self, item: WorkItem, analysis: str | None = None) -> None:
        path = self.build_cache_path(item)
        path.parent.mkdir(parents=True, exist_ok=True)

        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f"{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    self.build_cache_document(item),
                    handle,
                    ensure_ascii=True,
                    indent=2,
                )
            os.replace(temporary_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)
            raise

        if analysis:
            self.build_analysis_path(item).write_text(analysis, encoding="utf-8")
