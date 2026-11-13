"""User-facing dispatch scenarios at the GitHub and Zellij boundaries."""

import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from pytest_bdd import given, parsers, then, when

from gh_work_state_fake import AFTER_PUSH, HEAD_SHA, check_run, comment, failure, thread
from workdash.github_client import GitHubClient
from workdash.work_instruction import render_work_instruction

from .cli_orchestration import _run_workdash


@given(
    "`owner/repo` has an older unstarted issue #1 and issue #42 with authored pull request #51 whose CI failed"
)
def _ordered(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#42")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#42",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )


@given("pull request #51 has an existing worktree on branch `pr-branch`")
def _pr_worktree(scenario_state, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = subprocess.run
    run(["git", "init", "-q", str(repo)], check=True)
    run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    path = str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_51")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    run(["git", "-C", str(repo), "worktree", "add", "-qb", "pr-branch", path], check=True)
    scenario_state["git_run"] = run
    scenario_state["pr_worktrees"] = {51: (path, "pr-branch", "owner/repo")}


@given("GitHub repository `owner/repo` has open issue #42 with no sub-issues or pull request")
def _issue(scenario_state):
    scenario_state["github"].add_issue("owner/repo#42")


@given(
    "closed issue `owner/parent#42` has open nested descendant `owner/child#7` with no open descendants"
)
def _descendant(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/parent#42", state="CLOSED")
    github.add_issue("owner/parent#43", parent="owner/parent#42")
    github.add_issue("owner/child#7", parent="owner/parent#43")


@given("an unrelated issue in `owner/parent` is also ready")
def _unrelated(scenario_state):
    scenario_state["github"].add_issue("owner/parent#50")


@given("open issue `owner/repo#42` has only closed sub-issues")
def _closed_children(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#42")
    github.add_issue("owner/repo#43", parent="owner/repo#42", state="CLOSED")


@given(
    "GitHub issue `owner/repo#42` is open and closed by authored pull request `owner/repo#51` with merge conflicts, failed CI, and new review feedback"
)
def _multiple(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#42")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#42",),
        mergeable="CONFLICTING",
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
        threads=[thread(comment("reviewer", created=AFTER_PUSH))],
    )


@given("GitHub issue `owner/repo#42` has an authored open pull request waiting only for review")
def _waiting(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#42")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#42",))


@given("GitHub issue `owner/repo#43` has an open pull request of another author with failed CI")
def _external(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#43")
    github.add_pull_request(
        "owner/repo#52",
        closes=("owner/repo#43",),
        author="other",
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )


@given("issues `owner/repo#ISSUE-1` and `owner/repo#ISSUE-2` are ready in that order")
def _two(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2")


@given("issue `owner/repo#42` has an authored pull request whose only problem is a merge conflict")
def _conflict(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#42")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#42",), mergeable="CONFLICTING")


@given("the conflict is resolved after ready selection but before dispatch launch")
def _resolve(scenario_state, monkeypatch):
    find = GitHubClient.find_ready_work

    def resolve(self, **kwargs):
        result = find(self, **kwargs)
        scenario_state["github"].pull_requests["owner/repo#51"]["mergeable"] = "MERGEABLE"
        return result

    monkeypatch.setattr(GitHubClient, "find_ready_work", resolve)


@given("the conflict is resolved after eligibility is rechecked but before coding reads GitHub")
def _resolve_after_recheck(scenario_state, monkeypatch):
    session = scenario_state["api_session"]
    ready = session._ready
    calls = 0

    def resolve(*args, **kwargs):
        nonlocal calls
        result = ready(*args, **kwargs)
        calls += 1
        if calls == 2:
            scenario_state["github"].pull_requests["owner/repo#51"]["mergeable"] = "MERGEABLE"
        return result

    monkeypatch.setattr(session, "_ready", resolve)


@then(
    "the initial briefing describes pull request #51 waiting for review instead of repairing conflicts"
)
def _current_briefing(scenario_state):
    [launch] = scenario_state["agent_launches"]
    prompt = launch[1]
    assert "https://github.com/owner/repo/pull/51" in prompt
    assert "waiting for human review" in prompt
    assert "cannot merge cleanly" not in prompt
    assert "AUTOSTART: IMPLEMENT" not in prompt


@given("a GitHub read of `owner/repo` fails transiently")
def _transient_read(scenario_state, monkeypatch, request):
    run = subprocess.run
    remaining = [1]

    def flaky(command, *args, **kwargs):
        # The launch context is the read dispatch makes after choosing the issue.
        if command[:3] == ["gh", "issue", "view"]:
            if remaining:
                remaining.pop()
                raise failure("HTTP 502")
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps({"state": "OPEN", "title": "Fix the thing"})
            )
        return run(command, *args, **kwargs)

    def assert_injected_failure_fired():
        assert not remaining, "the injected transient GitHub read failure never fired"

    scenario_state["real_launch_context"] = True
    monkeypatch.setattr(subprocess, "run", flaky)
    request.addfinalizer(assert_injected_failure_fired)


@given("GitHub cannot report the work state")
def _github_down(scenario_state):
    scenario_state["github"].failures["ReadyRepositoryIssues"] = failure("HTTP 502")


@given("Zellij cannot report live panes")
def _zellij_down(scenario_state):
    scenario_state["zellij_unavailable"] = True


@given("a launched pane cannot be confirmed")
def _unconfirmed(scenario_state):
    scenario_state["launched_panes_invisible"] = True


@when(parsers.re(r"the user runs `workdash dispatch (?P<args>[^`]+)`"))
def _dispatch(args, scenario_state, monkeypatch, capsys):
    _run_workdash(["dispatch", *shlex.split(args)], scenario_state, monkeypatch, capsys)


@then("issue `owner/repo#ISSUE-42` precedes issue `owner/repo#ISSUE-1`")
def _preview(scenario_state):
    assert [item["id"] for item in json.loads(scenario_state["stdout"])["items"]] == [
        "owner/repo#ISSUE-42",
        "owner/repo#ISSUE-1",
    ]


@then(
    parsers.re(
        r"the result reports status `launched` and issue `(?P<issue>[^`]+)`(?: with action `(?P<action>[^`]+)`)?"
    )
)
def _launched(scenario_state, issue, action):
    result = json.loads(scenario_state["stdout"])
    assert result["status"] == "launched" and result["issue_id"] == issue
    assert result["pane_id"] == f"terminal_{scenario_state['panes'][-1]['id']}"
    if action:
        assert result["action"] == action


@then(
    "the result reports status `launched`, issue `owner/repo#ISSUE-42`, action `fix_ci`, and the actual pane ID"
)
def _ci_result(scenario_state):
    _launched(scenario_state, "owner/repo#ISSUE-42", "fix_ci")


@then("exactly one agent is launched on pull request #51's existing branch")
@then("exactly one agent starts on pull request #51's existing branch")
def _pr_launch(scenario_state):
    assert len(scenario_state["agent_launches"]) == 1
    path = scenario_state["agent_launches"][0][0]
    assert path == scenario_state["pr_worktrees"][51][0]
    assert scenario_state["panes"][-1]["pane_cwd"] == path
    branch = scenario_state["git_run"](
        ["git", "-C", path, "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert branch == "pr-branch"


@then("the agent is told to fix the failing checks using the same instruction as autoclose")
def _fix_ci(scenario_state):
    session = scenario_state["api_session"]
    [state] = GitHubClient().evaluate_pull_requests(
        [("owner/repo", 51)],
        github_username=session.config.github_username,
        todo_repository=session.config.todo_repository,
    )
    assert state.action == "fix_ci"
    assert render_work_instruction(state.action, state) in scenario_state["agent_launches"][0][1]


@then("the server supervises the pane after the command returns")
@then("the server supervises the launched pane")
def _supervised(scenario_state):
    result = json.loads(scenario_state["stdout"])
    assert result["autoclose_enabled"]
    assert (result["session"], result["pane_id"]) in scenario_state["api_session"].autoclose._panes


@then("exactly one agent is told to implement issue #42")
def _implement(scenario_state):
    assert len(scenario_state["agent_launches"]) == 1
    assert "github.com/owner/repo/issues/42" in scenario_state["agent_launches"][0][1]


@then("exactly one agent starts in group `child`")
def _group_child(scenario_state):
    assert len(scenario_state["agent_launches"]) == 1
    assert json.loads(scenario_state["stdout"])["group"] == "child"


@then("no agent is launched for the parent or unrelated issue")
def _no_parent(scenario_state):
    assert scenario_state["ensure_calls"][-1][1].number == 7


@then(
    "the agent is told to verify the parent's workflow and prove it with a test if already implemented by the sub-issues"
)
def _verify_parent(scenario_state):
    prompt = scenario_state["agent_launches"][0][1]
    assert (
        "If sub-issues already fulfilled the issue goal, verify the described workflow "
        "and add a test proving it works instead of reimplementing it."
    ) in prompt


@then(
    "the result reports issue `owner/repo#ISSUE-42` with action `address_review` and all three reasons"
)
def _reasons(scenario_state):
    result = json.loads(scenario_state["stdout"])
    assert result["issue_id"] == "owner/repo#ISSUE-42"
    assert result["action"] == "address_review"
    assert result["reasons"] == ["address_review", "fix_merge_conflicts", "fix_ci"]


@then(
    "the briefing includes the feedback references, merge conflicts, and failing checks in that order"
)
def _briefing(scenario_state):
    prompt = scenario_state["agent_launches"][0][1]
    assert (
        prompt.index("https://github.com/comment/")
        < prompt.index("confirmed merge conflicts")
        < prompt.index("failing checks: tests")
    )


@then("no new pull request is created by dispatch")
def _no_new_pr(scenario_state):
    assert list(scenario_state["github"].pull_requests) == ["owner/repo#51"]


@then("the result reports status `no_ready_task`")
def _empty(scenario_state):
    assert json.loads(scenario_state["stdout"])["status"] == "no_ready_task"


@then("no coding agent is launched")
def _no_launch(scenario_state):
    assert not scenario_state.get("agent_launches")


@then("the command reports an error and exits with a non-zero status")
def _error(scenario_state):
    assert scenario_state["exit_code"] != 0


@then("the result does not report `no_ready_task` or a confirmed launch")
def _not_success(scenario_state):
    assert (
        "no_ready_task" not in scenario_state["output"]
        and '"launched"' not in scenario_state["output"]
    )


@then("dispatch does not start a second agent")
def _at_most_one(scenario_state):
    assert len(scenario_state.get("agent_launches", [])) <= 1


@then(
    "the result reports status `launched`, issue `owner/repo#ISSUE-42`, agent `pi`, group `epic`, and the actual pane ID"
)
def _override(scenario_state):
    _launched(scenario_state, "owner/repo#ISSUE-42", None)
    result = json.loads(scenario_state["stdout"])
    assert result["agent"] == "pi" and result["group"] == "epic" and result["tab_name"] == "epic"


@when("two clients dispatch `owner/repo` at the same time and both initially select issue #1")
def _concurrent(scenario_state, monkeypatch, tmp_path):
    from webob import Request

    import workdash.control as control

    from .common import install_zellij_agent_fakes

    session = scenario_state["api_session"]
    install_zellij_agent_fakes(scenario_state, monkeypatch)

    def path(_workdir, item, **kwargs):
        directory = tmp_path / f"{item.repo.replace('/', '_')}_{item.number}"
        directory.mkdir(exist_ok=True)
        return directory

    monkeypatch.setattr(control, "existing_worktree_path", path)
    monkeypatch.setattr(
        control, "ensure_worktree", lambda workdir, item, **kwargs: str(path(workdir, item))
    )
    monkeypatch.setattr(control, "get_merge_base", lambda _, **kwargs: None)
    monkeypatch.setattr(control, "prepare_launch_agent_prompt", lambda *a, **kw: "PROMPT")
    ready = session._ready
    barrier = Barrier(2)
    observations = []

    def synchronized_ready(scope, fail_unconfirmed, **kwargs):
        result = ready(scope, fail_unconfirmed, **kwargs)
        if scope == "owner/repo":
            observations.append(control.format_work_item_id(result[0].item))
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(session, "_ready", synchronized_ready)
    app = control._make_turbogears_app(session)

    def send():
        request = Request.blank(
            "/api/v0/dispatch",
            method="POST",
            content_type="application/json",
            body=b'{"scope":"owner/repo"}',
        )
        response = request.get_response(app)
        assert response.status_int == 200
        return json.loads(response.text)["result"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        scenario_state["dispatch_results"] = list(executor.map(lambda _: send(), range(2)))
    assert observations == ["owner/repo#ISSUE-1", "owner/repo#ISSUE-1"]


@then("each response reports one launched pane for a different issue")
def _different(scenario_state):
    results = scenario_state["dispatch_results"]
    assert all(result["status"] == "launched" and result["pane_id"] for result in results)
    assert {result["issue_id"] for result in results} == {
        "owner/repo#ISSUE-1",
        "owner/repo#ISSUE-2",
    }
    assert len({result["pane_id"] for result in results}) == 2


@then("exactly two coding agents are launched")
def _two_agents(scenario_state):
    assert len(scenario_state["agent_launches"]) == 2
