"""Step definitions for the view (issue/PR document and viewer links) features."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from contextlib import nullcontext
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from textual.app import App
from textual.widgets import Markdown, Static

from view_fake import ISSUE_URL, fake_gh_view_run, issue, issue_routes
from workdash import launcher, markdownview, view
from workdash.config import WorkdashConfig
from workdash.git import GitHelper
from workdash.launcher import open_item_document
from workdash.models import WorkItem

from .common import modal_screen_names, run_app


def _pull_request_routes() -> dict[str, object]:
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(
        pull_request={"url": "https://api.github.com/repos/owner/repo/pulls/12"}
    )
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "unit tests",
                "conclusion": "FAILURE",
                "detailsUrl": "https://ci/unit",
            },
            {
                "__typename": "CheckRun",
                "name": "lint",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://ci/lint",
            },
        ],
        "reviews": [{"author": {"login": "reviewer"}, "state": "APPROVED", "body": "Looks good"}],
    }
    routes["api graphql"] = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "isResolved": True,
                                "isOutdated": False,
                                "path": "src/app.py",
                                "line": 42,
                                "comments": {
                                    "nodes": [
                                        {
                                            "author": {"login": "reviewer"},
                                            "body": "Rename this",
                                            "diffHunk": "@@ -1,2 +1,2 @@\n-old\n+new",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    return routes


def _run_view(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys, target: str
) -> None:
    documents: list[str] = []
    monkeypatch.setattr(view, "show_markdown", documents.append)
    monkeypatch.setattr(
        view.subprocess,
        "run",
        fake_gh_view_run(
            scenario_state.get("gh_routes") or issue_routes(),
            fails=scenario_state.get("gh_fails", False),
        ),
    )
    scenario_state["exit_code"] = view.run_view(target)
    scenario_state["document"] = documents[0] if documents else ""
    scenario_state["stderr"] = capsys.readouterr().err


# -- F-VIEW-ITEM ------------------------------------------------------------


@given("a GitHub issue with sub-issues and comments")
def _issue_with_nested_work(scenario_state: dict[str, Any]) -> None:
    scenario_state["gh_routes"] = issue_routes()
    scenario_state["target"] = ISSUE_URL


@given("a GitHub pull request with checks, reviews and review threads")
def _pull_request_with_discussion(scenario_state: dict[str, Any]) -> None:
    scenario_state["gh_routes"] = _pull_request_routes()
    scenario_state["target"] = "owner/repo#12"


@given(parsers.parse("the current directory's origin remote is `{repo}`"))
def _origin_remote(repo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitHelper, "get_repo_root", lambda self, repo_path=None: "/repo")
    monkeypatch.setattr(
        GitHelper, "remote_url", lambda self, candidate, name: f"git@github.com:{repo}.git"
    )


@given("the next GitHub lookup will fail")
def _next_lookup_fails(scenario_state: dict[str, Any]) -> None:
    scenario_state["gh_fails"] = True


@when("the user views it with `workdash view`")
def _view_it(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run_view(scenario_state, monkeypatch, capsys, scenario_state["target"])


@when(parsers.parse("the user views `{target}` with `workdash view`"))
def _view_target(
    target: str, scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _run_view(scenario_state, monkeypatch, capsys, target)


@then("the document shows the issue metadata, description, sub-issues and comments")
def _issue_document_sections(scenario_state: dict[str, Any]) -> None:
    document = scenario_state["document"]
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


@then("the document ends with the generation timestamp")
def _document_footer(scenario_state: dict[str, Any]) -> None:
    assert "*Generated from `gh` at " in scenario_state["document"]
    assert scenario_state["document"].rstrip().endswith("UTC.*")


@then(parsers.parse("the document is for `{item}`"))
def _document_for(item: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    assert f"**Item:** [{item}]({ISSUE_URL})" in scenario_state["document"]


@then("the system reports that the target is not recognised")
def _target_not_recognised(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 1
    assert "Unrecognised target 'not a target'" in scenario_state["stderr"]


@then("the system reports that the item could not be fetched")
def _item_not_fetched(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 1
    assert "Failed to fetch GitHub data with gh: no such issue" in scenario_state["stderr"]
    assert scenario_state["document"] == ""


# -- F-VIEW-LINKS -----------------------------------------------------------


def _viewer_document(scenario_state: dict[str, Any], tmp_path, link: str) -> None:
    path = tmp_path / "analysis.md"
    path.write_text(f"# Analysis\n\n[Link]({link})\n", encoding="utf-8")
    scenario_state["viewer_path"] = path


@given("the built-in Markdown viewer shows a document linking to a GitHub issue")
def _viewer_links_an_issue(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, ISSUE_URL)


@given("the built-in Markdown viewer shows a document linking to a pull request")
def _viewer_links_a_pull_request(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "https://github.com/owner/repo/pull/7")


@given("the built-in Markdown viewer shows a document linking to a web page")
def _viewer_links_a_web_page(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "https://example.com/source")


@given("the user is inside a Zellij session")
def _inside_zellij(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-current")
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"zellij", "workdash"} else None
    )


@given("the user is not inside a Zellij session")
def _outside_zellij(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)


@given("the configured open command will fail")
def _open_command_fails(scenario_state: dict[str, Any]) -> None:
    scenario_state["open_fails"] = True


@when("the user activates the issue link")
def _activate_issue_link(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _activate_link(scenario_state, monkeypatch)


@when("the user activates the pull request link")
def _activate_pull_request_link(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_link(scenario_state, monkeypatch)


@when("the user activates the web page link")
def _activate_web_page_link(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_link(scenario_state, monkeypatch)


def _activate_link(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    notices: list[str] = []
    executed: list[tuple[str, list[str]]] = []

    def fake_run(command, **kwargs):
        commands.append(list(command))
        if scenario_state.get("open_fails"):
            raise subprocess.CalledProcessError(1, command, stderr="cannot open display")
        return subprocess.CompletedProcess(command, 0, stdout="terminal_19\n", stderr="")

    def fake_popen(command, **kwargs):
        commands.append(list(command))
        if scenario_state.get("open_fails"):
            raise OSError("cannot open display")
        from unittest.mock import MagicMock

        process = MagicMock()
        process.wait.return_value = 0
        return process

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(os, "execvp", lambda file, args: executed.append((file, list(args))))
    monkeypatch.setattr(App, "suspend", lambda self: nullcontext())
    monkeypatch.setattr(
        markdownview, "load_config", lambda: WorkdashConfig(open_command="xdg-open")
    )
    monkeypatch.setattr(App, "notify", lambda self, message, **kwargs: notices.append(message))
    stayed_open: list[bool] = []

    def run(app):
        async def exercise():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.click(Markdown, offset=(2, 4))
                await pilot.pause()
                stayed_open.append(app.is_running and "Analysis" in app.export_screenshot())

        asyncio.run(exercise())

    monkeypatch.setattr(App, "run", run)
    markdownview.run_markdownview(str(scenario_state["viewer_path"]))
    scenario_state["link_commands"] = commands
    scenario_state["link_notices"] = notices
    scenario_state["executed"] = executed
    scenario_state["document_stayed_open"] = stayed_open


@then("a viewer for the issue opens in a new full-size floating pane")
def _issue_viewer_pane(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_notices"] == []
    assert scenario_state["link_commands"] == [
        [
            "/usr/bin/zellij",
            "--session",
            "workdash-current",
            "action",
            "new-pane",
            "-f",
            "-x",
            "0",
            "-y",
            "0",
            "--width",
            "100%",
            "--height",
            "100%",
            "--close-on-exit",
            "--",
            "sh",
            "-c",
            '"$@" || { printf "\\nPress Enter to close this pane.\\n"; read -r _; }',
            "workdash-view",
            "/usr/bin/workdash",
            "view",
            ISSUE_URL,
        ]
    ]


@then("the current document stays open")
def _document_stays_open(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["document_stayed_open"] == [True]


@then("the viewer process becomes a viewer for the pull request")
def _viewer_process_replaced(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["executed"] == [
        ("workdash", ["workdash", "view", "https://github.com/owner/repo/pull/7"])
    ]


@then("the configured open command receives the web page URL")
def _open_command_receives_url(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_commands"] == [["xdg-open", "https://example.com/source"]]


@then("the viewer reports the command it used")
def _viewer_reports_command(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_notices"] == ["Opened https://example.com/source with xdg-open"]


@then("the viewer reports that the link could not be opened")
def _viewer_reports_failed_open(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_notices"] == [
        "Failed to open https://example.com/source: "
        "Failed to open URL via xdg-open: cannot open display"
    ]


# -- F-VIEW-MENU ------------------------------------------------------------


@given("the next viewer launch will fail")
def _next_viewer_launch_fails(scenario_state: dict[str, Any]) -> None:
    scenario_state["view_fails"] = True


@when('the user chooses "view" from the view menu')
def _choose_view(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def view_callback(item: WorkItem) -> None:
        # Use the real launcher under a patched subprocess.run so the pane
        # command the TUI produces is what the scenario verifies.
        def fake_run(command, **kwargs):
            commands.append(list(command))
            if scenario_state.get("view_fails"):
                raise subprocess.CalledProcessError(1, command, stderr="zellij exploded")
            return subprocess.CompletedProcess(command, 0, stdout="terminal_19\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.setattr(
            shutil,
            "which",
            lambda name: f"/usr/bin/{name}" if name in {"zellij", "workdash"} else None,
        )
        open_item_document(item.url, zellij_session="workdash-current")

    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("v")
        for _ in range(20):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if "viewer" in status or "failed" in status:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain
        captured["modal_screen_names"] = modal_screen_names(app)

    run_app(work_items=list(work_items), view_callback=view_callback, interactions=interactions)
    scenario_state["view_commands"] = commands
    scenario_state["view_status"] = captured["status"]
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


@when("the user opens the view menu and cancels it")
def _cancel_menu(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    opened: list[WorkItem] = []
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await pilot.press("v")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        captured["modal_screen_names"] = modal_screen_names(app)

    run_app(
        work_items=list(work_items),
        open_callback=opened.append,
        view_callback=opened.append,
        interactions=interactions,
    )
    scenario_state["opened_items"] = opened
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


@then("the item is shown as a document in a full-size floating pane in the current session")
def _item_shown_in_pane(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["view_commands"] == [
        [
            "/usr/bin/zellij",
            "--session",
            "workdash-current",
            "action",
            "new-pane",
            "-f",
            "-x",
            "0",
            "-y",
            "0",
            "--width",
            "100%",
            "--height",
            "100%",
            "--close-on-exit",
            "--",
            "sh",
            "-c",
            '"$@" || { printf "\\nPress Enter to close this pane.\\n"; read -r _; }',
            "workdash-view",
            "/usr/bin/workdash",
            "view",
            scenario_state["selected_item"].url,
        ]
    ]


@then("the TUI reports that the viewer was opened")
def _tui_reports_viewer(scenario_state: dict[str, Any]) -> None:
    assert "Opened viewer for" in scenario_state["view_status"], scenario_state["view_status"]


@then("nothing is opened")
def _nothing_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["opened_items"] == []


@then("no dialog remains")
def _no_dialog_remains(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["modal_screen_names"] == []


@then("the system reports the viewer error details to the user")
def _reports_viewer_error(scenario_state: dict[str, Any]) -> None:
    status = scenario_state["view_status"]
    assert "View failed: Failed to open URL via workdash view: zellij exploded" in status, status
