"""Dashboard and terminal viewer analysis journeys."""

import asyncio
import io
import json
import shlex
import subprocess
import sys
import urllib.request
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pytest_bdd import given, parsers, then, when
from textual.app import App
from textual.widgets import DataTable, Markdown, OptionList, Static

from pane_launch_fake import install_pane_launch
from view_fake import fake_gh_view_run
from workdash import dashboard_record, launcher, view, workdash
from workdash.backend import WorkdashBackend
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import (
    WorkdashSession,
    format_work_item_id,
    show_config_payload,
)
from workdash.github_client import GitHubClient
from workdash.included_items import IncludedItemsStore
from workdash.tui import AnalyzeDialog, WorkdashApp
from workdash.view import Document, _MarkdownApp


@pytest.fixture
def analysis_dashboard(tmp_path, monkeypatch):
    issue = {
        "id": 52,
        "repo": "owner/repo",
        "number": 52,
        "title": "Improve cards",
        "url": "https://github.com/owner/repo/issues/52",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }
    github = MagicMock(spec=GitHubClient)
    github.list_open_assigned_issues.side_effect = lambda *a, **kw: [issue.copy()]
    github.fetch_analysis_context.side_effect = lambda item: {"body": item.title}

    # The agent is a local script, and the clone comes from a local Git remote.
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-m",
            "Initial",
        ],
        cwd=source,
        check=True,
        capture_output=True,
    )
    github.clone_repository.side_effect = lambda repo, path: subprocess.run(
        ["git", "clone", str(source), str(path)], check=True, capture_output=True
    )
    agent = tmp_path / "agent.py"
    agent.write_text(
        "import sys\n"
        "title = 'Updated' if 'Cards now support sorting' in sys.argv[-1] else 'Fresh'\n"
        "print(f'# {title} analysis\\n\\nReview the cards.')\n"
    )
    viewer = tmp_path / "viewer.py"
    displayed = tmp_path / "displayed"
    viewer.write_text(
        "import pathlib, sys, urllib.request\n"
        "output = pathlib.Path(sys.argv[1])\n"
        "assert sys.argv[2].startswith('file://')\n"
        "with urllib.request.urlopen(sys.argv[2]) as document:\n"
        "    pending = output.with_suffix('.pending')\n"
        "    pending.write_bytes(document.read())\n"
        "    pending.replace(output)\n"
    )
    install_pane_launch(monkeypatch)
    run_agent = launcher.zellij.launch_pane_and_wait
    agent_started = Event()
    release = Event()

    def hold_agent(*args, **kwargs):
        agent_started.set()
        assert release.wait(10), "test did not release analysis pane"
        run_agent(*args, **kwargs)

    def open_viewer(session, argv, *, executable):
        subprocess.run(argv, check=True, capture_output=True)
        return "terminal_17"

    focus = MagicMock()
    monkeypatch.setattr(launcher.zellij, "launch_pane_and_wait", hold_agent)
    monkeypatch.setattr(launcher.zellij, "open_viewer", open_viewer)
    monkeypatch.setattr(launcher.zellij, "focus_zellij_pane", focus)
    monkeypatch.setenv("SHELL", "/bin/sh")
    config = WorkdashConfig(
        github_username="testuser",
        workdir=str(tmp_path / "work"),
        repositories=("owner/repo",),
        todo_repository="testuser/todos",
        analysis_viewer={
            "command": shlex.join([sys.executable, str(viewer), str(displayed)]),
            "format": "markdown",
            "needs_pane": True,
        },
        agents={"pi": AgentConfig(analyze=shlex.join([sys.executable, str(agent)]))},
    )
    backend = WorkdashBackend(
        config=config,
        github_client=github,
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )
    yield {
        "issue": issue,
        "backend": backend,
        "displayed": displayed,
        "agent_started": agent_started,
        "release": release,
        "focus": focus,
    }
    release.set()


@given("a work item is selected")
@given("a work item is selected with no saved analysis")
def _selected_item(analysis_dashboard):
    analysis_dashboard["items"], _ = analysis_dashboard["backend"].load_items()
    assert len(analysis_dashboard["items"]) == 1


@given("a work item is selected with a saved analysis")
def _saved_analysis(analysis_dashboard):
    backend = analysis_dashboard["backend"]
    items, _ = backend.load_items()
    backend.analysis_cache.save(items[0], "# Saved analysis\n\nKeep the cards.")
    _selected_item(analysis_dashboard)


@given("an issue is selected with a saved analysis and the built-in viewer is configured")
def _saved_issue_analysis(analysis_dashboard, tmp_path):
    _saved_analysis(analysis_dashboard)
    config = analysis_dashboard["backend"].config
    executable = str(tmp_path / "custom bin" / "workdash")
    config.analysis_viewer.update(
        command=shlex.join([executable, "view"]), format="markdown", needs_pane=True
    )
    config.agents["studio"] = AgentConfig(launch="studio", analyze="studio -p")


