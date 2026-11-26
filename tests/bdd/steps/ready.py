"""Step definitions for the ready work feature scenarios."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from gh_work_state_fake import (
    AFTER_PUSH,
    BEFORE_PUSH,
    HEAD_SHA,
    OLD_HEAD_SHA,
    FakeGitHub,
    check_run,
    comment,
    install_fake_github,
    review,
    thread,
    todo_body,
)
from workdash.models import WorkItem

from .cli_orchestration import _run_workdash
from .common import ensure_api_session, make_work_item

REVIEWER = "reviewer"
NEW_HEAD_SHA = "c0ffee3"
# Checks reported on a head for each CI status; `unknown` reports a passing check
# while the base branch requires another one that never reported.
_CI_CHECKS = {
    "passing": [check_run("tests")],
    "failing": [check_run("tests", "FAILURE")],
    "pending": [check_run("tests", None, status="IN_PROGRESS")],
    "no_checks": None,
    "unknown": [check_run("tests")],
}
_MERGEABLE = {"mergeable": "MERGEABLE", "conflicting": "CONFLICTING", "unknown": "UNKNOWN"}
# Feedback phrase -> pull request field and the entry GitHub reports in it.
_FEEDBACK = {
    "a reviewer requested changes": (
        "reviews",
        lambda: review(
            REVIEWER,
            "CHANGES_REQUESTED",
            created=BEFORE_PUSH,
            commit=OLD_HEAD_SHA,
            body="Please rework this.",
        ),
    ),
    "a reviewer comment posted after the current head was pushed": (
        "comments",
        lambda: comment(REVIEWER, created=AFTER_PUSH),
    ),
    "a changes-requested review on an older head": (
        "reviews",
        lambda: review(
            REVIEWER,
            "CHANGES_REQUESTED",
            created=BEFORE_PUSH,
            commit=OLD_HEAD_SHA,
            body="Please rework this.",
        ),
    ),
    "an outdated review thread": (
        "threads",
        lambda: thread(comment(REVIEWER, created=AFTER_PUSH), outdated=True),
    ),
}


def _github(scenario_state: dict[str, Any]) -> FakeGitHub:
    return scenario_state["github"]


def _pull_request(scenario_state: dict[str, Any], number: int = 51) -> dict:
    return _github(scenario_state).pull_requests[f"owner/repo#{number}"]


def _set_ci(scenario_state: dict[str, Any], pull_request: dict, ci: str) -> None:
    checks = _CI_CHECKS[ci]
    pull_request["checks"] = {} if checks is None else {pull_request["head"]: checks}
    if ci == "unknown":
        _github(scenario_state).ruleset_checks[("owner/repo", "main")] = ["build"]


# -- Given -------------------------------------------------------------------


@given("a server-backed Workdash session is ready to answer")
def _ready_session(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_items[:] = [make_work_item(repo="elsewhere/dashboard", number=900)]
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    Path(session.config.workdir).mkdir(parents=True, exist_ok=True)
    scenario_state["github"] = install_fake_github(monkeypatch)
    scenario_state["github_lookup_installed"] = True


@given("no Workdash server is running")
def _no_workdash_server(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import workdash.control as control_module

    def refuse(request, timeout=None):
        scenario_state.setdefault("urlopen_requests", []).append(request)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(control_module.urllib.request, "urlopen", refuse)


@given(
    parsers.parse(
        "GitHub repository `{repo}` has open issues #{first:d} and #{second:d} "
        "and closed issue #{closed:d}"
    )
)
def _repository_with_open_and_closed_issues(
    scenario_state: dict[str, Any], repo: str, first: int, second: int, closed: int
) -> None:
    github = _github(scenario_state)
    github.add_issue(f"{repo}#{first}")
    github.add_issue(f"{repo}#{second}")
    github.add_issue(f"{repo}#{closed}", state="CLOSED")


@given("the dashboard lists none of them")
def _dashboard_lists_none(scenario_state: dict[str, Any]) -> None:
    listed = {(item.repo, item.number) for item in scenario_state["api_session"].work_items}
    assert not listed & {("owner/repo", number) for number in (1, 2, 3)}


@given(
    parsers.parse(
        "GitHub issue `{parent}` is {state} with open sub-issue #{open_child:d} "
        "and closed sub-issue #{closed_child:d}"
    )
)
def _issue_with_sub_issues(
    scenario_state: dict[str, Any], parent: str, state: str, open_child: int, closed_child: int
) -> None:
    github = _github(scenario_state)
    repo = parent.partition("#")[0]
    github.add_issue(parent, state=state.upper())
    github.add_issue(f"{repo}#{open_child}", parent=parent)
    github.add_issue(f"{repo}#{closed_child}", parent=parent, state="CLOSED")


@given(parsers.parse("GitHub repository `{repo}` has open issue #{number:d}"))
def _repository_with_open_issue(scenario_state: dict[str, Any], repo: str, number: int) -> None:
    _github(scenario_state).add_issue(f"{repo}#{number}")


@given(parsers.parse("GitHub repository `{repo}` has no open issues or pull requests"))
def _repository_without_work(scenario_state: dict[str, Any], repo: str) -> None:
    github = _github(scenario_state)
    assert not [ref for ref in (*github.issues, *github.pull_requests) if ref.startswith(repo)]


@given(
    parsers.parse(
        "the server's todo repository `{repo}` has open todo #{targeted:d} targeting `{target}` "
        "and open todo #{plain:d} without a target"
    )
)
def _todos_with_and_without_target(
    scenario_state: dict[str, Any], repo: str, targeted: int, target: str, plain: int
) -> None:
    github = _github(scenario_state)
    github.add_issue(f"{repo}#{targeted}", body=todo_body(target))
    github.add_issue(f"{repo}#{plain}", body=todo_body(None))


@given(
    parsers.parse(
        "the server's todo repository `{repo}` has open todo #{number:d} closed by "
        "authored pull request `{pull_request}` whose CI failed"
    )
)
def _todo_closed_by_failing_pull_request(
    scenario_state: dict[str, Any], repo: str, number: int, pull_request: str
) -> None:
    github = _github(scenario_state)
    github.add_issue(f"{repo}#{number}", body=todo_body(pull_request.partition("#")[0]))
    github.add_pull_request(
        pull_request,
        closes=(f"{repo}#{number}",),
        checks={HEAD_SHA: _CI_CHECKS["failing"]},
    )


@given(parsers.parse("the server tracks repository selectors `{first}` and `{second}`"))
def _server_tracks_selectors(scenario_state: dict[str, Any], first: str, second: str) -> None:
    session = scenario_state["api_session"]
    session.config = replace(session.config, repositories=(first, second))


@given(
    parsers.parse(
        "GitHub owner `{owner}` has repositories `{first}` and `{second}`, "
        "each with one open leaf issue"
    )
)
def _owner_repositories(
    scenario_state: dict[str, Any], owner: str, first: str, second: str
) -> None:
    github = _github(scenario_state)
    github.owners[owner] = [first, second]
    github.add_issue(f"{first}#1")
    github.add_issue(f"{second}#1")


@given(
    parsers.re(
        r"GitHub issue `(?P<issue>[^`]+)` is open and closed by (?:authored )?open pull request "
        r"`(?P<pull_request>[^`]+)`(?P<other> of another author)?(?P<failed> whose CI failed)?"
    )
)
def _issue_closed_by_pull_request(
    scenario_state: dict[str, Any],
    issue: str,
    pull_request: str,
    other: str | None,
    failed: str | None,
) -> None:
    github = _github(scenario_state)
    github.add_issue(issue)
    added = github.add_pull_request(
        pull_request, closes=(issue,), author="someone-else" if other else "testuser"
    )
    if failed:
        _set_ci(scenario_state, added, "failing")


@given(parsers.parse("pull request #51 has {ci} CI, {mergeability} mergeability, and no reviews"))
def _pull_request_waiting(scenario_state: dict[str, Any], ci: str, mergeability: str) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, ci)
    pull_request["mergeable"] = _MERGEABLE[mergeability]


@given("check `tests` failed on the current head of pull request #51, which has no reviews")
def _check_failed_on_head(scenario_state: dict[str, Any]) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, "failing")
    assert not pull_request["reviews"]


@when("that reviewer is asked to review pull request #51 again")
def _request_review_again(scenario_state: dict[str, Any]) -> None:
    _pull_request(scenario_state)["review_requests"].append(REVIEWER)


@given(parsers.re(r"pull request #51 has passing CI and (?P<feedback>.+)"))
def _pull_request_with_feedback(scenario_state: dict[str, Any], feedback: str) -> None:
    field, build = _FEEDBACK[feedback]
    _pull_request(scenario_state)[field].append(build())


@given("pull request #51 has merge conflicts, failed CI, and new review feedback")
def _pull_request_with_problems(scenario_state: dict[str, Any]) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, "failing")
    pull_request["mergeable"] = "CONFLICTING"
    pull_request["threads"].append(thread(comment(REVIEWER, created=AFTER_PUSH)))


@given(
    parsers.parse(
        "open pull request `{ref}` comes from a fork the server cannot push to and its CI failed"
    )
)
def _read_only_fork(scenario_state: dict[str, Any], ref: str) -> None:
    _github(scenario_state).add_pull_request(
        ref, head_permission="READ", checks={HEAD_SHA: _CI_CHECKS["failing"]}
    )


@given(
    parsers.parse("open pull request `{ref}` of another author closes no issue and its CI failed")
)
def _unlinked_failing_pull_request(scenario_state: dict[str, Any], ref: str) -> None:
    _github(scenario_state).add_pull_request(
        ref, author="someone-else", checks={HEAD_SHA: _CI_CHECKS["failing"]}
    )


@given(parsers.parse("GitHub repository `{repo}` has these ready items:"))
def _repository_with_ready_items(
    scenario_state: dict[str, Any], repo: str, datatable: list[list[str]]
) -> None:
    github = _github(scenario_state)
    for item, created, pr_created in datatable[1:]:
        number = int(item.split()[1])
        ref = f"{repo}#{number}"
        github.add_issue(ref, created=f"{created}T00:00:00Z")
        if "actionable" in item:
            github.add_pull_request(
                f"{repo}#{number + 100}",
                closes=(ref,),
                created=f"{pr_created}T00:00:00Z",
                checks={HEAD_SHA: _CI_CHECKS["failing"]},
            )


@given(parsers.parse("GitHub issue `{root}` is open with these sub-issues:"))
def _issue_tree(scenario_state: dict[str, Any], root: str, datatable: list[list[str]]) -> None:
    github = _github(scenario_state)
    github.add_issue(root)
    for ref, parent, state in datatable[1:]:
        github.add_issue(ref, parent=parent, state=state.upper())


@given(parsers.parse("open pull request `{ref}` has passing CI and is mergeable"))
def _mergeable_pull_request(scenario_state: dict[str, Any], ref: str) -> None:
    _github(scenario_state).add_pull_request(ref, base_sha="base1")


@when(
    "base branch `main` moves to a new head and GitHub reports pull request #51 conflicts with it"
)
def _base_branch_moved(scenario_state: dict[str, Any]) -> None:
    pull_request = _pull_request(scenario_state)
    pull_request["base_sha"] = "base2"
    pull_request["mergeable"] = "CONFLICTING"


@given(
    parsers.parse(
        "open pull request `{ref}` has a reviewer comment posted after its current head was pushed"
    )
)
def _pull_request_with_new_comment(scenario_state: dict[str, Any], ref: str) -> None:
    _github(scenario_state).add_pull_request(ref, comments=[comment(REVIEWER, created=AFTER_PUSH)])


# -- When --------------------------------------------------------------------


@when(parsers.re(r"the user runs `workdash ready (?P<scope>\S+)(?P<json> --json)?`"))
def _run_ready(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scope: str,
    json: str | None,
) -> None:
    argv = ["ready", scope, *(["--json"] if json else [])]
    _run_workdash(argv, scenario_state, monkeypatch, capsys)


# -- Then --------------------------------------------------------------------


@then(parsers.parse("the system prints `{text}`"))
def _prints_text(scenario_state: dict[str, Any], text: str) -> None:
    assert scenario_state["stdout"] == f"{text}\n"


@given(
    parsers.parse(
        "GitHub issue `{issue}` is open and closed by authored pull request `{pr}` which has passing CI and is mergeable"
    )
)
def _issue_with_mergeable_pr(scenario_state: dict[str, Any], issue: str, pr: str) -> None:
    github = _github(scenario_state)
    github.add_issue(issue)
    github.add_pull_request(pr, closes=(issue,), base_sha="base1")


@when(parsers.parse("the last open sub-issue `{ref}` closes"))
def _close_sub_issue(scenario_state: dict[str, Any], ref: str) -> None:
    _github(scenario_state).issues[ref]["state"] = "CLOSED"


@when(
    parsers.parse(
        "the user starts a Workdash dashboard tracking `{repo}`, where issue #{number:d} is ready"
    )
)
def _start_dashboard(scenario_state, work_items, tmp_path, monkeypatch, repo, number):
    _ready_session(scenario_state, work_items, tmp_path, monkeypatch)
    session = scenario_state["api_session"]
    session.config = replace(session.config, repositories=(repo,))
    _github(scenario_state).add_issue(f"{repo}#{number}")


@when("the user pushes a fix to pull request #51 and its CI passes")
def _push_ci_fix(scenario_state: dict[str, Any]) -> None:
    pull_request = _pull_request(scenario_state)
    pull_request["head"] = NEW_HEAD_SHA
    pull_request["pushed_at"] = "2026-03-10T00:00:00Z"
    pull_request["checks"][NEW_HEAD_SHA] = _CI_CHECKS["passing"]


@then(parsers.re(r"ready shows (?P<expectation>.+)"))
def _ready_shows(scenario_state: dict[str, Any], expectation: str) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    rows = [line.split() for line in scenario_state["stdout"].splitlines()]
    ids = [row[1] for row in rows]
    expected = re.findall(r"`([^`]+)`", expectation)
    assert expected, expectation
    if " with action " in expectation:
        issue, action = expected
        assert [row[2] for row in rows if row[1] == issue] == [action]
    elif " but not " in expectation:
        *present, absent = expected
        assert set(present) <= set(ids)
        assert absent not in ids
    elif " before " in expectation:
        *existing, unstarted = expected
        assert all(ids.index(item) < ids.index(unstarted) for item in existing)
    elif expectation.startswith("only "):
        assert ids == expected
    else:
        assert set(expected) <= set(ids)
