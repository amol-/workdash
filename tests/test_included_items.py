from pathlib import Path

from workdash.included_items import IncludedItemsStore


def test_included_items_store_load_returns_empty_for_missing_file(tmp_path: Path) -> None:
    store = IncludedItemsStore(tmp_path / "missing" / "included.json")
    assert store.load() == []


def test_included_items_store_save_then_load_roundtrips_and_add_is_idempotent(
    tmp_path: Path,
) -> None:
    store = IncludedItemsStore(tmp_path / "included.json")
    urls = [
        "https://github.com/owner/repo/pull/1",
        "https://github.com/owner/repo/issues/2",
    ]
    store.save(urls)
    assert store.load() == urls
    # Adding a URL that is already stored must not duplicate the entry on disk.
    store.add(urls[0])
    assert store.load() == urls


def test_included_items_store_load_ignores_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "included.json"
    path.write_text("not json", encoding="utf-8")
    assert IncludedItemsStore(path).load() == []


def test_included_items_store_load_ignores_non_url_entries(tmp_path: Path) -> None:
    path = tmp_path / "included.json"
    path.write_text('{"urls": ["https://github.com/o/r/pull/1", 42, "", null]}', encoding="utf-8")
    assert IncludedItemsStore(path).load() == ["https://github.com/o/r/pull/1"]
