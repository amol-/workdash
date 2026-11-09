"""Server-owned supervision of autoclosed coding panes.

Autoclose leaves the agent in its ordinary interactive pane and lets the server
decide from pane output and GitHub state when to nudge it and when to close it.
Nothing here starts, restarts, or takes over a pane: an entry is created only
for the pane a ``code`` request just launched, and it is dropped as soon as that
pane no longer matches the session, pane ID and title it was registered with.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from .config import WorkdashConfig
from .git import has_unpublished_work
from .launcher import (
    _format_zellij_pane_id,
    close_zellij_pane,
    dump_zellij_pane,
    is_live_zellij_pane,
    load_zellij_panes,
    press_zellij_pane_enter,
    send_zellij_pane_input,
)
from .models import REVIEW_KINDS, WorkItem, WorkItemType
from .work_instruction import render_autonomous_work_instruction
from .work_state import PullRequestWorkState, pull_request_states, work_pull_request

logger = logging.getLogger(__name__)

# Every supervised pane is checked this often.
POLL_SECONDS = 60.0
# Unchanged output and no Workdash message for this long makes a pane a candidate.
IDLE_SECONDS = 15 * 60.0
# Waits after successive failed checks, so an outage slows supervision instead of acting.
RETRY_SECONDS = (120.0, 300.0, 900.0)
# A pane's whole work state costs several queries, so it is resolved every fifth check:
# once to find the pull request an issue now has, and once to decide what an idle pane
# needs. A known pull request is only asked for its state, which every check can afford.
RESOLVE_SECONDS = 5 * POLL_SECONDS
# The only actions worth sending; every other action describes waiting for others.
_WORK_ACTIONS = {"implement", "fix_ci", "fix_merge_conflicts", "address_review"}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_COMPLETED_ACTIONS = {"awaiting_review", "awaiting_merge"}


class AutocloseSupervisor:
    """Nudge and close autoclosed coding panes from their output and GitHub state.

    One worker checks every registered pane once a minute. A merged pull request
    closes its pane on the next check; every other decision waits until the pane
    has shown no new output and received no Workdash message for fifteen minutes.
    """

    def __init__(self, *, config: WorkdashConfig, lock: threading.RLock) -> None:
        """
        :param threading.RLock lock: The session lock pane operations share with code launches.
        """
        self._config = config
        self._lock = lock
        self._panes: dict[tuple[str, str], _SupervisedPane] = {}
        self._stopped = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        """Poll in the background until :meth:`stop`; the server owns this lifetime."""

        if self._worker is None:
            self._worker = threading.Thread(
                target=self._run, name="workdash-autoclose", daemon=True
            )
            self._worker.start()

    def stop(self) -> None:
        """Stop polling; a check already talking to GitHub finishes unwatched."""

        self._stopped.set()

    def supervise(
        self, *, session: str, pane_id: str, pane_title: str, item: WorkItem, repo_path: str
    ) -> None:
        """Watch the pane a request just launched, keyed by its identity.

        :param str pane_id: The pane ID reported for the launch, revalidated before every action.
        :param str pane_title: The launch pane title, which Zellij reports back.
        :param str repo_path: The worktree the agent works in, checked for unpublished work.
        """

        self._panes[(session, pane_id)] = _SupervisedPane(
            session=session,
            pane_id=pane_id,
            pane_title=pane_title,
            item=item,
            repo_path=repo_path,
            launched_at=datetime.now(UTC),
            last_activity=time.monotonic(),
            review_only=item.kind in REVIEW_KINDS,
        )

    def poll_once(self) -> None:
        """Check every supervised pane once: close merged work, act on idle panes."""

        for entry in list(self._panes.values()):
            if self._stopped.is_set():
                return
            if time.monotonic() < entry.retry_at:
                continue
            try:
                self._check(entry)
            except Exception as error:
                # One pane that cannot be checked must not end supervision of the others.
                entry.failures += 1
                entry.retry_at = (
                    time.monotonic() + RETRY_SECONDS[min(entry.failures, len(RETRY_SECONDS)) - 1]
                )
                logger.warning("Autoclose check of pane %s failed: %s", entry.pane_id, error)
                if entry.failures > len(RETRY_SECONDS) and not entry.stalled:
                    # Past the longest retry the pane is effectively unsupervised; say so once.
                    entry.stalled = True
                    logger.warning(
                        "Autoclose supervision of pane %s is stalled: %d failed checks, retrying "
                        "every %d minutes",
                        entry.pane_id,
                        entry.failures,
                        RETRY_SECONDS[-1] // 60,
                    )
            else:
                entry.failures = 0
                entry.retry_at = 0.0
                entry.stalled = False

    def _run(self) -> None:
        while not self._stopped.wait(POLL_SECONDS):
            self.poll_once()

    def _check(self, entry: _SupervisedPane) -> None:
        if entry.awaiting_enter:
            # The instruction text is already in the pane, so only its Enter is retried.
            self._submit(entry)
            return
        with self._lock:
            if self._drop_unidentified(entry):
                return
            screen = _screen_signature(dump_zellij_pane(entry.session, entry.pane_id))
        now = time.monotonic()
        if not entry.screen_known:
            entry.screen, entry.screen_known = screen, True
        elif screen != entry.screen:
            entry.screen, entry.last_activity = screen, now
        state = self._merge_state(entry)
        if state == "MERGED":
            # A merge ends the work whether or not the agent is still writing.
            self._close(entry, after_idle=False)
        elif state == "CLOSED":
            # Closed without merging is neither completion nor work to redo on its own.
            self._panes.pop((entry.session, entry.pane_id), None)
            logger.info(
                "Pane %s needs review: its pull request was closed without merging",
                entry.pane_id,
            )
        elif (
            not entry.review_only
            and now >= entry.decide_at
            and now - entry.last_activity >= IDLE_SECONDS
        ):
            self._decide(entry)

    def _decide(self, entry: _SupervisedPane) -> None:
        """Send what GitHub says the idle pane's work needs, or close finished work."""

        entry.decide_at = time.monotonic() + RESOLVE_SECONDS
        state = self._work_state(entry)
        action = state.action if state is not None else "implement"
        if action in _WORK_ACTIONS:
            fingerprint = _state_fingerprint(action, state)
            if fingerprint != entry.notified:
                work = entry.item if state is None else state
                instruction = render_autonomous_work_instruction(
                    action, work, self._config.github_username
                )
                self._send(entry, instruction, fingerprint)
        elif state is not None and _finished(state):
            if has_unpublished_work(entry.repo_path, state.head_sha):
                logger.info(
                    "Keeping pane %s open: its worktree holds unpublished work", entry.pane_id
                )
            else:
                self._close(entry, after_idle=True)

    def _work_state(self, entry: _SupervisedPane) -> PullRequestWorkState | None:
        """The pull request carrying the pane's work, by the same resolution as the briefing."""

        return work_pull_request(
            entry.item,
            github_username=self._config.github_username,
            todo_repository=self._config.todo_repository,
        )

    def _merge_state(self, entry: _SupervisedPane) -> str | None:
        """The state of the pull request carrying the pane's work; cheap enough for every check."""

        if entry.item.item_type == WorkItemType.PR:
            pull_request = (entry.item.repo, entry.item.number)
        elif entry.pull_request is not None:
            pull_request = entry.pull_request
        elif time.monotonic() >= entry.discover_at:
            # Finding the pull request an issue now has costs a whole resolution, so look rarely.
            entry.discover_at = time.monotonic() + RESOLVE_SECONDS
            state = self._work_state(entry)
            # A pull request that merged before this pane existed is not the work it was
            # launched for, so the issue counts as having none yet. Its merge time, not its
            # last change, decides that: a comment after the launch changes nothing.
            if state is None or (
                state.merged_at is not None and state.merged_at < entry.launched_at
            ):
                return None
            pull_request = (state.item.repo, state.item.number)
            entry.pull_request = pull_request
        else:
            return None
        return pull_request_states([pull_request])[pull_request]

    def _send(self, entry: _SupervisedPane, instruction: str, fingerprint: str) -> None:
        """Send an instruction to an idle pane, revalidating the pane and its last screen."""

        with self._lock:
            if self._drop_unidentified(entry):
                return
            screen = _screen_signature(dump_zellij_pane(entry.session, entry.pane_id))
            if screen != entry.screen:
                # The agent is working again, so let it finish rather than interrupt it.
                entry.screen, entry.last_activity = screen, time.monotonic()
                logger.info("Postponed the autoclose instruction for pane %s", entry.pane_id)
                return
            send_zellij_pane_input(entry.session, entry.pane_id, instruction, raw=True)
            # The text carries the instruction, so it counts as delivered before its Enter,
            # and the pane now shows it, which is what a retried Enter is compared against.
            entry.notified, entry.awaiting_enter = fingerprint, True
            entry.screen = _screen_signature(dump_zellij_pane(entry.session, entry.pane_id))
            entry.last_activity = time.monotonic()
            press_zellij_pane_enter(entry.session, entry.pane_id)
            entry.awaiting_enter = False

    def _submit(self, entry: _SupervisedPane) -> None:
        """Submit the text an earlier check left in the pane, without typing it again.

        An Enter is only pressed into the screen the text was typed into: an agent that
        printed something since then moved on, so the instruction is dropped instead of
        answering whatever it now asks.
        """

        with self._lock:
            if self._drop_unidentified(entry):
                return
            screen = _screen_signature(dump_zellij_pane(entry.session, entry.pane_id))
            if screen != entry.screen:
                entry.screen, entry.last_activity = screen, time.monotonic()
                entry.awaiting_enter = False
                logger.info("Dropped the autoclose instruction for pane %s", entry.pane_id)
                return
            press_zellij_pane_enter(entry.session, entry.pane_id)
            entry.awaiting_enter = False
            entry.last_activity = time.monotonic()

    def _close(self, entry: _SupervisedPane, *, after_idle: bool) -> None:
        """Close a pane whose work is done, revalidating the pane and, except for merges, its screen."""

        with self._lock:
            if self._drop_unidentified(entry):
                return
            if after_idle:
                # The decision needed an idle pane; only a screen that changed since postpones it.
                screen = _screen_signature(dump_zellij_pane(entry.session, entry.pane_id))
                if screen != entry.screen:
                    entry.screen, entry.last_activity = screen, time.monotonic()
                    logger.info("Postponed closing the autoclosed pane %s", entry.pane_id)
                    return
            close_zellij_pane(entry.session, entry.pane_id)
            self._panes.pop((entry.session, entry.pane_id), None)
            logger.info("Closed the autoclosed pane %s", entry.pane_id)

    def _identified(self, entry: _SupervisedPane) -> bool:
        """Whether a live pane in this session still has the registered ID and launch title."""

        panes = load_zellij_panes(entry.session)
        pane = next(
            (pane for pane in panes if _format_zellij_pane_id(pane.get("id")) == entry.pane_id),
            None,
        )
        return (
            pane is not None and is_live_zellij_pane(pane) and pane.get("title") == entry.pane_title
        )

    def _drop_unidentified(self, entry: _SupervisedPane) -> bool:
        """Stop supervising a pane that no longer matches its identity; True when it was dropped."""

        if self._identified(entry):
            return False
        self._panes.pop((entry.session, entry.pane_id), None)
        return True


