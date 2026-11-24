"""Per-session record of the running Workdash dashboard."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .config import CONFIG_PATH

SESSIONS_DIR = CONFIG_PATH.parent / "sessions"
_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DashboardRecord:
    """Dashboard recorded as running for one Zellij session."""

    session: str
    pid: int
    address: str
    pane_id: str | None = None


class DashboardRecords:
    """Dashboard records keyed by Zellij session.

    A missing, unreadable, or malformed record is not an error; it loads as no
    record. Writes are atomic (temp file + ``os.replace``).
    """

    def load(self, session: str) -> DashboardRecord | None:
        path = self._path(session)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            return None
        if raw.get("session") != session:
            return None
        dashboard = raw.get("dashboard")
        if not isinstance(dashboard, dict):
            return None
        pid = dashboard.get("pid")
        address = dashboard.get("address")
        if not isinstance(pid, int) or pid <= 0:
            return None
        if not isinstance(address, str) or not address:
            return None
        pane_id = dashboard.get("pane_id")
        if not isinstance(pane_id, str) or not pane_id.isascii() or not pane_id.isdecimal():
            pane_id = None
        return DashboardRecord(session=session, pid=pid, address=address, pane_id=pane_id)

    def save(self, record: DashboardRecord) -> None:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        path = self._path(record.session)
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f"{path.name}.",
            suffix=".tmp",
            dir=SESSIONS_DIR,
        )
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "session": record.session,
                        "dashboard": {
                            "pid": record.pid,
                            "address": record.address,
                            "pane_id": record.pane_id,
                        },
                    },
                    handle,
                    ensure_ascii=True,
                    indent=2,
                )
            os.replace(temporary_path, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)
            raise

    def remove(self, session: str) -> None:
        with contextlib.suppress(OSError):
            self._path(session).unlink()

    def _path(self, session: str) -> Path:
        return SESSIONS_DIR / f"{quote(session, safe='')}.json"
