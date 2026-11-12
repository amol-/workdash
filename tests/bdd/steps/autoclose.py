"""Step definitions for autoclose supervision scenarios.

The scenarios drive a real ``WorkdashSession`` and its supervisor: only ``gh``,
Zellij, the worktree files, and the clock are faked, so a scenario proves what
the server sends into a pane and when it closes it.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.autoclose as autoclose_module
import workdash.control as control_module
from gh_work_state_fake import AFTER_PUSH, HEAD_SHA, check_run, review
from workdash.models import WorkItem

from .api import _call_api
from .common import install_zellij_agent_fakes

# The phrase each instructed action must contain to prove the right work was sent.
INSTRUCTION_PHRASES = {
    "address_review": "has actionable review feedback",
    "fix_ci": "has failing checks: tests",
    "fix_merge_conflicts": "cannot merge cleanly into main",
    "implement": "Continue the assigned issue",
}


def _install_supervision(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fake the boundaries of an autoclosed launch and of the supervisor's own checks."""

    install_zellij_agent_fakes(scenario_state, monkeypatch)
    worktree = tmp_path / "owner_repo_42"
    worktree.mkdir(parents=True, exist_ok=True)
    existing = scenario_state.get("existing_worktree")
    monkeypatch.setattr(control_module, "ensure_worktree", lambda *_, **_kwargs: str(worktree))
    monkeypatch.setattr(
        "workdash.github_client.GitHubClient.fetch_worktree_head",
        lambda self, item: ("pr-branch", item.repo),
    )
    monkeypatch.setattr(
        control_module,
        "GitHelper",
        lambda: SimpleNamespace(
            branch_tracks_repository=lambda *_: True, find_worktree_for_branch=lambda *_: None
        ),
    )
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *_: existing)
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(control_module, "prepare_launch_agent_prompt", lambda *a, **k: "PROMPT")

    clock = scenario_state.setdefault("clock", {"now": 0.0})
    panes = scenario_state.setdefault("panes", [])

    def screen(_session: str, _pane_id: str, *, full: bool = False) -> str:
        if not scenario_state.get("screen_keeps_changing"):
            return scenario_state.get("screen", "idle\n")
        counter = scenario_state.get("dumps", 0) + 1
        scenario_state["dumps"] = counter
        return f"output line {counter}\n"

    def send(_session: str, pane_id: str, data: str, *, raw: bool = False) -> None:
        scenario_state.setdefault("sent", []).append((pane_id, data))

    def press_enter(_session: str, pane_id: str) -> None:
        scenario_state.setdefault("submitted", []).append(pane_id)

    def close(_session: str, pane_id: str) -> None:
        scenario_state.setdefault("closed", []).append(pane_id)
        panes[:] = [pane for pane in panes if f"terminal_{pane['id']}" != pane_id]

    monkeypatch.setattr(
        autoclose_module, "load_zellij_panes", lambda _session: [dict(p) for p in panes]
    )
    monkeypatch.setattr(autoclose_module, "dump_zellij_pane", screen)
    monkeypatch.setattr(autoclose_module, "send_zellij_pane_input", send)
    monkeypatch.setattr(autoclose_module, "press_zellij_pane_enter", press_enter)
    monkeypatch.setattr(autoclose_module, "close_zellij_pane", close)
    monkeypatch.setattr(
        autoclose_module,
        "has_unpublished_work",
        lambda *_: scenario_state.get("unpublished_local_work", False),
    )
    # A fake clock makes minute-by-minute checks instant and deterministic.
    monkeypatch.setattr(autoclose_module.time, "monotonic", lambda: clock["now"])


def _request_code(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    item_id: str,
    **flags: bool,
) -> None:
    _call_api(
        scenario_state,
        work_items,
        tmp_path,
        "code",
        {"target": item_id, "agent": "pi", "autostart": True, **flags},
    )
    if scenario_state["api_status"] == 200:
        scenario_state["supervised_pane_id"] = scenario_state["api_payload"]["result"]["pane_id"]


def _supervise_new_pane(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    item_id: str,
) -> None:
    """Fake the boundaries, request autoclosed code, and prove it supervises the new pane."""

    # Supervision reports what it cannot act on, which scenarios observe in the log.
    caplog.set_level(logging.INFO, logger=autoclose_module.logger.name)
    _install_supervision(scenario_state, monkeypatch, tmp_path)
    _request_code(scenario_state, work_items, tmp_path, item_id, autoclose=True)
    assert scenario_state["api_payload"]["result"]["autoclose_enabled"] is True