@given("the viewer cannot reach GitHub", target_fixture="offline_github")
def _offline_github():
    return MagicMock(side_effect=fake_gh_view_run({}, fails=True))


@when(
    'the user opens the saved analysis with "a" twice and chooses a coding agent with "c"',
    target_fixture="coding_status",
)
def _code_from_saved_analysis(analysis_dashboard, tmp_path, monkeypatch, offline_github):
    backend = analysis_dashboard["backend"]
    config = backend.config
    issue = analysis_dashboard["issue"]
    viewer_args = []

    def open_viewer(session, argv, *, executable):
        assert session == "workdash-main"
        executable_index = argv.index(executable)
        viewer_args.extend(argv[executable_index + 1 :])
        return "terminal_17"

    async def open_saved_analysis(app):
        async with app.run_test() as pilot:
            await pilot.press("a", "a")
            await _wait_for(pilot, lambda: bool(viewer_args))

    monkeypatch.setattr(launcher.zellij, "open_viewer", open_viewer)
    monkeypatch.setattr(
        workdash.WorkdashCommands, "_load_config_and_backend", lambda _: (config, backend)
    )
    monkeypatch.setattr(workdash, "WorkdashControlServer", MagicMock())
    monkeypatch.setattr(dashboard_record, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8766")
    with patch.object(WorkdashApp, "run", lambda app, **_: asyncio.run(open_saved_analysis(app))):
        assert workdash.WorkdashCommands().interactive() == 0

    dashboard_address = "127.0.0.1:8766"

    def open_request(request, *, timeout):
        if request.host != dashboard_address:
            raise ConnectionRefusedError(request.host)
        if request.selector == "/api/v0/info":
            result = {"session": "workdash-main"}
        elif request.selector == "/api/v0/show_config":
            result = show_config_payload(config)
        else:
            assert request.selector == "/api/v0/code"
            assert json.loads(request.data) == {"target": issue["url"], "agent": "studio"}
            result = {"status": "launched", "pane_id": "terminal_31"}
        response = io.BytesIO(json.dumps({"ok": True, "result": result}).encode())
        response.geturl = lambda: request.full_url
        return response

    async def choose_agent(app):
        async with app.run_test() as pilot:
            await _wait_for(pilot, lambda: app.screen.active_bindings["c"].enabled)
            screenshot = app.export_screenshot().replace("&#160;", " ")
            assert "Saved analysis" in screenshot and "Keep the cards." in screenshot
            assert app.screen.active_bindings["d"].enabled
            await pilot.press("c")
            assert app.screen.query_one(OptionList).get_option_at_index(0).prompt == "studio"
            await pilot.press("enter")
            status = app.query_one("#status", Static)
            await _wait_for(pilot, lambda: "terminal_31" in status.render().plain)
            return status.render().plain

    monkeypatch.setattr(view, "load_config", lambda: config)
    monkeypatch.setattr(view.subprocess, "run", offline_github)
    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *_: SimpleNamespace(open=open_request)
    )
    statuses = []
    with patch.object(App, "run", lambda app: statuses.append(asyncio.run(choose_agent(app)))):
        assert workdash.main(viewer_args) == 0
    offline_github.assert_not_called()
    return statuses[0]


@then("the saved analysis viewer reports the launched coding pane")
def _saved_analysis_coding_pane(coding_status):
    assert coding_status == "Code launched: pane terminal_31"


@given("the work item changes on GitHub")
def _github_update(analysis_dashboard):
    analysis_dashboard["issue"].update(
        title="Cards now support sorting", updated_at="2026-01-03T00:00:00Z"
    )


@given(parsers.parse("the configured viewer reads {format} without a server"))
def _viewer_format(analysis_dashboard, format):
    analysis_dashboard["backend"].config.analysis_viewer.update(
        format=format.lower(), needs_pane=format == "Markdown"
    )


@when('the user presses "a" and then "1" to analyze with pi')
@when(
    'the user refreshes with "r", opens the analysis menu with "a", and presses "1" to analyze with pi'
)
def _fresh_analysis(analysis_dashboard):
    asyncio.run(_use_analysis_menu(analysis_dashboard, "1"))


@when('the user presses "a" to open the analysis menu and "a" again to open the saved analysis')
def _reopen_analysis(analysis_dashboard):
    asyncio.run(_use_analysis_menu(analysis_dashboard, "a"))


@when('the user presses "a" and then Esc in the analysis menu')
def _cancel_analysis(analysis_dashboard):
    asyncio.run(_use_analysis_menu(analysis_dashboard, "escape"))


@then("the user is back at the work table without opening an analysis")
def _returned_to_table(analysis_dashboard):
    assert analysis_dashboard["table_focused"]
    assert not analysis_dashboard["displayed"].exists()
    assert not analysis_dashboard["agent_started"].is_set()


