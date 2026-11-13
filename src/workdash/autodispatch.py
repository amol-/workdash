"""Server-owned sequences of supervised dispatches."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from . import launcher
from .log_diagnostics import safe_error

if TYPE_CHECKING:
    from .control import WorkdashSession

logger = logging.getLogger(__name__)

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
        self.selected_group: str | None = group
        self.stage = "checking"
        self.launch_reason: str | None = None
        self.iteration = 0
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
                "selected_group": self.selected_group,
                "stage": self.stage,
                "launch_reason": self.launch_reason,
                "iteration": self.iteration,
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
        stage = "configuration"
        try:
            logger.info("Session %s started", self.id)
            agents = self.owner.config.configured_code_agents()
            selected = self.agent or (agents[0] if agents else None)
            if selected is None or selected not in agents:
                raise RuntimeError(f"Coding agent {selected!r} has no launch command.")
            if selected not in self.owner.config.configured_analyze_agents():
                raise RuntimeError(f"Coding agent {selected!r} has no analysis command.")
            stage = "prepare"
            self.owner.prepare_autodispatch(self.scope)
            previous = None
            consecutive = 0

            def check_limit(item_id: str) -> None:
                if item_id == previous and consecutive >= 9:
                    raise RuntimeError(f"Issue {item_id} selected 10 consecutive times.")

            while not self.stop_event.is_set():
                with self.lock:
                    self.iteration += 1
                    self.stage = "checking"
                    self.current_item = None
                    self.launch_reason = None
                stage = "dispatch"
                result = self.owner.dispatch_once(
                    scope=self.scope, group=self.group, agent=selected, before_launch=check_limit
                )
                with self.lock:
                    if self.stop_event.is_set():
                        return
                    if result["status"] == "no_ready_task":
                        logger.info("Session %s finished", self.id)
                        self.status = self.Status.FINISHED
                        self.current_item = None
                        self.ended = time.monotonic()
                        return
                    logger.info("Session %s launched pane %s", self.id, result["pane_id"])
                    self.current_item = str(result["item_id"])
                    self.selected_group = result.get("tab_name") or result.get("group")
                    self.launch_reason = result.get("launch_reason")
                    self.stage = "developing"
                    consecutive = consecutive + 1 if self.current_item == previous else 1
                    previous = self.current_item
                stage = "pane polling"
                absent = 0
                while not self.stop_event.wait(_AUTODISPATCH_POLL_SECONDS):
                    panes = launcher.zellij.load_zellij_panes(str(result["session"]))
                    present = any(
                        _format_pane_id(pane.get("id")) == result["pane_id"]
                        and pane.get("is_plugin") is not True
                        for pane in panes
                    )
                    # A listing can miss a live pane, so one miss is not proof it is gone.
                    absent = 0 if present else absent + 1
                    if absent >= 2:
                        break
                if not self.stop_event.is_set():
                    with self.lock:
                        self.stage = "reviewing"
                        self.launch_reason = None
                    stage = "review"
                    review = self.owner.review(
                        target=str(result.get("pr_id", result["item_id"])),
                        agent=selected,
                        group=self.group,
                    )
                    if "pr_id" in result and review["status"] == "nothing_to_review":
                        self.owner.review(
                            target=str(result["item_id"]), agent=selected, group=self.group
                        )
        except Exception as error:
            logger.error("Session %s failed during %s: %s", self.id, stage, safe_error(error))
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