@given(
    parsers.re(
        r"a supervised coding pane for issue `(?P<issue>[^`]+)` "
        r"whose pull request `(?P<pull_request>[^`]+)` is open"
    )
)
def _supervised_coding_pane(
    issue: str,
    pull_request: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, _, number = issue.partition("#")
    github = scenario_state["github"]
    github.add_issue(f"{repo}#{number}")
    github.add_pull_request(pull_request, closes=(f"{repo}#{number}",))
    _supervise_new_pane(
        scenario_state, work_items, tmp_path, monkeypatch, caplog, f"{repo}#ISSUE-{number}"
    )


@given(
    parsers.re(
        r"a supervised coding pane for issue `(?P<issue>[^`]+)` "
        r"whose pull request `(?P<pull_request>[^`]+)` merged before the launch"
    )
)
def _supervised_coding_pane_with_a_merged_pull_request(
    issue: str,
    pull_request: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, _, number = issue.partition("#")
    github = scenario_state["github"]
    github.add_issue(f"{repo}#{number}")
    github.add_pull_request(
        pull_request,
        closes=(f"{repo}#{number}",),
        state="MERGED",
        merged=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    )
    _supervise_new_pane(
        scenario_state, work_items, tmp_path, monkeypatch, caplog, f"{repo}#ISSUE-{number}"
    )


@given(parsers.parse("a supervised coding pane for issue `{issue}` with no pull request yet"))
def _supervised_coding_pane_without_pull_request(
    issue: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo, _, number = issue.partition("#")
    scenario_state["github"].add_issue(f"{repo}#{number}")
    _supervise_new_pane(
        scenario_state, work_items, tmp_path, monkeypatch, caplog, f"{repo}#ISSUE-{number}"
    )


@given(parsers.parse("a supervised coding pane for pull request `{item_id}`"))
def _supervised_pull_request_pane(
    item_id: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _supervise_new_pane(scenario_state, work_items, tmp_path, monkeypatch, caplog, item_id)


@given(
    parsers.parse("an open pull request `{ref}` by somebody else with a failing check `{check}`")
)
def _somebody_elses_pull_request(ref: str, check: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].add_pull_request(
        ref, author="someone-else", checks={HEAD_SHA: [check_run(check, "FAILURE")]}
    )


@given(parsers.parse("pull request `{ref}` has a failing check `{check}` on its current head"))
def _failing_check(ref: str, check: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["checks"] = {
        HEAD_SHA: [check_run(check, "FAILURE")]
    }


@given(parsers.parse("pull request `{ref}` has merge conflicts with its base branch"))
@when(parsers.parse("pull request `{ref}` has merge conflicts with its base branch"))
def _merge_conflicts(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["mergeable"] = "CONFLICTING"


@given(parsers.parse("pull request `{ref}` has passing CI, no review feedback, and merges cleanly"))
def _finished_pull_request(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref].update(
        {
            "checks": {HEAD_SHA: [check_run("tests")]},
            "mergeable": "MERGEABLE",
            "reviews": [],
            "threads": [],
            "comments": [],
        }
    )


@given(parsers.parse("pull request `{ref}` has no checks, no review feedback, and merges cleanly"))
def _pull_request_without_checks(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref].update(
        {"checks": {}, "mergeable": "MERGEABLE", "reviews": [], "threads": [], "comments": []}
    )


@given(parsers.parse("pull request `{ref}` is {condition}"))
@when(parsers.parse("pull request `{ref}` is {condition}"))
def _pull_request_condition(ref: str, condition: str, scenario_state: dict[str, Any]) -> None:
    pull_request = scenario_state["github"].pull_requests[ref]
    if condition == "waiting on running checks":
        pull_request["checks"] = {HEAD_SHA: [check_run("tests", None, status="IN_PROGRESS")]}
    elif condition == "closed without being merged":
        pull_request["state"] = "CLOSED"
    elif condition == "reporting unknown mergeability":
        pull_request["mergeable"] = "UNKNOWN"
    else:
        raise AssertionError(f"Unknown pull request condition {condition!r}")


@given("the supervised pane keeps printing new output")
def _pane_keeps_printing(scenario_state: dict[str, Any]) -> None:
    scenario_state["screen_keeps_changing"] = True


@when("the supervised pane prints new output")
def _pane_prints_output(scenario_state: dict[str, Any]) -> None:
    scenario_state["screen"] = "a new line\n"


@given("the supervised worktree has uncommitted changes")
def _worktree_has_local_work(scenario_state: dict[str, Any]) -> None:
    scenario_state["unpublished_local_work"] = True


@given("another program takes over the supervised pane")
def _pane_title_changes(scenario_state: dict[str, Any]) -> None:
    [pane] = scenario_state["panes"]
    scenario_state["pane_title"] = pane["title"]
    pane["title"] = "vim"


@when("the other program gives the pane back")
def _pane_title_restored(scenario_state: dict[str, Any]) -> None:
    [pane] = scenario_state["panes"]
    pane["title"] = scenario_state["pane_title"]


@when(parsers.parse("the base branch of pull request `{ref}` moves"))
def _base_branch_moves(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["base_sha"] = "base2"


@when(parsers.parse("pull request `{ref}` merges"))
def _pull_request_merges(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["state"] = "MERGED"


@when(parsers.parse("pull request `{ref}` opens and merges for issue `{issue}`"))
def _pull_request_opens_and_merges(ref: str, issue: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].add_pull_request(
        ref, closes=(issue,), state="MERGED", merged=datetime.now(UTC).isoformat()
    )


@when(parsers.parse("a reviewer requests changes on pull request `{ref}`"))
def _reviewer_requests_changes(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["reviews"].append(
        review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH)
    )


@when(parsers.re(r"(?:(?:another|one more|a) minute|(?P<minutes>\d+) minutes) pass(?:es)?"))
def _minutes_pass(minutes: str | None, scenario_state: dict[str, Any]) -> None:
    clock = scenario_state.setdefault("clock", {"now": 0.0})
    for _ in range(int(minutes or 1)):
        clock["now"] += 60.0
        scenario_state["api_session"].autoclose.poll_once()


@when(
    parsers.parse(
        "a client requests autoclosed code with autostart `{autostart}` "
        "for `{item_id}` with agent `{agent}`"
    )
)
def _client_requests_autoclosed_code(
    autostart: str,
    item_id: str,
    agent: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_supervision(scenario_state, monkeypatch, tmp_path)
    _call_api(
        scenario_state,
        work_items,
        tmp_path,
        "code",
        {"target": item_id, "agent": agent, "autostart": autostart == "true", "autoclose": True},
    )


@then(parsers.parse("the response reports the new agent pane as `{status}`"))
def _response_reports_new_pane(status: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_payload"]["result"]["status"] == status


@then(parsers.parse("the response reports monitoring `{monitoring}`"))
def _response_reports_monitoring(monitoring: str, scenario_state: dict[str, Any]) -> None:
    enabled = scenario_state["api_payload"]["result"].get("autoclose_enabled")
    assert bool(enabled) == (monitoring == "enabled")


@then(parsers.parse("the supervised pane receives the `{action}` instruction"))
def _pane_receives_instruction(action: str, scenario_state: dict[str, Any]) -> None:
    pane_id, instruction = scenario_state["sent"][-1]
    assert pane_id == scenario_state["supervised_pane_id"]
    assert INSTRUCTION_PHRASES[action] in instruction
    # The text is followed by its Enter, so the agent reads it and starts working.
    assert scenario_state["submitted"][-1] == pane_id
    scenario_state["sent_count"] = len(scenario_state["sent"])


@then("the instruction permits publishing")
def _instruction_permits_publishing(scenario_state: dict[str, Any]) -> None:
    instruction = scenario_state["sent"][-1][1]
    assert "Commit and push are explicitly requested and permitted" in instruction
    assert "--add-assignee" not in instruction


@then(parsers.parse("the supervised pane receives the `{action}` instruction again"))
def _pane_receives_instruction_again(action: str, scenario_state: dict[str, Any]) -> None:
    sent_count = scenario_state["sent_count"]
    _pane_receives_instruction(action, scenario_state)
    assert len(scenario_state["sent"]) == sent_count + 1
    assert scenario_state["sent"][-1][1] == scenario_state["sent"][-2][1]


@then("nothing is sent to the supervised pane")
def _nothing_sent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("sent", []) == []


@then("nothing more is sent to the supervised pane")
def _nothing_more_sent(scenario_state: dict[str, Any]) -> None:
    assert len(scenario_state.get("sent", [])) == scenario_state["sent_count"]


@then("the supervised pane is closed")
def _supervised_pane_closed(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("closed") == [scenario_state["supervised_pane_id"]]


@then("no pane is closed")
def _no_pane_closed(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("closed", []) == []


@then("the server reports the pane as needing review")
def _server_reports_needs_review(caplog: pytest.LogCaptureFixture) -> None:
    assert "needs review" in caplog.text


@then("the server reports the pane as waiting for unpublished work")
def _server_reports_unpublished_work(caplog: pytest.LogCaptureFixture) -> None:
    assert "unpublished work" in caplog.text