@then("the dashboard shows analysis in progress")
def _analysis_progress(analysis_dashboard):
    assert analysis_dashboard["progress"] == "Analyzing with pi..."


@then("the configured viewer opens the fresh analysis and receives focus")
def _opened_fresh(analysis_dashboard):
    assert analysis_dashboard["displayed"].read_text() == "# Fresh analysis\n\nReview the cards."
    analysis_dashboard["focus"].assert_called_once_with("workdash-main", "terminal_17")


@then("the menu offers to open the saved analysis or run a fresh one")
def _menu_offers_saved_and_fresh_analysis(analysis_dashboard):
    menu = analysis_dashboard["menu"]
    assert "Open analysis" in menu
    assert "Analyze with pi" in menu


@then("the configured viewer opens the saved analysis without running the agent")
def _opened_saved(analysis_dashboard):
    shown = analysis_dashboard["displayed"].read_text()
    if analysis_dashboard["backend"].config.analysis_viewer["format"] == "markdown":
        assert shown == "# Saved analysis\n\nKeep the cards."
        analysis_dashboard["focus"].assert_called_once_with("workdash-main", "terminal_17")
    else:
        assert "<h1>Saved analysis</h1>" in shown
        assert "<p>Keep the cards.</p>" in shown
    assert not analysis_dashboard["agent_started"].is_set()


@then("the menu does not offer the previous analysis")
def _old_analysis_unavailable(analysis_dashboard):
    assert "Open analysis" not in analysis_dashboard["menu"]


@then("the configured viewer opens the updated analysis")
def _opened_updated(analysis_dashboard):
    assert analysis_dashboard["displayed"].read_text() == "# Updated analysis\n\nReview the cards."


@given("a Markdown document is open in the viewer", target_fixture="markdown_viewer")
def _markdown_document():
    return _MarkdownApp(Document("Copy this sentence.", "file:///document.md"), "xdg-open", False)


@when("the user selects text and presses Ctrl+C", target_fixture="clipboard")
def _copy_markdown_selection(markdown_viewer, monkeypatch):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "test")

    async def exercise():
        async with markdown_viewer.run_test(size=(80, 24)) as pilot:
            await pilot.mouse_down(Markdown, offset=(2, 0))
            await pilot.hover(Markdown, offset=(10, 0))
            await pilot.mouse_up(Markdown, offset=(10, 0))
            with patch.object(markdown_viewer._driver, "write") as terminal:
                await pilot.press("ctrl+c")
            return markdown_viewer.clipboard, terminal.call_args_list

    return asyncio.run(exercise())


@then("the selected text is copied")
def _selection_copied(clipboard):
    text, terminal_writes = clipboard
    assert text == "Copy this"
    assert any(call.args == ("\x1b]52;c;Q29weSB0aGlz\a",) for call in terminal_writes)


async def _use_analysis_menu(dashboard, choice):
    backend = dashboard["backend"]
    items = dashboard["items"]
    viewer = backend.config.analysis_viewer
    zellij_session = "workdash-main" if viewer["needs_pane"] else None
    session = WorkdashSession(
        config=backend.config,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=zellij_session,
    )
    app = WorkdashApp(
        work_items=items,
        refresh_callback=lambda: session.list_items(refresh=True),
        analyze_choices=backend.config.tui_analyze_choices(),
        analyze_callback=lambda item, tool: session.analyze(
            target=format_work_item_id(item), agent=tool, prefer_cache=tool == "cached"
        )["path"],
        open_markdown_callback=lambda path, _item_url: launcher.open_markdown(
            path, zellij_session=zellij_session, focus=True, **viewer
        ),
        session=session,
    )
    async with app.run_test(size=(100, 30)) as pilot:
        table = app.query_one(DataTable)
        if dashboard["issue"]["title"] != items[0].title:
            await pilot.press("r")
            await _wait_for(
                pilot,
                lambda: any(
                    "Cards now support sorting" in str(cell) for cell in table.get_row_at(0)
                ),
            )
        await pilot.press("a")
        assert isinstance(app.screen, AnalyzeDialog)
        dashboard["menu"] = "\n".join(
            line.render().plain for line in app.screen.query(".analyze-line")
        )
        await pilot.press(choice)
        if choice == "escape":
            dashboard["table_focused"] = table.has_focus
            return
        if choice == "1":
            try:
                await _wait_for(pilot, dashboard["agent_started"].is_set)
                dashboard["progress"] = app.screen.query_one("#busy-message", Static).render().plain
            finally:
                dashboard["release"].set()
        await _wait_for(pilot, dashboard["displayed"].is_file)


async def _wait_for(pilot, condition):
    async with asyncio.timeout(8):
        while not condition():
            await pilot.pause(0.01)
