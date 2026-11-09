"""Step definitions for the ready work feature scenarios."""

from __future__ import annotations

import json
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
    failure,
    install_fake_github,
    review,
    thread,
    todo_body,
)
from workdash.models import WorkItem
from workdash.repo_worktree import worktree_path

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
    "a reviewer requesting changes on the current head": (
        "reviews",
        lambda: review(REVIEWER, "CHANGES_REQUESTED", created=AFTER_PUSH),
    ),
    "a reviewer comment posted after the current head was pushed": (
        "comments",
        lambda: comment(REVIEWER, created=AFTER_PUSH),
    ),
    "an unresolved review thread a reviewer opened after the last push": (
        "threads",
        lambda: thread(comment(REVIEWER, created=AFTER_PUSH)),
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
    "a dismissed changes-requested review": (
        "reviews",
        lambda: review(REVIEWER, "DISMISSED", created=AFTER_PUSH, body="Please rework this."),
    ),
    "a reviewer comment posted before the current head was pushed": (
        "comments",
        lambda: comment(REVIEWER, created=BEFORE_PUSH),
    ),
    "a bot comment posted after the current head was pushed": (
        "comments",
        lambda: comment("coverage-report[bot]", created=AFTER_PUSH),
    ),
    "an author reply posted after the current head was pushed": (
        "comments",
        lambda: comment("testuser", created=AFTER_PUSH),
    ),
    "a resolved review thread": (
        "threads",
        lambda: thread(comment(REVIEWER, created=AFTER_PUSH), resolved=True),
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


def _approve(pull_request: dict) -> None:
    pull_request["reviews"].append(review(REVIEWER, "APPROVED", created=AFTER_PUSH))
    pull_request["review_decision"] = "APPROVED"


def _ready_items(scenario_state: dict[str, Any]) -> list[dict]:
    assert scenario_state["exit_code"] == 0, scenario_state
    return json.loads(scenario_state["stdout"])["items"]


def _ready_ids(scenario_state: dict[str, Any]) -> list[str]:
    return [item["id"] for item in _ready_items(scenario_state)]


def _entry(scenario_state: dict[str, Any], item_id: str) -> dict:
    [entry] = [item for item in _ready_items(scenario_state) if item["id"] == item_id]
    scenario_state["ready_entry"] = entry
    return entry


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
    scenario_state["dashboard_before"] = list(session.work_items)


@given("no Workdash server is running")
def _no_workdash_server(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    import workdash.control as control_module

    def refuse(request):
        scenario_state.setdefault("urlopen_requests", []).append(request)
        raise OSError("connection refused")

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


@given(parsers.parse("GitHub issue `{parent}` is open and all its sub-issues are closed"))
def _issue_with_closed_sub_issues(scenario_state: dict[str, Any], parent: str) -> None:
    github = _github(scenario_state)
    github.add_issue(parent)
    for number in (21, 22):
        github.add_issue(f"owner/repo#{number}", parent=parent, state="CLOSED")


@given(parsers.parse("GitHub issue `{ref}` is open and blocked by a {blocker} issue"))
def _blocked_issue(scenario_state: dict[str, Any], ref: str, blocker: str) -> None:
    github = _github(scenario_state)
    github.add_issue("owner/blockers#1", state=blocker.upper())
    github.add_issue(ref, blocked_by=("owner/blockers#1",))


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
        "the server's todo repository `{repo}` has open todo #{number:d} targeting `{target}`"
    )
)
def _targeted_todo(scenario_state: dict[str, Any], repo: str, number: int, target: str) -> None:
    _github(scenario_state).add_issue(f"{repo}#{number}", body=todo_body(target))


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


@given(
    parsers.re(
        r"pull request #51 has (?P<ci>\w+) CI, (?P<mergeability>\w+) mergeability, "
        r"and (?P<reviews>no reviews|an approval)"
    )
)
def _pull_request_waiting(
    scenario_state: dict[str, Any], ci: str, mergeability: str, reviews: str
) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, ci)
    pull_request["mergeable"] = _MERGEABLE[mergeability]
    if reviews == "an approval":
        _approve(pull_request)


@given("no agent pane works on either of them")
def _no_agent_pane(scenario_state: dict[str, Any]) -> None:
    scenario_state["panes"] = []


@given("check `tests` failed on the current head of pull request #51, which has no reviews")
def _check_failed_on_head(scenario_state: dict[str, Any]) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, "failing")
    assert not pull_request["reviews"]


