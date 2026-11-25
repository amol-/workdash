"""Step definitions for reading documents with the view command."""

from __future__ import annotations

import asyncio
import io
import subprocess
import urllib.request
from html import unescape
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from textual.app import App

from gh_work_state_fake import AFTER_PUSH, HEAD_SHA, FakeGitHub, check_run, review
from view_fake import (
    fake_gh_view_run,
    issue,
    issue_routes,
    pull_request_routes,
    sub_issue,
)
from workdash import view
from workdash.config import WorkdashConfig
from workdash.control import WorkdashControlClient, WorkdashControlError
from workdash.workdash import main


@given("a GitHub issue with sub-issues and comments")
def _issue_with_nested_work(scenario_state: dict[str, Any]) -> None:
    scenario_state["gh_routes"] = issue_routes()


@given("a GitHub pull request with checks, reviews and review threads")
def _pull_request_with_discussion(scenario_state: dict[str, Any]) -> None:
    scenario_state["gh_routes"] = pull_request_routes()


@when(parsers.parse('I run `workdash view "{target}"`'))
def _view_target(target, scenario_state, monkeypatch):
    monkeypatch.setattr(
        view.subprocess,
        "run",
        fake_gh_view_run(
            scenario_state.get("gh_routes") or issue_routes(),
            project=scenario_state.get("project"),
        ),
    )

    def info(_client, endpoint, **kwargs):
        assert endpoint == "info"
        if "project_panes" not in scenario_state:
            raise WorkdashControlError("server_unreachable", "No Workdash server")
        return {"session": "workdash-test", "panes": scenario_state["project_panes"]}

    def run(app):
        async def exercise():
            async with app.run_test(size=(160, 100)) as pilot:
                await pilot.pause()
                scenario_state["screen"] = unescape(app.export_screenshot()).replace("\xa0", " ")
                scenario_state["document"] = app._document.content
                await pilot.press("q")

        asyncio.run(exercise())

    monkeypatch.setattr(WorkdashControlClient, "request", info)
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(github_username="testuser"))
    monkeypatch.setattr(App, "run", run)
    assert main(["view", target]) == 0


@then("the document shows the issue metadata, description, sub-issues and comments")
def _issue_document_sections(scenario_state: dict[str, Any]) -> None:
    document = scenario_state["document"]
    for text in ("Crash on startup", "Reproduce with #34 and #35.", "Sub issue 13", "Fixed in #34"):
        assert text in scenario_state["screen"]
    assert document.startswith("# Crash on startup\n")
    assert "**State:** open · **Item:** [owner/repo#12]" in document
    assert "**Author:** @octocat · **Updated:** 2026-02-25" in document
    assert (
        "**Labels:** bug · **Assignees:** @hubot · **Milestone:** v1 · **Comments:** 1" in document
    )
    assert "## Description" in document
    assert "## Steps" in document
    assert "Reproduce with #34 and #35." in document
    assert "## Sub-issues (2)" in document
    assert "| └─ [owner/repo#13](https://github.com/owner/repo/issues/13) | open |" in document
    assert "| └─ └─ [owner/repo#14](https://github.com/owner/repo/issues/14) | closed |" in document
    assert "## Comments (1)" in document
    assert "### @octocat · 2026-02-26" in document
    assert "Fixed in #34" in document
    assert "## CI" not in document


@then(
    "the document shows the pull request metadata, description, CI, reviews, review threads and comments"
)
def _pull_request_document_sections(scenario_state: dict[str, Any]) -> None:
    document = scenario_state["document"]
    assert "**State:** open · **Item:** [owner/repo#12]" in document
    for text in ("MERGEABLE", "CI — failure", "unit tests", "src/app.py:42", "Fixed in #34"):
        assert text in scenario_state["screen"]
    assert "**Draft:** yes · **Mergeable:** MERGEABLE" in document
    assert "**Merge state:** CLEAN · **Review:** APPROVED" in document
    assert "## Description" in document
    assert "## CI — failure" in document
    assert "1 of 2 checks passing." in document
    assert "- [unit tests](https://ci/unit) — FAILURE" in document
    assert "## Reviews (1)" in document
    assert "### @reviewer — APPROVED" in document
    assert "## Review threads (1)" in document
    assert "### src/app.py:42 — resolved" in document
    assert "```diff\n@@ -1,2 +1,2 @@\n-old\n+new\n```" in document
    assert "## Comments (1)" in document


