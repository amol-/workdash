"""Server-owned sequences of supervised dispatches."""

from __future__ import annotations

import threading
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from . import launcher

if TYPE_CHECKING:
    from .control import WorkdashSession

_AUTODISPATCH_POLL_SECONDS = 30.0


class _AutodispatchSession:
    """One independently running sequence of supervised dispatches."""

    class Status(StrEnum):
        RUNNING = "running"
        STOPPED = "stopped"
        FINISHED = "finished"
        FAILED = "failed"

    def __init__(self, owner: WorkdashSession, scope: str, group: str | None, agent: str | None):
        self.owner, self.scope, self.group, self.agent = owner, scope, group, agent
        self.id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.started = time.monotonic()
        self.ended: float | None = None
        self.status = self.Status.RUNNING
        self.current_item: str | None = None
        self.error: str | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name=f"autodispatch-{self.id}", daemon=True)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "id": self.id,
                "scope": self.scope,
                "group": self.group,
                "agent": self.agent,
                "started_at": self.started_at,
                "elapsed_seconds": int((self.ended or time.monotonic()) - self.started),
                "current_item": self.current_item,
                "status": str(self.status),
                "error": self.error,
            }

    def stop(self) -> None:
        with self.lock:
            self.stop_event.set()
            if self.status == self.Status.RUNNING:
                self.status = self.Status.STOPPED
                self.ended = time.monotonic()

    def run(self) -> None:
        try:
            while not self.stop_event.is_set():
                result = self.owner.dispatch_once(
                    scope=self.scope, group=self.group, agent=self.agent
                )
                with self.lock:
                    if self.stop_event.is_set():
                        return
                    if result["status"] == "no_ready_task":
                        self.status = self.Status.FINISHED
                        self.current_item = None
                        self.ended = time.monotonic()
                        return
                    self.current_item = str(result["item_id"])
                while not self.stop_event.wait(_AUTODISPATCH_POLL_SECONDS):
                    panes = launcher.zellij.load_zellij_panes(str(result["session"]))
                    if not any(
                        _format_pane_id(pane.get("id")) == result["pane_id"]
                        and pane.get("is_plugin") is not True
                        for pane in panes
                    ):
                        break
        except Exception as error:
            with self.lock:
                if not self.stop_event.is_set():
                    self.status = self.Status.FAILED
                    self.error = str(error)
                    self.ended = time.monotonic()


def _format_pane_id(pane_id: object) -> str:
    pane_id_text = str(pane_id)
    if pane_id_text.startswith("terminal_"):
        return pane_id_text
    return f"terminal_{pane_id_text}"
