"""Step definitions for autoclose supervision scenarios.

The scenarios drive a real ``WorkdashSession`` and its supervisor with fake
GitHub, Zellij, worktree, and clock boundaries. The CLI journeys also run the
real command against that session without opening a network connection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.autoclose as autoclose_module
import workdash.control as control_module
import workdash.workdash as workdash_module
from gh_work_state_fake import AFTER_PUSH, HEAD_SHA, check_run, review
from workdash.models import WorkItem

from .api import _call_api
from .common import install_zellij_agent_fakes

# The phrase each instructed action must contain to prove the right work was sent.
INSTRUCTION_PHRASES = {
    "address_review": "has actionable review feedback",
    "fix_ci": "has failing checks: tests",
}


def _install_supervision(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fake the boundaries of an autoclosed launch and of the supervisor's own checks."""

    install_zellij_agent_fakes(scenario_state, monkeypatch)
    worktree = tmp_path / "owner_repo_42"
    worktree.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda *_, **_kwargs: str(worktree))
    monkeypatch.setattr(
        "workdash.github_client.GitHubClient.fetch_worktree_head",
        lambda self, item: ("pr-branch", item.repo),
    )
    monkeypatch.setattr(
        control_module,
        "GitHelper",
        lambda: SimpleNamespace(
            branch_tracks_repository=lambda *_: True,
            find_worktree_for_branch=lambda *_: None,
            has_unpublished_work=lambda *_: False,
        ),
    )
    if "api_session" in scenario_state:
        scenario_state["api_session"].git = control_module.GitHelper()
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *_, **_kwargs: None)
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path, **kwargs: None)
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
        autoclose_module.launcher.zellij,
        "load_zellij_panes",
        lambda _session: [dict(p) for p in panes],
    )
    monkeypatch.setattr(autoclose_module.launcher.zellij, "dump_zellij_pane", screen)
    monkeypatch.setattr(autoclose_module.launcher.zellij, "send_zellij_pane_input", send)
    monkeypatch.setattr(autoclose_module.launcher.zellij, "press_zellij_pane_enter", press_enter)
    monkeypatch.setattr(autoclose_module.launcher.zellij, "close_zellij_pane", close)
    # A fake clock makes minute-by-minute checks instant and deterministic.
    monkeypatch.setattr(autoclose_module.time, "monotonic", lambda: clock["now"])


def _supervised_coding_pane_without_pull_request(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario_state["github"].add_issue("owner/repo#42")
    _install_supervision(scenario_state, monkeypatch, tmp_path)
    _call_api(
        scenario_state,
        work_items,
        tmp_path,
        "code",
        {"target": "owner/repo#ISSUE-42", "agent": "pi", "autostart": True, "autoclose": True},
    )
    result = scenario_state["api_payload"]["result"]
    assert result["autoclose_enabled"] is True
    scenario_state["supervised_pane_id"] = result["pane_id"]


@when(parsers.parse("pull request `{ref}` develops a failing check `{check}`"))
def _failing_check(ref: str, check: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["checks"] = {
        HEAD_SHA: [check_run(check, "FAILURE")]
    }


@when(parsers.parse("a reviewer requests changes on pull request `{ref}`"))
def _reviewer_requests_changes(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["github"].pull_requests[ref]["reviews"].append(
        review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH)
    )


@given(parsers.parse("issue `{issue}` has an open pull request `{pull_request}`"))
@given(
    parsers.parse(
        "issue `{issue}` has an open pull request `{pull_request}` ready to merge with all changes published"
    )
)
def _issue_has_pull_request(issue: str, pull_request: str, scenario_state: dict[str, Any]) -> None:
    github = scenario_state["github"]
    github.add_issue(issue)
    github.add_pull_request(pull_request, closes=(issue,))


@when(parsers.parse("the user runs `workdash code {item_id} --agent pi --autoclose`"))
def _run_code_with_autoclose(
    item_id: str,
    scenario_state: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _install_supervision(scenario_state, monkeypatch, tmp_path)
    session = scenario_state["api_session"]

    def request(endpoint, payload):
        assert endpoint == "code"
        return session.code(**payload)

    client = SimpleNamespace(request=request)
    monkeypatch.setattr(workdash_module, "WorkdashControlClient", lambda *_: client)
    assert workdash_module.main(["code", item_id, "--agent", "pi", "--autoclose"]) == 0
    scenario_state["stdout"] = capsys.readouterr().out
    [pane] = scenario_state["panes"]
    scenario_state["supervised_pane_id"] = f"terminal_{pane['id']}"


@then("Workdash reports the new agent pane with autoclose enabled")
def _reports_autoclose_launch(scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["stdout"].splitlines()
    assert "Status: launched" in lines
    assert f"Pane id: {scenario_state['supervised_pane_id']}" in lines
    assert "Autoclose: enabled" in lines


@when("the agent goes idle")
def _agent_goes_idle(scenario_state: dict[str, Any]) -> None:
    for _ in range(10):
        scenario_state["clock"]["now"] += 30
        scenario_state["api_session"].autoclose.poll_once()


@when(parsers.parse("pull request `{ref}` merges while the agent is still producing output"))
def _active_pane_merges(ref: str, scenario_state: dict[str, Any]) -> None:
    scenario_state["screen_keeps_changing"] = True
    supervisor = scenario_state["api_session"].autoclose
    supervisor.poll_once()
    scenario_state["github"].pull_requests[ref].update(
        state="MERGED", merged=datetime.now(UTC).isoformat()
    )
    scenario_state["clock"]["now"] += 30
    supervisor.poll_once()


@then(parsers.parse("the agent receives a message to {task}"))
def _agent_receives_message(task: str, scenario_state: dict[str, Any]) -> None:
    action = {
        "fix the failing checks": "fix_ci",
        "address the review feedback": "address_review",
    }[task]
    pane_id, instruction = scenario_state["sent"][-1]
    assert pane_id == scenario_state["supervised_pane_id"]
    assert INSTRUCTION_PHRASES[action] in instruction
    assert scenario_state["submitted"][-1] == pane_id


@then("the supervised pane is closed")
def _supervised_pane_closed(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("closed") == [scenario_state["supervised_pane_id"]]
