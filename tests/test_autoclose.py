"""Server-owned autoclose supervision: what it sends into a pane and when it closes it."""

from __future__ import annotations

import logging
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import workdash.autoclose as autoclose_module
import workdash.control as control_module
from gh_work_state_fake import (
    AFTER_PUSH,
    HEAD_SHA,
    OLD_HEAD_SHA,
    check_run,
    failure,
    install_fake_github,
    review,
)
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession
from workdash.models import WorkItem, WorkItemKind, WorkItemType


class Supervision:
    """A real session with an autoclosed launch, its fake clock, and recorded pane actions."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.github = install_fake_github(monkeypatch)
        self.github.add_issue("owner/repo#42")
        self.github.add_pull_request("owner/repo#51", closes=("owner/repo#42",))
        self.worktree = tmp_path / "owner_repo_42"
        self.worktree.mkdir()
        self.now = 0.0
        self.screen = "idle\n"
        self.sequence: list[str] = []
        self.writing = False
        self.local_work = False
        self.sent: list[tuple[str, str]] = []
        self.enters: list[str] = []
        self.enter_fails = False
        self.closed: list[str] = []
        self.panes: list[dict[str, object]] = []
        self.pane_leaves_at_decision = False
        self.unpublished_checks: list[tuple[str, str]] = []

        def unpublished(worktree: str, head_sha: str) -> bool:
            self.unpublished_checks.append((worktree, head_sha))
            return self.local_work

        monkeypatch.setattr(autoclose_module.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(
            autoclose_module, "load_zellij_panes", lambda _session: [dict(p) for p in self.panes]
        )
        monkeypatch.setattr(autoclose_module, "dump_zellij_pane", self._dump)
        monkeypatch.setattr(autoclose_module, "send_zellij_pane_input", self._type)
        monkeypatch.setattr(autoclose_module, "press_zellij_pane_enter", self._press_enter)
        monkeypatch.setattr(autoclose_module, "close_zellij_pane", self._close)
        self._answer_work_state = autoclose_module.work_pull_request
        monkeypatch.setattr(autoclose_module, "work_pull_request", self._work_state)

        monkeypatch.setattr(control_module, "load_zellij_panes", lambda _session: list(self.panes))
        monkeypatch.setattr(
            control_module, "existing_worktree_path", lambda _wd, _item, **kwargs: None
        )
        monkeypatch.setattr(
            control_module, "ensure_worktree", lambda *_args, **kwargs: str(self.worktree)
        )
        monkeypatch.setattr(
            control_module,
            "GithubHelper",
            lambda: SimpleNamespace(fetch_worktree_head=lambda item: ("pr-branch", item.repo)),
        )
        monkeypatch.setattr(
            control_module,
            "GitHelper",
            lambda: SimpleNamespace(
                branch_tracks_repository=lambda *_: True,
                find_worktree_for_branch=lambda *_: None,
                has_unpublished_work=unpublished,
            ),
        )
        monkeypatch.setattr(control_module, "get_merge_base", lambda _path, **kwargs: None)
        monkeypatch.setattr(
            control_module, "prepare_launch_agent_prompt", lambda *args, **kwargs: "PROMPT"
        )
        monkeypatch.setattr(control_module, "launch_agent_context", self._launch)
        monkeypatch.setattr(control_module, "_LAUNCH_VISIBILITY_INTERVAL_SECONDS", 0)

        self.session = WorkdashSession(
            config=WorkdashConfig(
                github_username="testuser",
                agents={"pi": AgentConfig(launch="pi")},
                workdir=str(tmp_path),
                todo_repository="testuser/todos",
            ),
            backend=MagicMock(),
            work_items=[
                _item(WorkItemKind.ASSIGNED_ISSUE, WorkItemType.ISSUE, 42),
                _item(WorkItemKind.TRACKED_PR, WorkItemType.PR, 52),
            ],
            suggestion_markers={},
            zellij_session="workdash-main",
        )

    def launch(self, target: str = "owner/repo#ISSUE-42", **flags: bool) -> dict[str, object]:
        return self.session.code(target=target, agent="pi", autostart=True, **flags)

    def poll(self, minutes: int = 1) -> None:
        """Advance the fake clock a minute at a time, checking the panes after each."""

        for _ in range(minutes):
            self.now += 60.0
            self.session.autoclose.poll_once()

    def _create_agent_pane(self, repo_path: str) -> None:
        self.panes.append(
            {"id": 21, "title": f"code_{Path(repo_path).name}", "pane_cwd": repo_path}
        )

    def _launch(self, repo_path, _prompt, agent_command_tokens=None, *, zellij_session=None):
        self._create_agent_pane(repo_path)
        return SimpleNamespace(
            session=zellij_session,
            pane_id=None,
            pane_title=f"code_{Path(repo_path).name}",
            cwd=repo_path,
        )

    def _dump(self, _session: str, _pane_id: str, *, full: bool = False) -> str:
        if self.sequence:
            return self.sequence.pop(0)
        return f"working at {self.now}\n" if self.writing else self.screen

    def _type(self, _session: str, pane_id: str, data: str, *, raw: bool = False) -> None:
        self.sent.append((pane_id, data))

    def _press_enter(self, _session: str, pane_id: str) -> None:
        self.enters.append(pane_id)
        if self.enter_fails:
            raise RuntimeError("Zellij is unavailable")

    def _close(self, _session: str, pane_id: str) -> None:
        self.closed.append(pane_id)
        self.panes = [pane for pane in self.panes if f"terminal_{pane['id']}" != pane_id]

    def _work_state(self, *args, **kwargs):
        state = self._answer_work_state(*args, **kwargs)
        if self.pane_leaves_at_decision and self.now >= autoclose_module.IDLE_SECONDS:
            # The pane is gone between the check that decided and the action itself.
            self.pane_leaves_at_decision = False
            self.panes = []
        return state


def _item(kind: WorkItemKind, item_type: WorkItemType, number: int) -> WorkItem:
    created_at = datetime(2026, 2, 1, tzinfo=UTC)
    return WorkItem(
        kind=kind,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title=f"Work {number}",
        created_at=created_at,
        updated_at=created_at,
        url=f"https://github.com/owner/repo/{'pull' if item_type is WorkItemType.PR else 'issues'}/{number}",
    )


def failing_head(supervision: Supervision, head: str = HEAD_SHA) -> None:
    """Make the supervised pull request fail its `tests` check on ``head``."""

    supervision.github.pull_requests["owner/repo#51"].update(
        {"head": head, "checks": {head: [check_run("tests", "FAILURE")]}}
    )


def test_awaiting_launch_gets_publishing_rules_when_ci_fails(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.github.pull_requests["owner/repo#51"]["checks"] = {
        HEAD_SHA: [check_run("tests", None, status="IN_PROGRESS")]
    }
    launch = MagicMock(side_effect=supervision._launch)
    monkeypatch.setattr(control_module, "launch_agent_context", launch)

    supervision.launch(autoclose=True)
    briefing = launch.call_args.args[1]
    assert "still without a final CI result" in briefing
    assert "AUTOSTART: IMPLEMENT, PUBLISH AND VERIFY" not in briefing
    supervision.poll(15)
    assert not supervision.sent

    failing_head(supervision)
    supervision.poll(5)
    instruction = supervision.sent[-1][1]
    assert "has failing checks: tests" in instruction
    assert "Commit and push are explicitly requested and permitted" in instruction
    assert "--add-assignee" not in instruction


def test_the_worker_waits_a_minute_between_checks(monkeypatch) -> None:
    supervisor = autoclose_module.AutocloseSupervisor(
        config=WorkdashConfig(), lock=threading.RLock(), git=MagicMock()
    )
    waits: list[float] = []
    checks: list[bool] = []

    class StopsOnItsThirdWait:
        def wait(self, timeout: float) -> bool:
            waits.append(timeout)
            return len(waits) == 3

    monkeypatch.setattr(supervisor, "_stopped", StopsOnItsThirdWait())
    monkeypatch.setattr(supervisor, "poll_once", lambda: checks.append(True))

    supervisor._run()

    assert waits == [60.0, 60.0, 60.0]
    assert checks == [True, True]


def test_finding_the_pull_request_of_work_without_one_runs_every_fifth_check(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.github.pull_requests.clear()
    supervision.launch(autoclose=True)
    supervision.writing = True

    supervision.poll()
    supervision.github.operations.clear()
    supervision.poll(4)
    assert supervision.github.operations == []

    supervision.poll()
    assert supervision.github.operations


def test_a_known_pull_request_is_only_asked_for_its_state_on_each_check(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.launch(autoclose=True)
    supervision.writing = True
    supervision.poll()

    supervision.github.operations.clear()
    supervision.poll(5)
    assert supervision.github.operations == ["PullRequestStates"] * 5


def test_replacing_session_git_changes_the_unpublished_work_check(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.launch(autoclose=True)
    replacement = MagicMock()
    replacement.has_unpublished_work.return_value = True
    supervision.session.git = replacement
    assert supervision.session.__dict__["_git"] is replacement

    supervision.poll(15)

    replacement.has_unpublished_work.assert_called_once_with(str(supervision.worktree), HEAD_SHA)
    assert supervision.closed == []


def test_an_idle_pane_resolves_its_work_state_every_fifth_check(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.local_work = True
    supervision.launch(autoclose=True)

    # Finished work held open by unpublished files is decided again every fifth check, so
    # the four checks between them only ask the known pull request for its state.
    supervision.poll(15)
    # The worktree and the head GitHub reported are what the unpublished work is judged against.
    assert supervision.unpublished_checks == [(str(supervision.worktree), HEAD_SHA)]
    supervision.github.operations.clear()
    supervision.poll(4)
    assert supervision.github.operations == ["PullRequestStates"] * 4

    supervision.poll()
    assert "ReadyPullRequests" in supervision.github.operations


def test_a_pull_request_that_merged_before_the_launch_does_not_close_the_pane(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    merged_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # The issue already has the user's merged pull request, which is not this pane's work.
    supervision.github.pull_requests["owner/repo#51"].update(
        {"state": "MERGED", "merged": merged_at}
    )
    supervision.launch(autoclose=True)
    # A comment after the launch makes the pull request look newer, but it merged long before.
    supervision.github.pull_requests["owner/repo#51"]["updated"] = datetime.now(UTC).isoformat()

    supervision.poll(15)

    assert (supervision.sent, supervision.closed) == ([], [])
    # The pane is still checked, so it is still supervised.
    supervision.github.operations.clear()
    supervision.poll(10)
    assert supervision.github.operations


def test_work_that_merges_inside_the_discovery_gap_closes_its_pane(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.github.pull_requests.clear()
    supervision.launch(autoclose=True)

    # The first check finds no pull request yet, then discovery waits five minutes.
    supervision.poll()
    supervision.github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#42",),
        state="MERGED",
        merged=datetime.now(UTC).isoformat(),
    )
    supervision.poll(5)

    assert supervision.closed == ["terminal_21"]
    assert supervision.sent == []


def test_a_message_resets_the_timer_and_unchanged_work_is_never_messaged_twice(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    supervision.poll(15)
    assert len(supervision.sent) == 1

    # A new head a minute later is new work, but the message restarted the wait: it is
    # instructed only once fifteen quiet minutes have passed since that message.
    failing_head(supervision, head=OLD_HEAD_SHA)
    supervision.poll()
    assert len(supervision.sent) == 1
    supervision.poll(13)
    assert len(supervision.sent) == 1
    supervision.poll()
    assert len(supervision.sent) == 2
    assert OLD_HEAD_SHA in supervision.sent[1][1]

    # The same work is never messaged twice, however long the pane stays idle.
    supervision.poll(15)
    assert len(supervision.sent) == 2


def test_a_new_failing_check_or_another_review_is_messaged_again(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    supervision.poll(15)
    assert len(supervision.sent) == 1

    # A check that newly failed keeps the action at fix_ci but is a new state.
    supervision.github.pull_requests["owner/repo#51"]["checks"][HEAD_SHA].append(
        check_run("lint", "FAILURE")
    )
    supervision.poll(15)
    assert len(supervision.sent) == 2
    assert "lint" in supervision.sent[1][1]

    # So is a review joining the feedback the pane was already told to address.
    supervision.github.pull_requests["owner/repo#51"]["reviews"].append(
        review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH)
    )
    supervision.poll(15)
    assert len(supervision.sent) == 3
    supervision.github.pull_requests["owner/repo#51"]["reviews"].append(
        review("reviewer", "COMMENTED", created=AFTER_PUSH, body="Also rename the helper")
    )
    supervision.poll(15)
    assert len(supervision.sent) == 4


def test_a_screen_that_changes_after_the_idle_check_cancels_the_message(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    supervision.poll(14)

    # The recheck immediately before sending sees the agent writing again.
    supervision.sequence = ["idle\n", "a new line\n"]
    supervision.poll()
    assert supervision.sent == []

    supervision.screen = "a new line\n"  # the agent goes quiet on what it printed
    supervision.poll(15)
    assert len(supervision.sent) == 1


def test_finished_work_is_not_closed_while_its_screen_changes(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.launch(autoclose=True)
    supervision.poll(14)

    # The recheck immediately before closing sees the agent writing again.
    supervision.sequence = ["idle\n", "still working\n"]
    supervision.poll()
    assert supervision.closed == []

    supervision.screen = "still working\n"  # the agent goes quiet on what it printed
    supervision.poll(15)
    assert supervision.closed == ["terminal_21"]


def test_an_instruction_is_not_typed_into_a_pane_that_left_before_the_action(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    supervision.pane_leaves_at_decision = True

    supervision.poll(15)

    assert (supervision.sent, supervision.enters, supervision.closed) == ([], [], [])


def test_finished_work_is_not_closed_when_the_pane_left_before_the_action(
    monkeypatch, tmp_path
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    supervision.launch(autoclose=True)
    supervision.pane_leaves_at_decision = True

    supervision.poll(15)

    assert (supervision.sent, supervision.enters, supervision.closed) == ([], [], [])


def test_an_instruction_whose_enter_failed_is_not_typed_again(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.enter_fails = True
    supervision.launch(autoclose=True)

    supervision.poll(15)
    assert len(supervision.sent) == 1

    # The text reached the pane, so the next check only submits it.
    supervision.enter_fails = False
    supervision.poll(3)
    assert (len(supervision.sent), supervision.enters) == (1, ["terminal_21"] * 2)


def test_a_submit_is_not_repeated_into_a_prompt_that_since_appeared(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.enter_fails = True
    supervision.launch(autoclose=True)
    supervision.poll(15)

    # The agent answered on its own, so its Enter would land on whatever it now asks.
    supervision.enter_fails = False
    supervision.screen = "Overwrite the branch? [y/N]"
    supervision.poll()
    assert supervision.enters == ["terminal_21"]

    # The abandoned instruction is not typed again either.
    supervision.poll(15)
    assert (len(supervision.sent), len(supervision.enters)) == (1, 1)


def test_a_failed_github_lookup_backs_off_and_never_touches_the_pane(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    supervision.github.failures["PullRequestStates"] = failure("HTTP 502: Bad Gateway")

    supervision.poll()
    assert (supervision.sent, supervision.closed) == ([], [])

    # Two minutes of backoff after the first failure, then five after the second.
    supervision.github.operations.clear()
    supervision.poll()
    assert supervision.github.operations == []
    supervision.poll()
    supervision.github.operations.clear()
    supervision.poll(4)
    assert supervision.github.operations == []

    del supervision.github.failures["PullRequestStates"]
    supervision.poll(8)
    assert len(supervision.sent) == 1


def test_a_stall_is_reported_once_and_a_new_one_after_a_recovery(
    monkeypatch, tmp_path, caplog
) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    supervision.github.failures["PullRequestStates"] = failure("HTTP 502: Bad Gateway")
    caplog.set_level(logging.WARNING, logger=autoclose_module.logger.name)

    # An hour of failures reaches the longest retry and keeps failing there.
    supervision.poll(60)
    assert caplog.text.count("is stalled") == 1

    # A check that succeeds ends the outage, so the next one is reported on its own.
    del supervision.github.failures["PullRequestStates"]
    supervision.poll(30)
    supervision.github.failures["PullRequestStates"] = failure("HTTP 502: Bad Gateway")
    supervision.poll(60)

    assert caplog.text.count("is stalled") == 2


def test_a_stopped_supervisor_acts_on_no_pane(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)

    # The control server stops the worker when it shuts down.
    supervision.session.autoclose.stop()
    supervision.poll(15)

    assert (supervision.sent, supervision.closed) == ([], [])


def test_github_is_read_outside_the_session_lock(monkeypatch, tmp_path) -> None:
    supervision = Supervision(monkeypatch, tmp_path)
    failing_head(supervision)
    supervision.launch(autoclose=True)
    lock_states: list[bool] = []
    run_github = supervision.github.run

    def spy(command, *args, **kwargs):
        lock_states.append(supervision.session._lock._is_owned())
        return run_github(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    supervision.poll(15)

    assert lock_states and not any(lock_states)


def test_a_screen_signature_ignores_control_sequences_and_trailing_blanks() -> None:
    styled = "\x1b[32mworking\x1b[0m   \r\n\x1b]0;title\x07\nidle   \n\n\n"

    assert autoclose_module._screen_signature(styled) == "working\n\nidle"