@given(parsers.re(r"pull request #51 has passing CI and (?:only )?(?P<feedback>.+)"))
def _pull_request_with_feedback(scenario_state: dict[str, Any], feedback: str) -> None:
    field, build = _FEEDBACK[feedback]
    entry = build()
    _pull_request(scenario_state)[field].append(entry)
    scenario_state["feedback_url"] = (
        entry["comments"]["nodes"][-1]["url"] if field == "threads" else entry["url"]
    )


@given(
    parsers.re(
        r"pull request #51 has (?P<problems>failed CI and new review feedback|"
        r"merge conflicts and failed CI|merge conflicts, failed CI, and new review feedback|"
        r"merge conflicts and pending CI)"
    )
)
def _pull_request_with_problems(scenario_state: dict[str, Any], problems: str) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, "failing" if "failed CI" in problems else "pending")
    if "merge conflicts" in problems:
        pull_request["mergeable"] = "CONFLICTING"
    if "review feedback" in problems:
        pull_request["comments"].append(comment(REVIEWER, created=AFTER_PUSH))


@when(parsers.parse("a new head is pushed to pull request #51 and its CI is {current}"))
def _new_head_pushed(scenario_state: dict[str, Any], current: str) -> None:
    pull_request = _pull_request(scenario_state)
    assert pull_request["checks"][HEAD_SHA] == _CI_CHECKS["failing"]
    pull_request["head"] = NEW_HEAD_SHA
    pull_request["pushed_at"] = "2026-03-10T00:00:00Z"
    pull_request["checks"][NEW_HEAD_SHA] = _CI_CHECKS[current]


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


@given(
    parsers.re(
        r"an? (?P<kind>agent|terminal) pane is running in the worktree of "
        r"(?:issue|pull request) `(?P<ref>[^`]+)`"
    )
)
def _pane_in_issue_worktree(scenario_state: dict[str, Any], kind: str, ref: str) -> None:
    repo, _, number = ref.partition("#")
    path = worktree_path(scenario_state["api_session"].config.workdir, repo, int(number))
    path.mkdir(parents=True, exist_ok=True)
    scenario_state.setdefault("git_origins", {})[str(path)] = repo
    panes = scenario_state.setdefault("panes", [])
    title = f"{'code' if kind == 'agent' else 'terminal'}_{path.name}"
    panes.append({"id": len(panes) + 1, "title": title, "pane_cwd": str(path)})


@given("Zellij cannot list the panes of the Workdash session")
def _zellij_cannot_list_panes(scenario_state: dict[str, Any]) -> None:
    scenario_state["zellij_unavailable"] = True


@given("GitHub answers repository queries with an outage")
def _github_outage(scenario_state: dict[str, Any]) -> None:
    _github(scenario_state).failures["ReadyRepositoryIssues"] = failure(
        "HTTP 502: Bad Gateway (https://api.github.com/graphql)"
    )