@dataclass
class _SupervisedPane:
    """One newly launched coding pane and what the supervisor last saw there."""

    session: str
    pane_id: str
    pane_title: str
    item: WorkItem
    repo_path: str
    launched_at: datetime
    last_activity: float
    review_only: bool
    screen: str = ""
    screen_known: bool = False
    pull_request: tuple[str, int] | None = None
    notified: str | None = None
    awaiting_enter: bool = False
    discover_at: float = 0.0
    decide_at: float = 0.0
    failures: int = 0
    retry_at: float = 0.0
    stalled: bool = False


def _finished(state: PullRequestWorkState) -> bool:
    """Whether GitHub reports nothing left for the agent on the current head.

    Awaiting review or merge means the current head's checks pass or are verifiably
    absent, no review asks for changes and no feedback is outstanding; a clean
    mergeability answer is required on top, so unknown or conflicting holds the pane.
    """

    return (
        state.action in _COMPLETED_ACTIONS
        and state.head_writable
        and state.mergeability == "mergeable"
    )


def _state_fingerprint(action: str, state: PullRequestWorkState | None) -> str:
    """Identify the GitHub state an instruction was rendered from."""

    if state is None:
        return action
    parts = [
        action,
        state.head_sha,
        state.base_head_sha or "",
        state.mergeability,
        *state.failing_checks,
        *(feedback.url for feedback in state.feedback),
    ]
    return "|".join(parts)


def _screen_signature(screen: str) -> str:
    """Return pane text without terminal control sequences and trailing blanks."""

    text = _ANSI_RE.sub("", screen)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip("\n")