@given("a local Markdown document")
def _local_document(tmp_path, monkeypatch):
    (tmp_path / "local notes.md").write_text("# Local notes\n\nlocal body\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)


@then("the document shows the local content")
def _local_content(scenario_state):
    assert "local body" in scenario_state["screen"]
    assert scenario_state["document"] == "# Local notes\n\nlocal body\n"


@given("a document served at an HTTPS URL")
def _remote_document(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    document = io.BytesIO(b"# Remote notes")
    document.geturl = lambda: "https://docs.example/guide.md"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: document)


@then("the document shows the remote content")
def _remote_content(scenario_state):
    assert "Remote notes" in scenario_state["screen"]
    assert scenario_state["document"] == "# Remote notes"


@given("an open project with a ready child, a blocked child, and a closed child")
def _open_project(scenario_state):
    _project(scenario_state, closed=False)


@given("a closed project with an open child")
def _closed_project(scenario_state):
    _project(scenario_state, closed=True)


def _project(scenario_state, *, closed):
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(
        state="closed" if closed else "open", state_reason="completed" if closed else None
    )
    children = [sub_issue(13, children=0)]
    project = FakeGitHub(subprocess.run)
    project.add_issue("owner/repo#12", state="CLOSED" if closed else "OPEN")
    project.add_issue("owner/repo#13", parent="owner/repo#12")
    if not closed:
        children += [
            sub_issue(14, children=0),
            sub_issue(15, children=0, state="closed") | {"state_reason": "completed"},
        ]
        project.add_issue("shared/deps#8")
        project.add_issue("owner/repo#14", parent="owner/repo#12", blocked_by=("shared/deps#8",))
        project.add_issue("owner/repo#15", parent="owner/repo#12", state="CLOSED")
    routes["api repos/owner/repo/issues/12/sub_issues"] = children
    scenario_state.update(gh_routes=routes, project=project)


@given("the ready child has the user's writable pull request with failing checks")
def _project_repair(scenario_state):
    scenario_state["project"].add_pull_request(
        "owner/repo#21",
        closes=("owner/repo#13",),
        author="testuser",
        head_permission="WRITE",
        checks={HEAD_SHA: [check_run("unit tests", "FAILURE")]},
    )


@given("the blocked child has a linked pull request with review feedback and failing checks")
def _project_review(scenario_state):
    scenario_state["project"].add_pull_request(
        "owner/repo#20",
        closes=("owner/repo#14",),
        review_decision="CHANGES_REQUESTED",
        reviews=[review("human", "CHANGES_REQUESTED", created=AFTER_PUSH)],
        checks={HEAD_SHA: [check_run("unit tests", "FAILURE")]},
    )


@given("the Workdash server is unavailable")
def _view_server_unavailable(scenario_state):
    scenario_state.pop("project_panes", None)


@given("Workdash is coding on the child's linked pull request, whose checks are failing")
def _project_coding(scenario_state, monkeypatch):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1")
    scenario_state["project"].add_pull_request(
        "owner/repo#20",
        closes=("owner/repo#13",),
        author="testuser",
        checks={HEAD_SHA: [check_run("unit tests", "FAILURE")]},
    )
    scenario_state["project_panes"] = [
        {
            "kind": "agent",
            "item": "owner/repo#PR-20",
            "item_url": "https://github.com/owner/repo/pull/20",
        }
    ]


@given(
    "an open project whose only child has a clean linked pull request with a pending review request"
)
def _project_review_progress(scenario_state):
    _project(scenario_state, closed=False)
    scenario_state["gh_routes"]["api repos/owner/repo/issues/12/sub_issues"] = [
        sub_issue(13, children=0)
    ]
    scenario_state["project"].add_pull_request(
        "owner/repo#20",
        closes=("owner/repo#13",),
        review_requests=["human"],
        checks={HEAD_SHA: [check_run("tests")]},
    )


@then("the parent and child remain open without any work marked ready")
def _open_without_ready_work(scenario_state):
    document = scenario_state["document"]
    assert "Review requested; Reported checks passing" in scenario_state["screen"]
    assert "**State:** open" in document
    assert "| open |" in _project_row(scenario_state, 13)
    assert "Ready on GitHub" not in document


@then(
    "the child's linked pull request shows that it is awaiting review and its reported checks are passing"
)
def _shows_review_progress(scenario_state):
    assert "Review requested; Reported checks passing" in _project_row(scenario_state, 13)


def _project_row(scenario_state, number):
    return next(
        line
        for line in scenario_state["document"].splitlines()
        if f"[owner/repo#{number}]" in line and line.startswith("|")
    )


@then("the document shows the open parent as blocked by its open sub-issues")
def _project_parent_blocked(scenario_state):
    document = scenario_state["document"]
    assert "Blocked by open sub-issues" in scenario_state["screen"]
    assert "**State:** open" in document
    parent_status = next(
        line for line in document.splitlines() if line.startswith("**Parent status:**")
    )
    assert "Blocked by open sub-issues" in parent_status
    assert "Ready on GitHub" not in parent_status


@then(
    "it shows the ready child as ready on GitHub with failing PR checks and the closed child as completed"
)
def _project_readiness(scenario_state):
    ready = _project_row(scenario_state, 13)
    assert "Ready on GitHub" in ready and "Checks failing" in ready
    closed = _project_row(scenario_state, 15)
    assert "| closed |" in closed and "Completed" in closed


@then("the blocked child's status links its blocker and shows its pull request's review and checks")
def _project_blocker_and_review(scenario_state):
    row = _project_row(scenario_state, 14)
    assert "Blocked by [shared/deps#8](https://github.com/shared/deps/issues/8)" in row
    assert "[PR owner/repo#20](https://github.com/owner/repo/pull/20)" in row
    assert "Changes requested" in row and "Checks failing" in row
    assert "Ready on GitHub" not in row


@then("the document says it couldn't check whether Workdash is working on this")
def _project_activity_unavailable(scenario_state):
    assert (
        "Couldn’t check whether Workdash is working on some project items"
        in scenario_state["document"]
    )


@then("the document keeps the parent closed and the child open")
def _project_already_coding(scenario_state):
    assert "Workdash coding pane" in scenario_state["screen"]
    assert "**State:** closed" in scenario_state["document"]
    assert "**Parent status:** Completed" not in scenario_state["document"]
    assert "| open |" in _project_row(scenario_state, 13)


@then(
    "the child's linked pull request shows failing checks and the coding pane Workdash maps to it"
)
def _project_coding_pr_checks(scenario_state):
    row = _project_row(scenario_state, 13)
    readiness, pull = row.split("[PR owner/repo#20]", 1)
    assert "Workdash coding pane" not in readiness
    assert "Checks failing" in pull
    assert "Workdash coding pane" in pull
    assert "Ready on GitHub" not in readiness
