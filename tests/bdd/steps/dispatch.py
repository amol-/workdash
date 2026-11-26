"""User-facing dispatch scenarios at the GitHub and Zellij boundaries."""

import shlex
from pathlib import Path

from pytest_bdd import given, parsers, then, when

from gh_work_state_fake import HEAD_SHA, check_run

from .cli_orchestration import _run_workdash


@given(
    "`owner/repo` has an older unstarted issue #1 and issue #42 with authored pull request #51 whose CI failed"
)
def _ordered(scenario_state):
    github = scenario_state["github"]
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#42", created="2026-01-02T00:00:00Z")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#42",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )


@given("pull request #51 has an existing worktree")
def _pr_worktree(scenario_state):
    path = Path(scenario_state["api_session"].config.workdir) / "owner_repo_51"
    path.mkdir(parents=True)
    scenario_state["git_origins"] = {str(path): "owner/repo"}
    scenario_state["pr_worktree_paths"] = {51: str(path)}


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


@when(parsers.re(r"the user runs `workdash dispatch (?P<args>[^`]+)`"))
def _dispatch(args, scenario_state, monkeypatch, capsys):
    _run_workdash(["dispatch", *shlex.split(args)], scenario_state, monkeypatch, capsys)


@then("the preview lists `owner/repo#ISSUE-42` first as work to fix CI")
def _preview(scenario_state):
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    first = scenario_state["stdout"].splitlines()[0]
    assert first.split()[:3] == ["ISSUE", "owner/repo#ISSUE-42", "fix_ci"]


@then(parsers.parse("dispatch reports launching `{issue}` to `{action}`"))
def _launched(scenario_state, issue, action):
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    lines = scenario_state["stdout"].splitlines()
    assert "Status: launched" in lines
    assert f"Item: {issue}" in lines
    assert f"Action: {action}" in lines


@then("dispatch reports the existing pull request worktree as the agent's working directory")
def _existing_checkout(scenario_state):
    path = scenario_state["pr_worktree_paths"][51]
    assert f"Cwd: {path}" in scenario_state["stdout"].splitlines()


@then("dispatch reports that autoclose is enabled")
def _autoclose(scenario_state):
    assert "Autoclose: enabled" in scenario_state["stdout"].splitlines()


@then(parsers.parse("dispatch reports the agent in tab `{tab}`"))
def _agent_tab(scenario_state, tab):
    assert f"Tab: {tab}" in scenario_state["stdout"].splitlines()


@then(parsers.parse("dispatch reports agent `{agent}` in tab `{tab}`"))
def _named_agent_tab(scenario_state, agent, tab):
    _agent_tab(scenario_state, tab)
    assert f"Agent: {agent}" in scenario_state["stdout"].splitlines()
