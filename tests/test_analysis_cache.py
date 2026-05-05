import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from workdash.analysis_cache import (
    ANALYSIS_CACHE_SCHEMA_VERSION,
    AnalysisCache,
    sanitize_cache_filename_component,
)
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def make_work_item(*, item_type: WorkItemType, repo: str, number: int) -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=item_type,
        repo=repo,
        number=number,
        title="Title",
        created_at=datetime(2026, 2, 20, 12, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 21, 12, 0, 0, tzinfo=UTC),
        url="https://example.com/item",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("owner", "owner"),
        (" owner name ", "owner_name"),
        ("repo/name", "repo_name"),
        ("repo---name", "repo---name"),
        ("....", "unknown"),
        (" / : * ? ", "unknown"),
    ],
)
def test_sanitize_cache_filename_component_handles_unsafe_values_predictably(
    raw: str, expected: str
) -> None:
    assert sanitize_cache_filename_component(raw) == expected


@pytest.mark.parametrize(
    ("item_type", "expected_name"),
    [
        (WorkItemType.ISSUE, "owner_repo_ISSUE15.json"),
        (WorkItemType.PR, "owner_repo_PR15.json"),
    ],
)
def test_build_cache_path_matches_plan_format(
    tmp_path: Path, item_type: WorkItemType, expected_name: str
) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=item_type, repo="owner/repo", number=15)

    assert cache.build_cache_path(item) == tmp_path / "analyses" / expected_name


def test_build_cache_path_sanitizes_owner_and_repo_components(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(
        item_type=WorkItemType.PR,
        repo="Owner Name/repo:unsafe/value",
        number=42,
    )

    assert (
        cache.build_cache_path(item)
        == tmp_path / "analyses" / "Owner_Name_repo_unsafe_value_PR42.json"
    )


def test_build_cache_path_rejects_repo_without_owner_repo_shape(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.ISSUE, repo="not-a-repo", number=10)

    with pytest.raises(ValueError, match="owner/repo"):
        cache.build_cache_path(item)


def test_build_cache_document_returns_expected_schema_shape() -> None:
    cache = AnalysisCache(Path("/tmp/workdash"))
    item = make_work_item(item_type=WorkItemType.PR, repo="owner/repo", number=100)

    assert cache.build_cache_document(item) == {
        "schema_version": ANALYSIS_CACHE_SCHEMA_VERSION,
        "work_item": {
            "item_type": "pr",
            "repo": "owner/repo",
            "number": 100,
            "updated_at": "2026-02-21T12:00:00+00:00",
        },
    }


def test_cache_save_and_load_roundtrip(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.PR, repo="owner/repo", number=12)

    cache.save(item)

    assert cache.is_valid(item)
    assert cache.load_analysis(item) is None


def test_cache_save_and_load_roundtrip_with_analysis(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.ISSUE, repo="owner/repo", number=16)

    cache.save(item, "## Summary\nDetailed analysis")

    assert cache.is_valid(item)
    assert cache.load_analysis(item) == "## Summary\nDetailed analysis"
    assert cache.build_analysis_path(item).exists()


def test_cache_load_returns_none_when_item_updated_at_changed(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.ISSUE, repo="owner/repo", number=33)
    cache.save(item)
    changed_item = WorkItem(
        kind=item.kind,
        item_type=item.item_type,
        repo=item.repo,
        number=item.number,
        title=item.title,
        created_at=item.created_at,
        updated_at=item.updated_at + timedelta(minutes=1),
        url=item.url,
    )

    assert not cache.is_valid(changed_item)


def test_cache_load_returns_none_when_cached_updated_at_mismatches_item(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.ISSUE, repo="owner/repo", number=34)
    path = cache.build_cache_path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": ANALYSIS_CACHE_SCHEMA_VERSION,
                "work_item": {
                    "item_type": item.item_type.value,
                    "repo": item.repo,
                    "number": item.number,
                    "updated_at": (item.updated_at + timedelta(minutes=5)).isoformat(),
                },
            }
        ),
        encoding="utf-8",
    )

    assert not cache.is_valid(item)


def test_cache_load_returns_none_when_cache_file_is_missing(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.PR, repo="owner/repo", number=7)

    assert not cache.is_valid(item)


def test_cache_load_returns_none_for_malformed_json(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.ISSUE, repo="owner/repo", number=8)
    path = cache.build_cache_path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")

    assert not cache.is_valid(item)


def test_cache_load_returns_none_for_invalid_schema_document(tmp_path: Path) -> None:
    cache = AnalysisCache(tmp_path)
    item = make_work_item(item_type=WorkItemType.ISSUE, repo="owner/repo", number=9)
    path = cache.build_cache_path(item)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"schema_version": 99, "work_item": {}}',
        encoding="utf-8",
    )

    assert not cache.is_valid(item)