@given(
    parsers.re(
        r"GitHub issue `(?P<issue>[^`]+)` is open and was linked to pull request "
        r"`(?P<pull_request>[^`]+)`, which is now (?P<state>closed unmerged|merged)"
    )
)
def _issue_linked_to_finished_pull_request(
    scenario_state: dict[str, Any], issue: str, pull_request: str, state: str
) -> None:
    github = _github(scenario_state)
    github.add_issue(issue)
    github.add_pull_request(
        pull_request,
        closes=(issue,),
        state="MERGED" if state == "merged" else "CLOSED",
        checks={HEAD_SHA: _CI_CHECKS["failing"]},
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
        github.add_issue(ref, parent=parent, state="CLOSED" if state == "closed" else "OPEN")
        if "pull request #110" in state:
            github.add_pull_request(
                "owner/repo#110", closes=(ref,), checks={HEAD_SHA: _CI_CHECKS["failing"]}
            )


@given(
    "GitHub repository `owner/repo` also has open issue #200 and open pull request #210 "
    "whose CI failed, outside that tree"
)
def _work_outside_the_tree(scenario_state: dict[str, Any]) -> None:
    github = _github(scenario_state)
    github.add_issue("owner/repo#200")
    github.add_pull_request("owner/repo#210", checks={HEAD_SHA: _CI_CHECKS["failing"]})


@given(
    parsers.re(
        r"GitHub issue `(?P<ref>[^`]+)` (?P<state>is open|is closed|"
        r"is open and blocked by an open issue|"
        r"is open and closed by authored open pull request #51 with pending CI|"
        r"is (?:open|closed) and closed by authored open pull request #51 whose CI failed)"
    )
)
def _leaf_issue(scenario_state: dict[str, Any], ref: str, state: str) -> None:
    github = _github(scenario_state)
    blocked_by = ()
    if "blocked" in state:
        github.add_issue("owner/blockers#1")
        blocked_by = ("owner/blockers#1",)
    github.add_issue(
        ref, state="CLOSED" if state.startswith("is closed") else "OPEN", blocked_by=blocked_by
    )
    if "pull request #51" in state:
        pull_request = github.add_pull_request("owner/repo#51", closes=(ref,))
        _set_ci(scenario_state, pull_request, "pending" if "pending" in state else "failing")


@given(
    parsers.re(
        r"pull request #51 has (?P<ci>\w+) CI, an approval, and GitHub reports it conflicts "
        r"with base branch `(?P<base>[^`]+)`"
    )
)
def _approved_conflicting_pull_request(scenario_state: dict[str, Any], ci: str, base: str) -> None:
    pull_request = _pull_request(scenario_state)
    _set_ci(scenario_state, pull_request, ci)
    _approve(pull_request)
    pull_request["mergeable"] = "CONFLICTING"
    pull_request["base_ref"] = base
    pull_request["base_sha"] = f"{base}-head"


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


@given("GitHub reports no check or status on its current head")
def _no_checks_on_head(scenario_state: dict[str, Any]) -> None:
    _pull_request(scenario_state)["checks"] = {}


@given(parsers.parse("the base branch required checks are {required}"))
def _base_branch_required_checks(scenario_state: dict[str, Any], required: str) -> None:
    rulesets = {"`build` from a ruleset": ["build"], "unreadable": "unreadable"}
    if required in rulesets:
        _github(scenario_state).ruleset_checks[("owner/repo", "main")] = rulesets[required]


# -- When --------------------------------------------------------------------


@when(
    parsers.re(
        r"the user runs `workdash ready (?P<scope>.+?)(?: --host (?P<host>\S+))?"
        r"(?P<json> --json)?`"
    )
)
def _run_ready(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scope: str,
    host: str | None,
    json: str | None,
) -> None:
    argv = ["ready", scope, *(["--host", host] if host else []), *(["--json"] if json else [])]
    _run_workdash(argv, scenario_state, monkeypatch, capsys)


# -- Then --------------------------------------------------------------------


@then(parsers.re(r"the result lists only `(?P<item_id>[^`]+)`"))
def _lists_only(scenario_state: dict[str, Any], item_id: str) -> None:
    assert _ready_ids(scenario_state) == [item_id]


@then("the result lists nothing")
def _lists_nothing(scenario_state: dict[str, Any]) -> None:
    assert _ready_ids(scenario_state) == []


@then(
    parsers.re(
        r"the result lists `(?P<first>[^`]+)` and `(?P<second>[^`]+)` "
        r"with action `(?P<action>\w+)`"
    )
)
def _lists_two_with_action(
    scenario_state: dict[str, Any], first: str, second: str, action: str
) -> None:
    items = _ready_items(scenario_state)
    assert [(item["id"], item["action"]) for item in items] == [(first, action), (second, action)]


@then(parsers.re(r"the result lists `(?P<item_id>[^`]+)` with action `(?P<action>\w+)`"))
def _lists_with_action(scenario_state: dict[str, Any], item_id: str, action: str) -> None:
    assert _entry(scenario_state, item_id)["action"] == action


@then(
    parsers.re(
        r"the result lists `(?P<item_id>[^`]+)`(?P<once> once)? with action `(?P<action>\w+)` "
        r"and reasons `(?P<reasons>[\w,]+)`"
    )
)
def _lists_with_reasons(
    scenario_state: dict[str, Any], item_id: str, once: str | None, action: str, reasons: str
) -> None:
    entry = _entry(scenario_state, item_id)
    assert (entry["action"], entry["reasons"]) == (action, reasons.split(","))


@then(
    parsers.re(
        r"the result lists `(?P<item_id>[^`]+)` once with action `(?P<action>\w+)` "
        r"and issue `(?P<issue_id>[^`]+)`"
    )
)
def _lists_once_with_issue(
    scenario_state: dict[str, Any], item_id: str, action: str, issue_id: str
) -> None:
    entry = _entry(scenario_state, item_id)
    assert (entry["action"], entry["issue_id"]) == (action, issue_id)


@then(
    parsers.re(
        r"the result lists `(?P<item_id>[^`]+)` with action `fix_merge_conflicts` "
        r"and the new base head SHA"
    )
)
def _lists_with_new_base(scenario_state: dict[str, Any], item_id: str) -> None:
    entry = _entry(scenario_state, item_id)
    assert (entry["action"], entry["base_head_sha"]) == ("fix_merge_conflicts", "base2")


@then(
    parsers.re(
        r"the result lists `(?P<item_id>[^`]+)` with action `(?P<action>\w+)` "
        r"and CI status `(?P<ci_status>\w+)`"
    )
)
def _lists_with_ci_status(
    scenario_state: dict[str, Any], item_id: str, action: str, ci_status: str
) -> None:
    entry = _entry(scenario_state, item_id)
    assert (entry["action"], entry["ci_status"]) == (action, ci_status)


@then(
    parsers.re(
        r"the result lists `(?P<first>[^`/]+/[^`/]+)`, `(?P<second>[^`/]+/[^`/]+)`, "
        r"and `(?P<item_id>[^`]+)` issues once each"
    )
)
def _lists_issues_once_each(
    scenario_state: dict[str, Any], first: str, second: str, item_id: str
) -> None:
    # The issues share a creation time, so they are ordered by repository.
    assert [item for item in _ready_ids(scenario_state) if "#ISSUE-WT" not in item] == [
        item_id,
        f"{first}#ISSUE-1",
        f"{second}#ISSUE-1",
    ]


@then(parsers.re(r"the result lists (?P<ids>`[^`]+`(?:, `[^`]+`)+) in this order"))
def _lists_in_order(scenario_state: dict[str, Any], ids: str) -> None:
    assert _ready_ids(scenario_state) == ids.strip("`").split("`, `")


@then(
    parsers.re(r"(?:issue|pull request|todo) `(?P<item_id>[^`]+)` is (?P<listed>listed|not listed)")
)
def _item_is_listed(scenario_state: dict[str, Any], item_id: str, listed: str) -> None:
    assert (item_id in _ready_ids(scenario_state)) == (listed == "listed")


@then(parsers.re(r"neither `(?P<first>[^`]+)` nor `(?P<second>[^`]+)` is listed"))
def _neither_is_listed(scenario_state: dict[str, Any], first: str, second: str) -> None:
    ids = _ready_ids(scenario_state)
    assert first not in ids
    assert second not in ids


@then(
    parsers.re(r"neither issue (?P<issues>#\d+(?:, #\d+)*) nor pull request #(?P<pr>\d+) is listed")
)
def _neither_tree_outsider_is_listed(scenario_state: dict[str, Any], issues: str, pr: str) -> None:
    ids = _ready_ids(scenario_state)
    for number in issues.replace("#", "").split(", "):
        assert f"owner/repo#ISSUE-{number}" not in ids
    assert f"owner/repo#PR-{pr}" not in ids


@then(parsers.parse("issue #{number:d} is not listed"))
def _issue_number_not_listed(scenario_state: dict[str, Any], number: int) -> None:
    assert all(item["number"] != number for item in _ready_items(scenario_state))


@then(
    parsers.parse(
        "that entry links pull request `{issue_id}`, reports CI status `{ci_status}` with failing "
        "check `{check}`, and the current head SHA"
    )
)
def _entry_reports_failing_ci(
    scenario_state: dict[str, Any], issue_id: str, ci_status: str, check: str
) -> None:
    entry = scenario_state["ready_entry"]
    assert entry["pr_id"] == issue_id
    assert entry["issue_url"] == "https://github.com/owner/repo/issues/42"
    assert (entry["ci_status"], entry["failing_checks"]) == (ci_status, [check])
    assert entry["pr_head_sha"] == HEAD_SHA


@then(
    parsers.parse(
        "that entry references the actionable feedback and links pull request `{issue_id}`"
    )
)
def _entry_references_feedback(scenario_state: dict[str, Any], issue_id: str) -> None:
    entry = scenario_state["ready_entry"]
    assert [feedback["url"] for feedback in entry["feedback"]] == [scenario_state["feedback_url"]]
    assert entry["feedback"][0]["author"] == REVIEWER
    assert entry["pr_id"] == issue_id


@then(
    parsers.parse(
        "that entry reports mergeability `{mergeability}`, base `{base}` with its head SHA, "
        "and the current head SHA"
    )
)
def _entry_reports_mergeability(
    scenario_state: dict[str, Any], mergeability: str, base: str
) -> None:
    entry = scenario_state["ready_entry"]
    assert (entry["mergeability"], entry["base_ref"], entry["base_head_sha"]) == (
        mergeability,
        base,
        f"{base}-head",
    )
    assert entry["pr_head_sha"] == HEAD_SHA


@then("the dashboard list is unchanged")
def _dashboard_unchanged(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_session"].work_items == scenario_state["dashboard_before"]


@then("no pane is launched")
def _no_pane_launched(scenario_state: dict[str, Any]) -> None:
    assert not scenario_state.get("agent_launches")
    assert not scenario_state.get("terminal_launch_calls")
    assert not scenario_state.get("ensure_calls")


@then(parsers.parse("the command requests ready work for scope `{scope}` from the Workdash server"))
def _requests_ready_work(scenario_state: dict[str, Any], scope: str) -> None:
    assert scenario_state["control_requests"] == [
        {"endpoint": "ready", "payload": {"scope": scope}}
    ]


@then("the system reports that the Workdash server is not reachable")
def _reports_server_unreachable(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("urlopen_requests"), scenario_state
    assert "No Workdash server is reachable at 127.0.0.1:8765" in scenario_state["stderr"]


@then("the system reports that Zellij is unavailable")
def _reports_zellij_unavailable(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["control_error_code"] == "zellij_unavailable"
    assert "Failed to list Zellij panes" in scenario_state["stderr"]


@then("the system reports that the GitHub lookup failed")
def _reports_github_lookup_failed(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["control_error_code"] == "github_error"
    assert "GitHub lookup for 'owner/repo' failed" in scenario_state["stderr"]
    assert "HTTP 502" in scenario_state["stderr"]


@then(parsers.parse("the system reports error `{code}`"))
def _reports_error_code(scenario_state: dict[str, Any], code: str) -> None:
    assert scenario_state["control_error_code"] == code
    assert scenario_state["stderr"].startswith("Error: ")


@then(parsers.parse("the system prints `{text}`"))
def _prints_text(scenario_state: dict[str, Any], text: str) -> None:
    assert scenario_state["stdout"] == f"{text}\n"


@then(
    "`workdash ready owner/repo` prints the same items in the same order with type, ID, "
    "action, title, and URL"
)
def _human_output_matches(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    items = _ready_items(scenario_state)
    _run_workdash(["ready", "owner/repo"], scenario_state, monkeypatch, capsys)
    lines = scenario_state["stdout"].splitlines()
    assert len(lines) == len(items)
    for line, item in zip(lines, items, strict=True):
        assert line.split()[:3] == [item["type"].upper(), item["id"], item["action"]]
        assert f"{item['title']} {item['url']}" in line


@then(parsers.re(r"the result lists `(?P<first>[^`]+)` and `(?P<second>[^`]+)`"))
def _lists_two(scenario_state: dict[str, Any], first: str, second: str) -> None:
    assert set(_ready_ids(scenario_state)) == {first, second}


@then(
    parsers.parse(
        "the result lists `{item_id}` once with action `{action}` and linked pull request `{pr_id}`"
    )
)
def _lists_issue_with_pr(
    scenario_state: dict[str, Any], item_id: str, action: str, pr_id: str
) -> None:
    entry = _entry(scenario_state, item_id)
    assert (entry["action"], entry["pr_id"]) == (action, pr_id)


@given(
    parsers.parse(
        "GitHub issue `{issue}` is open and closed by authored pull request `{pr}` from a fork the server cannot push to, and its CI failed"
    )
)
def _issue_with_read_only_pr(scenario_state: dict[str, Any], issue: str, pr: str) -> None:
    github = _github(scenario_state)
    github.add_issue(issue)
    github.add_pull_request(
        pr, closes=(issue,), head_permission="READ", checks={HEAD_SHA: _CI_CHECKS["failing"]}
    )


@given(
    parsers.parse(
        "GitHub issue `{issue}` is open and closed by authored pull request `{pr}` which has passing CI and is mergeable"
    )
)
def _issue_with_mergeable_pr(scenario_state: dict[str, Any], issue: str, pr: str) -> None:
    github = _github(scenario_state)
    github.add_issue(issue)
    github.add_pull_request(pr, closes=(issue,), base_sha="base1")


@given(
    parsers.parse(
        "GitHub issue `{issue}` is open and closed by authored pull request `{pr}` with a reviewer comment posted after its current head was pushed"
    )
)
def _issue_with_commented_pr(scenario_state: dict[str, Any], issue: str, pr: str) -> None:
    github = _github(scenario_state)
    github.add_issue(issue)
    github.add_pull_request(pr, closes=(issue,), comments=[comment(REVIEWER, created=AFTER_PUSH)])


@then(parsers.parse("pull request `{item_id}` is not listed as a ready item"))
def _pr_not_listed(scenario_state: dict[str, Any], item_id: str) -> None:
    assert item_id not in _ready_ids(scenario_state)
