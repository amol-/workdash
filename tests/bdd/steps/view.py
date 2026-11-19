"""Step definitions for the view (issue/PR document and viewer links) features."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
import urllib.request
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.app import App
from textual.widgets import Markdown, Static

from view_fake import ISSUE_URL, fake_gh_view_run, issue, issue_routes
from workdash import launcher, view
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
    documents: list[view.Document] = []
    monkeypatch.setattr(view, "show_document", documents.append)
    commands: list[list[str]] = scenario_state.setdefault("gh_commands", [])
    monkeypatch.setattr(
        view.subprocess,
        "run",
        fake_gh_view_run(
            scenario_state.get("gh_routes") or issue_routes(),
            recorded=commands,
            fails=scenario_state.get("gh_fails", False),
        ),
    )
    scenario_state["exit_code"] = view.run_view(target)
    scenario_state["document"] = documents[0].content if documents else ""
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


@then("the system reports that the document could not be read")
def _document_not_read(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 1
    assert "Failed to read a missing file.md" in scenario_state["stderr"]
    assert scenario_state["document"] == ""


@then("the system reports that the target is not supported")
def _target_not_supported(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 1
    assert (
        "Unsupported target ftp://example.test/doc.md: expected a local path, a file URL, "
        "an HTTP(S) URL, or a GitHub issue or pull request."
    ) in scenario_state["stderr"]
    assert scenario_state["document"] == ""


@then("the system reports that the item could not be fetched")
def _item_not_fetched(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 1
    assert "Failed to fetch GitHub data with gh: no such issue" in scenario_state["stderr"]
    assert scenario_state["document"] == ""


@given("a local Markdown document")
def _local_document(scenario_state: dict[str, Any], tmp_path) -> None:
    path = tmp_path / "local notes.md"
    path.write_text("# Local notes\n\nlocal body\n", encoding="utf-8")
    scenario_state["local_document"] = path
    scenario_state["target"] = str(path)


@when("the user views its file URL with `workdash view`")
def _view_file_url(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run_view(
        scenario_state, monkeypatch, capsys, scenario_state["local_document"].resolve().as_uri()
    )


@then("the document shows the local content")
def _local_content(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    assert scenario_state["document"] == "# Local notes\n\nlocal body\n"


@given("a document served at an HTTP URL")
def _remote_document(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    document = io.BytesIO(b"# Remote notes")
    document.geturl = lambda: "https://docs.example/guide.md"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: document)
    scenario_state["target"] = "https://docs.example/guide.md"


@when("the user views that URL with `workdash view`")
def _view_remote_url(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _run_view(scenario_state, monkeypatch, capsys, scenario_state["target"])


@then("the document shows the remote content")
def _remote_content(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    assert scenario_state["document"] == "# Remote notes"


@then("no GitHub lookup happens")
def _no_github_lookup(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["gh_commands"] == []


# -- F-VIEW-LINKS -----------------------------------------------------------


def _viewer_document(scenario_state: dict[str, Any], tmp_path, link: str) -> None:
    path = tmp_path / "analysis.md"
    path.write_text(f"# Analysis\n\n[Link]({link})\n", encoding="utf-8")
    scenario_state["viewer_path"] = path


def _viewer_source(app) -> str:
    return app.query_one(view._DocumentViewer).document.source


def _viewer_status(app) -> str:
    return app.query_one("#status", Static).render().plain


def _back_offered(app) -> bool:
    return "b" in app.screen.active_bindings


def _settled_status(app) -> str:
    """The status line once the viewer is done loading, or "" while it still loads."""
    status = _viewer_status(app)
    return "" if status.startswith("Loading ") else status


async def _settle(pilot, condition, app) -> None:
    for _ in range(200):
        await pilot.pause()
        if condition(app):
            await pilot.pause()
            return
    raise AssertionError("the viewer did not finish following the link")


@given("the built-in Markdown viewer shows a document linking to a GitHub issue")
def _viewer_links_an_issue(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, ISSUE_URL)


@given("the built-in Markdown viewer shows a document linking to a pull request")
def _viewer_links_a_pull_request(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "https://github.com/owner/repo/pull/12")
    scenario_state["gh_routes"] = _pull_request_routes()


@given("the built-in Markdown viewer shows a document linking to a web page")
def _viewer_links_a_web_page(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "https://example.com/source")


@given("the built-in Markdown viewer shows a document linking to another Markdown document")
def _viewer_links_a_markdown_document(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "linked.md")
    (tmp_path / "linked.md").write_text("# Replacement\n\nlinked body\n", encoding="utf-8")


@given(
    "the built-in Markdown viewer shows a document linking to a section of another "
    "Markdown document"
)
def _viewer_links_a_markdown_section(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "guide.md#part")
    (tmp_path / "guide.md").write_text(
        "# Guide\n\n" + "Paragraph\n\n" * 40 + "# Part\n\nsection body\n", encoding="utf-8"
    )


@given("the built-in Markdown viewer shows a document linking to a missing Markdown document")
def _viewer_links_a_missing_markdown_document(scenario_state: dict[str, Any], tmp_path) -> None:
    _viewer_document(scenario_state, tmp_path, "missing-file.md")


@given("the built-in Markdown viewer shows a document with a heading link")
def _viewer_links_a_heading(scenario_state: dict[str, Any], tmp_path) -> None:
    path = tmp_path / "analysis.md"
    path.write_text(
        "# Analysis\n\n[Jump](#destination)\n\n" + "Paragraph\n\n" * 30 + "# Destination\n",
        encoding="utf-8",
    )
    scenario_state["viewer_path"] = path


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
    _activate_link(
        scenario_state, monkeypatch, lambda app: "Crash on startup" in _viewer_source(app)
    )


@when("the user activates the pull request link")
def _activate_pull_request_link(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_link(scenario_state, monkeypatch, lambda app: "**Draft:**" in _viewer_source(app))


@when("the user activates the web page link")
def _activate_web_page_link(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_link(scenario_state, monkeypatch, lambda app: _viewer_status(app) != "")


@when("the user activates the Markdown link")
def _activate_markdown_link(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_link(
        scenario_state,
        monkeypatch,
        lambda app: "Replacement" in _viewer_source(app) or _settled_status(app) != "",
    )


@when("the user activates the Markdown section link")
def _activate_markdown_section_link(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def shows_the_section(app) -> bool:
        return (
            _viewer_source(app).startswith("# Guide")
            and _viewer_status(app) == ""
            and "Part" in app.export_screenshot()
        )

    _activate_link(scenario_state, monkeypatch, shows_the_section)


@when("the user activates the heading link")
def _activate_heading_link(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _activate_link(
        scenario_state, monkeypatch, lambda app: "Destination" in app.export_screenshot()
    )


@when("the user follows the Markdown link and presses the back key")
def _follow_then_back(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    _activate_link(
        scenario_state,
        monkeypatch,
        lambda app: "Replacement" in _viewer_source(app),
        press=("b",),
    )


def _activate_link(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settle,
    *,
    press: tuple[str, ...] = (),
) -> None:
    opened: list[list[str]] = []
    panes: list[list[str]] = []
    executed: list[tuple[str, list[str]]] = []
    statuses: list[str] = []
    records: dict[str, Any] = {}
    gh = fake_gh_view_run(scenario_state.get("gh_routes") or issue_routes())

    def fake_run(command, **kwargs):
        if command[0] == "gh":
            return gh(command, **kwargs)
        opened.append(list(command))
        if scenario_state.get("open_fails"):
            raise subprocess.CalledProcessError(1, command, stderr="cannot open display")
        return subprocess.CompletedProcess(command, 0, stdout="terminal_19\n", stderr="")

    def fake_popen(command, **kwargs):
        opened.append(list(command))
        if scenario_state.get("open_fails"):
            raise OSError("cannot open display")
        process = MagicMock()
        process.wait.return_value = 0
        return process

    original_status = view._MarkdownApp._set_status

    def record_status(self, message: str) -> None:
        statuses.append(message)
        original_status(self, message)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher.zellij, "open_viewer", lambda *args, **kwargs: panes.append(args))
    monkeypatch.setattr(os, "execvp", lambda file, args: executed.append((file, list(args))))
    monkeypatch.setattr(view._MarkdownApp, "_set_status", record_status)
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(open_command="xdg-open"))

    def run(app):
        async def exercise():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                records["offered_before"] = _back_offered(app)
                await pilot.click(Markdown, offset=(2, 4))
                await _settle(pilot, settle, app)
                records["source"] = _viewer_source(app)
                records["status"] = _viewer_status(app)
                records["offered_after"] = _back_offered(app)
                records["screenshot"] = app.export_screenshot()
                records["running"] = app.is_running
                for key in press:
                    await pilot.press(key)
                    await pilot.pause()
                records["source_latest"] = _viewer_source(app)
                records["offered_latest"] = _back_offered(app)

        asyncio.run(exercise())

    monkeypatch.setattr(App, "run", run)
    assert view.run_view(str(scenario_state["viewer_path"])) == 0
    scenario_state["opened_commands"] = opened
    scenario_state["panes"] = panes
    scenario_state["executed"] = executed
    scenario_state["statuses"] = statuses
    scenario_state["link_records"] = records


@then("the viewer shows the issue document")
def _shows_issue_document(scenario_state: dict[str, Any]) -> None:
    records = scenario_state["link_records"]
    assert records["source"].startswith("# Crash on startup\n"), records["source"]
    assert "## Description" in records["source"]
    assert records["running"] is True


@then("the viewer shows the pull request document")
def _shows_pull_request_document(scenario_state: dict[str, Any]) -> None:
    records = scenario_state["link_records"]
    assert records["source"].startswith("# Crash on startup\n"), records["source"]
    assert "**Draft:** yes" in records["source"]
    assert records["running"] is True


@then("the viewer shows the linked document")
def _shows_linked_document(scenario_state: dict[str, Any]) -> None:
    records = scenario_state["link_records"]
    assert records["source"].startswith("# Replacement\n"), records["source"]
    assert records["running"] is True


@then("the viewer shows the previous document")
def _shows_previous_document(scenario_state: dict[str, Any]) -> None:
    source = scenario_state["link_records"]["source_latest"]
    assert source.startswith("# Analysis\n"), source


@then("no pane is opened")
def _no_pane_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["panes"] == []


@then("the viewer process is not replaced")
def _process_not_replaced(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["executed"] == []


@then("the viewer offers Back")
def _viewer_offers_back(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_records"]["offered_after"] is True


@then("the viewer does not offer Back")
def _viewer_no_back(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_records"]["offered_latest"] is False


@then("the viewer did not offer Back while showing the first document")
def _viewer_no_back_before(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_records"]["offered_before"] is False


@then("the viewer scrolls to that heading")
def _viewer_scrolled(scenario_state: dict[str, Any]) -> None:
    assert "Destination" in scenario_state["link_records"]["screenshot"]


@then("the viewer shows the linked document at the section")
def _viewer_shows_linked_section(scenario_state: dict[str, Any]) -> None:
    records = scenario_state["link_records"]
    assert records["source"].startswith("# Guide\n"), records["source"]
    assert "Part" in records["screenshot"], records["screenshot"]


@then("the viewer reports that the document is loading")
def _viewer_reports_loading(scenario_state: dict[str, Any]) -> None:
    statuses = scenario_state["statuses"]
    assert any(status.startswith("Loading ") for status in statuses), statuses


@then("the viewer reports that the document could not be loaded")
def _viewer_reports_load_failure(scenario_state: dict[str, Any]) -> None:
    status = scenario_state["link_records"]["status"]
    assert status.startswith("Failed to read "), status
    assert "missing-file.md" in status


@then("the current document stays open")
def _document_stays_open(scenario_state: dict[str, Any]) -> None:
    records = scenario_state["link_records"]
    assert records["running"] is True
    assert records["source"].startswith("# Analysis\n")


@then("the configured open command receives the web page URL")
def _open_command_receives_url(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["opened_commands"] == [["xdg-open", "https://example.com/source"]]


@then("the viewer reports the command it used")
def _viewer_reports_command(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_records"]["status"] == (
        "Opened https://example.com/source with xdg-open"
    )


@then("the viewer reports that the link could not be opened")
def _viewer_reports_failed_open(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["link_records"]["status"] == (
        "Failed to open https://example.com/source: "
        "Failed to open URL via xdg-open: cannot open display"
    )


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
