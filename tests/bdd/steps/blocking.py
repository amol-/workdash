"""Dashboard blocking-operation behavior at the session and control-server boundary."""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import DataTable, Input, Static

import workdash.control as control_module
import workdash.launcher as launcher_module
from workdash.control import WorkdashControlServer, WorkdashSession
from workdash.tui import BusyScreen, SearchDialog
from workdash.work_state import ReadyWork, ScopedReadyWork

from .common import make_work_item, run_app

ITEM = "owner/repo#ISSUE-1"


@pytest.fixture
def blocking_state(tmp_path):
    session = WorkdashSession(
        config=MagicMock(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.config.configured_analyze_agents.return_value = ["pi"]
    session.config.configured_code_agents.return_value = ["pi"]
    # A workdir without worktrees keeps the ready work unclaimed.
    session.config.workdir = str(tmp_path / "worktrees")
    # These scenarios are about the dialog, not about Zellij being installed.
    session._live_panes = lambda: []
    analysis = tmp_path / "analysis.md"
    analysis.write_text("# Analysis\n")
    state = {
        "session": session,
        "analysis_path": analysis,
        "release": threading.Event(),
        "started": threading.Event(),
    }
    yield state
    state["release"].set()
    session.stop_autodispatch_sessions()


def _hold(state, result):
    """Block the call the requested operation starts with, then return ``result``."""

    def slow_call(*_args, **_kwargs):
        state["started"].set()
        state["release"].wait(5)
        if (error := state.get("error")) is not None:
            raise error
        return result

    return slow_call


def _serve_and_drive(state, monkeypatch, tmp_path, interactions) -> None:
    """Serve the API and drive the dashboard while the scenario's request runs."""

    monkeypatch.setattr(control_module, "server_address", lambda: ("127.0.0.1", 0))
    monkeypatch.setattr(control_module, "SERVER_LOG_PATH", tmp_path / "server.log")
    server = WorkdashControlServer(state["session"])
    server.start()
    host, port = server._httpds[0].server_address
    state["url"] = f"http://{host}:{port}/api/v0"
    try:
        run_app(
            work_items=list(state["session"].work_items),
            session=state["session"],
            interactions=interactions,
        )
    finally:
        server.stop()


def _request(state, path, payload) -> dict:
    request = urllib.request.Request(
        f"{state['url']}/{path}",
        data=json.dumps(payload).encode(),
        headers={"X-Workdash": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        return json.load(error)


def _dialog(app) -> str | None:
    if isinstance(app.screen, BusyScreen):
        return app.screen.query_one("#busy-message", Static).render().plain
    return None


def _status(app) -> str:
    return app.query_one("#status-footer", Static).render().plain


async def _wait_until(predicate, pilot) -> bool:
    """Let the dashboard run until ``predicate`` holds, and report whether it did."""

    for _ in range(40):
        if predicate():
            return True
        await pilot.pause(0.1)
    return predicate()


_OPERATIONS = {
    "a readiness check": ("ready", {"scope": "owner/repo"}),
    "an analysis": ("analyze", {"target": ITEM, "agent": "pi"}),
    "an autodispatch start": ("autodispatch", {"scope": ITEM, "agent": "pi"}),
}


def _run_request(state, monkeypatch, tmp_path, operation: str, *, press_keys=False) -> None:
    """Request an operation while the dashboard runs, and record what it showed."""

    path, payload = _OPERATIONS[operation]
    session = state["session"]
    if operation == "an analysis":
        session.backend.analyze_item.side_effect = _hold(state, str(state["analysis_path"]))
    else:
        session.backend.github_client.find_ready_work.side_effect = _hold(
            state, ScopedReadyWork([])
        )

    async def interact(app, pilot):
        request = threading.Thread(
            target=lambda: state.update(response=_request(state, path, payload))
        )
        request.start()
        assert state["started"].wait(5)
        await _wait_until(lambda: _dialog(app) is not None, pilot)
        state["dialog"] = _dialog(app)
        if press_keys:
            # The dashboard is only driven once per scenario, so keys are pressed here,
            # while the operation still holds the dialog.
            await pilot.press("/")
            await pilot.pause(0.2)
            state["keys_ignored"] = not isinstance(app.screen, SearchDialog)
        state["release"].set()
        await _wait_until(lambda: _dialog(app) is None, pilot)
        state["dialog_after"] = _dialog(app)
        request.join(10)
        if press_keys:
            await pilot.press("/")
            await pilot.pause(0.2)
            state["search_opened"] = isinstance(app.screen, SearchDialog)
            await pilot.press("escape")
            await pilot.pause(0.2)

    _serve_and_drive(state, monkeypatch, tmp_path, interact)


async def _start_autodispatch(state, app, pilot) -> None:
    """Start autodispatch through the API and wait for it to develop in the background.

    The start request holds the dashboard until it is released here, so the scenario
    sees that dialog go away instead of racing a fast start.
    """

    request = asyncio.create_task(
        asyncio.to_thread(_request, state, "autodispatch", {"scope": ITEM, "agent": "pi"})
    )
    assert await asyncio.to_thread(state["started"].wait, 5)
    await _wait_until(lambda: _dialog(app) is not None, pilot)
    state["dialog"] = _dialog(app)
    # The API must answer while the session is still starting, not after it finishes.
    state["answered_while_starting"] = await _wait_until(request.done, pilot)
    state["release"].set()
    state["response"] = await request
    await _wait_until(lambda: _status(app).startswith("Started autodispatch"), pilot)
    state["status"] = _status(app)
    developed = await _wait_until(
        lambda: any(
            entry["stage"] == "developing" for entry in state["session"].autodispatch_sessions()
        ),
        pilot,
    )
    assert developed, "the autodispatch session never started developing"
    await _wait_until(lambda: _dialog(app) is None, pilot)
    state["dialog_after"] = _dialog(app)


@given("Workdash has work items to show")
def workdash_has_work_items(blocking_state):
    blocking_state["session"].work_items = [make_work_item(number=1)]


@given("Workdash has a work item to search")
def workdash_has_a_searchable_item(blocking_state):
    item = make_work_item(number=1, title="Searchable work")
    blocking_state["session"].work_items = [item]
    blocking_state["item"] = item


@given("the next GitHub lookup fails")
def github_lookup_fails(blocking_state):
    blocking_state["error"] = RuntimeError("GitHub is unreachable")


@given("autodispatch can launch the ready issue")
def autodispatch_can_launch(blocking_state, monkeypatch):
    """Let the session launch the ready issue without a real agent or Zellij pane.

    The readiness lookup holds until the scenario releases it, so the start request's
    dialog is observable instead of racing a fast start.
    """

    session = blocking_state["session"]
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 5)
    monkeypatch.setattr(launcher_module, "zellij", MagicMock(list_workdash_panes=lambda _: []))
    monkeypatch.setattr(
        session,
        "code",
        MagicMock(
            return_value={
                "status": "launched",
                "item_id": ITEM,
                "pane_id": "terminal_1",
                "session": "workdash",
                "launch_reason": "implement",
            }
        ),
    )
    blocking_state["ready"] = [ReadyWork(item=session.work_items[0])]
    session.backend.github_client.find_ready_work.side_effect = _hold(
        blocking_state, ScopedReadyWork(blocking_state["ready"])
    )


@when(parsers.parse("{operation} is requested through the API while the dashboard is open"))
def request_operation(blocking_state, monkeypatch, tmp_path, operation):
    _run_request(blocking_state, monkeypatch, tmp_path, operation)


@when("a readiness check is requested through the API while the user presses keys on the dashboard")
def request_operation_while_pressing_keys(blocking_state, monkeypatch, tmp_path):
    _run_request(blocking_state, monkeypatch, tmp_path, "a readiness check", press_keys=True)


@when("a background autodispatch session is started through the API while the dashboard is open")
def start_background_autodispatch(blocking_state, monkeypatch, tmp_path):
    async def interact(app, pilot):
        await _start_autodispatch(blocking_state, app, pilot)

    _serve_and_drive(blocking_state, monkeypatch, tmp_path, interact)


@when(
    "a background autodispatch session is started through the API while the user searches the dashboard"
)
def start_autodispatch_while_searching(blocking_state, monkeypatch, tmp_path):
    async def interact(app, pilot):
        await _start_autodispatch(blocking_state, app, pilot)
        await pilot.press("/", "s", "e", "a", "r", "c", "h", "enter")
        await pilot.pause(0.2)
        table = app.query_one("#work-items", DataTable)
        blocking_state["rows"] = [
            [str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)
        ]
        blocking_state["status"] = _status(app)
        blocking_state["dialog_after"] = _dialog(app)

    _serve_and_drive(blocking_state, monkeypatch, tmp_path, interact)


@when("the user opens search and types while a readiness check is requested through the API")
def search_while_readiness_check_requested(blocking_state, monkeypatch, tmp_path):
    session = blocking_state["session"]
    session.backend.github_client.find_ready_work.side_effect = _hold(
        blocking_state, ScopedReadyWork([])
    )

    async def interact(app, pilot):
        await pilot.press("/")
        await pilot.pause(0.2)
        search = app.screen
        assert isinstance(search, SearchDialog)
        request = asyncio.create_task(
            asyncio.to_thread(_request, blocking_state, "ready", {"scope": "owner/repo"})
        )
        assert await asyncio.to_thread(blocking_state["started"].wait, 5)
        await pilot.press("s", "e", "a")
        # Let several busy-screen polls run while the operation holds the session.
        await pilot.pause(0.6)
        blocking_state["typed"] = search.query_one(Input).value
        blocking_state["busy_screens"] = sum(
            isinstance(screen, BusyScreen) for screen in app.screen_stack
        )
        blocking_state["release"].set()
        await request

    _serve_and_drive(blocking_state, monkeypatch, tmp_path, interact)


@then(parsers.parse('the dashboard shows a blocking dialog saying "{message}"'))
def dialog_says(blocking_state, message):
    assert blocking_state["dialog"] == message


@then("the dashboard clears the dialog when the operation finishes")
def dialog_clears(blocking_state):
    assert blocking_state["dialog_after"] is None


@then("the dashboard ignores the keys pressed while the dialog is shown")
def keys_ignored(blocking_state):
    assert blocking_state["keys_ignored"]


@then("the dashboard opens the search dialog once the dialog clears")
def search_opens_after_dialog(blocking_state):
    assert blocking_state["search_opened"]


@then("the API reports that the readiness check failed")
def api_reports_failure(blocking_state):
    response = blocking_state["response"]
    assert response["ok"] is False
    assert response["error"]["code"] == "github_error"
    assert "GitHub is unreachable" in response["error"]["message"]


@then("the dashboard reports in the status bar that autodispatch started")
def status_reports_autodispatch(blocking_state):
    session_id = blocking_state["response"]["result"]["id"]
    assert blocking_state["status"] == f"Started autodispatch for {ITEM}: {session_id}."


@then("the dashboard shows no blocking dialog while the session works in the background")
def no_blocking_dialog_in_background(blocking_state):
    assert blocking_state["dialog_after"] is None


@then("the dashboard shows the typed text in the search box")
def typed_text_shown(blocking_state):
    assert blocking_state["typed"] == "sea"


@then("the dashboard shows no blocking dialog over the search dialog")
def no_dialog_over_search(blocking_state):
    assert blocking_state["busy_screens"] == 0


@then("the dashboard shows the matching work item")
def matching_work_item_shown(blocking_state):
    assert blocking_state["status"] == "Search matched 1 of 1 item(s)."
    titles = [row[2].lstrip("* ") for row in blocking_state["rows"]]
    assert titles == [blocking_state["item"].title]


@then("the API reports the started autodispatch session")
def api_reports_autodispatch_session(blocking_state):
    session = blocking_state["response"]["result"]
    assert session["id"]
    assert session["scope"] == ITEM


@then("the API answered while the session was still starting")
def api_answered_while_starting(blocking_state):
    assert blocking_state["answered_while_starting"]
