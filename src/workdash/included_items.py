"""Persistent store of explicitly-included work item URLs."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path


class IncludedItemsStore:
    """JSON-backed list of canonical GitHub URLs for included items.

    Missing or empty file is not an error; it loads as an empty list.
    Writes are atomic (temp file + ``os.replace``).
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict):
            return []
        urls = raw.get("urls")
        if not isinstance(urls, list):
            return []
        return [url for url in urls if isinstance(url, str) and url]

    def save(self, urls: Iterable[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f"{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump({"urls": list(urls)}, handle, ensure_ascii=True, indent=2)
            os.replace(temporary_path, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)
            raise

    def add(self, url: str) -> None:
        urls = self.load()
        if url in urls:
            return
        urls.append(url)
        self.save(urls)
