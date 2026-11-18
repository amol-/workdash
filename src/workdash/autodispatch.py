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
from .github_client import MergeConflictError, MergeHeadChangedError
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

    def __init__(
        self,
        owner: WorkdashSession,
        scope: str,
        group: str | None,
        agent: str | None,
        automerge: bool = False,
    ):
        self.owner, self.scope, self.group, self.agent = owner, scope, group, agent
        self.automerge = automerge
        self.id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.started = time.monotonic()
        self.ended: float | None = None
        self.status = self.Status.RUNNING
        self.current_item: str | None = None
        self.pane: tuple[str, str] | None = None
        self.selected_group: str | None = group
        self.stage = "checking"
        self.launch_reason: str | None = None
        self.iteration = 0
        self.error: str | None = None
        self.dismissed = False
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name=f"autodispatch-{self.id}", daemon=True)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            blocked = self.pane and self.owner.autoclose.blocked(*self.pane)
            return {
                "id": self.id,
                "scope": self.scope,
                "group": self.group,
                "agent": self.agent,
                "automerge": self.automerge,
                "started_at": self.started_at,
                "elapsed_seconds": int((self.ended or time.monotonic()) - self.started),
                "current_item": self.current_item,
                "pane_id": self.pane[1] if self.pane else None,
                "scope_is_current_item": bool(
                    self.current_item
                    and self.owner._scope_identity(self.scope)
                    == self.owner._scope_identity(self.current_item)
                ),
                "selected_group": self.selected_group,
                "stage": self.stage,
                "launch_reason": self.launch_reason,
                "iteration": self.iteration,
                "status": "blocked"
                if self.status == self.Status.RUNNING and blocked
                else str(self.status),
                "error": self.error,
                "dismissed": self.dismissed,
            }

    def stop(self, *, close_blocked: bool = False) -> None:
        with self.lock:
            if close_blocked and self.pane and self.owner.autoclose.blocked(*self.pane):
                self.owner.autoclose.stop_blocked(*self.pane)
            self.stop_event.set()
            if self.status == self.Status.RUNNING:
                self.status = self.Status.STOPPED
                self.ended = time.monotonic()
                self.owner.completed_autodispatch_item(self.current_item)

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
                    self.owner.completed_autodispatch_item(self.current_item)
                    self.current_item = None
                    self.pane = None
                    self.launch_reason = None
                stage = "dispatch"
                result = self.owner.dispatch_once(
                    scope=self.scope, group=self.group, agent=selected, before_launch=check_limit
                )
                with self.lock:
                    if self.stop_event.is_set():
                        if result["status"] == "launched":
                            self.owner.completed_autodispatch_item(str(result["item_id"]))
                        return
                    if result["status"] == "no_ready_task":
                        logger.info("Session %s finished", self.id)
                        self.status = self.Status.FINISHED
                        self.current_item = None
                        self.ended = time.monotonic()
                        return
                    logger.info("Session %s launched pane %s", self.id, result["pane_id"])
                    self.current_item = str(result["item_id"])
                    self.pane = (str(result["session"]), str(result["pane_id"]))
                    self.selected_group = result.get("tab_name") or result.get("group")
                    self.launch_reason = result.get("launch_reason")
                    self.stage = "developing"
                    consecutive = consecutive + 1 if self.current_item == previous else 1
                    previous = self.current_item
                stage = "pane polling"
                absent = 0
                while not self.stop_event.wait(_AUTODISPATCH_POLL_SECONDS):
                    if (
                        self.pane
                        and getattr(self.owner, "autoclose", None)
                        and self.owner.autoclose.blocked(*self.pane) is True
                    ):
                        absent = 0
                        continue
                    panes = launcher.zellij.list_workdash_panes(str(result["session"]))
                    present = launcher.find_zellij_pane(panes, result["pane_id"]) is not None
                    # A listing can miss a live pane, so one miss is not proof it is gone.
                    absent = 0 if present else absent + 1
                    if absent >= 2:
                        break
                if not self.stop_event.is_set():
                    with self.lock:
                        self.stage = "reviewing"
                        self.launch_reason = None
                    target = str(result.get("pr_id", result["item_id"]))
                    retry_head = False
                    for _ in range(4):
                        if self.stop_event.is_set():
                            return
                        stage = "review"
                        with self.lock:
                            require_fresh = self.automerge
                        review_options = {"require_agent": True}
                        if retry_head:
                            review_options["detached"] = True
                        review = self.owner.review(
                            target=target,
                            agent=selected,
                            group=self.group,
                            require_fresh=require_fresh,
                            **review_options,
                        )
                        if "pr_id" in result and review["status"] == "nothing_to_review":
                            target = str(result["item_id"])
                            review = self.owner.review(
                                target=target,
                                agent=selected,
                                group=self.group,
                                require_fresh=require_fresh,
                                **review_options,
                            )
                        with self.lock:
                            merge = self.automerge and not self.stop_event.is_set()
                        if not merge or review.get("status") != "reviewed":
                            break
                        if not require_fresh:
                            continue
                        item = self.owner._require_item(str(review["item_id"]))
                        stage = "merge"
                        head = review.get("head_sha")
                        while True:
                            with self.lock:
                                merge = self.automerge and not self.stop_event.is_set()
                            if not merge:
                                break
                            state = self.owner.backend.github_client.work_pull_request(
                                item,
                                github_username=self.owner.config.github_username,
                                todo_repository=self.owner.config.todo_repository,
                            )
                            if not head or not state or state.state != "OPEN" or not state.head_sha:
                                raise RuntimeError("Pull request head is unavailable after review.")
                            if state.head_sha != head:
                                retry_head = True
                                break
                            if not state.review_threads_complete:
                                raise RuntimeError(
                                    "Cannot verify all pull request review feedback."
                                )
                            if state.reasons:
                                if not state.head_writable:
                                    raise RuntimeError(
                                        "Pull request needs fixes but its head is not writable."
                                    )
                                break
                            if (
                                not state.changes_requested
                                and state.mergeability == "mergeable"
                                and state.ci_status in {"passing", "no_checks"}
                            ):
                                break
                            with self.lock:
                                self.stage = "waiting"
                            if self.stop_event.wait(_AUTODISPATCH_POLL_SECONDS):
                                return
                        with self.lock:
                            merge = merge and self.automerge and not self.stop_event.is_set()
                            self.stage = "reviewing"
                        if not merge:
                            break
                        if state.head_sha != head:
                            continue
                        if state.reasons:
                            break
                        if self.stop_event.is_set():
                            return
                        try:
                            self.owner.backend.github_client.merge_pull_request(
                                item.repo, item.number, head
                            )
                        except MergeHeadChangedError:
                            retry_head = True
                            continue
                        except MergeConflictError:
                            if self.stop_event.is_set():
                                raise
                            state = self.owner.backend.github_client.work_pull_request(
                                item,
                                github_username=self.owner.config.github_username,
                                todo_repository=self.owner.config.todo_repository,
                            )
                            if not state or "fix_merge_conflicts" not in state.reasons:
                                raise
                        break
                    else:
                        raise RuntimeError("Pull request head changed too often to merge safely.")

        except Exception as error:
            logger.error("Session %s failed during %s: %s", self.id, stage, safe_error(error))
            with self.lock:
                if not self.stop_event.is_set():
                    self.status = self.Status.FAILED
                    self.error = str(error)
                    self.ended = time.monotonic()
                    self.owner.completed_autodispatch_item(self.current_item)
        finally:
            with self.lock:
                if stage in {"review", "merge"} and self.stop_event.is_set():
                    self.owner.completed_autodispatch_item(self.current_item)
