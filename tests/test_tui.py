import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

pytest.importorskip("textual")

from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from workdash.backend import IncludeResult
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import AnalyzeDialog, CodeDialog, SearchDialog, WorkdashApp

_DEFAULT_TUI_CONFIG = WorkdashConfig(
    claude=AgentConfig(analyze="claude -p", launch="claude"),
    codex=AgentConfig(analyze="codex exec", launch="codex"),
    pi=AgentConfig(launch="pi"),
)


def test_workdash_app_renders_type_repo_title_age_last_update_and_analysis_columns() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=22,
            title="Implement renderer",
            created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/pull/22",
        ),
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=11,
            title="Fix parser",
            created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/issues/11",
        ),
    ]
    app = WorkdashApp(
        work_items=work_items,
        suggestion_markers={(WorkItemType.ISSUE, "owner/repo", 11): "*"},
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as _:
            table = app.query_one("#work-items", DataTable)
            assert [str(column.label) for column in table.columns.values()] == [
                "Type",
                "Repo",
                "Title",
                "Age",
                "Last Update",
            ]
            assert table.row_count == 2
            # Sorted by updated_at descending; PR #22 (updated 2/25) before issue #11 (updated 2/20)
            # PR #22 is within 24h of now_utc so cells are bold Text objects
            assert [str(c) for c in table.get_row_at(0)] == [
                "PR#22",
                "owner/repo",
                "Implement renderer",
                "1d",
                "1d",
            ]
            assert table.get_row_at(1) == [
                "ISSUE#11",
                "owner/repo",
                "* Fix parser",
                "6d",
                "6d",
            ]

    asyncio.run(run_smoke())


def test_workdash_app_truncates_a_long_repo_column_on_the_left() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="posit-dev/rsconnect-python",  # exactly the column width
            number=22,
            title="Implement renderer",
            created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/pull/22",
        ),
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="posit-dev/rsconnect-python-longer",
            number=11,
            title="Fix parser",
            created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/issues/11",
        ),
    ]
    app = WorkdashApp(work_items=work_items, now_utc=now_utc)

    async def run_smoke() -> None:
        async with app.run_test() as _:
            table = app.query_one("#work-items", DataTable)
            assert str(table.get_row_at(0)[1]) == "posit-dev/rsconnect-python"
            # The owner is cut, the repository name itself stays readable.
            assert str(table.get_row_at(1)[1]) == "…v/rsconnect-python-longer"
            assert len(str(table.get_row_at(1)[1])) == len("posit-dev/rsconnect-python")

    asyncio.run(run_smoke())


def test_workdash_app_keys_table_rows_by_work_item_identity_not_included_label() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )
    app = WorkdashApp(work_items=[work_item], now_utc=now_utc)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            row_key = "pr:owner/repo#22"
            assert [key.value for key in table.rows] == [row_key]
            assert [str(c) for c in table.get_row(row_key)] == [
                "PR#22",
                "owner/repo",
                "Implement renderer",
                "6d",
                "6d",
            ]

            work_item.included = True
            app._render_table()

            assert [key.value for key in table.rows] == [row_key]
            assert [str(c) for c in table.get_row(row_key)] == [
                "PR+#22",
                "owner/repo",
                "Implement renderer",
                "6d",
                "6d",
            ]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_renders_review_for_review_requested_pr_type() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_items = [
        WorkItem(
            kind=WorkItemKind.REVIEW_REQUESTED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=22,
            title="Needs review",
            created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/pull/22",
        ),
    ]
    app = WorkdashApp(
        work_items=work_items,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as _:
            table = app.query_one("#work-items", DataTable)
            assert [str(c) for c in table.get_row_at(0)] == [
                "REVIEW#22",
                "owner/repo",
                "Needs review",
                "1d",
                "1d",
            ]

    asyncio.run(run_smoke())


def test_workdash_app_keybindings_invoke_callbacks_for_selected_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: None)
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=11,
            title="Fix parser",
            created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/issues/11",
        ),
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=22,
            title="Implement renderer",
            created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
            url="https://example.com/pull/22",
        ),
    ]
    open_calls: list[tuple[WorkItemType, int]] = []
    analyze_calls: list[tuple[WorkItemType, int, str]] = []
    launch_calls: list[tuple[WorkItemType, int, str]] = []
    terminal_calls: list[tuple[WorkItemType, int]] = []

    def open_callback(item: WorkItem) -> None:
        open_calls.append((item.item_type, item.number))

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str:
        analyze_calls.append((item.item_type, item.number, tool))

        return "/tmp/analyses/analysis.md"

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item.item_type, item.number, tool))

    def terminal_callback(item: WorkItem) -> None:
        terminal_calls.append((item.item_type, item.number))

    app = WorkdashApp(
        work_items=work_items,
        open_callback=open_callback,
        analyze_callback=analyze_callback,
        launch_callback=launch_callback,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        terminal_callback=terminal_callback,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            # PR #22 is row 0 (sorted by updated_at desc: 2/25 before 2/20)
            await pilot.pause()

            await pilot.press("o")
            await pilot.pause()

            # Press "a" to open the AnalyzeDialog, then "1" to choose Claude
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()

            # Press "c" to open CodeDialog, then "2" to choose Codex
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()

            # Press "c" to open CodeDialog, then "3" to choose VSCode
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            # Press "c" to open CodeDialog, then "4" to choose pi
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()

            await pilot.press("t")
            await pilot.pause()

            await pilot.press("q")

            assert open_calls == [(WorkItemType.PR, 22)]
            assert analyze_calls == [(WorkItemType.PR, 22, "claude")]
            assert launch_calls == [
                (WorkItemType.PR, 22, "codex"),
                (WorkItemType.PR, 22, "vscode"),
                (WorkItemType.PR, 22, "pi"),
            ]
            assert terminal_calls == [(WorkItemType.PR, 22)]
            assert [str(c) for c in table.get_row_at(0)] == [
                "PR#22",
                "owner/repo",
                "Implement renderer",
                "1d",
                "1d",
            ]

    asyncio.run(run_smoke())


def test_workdash_app_refresh_keybinding_invokes_callback_and_reloads_rows() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    initial_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=11,
        title="Fix parser",
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/issues/11",
    )
    refreshed_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=33,
        title="Ship refresh",
        created_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/33",
    )
    refresh_calls: list[str] = []

    def refresh_callback() -> tuple[list[WorkItem], dict[tuple[WorkItemType, str, int], str]]:
        refresh_calls.append("called")
        return [refreshed_item], {(WorkItemType.PR, "owner/repo", 33): "*"}

    app = WorkdashApp(
        work_items=[initial_item],
        refresh_callback=refresh_callback,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("r")
            await pilot.press("q")

            table = app.query_one("#work-items", DataTable)
            assert refresh_calls == ["called"]
            assert table.row_count == 1
            assert [str(c) for c in table.get_row_at(0)] == [
                "PR#33",
                "owner/repo",
                "* Ship refresh",
                "0d",
                "0d",
            ]

    asyncio.run(run_smoke())


def test_workdash_app_uses_session_state_for_first_render() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    initial_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=11,
        title="Stale constructor copy",
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/issues/11",
    )
    refreshed_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=33,
        title="Session state before Textual starts",
        created_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/33",
    )
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=MagicMock(),
        work_items=[initial_item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    app = WorkdashApp(
        work_items=session.work_items,
        suggestion_markers=session.suggestion_markers,
        session=session,
        now_utc=now_utc,
    )
    session.work_items = [refreshed_item]
    session.suggestion_markers = {(WorkItemType.PR, "owner/repo", 33): "*"}

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert table.row_count == 1
            assert [str(c) for c in table.get_row_at(0)] == [
                "PR#33",
                "owner/repo",
                "* Session state before Textual starts",
                "0d",
                "0d",
            ]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_refresh_from_session_reloads_visible_rows() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    initial_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=11,
        title="Fix parser",
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/issues/11",
    )
    refreshed_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=33,
        title="Ship refresh",
        created_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/33",
    )

    class Session:
        work_items = [initial_item]
        suggestion_markers = {}

    session = Session()
    app = WorkdashApp(
        work_items=[initial_item],
        session=session,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert table.get_row_at(0) == [
                "ISSUE#11",
                "owner/repo",
                "Fix parser",
                "6d",
                "6d",
            ]

            session.work_items = [refreshed_item]
            session.suggestion_markers = {(WorkItemType.PR, "owner/repo", 33): "*"}
            app.refresh_from_session()

            assert table.row_count == 1
            assert [str(c) for c in table.get_row_at(0)] == [
                "PR#33",
                "owner/repo",
                "* Ship refresh",
                "0d",
                "0d",
            ]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_footer_shows_success_status_for_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: None)
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str:

        return "/tmp/analysis.md"

    app = WorkdashApp(
        work_items=[work_item],
        open_callback=lambda _: None,
        refresh_callback=lambda: [work_item],
        analyze_callback=analyze_callback,
        launch_callback=lambda _, __tool="codex": None,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        terminal_callback=lambda _: None,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)
            assert footer.render().plain == "Ready."

            await pilot.press("o")
            await pilot.pause()
            assert footer.render().plain == "Opened pr owner/repo#22."

            await pilot.press("r")
            await pilot.pause()
            assert footer.render().plain == "Refreshed 1 item(s)."

            # Press "a" to open AnalyzeDialog, then "1" to choose Claude
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("1")
            await pilot.pause()
            assert footer.render().plain == "Analyzed pr owner/repo#22 with Claude."

            # Press "c" to open CodeDialog, then "2" to choose Codex
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            assert footer.render().plain == "Launched Codex for pr owner/repo#22."

            # Press "c" to open CodeDialog, then "3" to choose VSCode
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            assert footer.render().plain == "Launched VSCode for pr owner/repo#22."

            # Press "c" to open CodeDialog, then "4" to choose pi
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("4")
            await pilot.pause()
            assert footer.render().plain == "Launched pi for pr owner/repo#22."

            await pilot.press("t")
            await pilot.pause()
            assert footer.render().plain == "Opened terminal for pr owner/repo#22."

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_analyze_opens_markdown_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=11,
        title="Fix parser",
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/issues/11",
    )
    opened_paths: list[str] = []
    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: opened_paths.append(path))

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str:

        return "/tmp/analyses/owner_repo_ISSUE11.md"

    app = WorkdashApp(
        work_items=[work_item],
        analyze_callback=analyze_callback,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            # Press "a" to open AnalyzeDialog, then "2" to choose Codex
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            assert opened_paths == ["/tmp/analyses/owner_repo_ISSUE11.md"]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_analyze_runs_without_blocking_ui_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: None)
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=11,
        title="Fix parser",
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/issues/11",
    )
    analyze_started = threading.Event()
    allow_finish = threading.Event()

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str:
        analyze_started.set()
        allow_finish.wait(timeout=30)

        return "/tmp/analysis.md"

    app = WorkdashApp(
        work_items=[work_item],
        analyze_callback=analyze_callback,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            # Press "a" to open the dialog, then "1" to trigger analysis
            await pilot.press("a")
            await pilot.pause()
            analyze_press = asyncio.create_task(pilot.press("1"))
            while not analyze_started.is_set():
                await pilot.pause()
            table = app.query_one("#work-items", DataTable)
            assert table.row_count == 1
            assert analyze_press.done() is False
            allow_finish.set()
            await analyze_press
            await pilot.pause()
            assert table.get_row_at(0) == [
                "ISSUE#11",
                "owner/repo",
                "Fix parser",
                "6d",
                "6d",
            ]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_shows_command_hint_bar() -> None:
    app = WorkdashApp()

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            command_bar = app.query_one("#command-footer", Static)
            assert command_bar.render().plain == (
                "(/)search (o)pen (r)efresh (a)nalyze (c)ode (d)iff "
                "(t)erminal (i)nclude (w)todo (q)uit"
            )
            await pilot.press("q")

    asyncio.run(run_smoke())


def _assert_no_modal_screens(app: WorkdashApp) -> None:
    lingering = [
        screen.__class__.__name__ for screen in app.screen_stack if isinstance(screen, ModalScreen)
    ]
    assert lingering == []


async def _wait_for_footer(footer: Static, expected: str, pilot) -> None:
    for _ in range(20):
        await pilot.pause()
        if footer.render().plain == expected:
            return
    assert footer.render().plain == expected


def test_workdash_app_uses_only_configured_tui_agent_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: None)
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )
    analyze_calls: list[tuple[WorkItem, str]] = []
    launch_calls: list[tuple[WorkItem, str]] = []
    worktree_attempts: list[WorkItem] = []

    app = WorkdashApp(
        work_items=[work_item],
        analyze_callback=lambda item, tool="codex": (
            analyze_calls.append((item, tool)) or "/tmp/analysis.md"
        ),
        worktree_callback=lambda item: worktree_attempts.append(item) or "/tmp/worktree",
        launch_callback=lambda item, tool="codex": launch_calls.append((item, tool)),
        analyze_choices=WorkdashConfig(
            codex=AgentConfig(analyze="codex exec")
        ).tui_analyze_choices(),
        code_choices=WorkdashConfig().tui_code_choices(),
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AnalyzeDialog)
            await pilot.press("2")
            await pilot.pause()
            assert analyze_calls == []
            assert isinstance(app.screen, AnalyzeDialog)
            await pilot.press("1")
            await pilot.pause()
            assert analyze_calls == [(work_item, "codex")]

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, CodeDialog)
            await pilot.press("2")
            await pilot.pause()
            assert worktree_attempts == []
            assert launch_calls == []
            assert isinstance(app.screen, CodeDialog)
            await pilot.press("1")
            await pilot.pause()
            assert worktree_attempts == [work_item]
            assert launch_calls == [(work_item, "vscode")]

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_analyze_uses_shared_callback_without_tui_worktree_prep() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )
    analyze_calls: list[tuple[WorkItem, str]] = []
    worktree_attempts: list[WorkItem] = []

    def worktree_callback(item: WorkItem) -> str:
        worktree_attempts.append(item)
        raise RuntimeError("unexpected TUI worktree prep")

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str:
        analyze_calls.append((item, tool))
        raise RuntimeError("Invalid configured analysis command for agent 'claude'")

    app = WorkdashApp(
        work_items=[work_item],
        worktree_callback=worktree_callback,
        analyze_callback=analyze_callback,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)

            await pilot.press("a")
            await pilot.pause()
            await pilot.press("1")
            await _wait_for_footer(
                footer,
                "Analyze failed: Invalid configured analysis command for agent 'claude'",
                pilot,
            )
            _assert_no_modal_screens(app)
            assert analyze_calls == [(work_item, "claude")]
            assert worktree_attempts == []

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_footer_shows_error_status_for_action_failures() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )

    def failing_open_callback(_: WorkItem) -> None:
        raise RuntimeError("xdg-open failed")

    def failing_refresh_callback() -> list[WorkItem]:
        raise RuntimeError("gh refresh failed")

    def failing_analyze_callback(_: WorkItem, __tool: str = "codex") -> str:
        raise RuntimeError("codex analyze failed")

    def failing_launch_callback(_: WorkItem, __tool: str = "codex") -> None:
        raise RuntimeError("codex launch failed")

    def failing_terminal_callback(_: WorkItem) -> None:
        raise RuntimeError("zellij pane failed")

    app = WorkdashApp(
        work_items=[work_item],
        open_callback=failing_open_callback,
        refresh_callback=failing_refresh_callback,
        analyze_callback=failing_analyze_callback,
        launch_callback=failing_launch_callback,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        terminal_callback=failing_terminal_callback,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)

            await pilot.press("o")
            await _wait_for_footer(footer, "Open failed: xdg-open failed", pilot)
            _assert_no_modal_screens(app)

            await pilot.press("r")
            await _wait_for_footer(footer, "Refresh failed: gh refresh failed", pilot)
            _assert_no_modal_screens(app)

            # Press "a" to open AnalyzeDialog, then "1" to choose Claude
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("1")
            await _wait_for_footer(footer, "Analyze failed: codex analyze failed", pilot)
            _assert_no_modal_screens(app)

            # Press "c" to open CodeDialog, then "2" to choose Codex
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("2")
            await _wait_for_footer(footer, "Launch failed: codex launch failed", pilot)
            _assert_no_modal_screens(app)

            await pilot.press("t")
            await _wait_for_footer(footer, "Terminal failed: zellij pane failed", pilot)
            _assert_no_modal_screens(app)

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_worktree_failure_closes_dialogs_and_skips_actions() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )
    launch_calls: list[tuple[WorkItem, str]] = []
    terminal_calls: list[WorkItem] = []
    worktree_attempts: list[WorkItem] = []

    def failing_worktree_callback(item: WorkItem) -> str:
        worktree_attempts.append(item)
        raise RuntimeError(f"worktree failed {len(worktree_attempts)}")

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item, tool))

    def terminal_callback(item: WorkItem) -> None:
        terminal_calls.append(item)

    app = WorkdashApp(
        work_items=[work_item],
        worktree_callback=failing_worktree_callback,
        launch_callback=launch_callback,
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        terminal_callback=terminal_callback,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)

            await pilot.press("c")
            await pilot.pause()
            await pilot.press("2")
            await _wait_for_footer(footer, "Worktree setup failed: worktree failed 1", pilot)
            _assert_no_modal_screens(app)
            assert launch_calls == []

            await pilot.press("t")
            await _wait_for_footer(footer, "Worktree setup failed: worktree failed 2", pilot)
            _assert_no_modal_screens(app)
            assert terminal_calls == []
            assert len(worktree_attempts) == 2

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_include_recomputes_suggestion_marker() -> None:
    """A successful include must reshuffle the suggestion marker to the new oldest item.

    The v1 suggestion heuristic picks the item with the earliest ``created_at``.
    If ``_perform_include`` appends an item without recomputing the markers,
    the render would still star the previously-suggested row even though the
    freshly fetched item is now the oldest (and thus the true suggestion).
    """

    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    existing_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Previously suggested",
        created_at=datetime(2026, 2, 10, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/22",
    )
    # Older created_at so after the include this becomes the new suggestion.
    newly_fetched_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=77,
        title="Freshly fetched older issue",
        created_at=datetime(2026, 1, 5, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 24, 0, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/issues/77",
        included=True,
    )

    app = WorkdashApp(
        work_items=[existing_item],
        suggestion_markers={(WorkItemType.PR, "owner/repo", 22): "*"},
        include_callback=lambda _url, _identities: IncludeResult(fetched_item=newly_fetched_item),
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._perform_include("https://github.com/owner/repo/issues/77")
            await pilot.pause()
            # Marker must now point at the freshly included item's identity.
            assert app._suggestion_markers == {(WorkItemType.ISSUE, "owner/repo", 77): "*"}
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_include_updates_server_session_state() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    existing_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Previously suggested",
        created_at=datetime(2026, 2, 10, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/22",
    )
    newly_fetched_item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=77,
        title="Freshly fetched older issue",
        created_at=datetime(2026, 1, 5, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 24, 0, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/issues/77",
        included=True,
    )
    backend = MagicMock()
    backend.include_item_by_url.return_value = IncludeResult(fetched_item=newly_fetched_item)
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[existing_item],
        suggestion_markers={(WorkItemType.PR, "owner/repo", 22): "*"},
        zellij_session="workdash-main",
    )
    app = WorkdashApp(
        work_items=session.work_items,
        suggestion_markers=session.suggestion_markers,
        include_callback=lambda url, _identities: session.include_item_by_url(url),
        session=session,
        now_utc=now_utc,
    )
    session.items_changed_callback = lambda: app.call_from_thread(app.refresh_from_session)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._perform_include("https://github.com/owner/repo/issues/77")
            await pilot.pause()
            table = app.query_one("#work-items", DataTable)
            assert table.row_count == 2
            assert session.work_items == [existing_item, newly_fetched_item]
            assert session.suggestion_markers == {(WorkItemType.ISSUE, "owner/repo", 77): "*"}
            assert session.list_items()["items"][0]["id"] == "owner/repo#ISSUE-77"
            await pilot.press("q")

    asyncio.run(run_smoke())
    backend.include_item_by_url.assert_called_once_with(
        "https://github.com/owner/repo/issues/77", {(WorkItemType.PR, "owner/repo", 22)}
    )


def test_workdash_app_include_duplicate_url_surfaces_persist_failure() -> None:
    """An OSError raised by the backend callback must be reported, not raised.

    When the pasted URL matches an already-tracked item the backend
    persists the canonical URL synchronously; if that write fails the TUI
    must keep running, surface the error via status, and MUST NOT flip
    ``included=True`` since the store and the UI would otherwise disagree.
    """

    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/22",
    )
    assert work_item.included is False

    def failing_include_callback(_url: str, _identities) -> IncludeResult:
        raise OSError("disk full")

    app = WorkdashApp(
        work_items=[work_item],
        include_callback=failing_include_callback,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._perform_include("https://github.com/owner/repo/pull/22")
            await pilot.pause()
            footer = app.query_one("#status-footer", Static)
            assert footer.render().plain == "Failed to persist URL: disk full"
            # The in-memory flag must not have been flipped before the write
            # succeeded, otherwise the UI and the store disagree.
            assert work_item.included is False
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_capture_todo_reports_the_failure_and_keeps_the_app_alive() -> None:
    """A gh failure while capturing must reach the footer instead of tearing down the TUI."""

    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/22",
    )

    def failing_todo_callback(_text: str, _target: str | None) -> dict[str, object]:
        raise RuntimeError("Failed to create the todo issue in testuser/todos: HTTP 401")

    app = WorkdashApp(
        work_items=[work_item],
        todo_callback=failing_todo_callback,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._perform_todo("Fix the flaky test", "")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Todo failed: Failed to create the todo issue in testuser/todos: HTTP 401",
                pilot,
            )
            assert app.is_running
            _assert_no_modal_screens(app)
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_code_dialog_shows_focus_option_with_active_agent() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )
    focus_calls: list[str] = []
    active_panes = [
        {"pane_id": "terminal_1", "title": "code_repo", "cwd": "/tmp/wt", "kind": "agent"}
    ]

    app = WorkdashApp(
        work_items=[work_item],
        code_choices=WorkdashConfig().tui_code_choices(),
        launch_callback=lambda item, tool: None,  # Required to pass the check
        focus_callback=lambda pane_id: focus_calls.append(pane_id),
        list_agent_panes_callback=lambda item: active_panes,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            # Ensure cursor is on row 0
            await pilot.pause()
            # Check that we have a selected item
            selected = app._selected_item()
            assert selected is not None, "Should have a selected item"
            await pilot.press("c")
            await pilot.pause()
            # Verify CodeDialog is open
            from workdash.tui import CodeDialog

            dialog = None
            for screen in app.screen_stack:
                if isinstance(screen, CodeDialog):
                    dialog = screen
                    break
            assert dialog is not None, (
                f"CodeDialog should be open. Screens: {[type(s).__name__ for s in app.screen_stack]}"
            )
            # Check that the dialog has the focus option
            assert dialog._active_agent_panes == active_panes
            # Press 0 to focus
            await pilot.press("0")
            await pilot.pause()
            # Verify focus callback was called
            assert len(focus_calls) == 1
            assert focus_calls[0] == "terminal_1"
            # Verify no launch happened
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_code_dialog_no_focus_option_without_active_agent() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )

    app = WorkdashApp(
        work_items=[work_item],
        code_choices=WorkdashConfig().tui_code_choices(),
        launch_callback=lambda item, tool: None,
        list_agent_panes_callback=lambda item: [],
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()
            from workdash.tui import CodeDialog

            dialog = None
            for screen in app.screen_stack:
                if isinstance(screen, CodeDialog):
                    dialog = screen
                    break
            assert dialog is not None
            # Verify no active panes
            assert dialog._active_agent_panes == []
            await pilot.press("q")

    asyncio.run(run_smoke())


_SEARCH_NOW_UTC = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)


def _search_work_items() -> list[WorkItem]:
    """Three items whose Type/Repo/Title columns are pairwise distinguishable."""

    return [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=11,
            title="Fix the parser",
            created_at=datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
            url="https://github.com/owner/repo/issues/11",
        ),
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/other",
            number=22,
            title="Ship the docs",
            created_at=datetime(2026, 2, 10, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 21, 0, 0, 0, tzinfo=UTC),
            url="https://github.com/owner/other/pull/22",
        ),
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=33,
            title="Unrelated chore",
            created_at=datetime(2026, 2, 15, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, 22, 0, 0, 0, tzinfo=UTC),
            url="https://github.com/owner/repo/issues/33",
        ),
    ]


async def _submit_search(app: WorkdashApp, pilot, query: str) -> None:
    """Open the search box and submit ``query``; no filter may be active."""

    await pilot.press("/")
    await pilot.pause()
    assert isinstance(app.screen, SearchDialog)
    app.screen.query_one("#search-text", Input).value = query
    await pilot.press("enter")
    await pilot.pause()


def _listed_titles(app: WorkdashApp) -> list[str]:
    table = app.query_one("#work-items", DataTable)
    return [str(table.get_row_at(index)[2]) for index in range(table.row_count)]


# The unfiltered listing of _search_work_items(), newest update first.
_ALL_SEARCH_TITLES = ["Unrelated chore", "Ship the docs", "Fix the parser"]


def test_workdash_app_search_matches_the_owner_cut_from_the_repo_column() -> None:
    work_items = _search_work_items()
    work_items[0].repo = "a-very-long-owner-name/some-repository"
    app = WorkdashApp(work_items=work_items, now_utc=_SEARCH_NOW_UTC)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "a-very-long-owner-name")
            assert _listed_titles(app) == ["Fix the parser"]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_search_matches_rendered_columns_ignoring_case() -> None:
    work_items = _search_work_items()
    app = WorkdashApp(
        work_items=work_items,
        # The oldest item carries the suggestion marker, so its rendered title
        # is "* Fix the parser".
        suggestion_markers={(WorkItemType.ISSUE, "owner/repo", 11): "*"},
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)
            command_bar = app.query_one("#command-footer", Static)

            # Case is ignored and internal spaces must match literally.
            await _submit_search(app, pilot, "fix the PARSER")
            assert _listed_titles(app) == ["* Fix the parser"]
            assert footer.render().plain == "Search matched 1 of 3 item(s)."

            await pilot.press("/")
            await pilot.pause()
            # The same words in reversed order carry no match: the needle is one
            # literal substring, not a set of independently matched words.
            await _submit_search(app, pilot, "parser fix")
            assert _listed_titles(app) == []

            await pilot.press("/")
            await pilot.pause()
            assert _listed_titles(app) == ["Unrelated chore", "Ship the docs", "* Fix the parser"]
            assert footer.render().plain == "Search cleared; 3 item(s) shown."
            # The search action is emphasized only while a filter is active.
            assert command_bar.render().spans == []

            await _submit_search(app, pilot, "OWNER/OTHER")
            assert _listed_titles(app) == ["Ship the docs"]

            await pilot.press("/")
            await pilot.pause()
            await _submit_search(app, pilot, "issue#33")
            assert _listed_titles(app) == ["Unrelated chore"]

            await pilot.press("/")
            await pilot.pause()
            # The Age and Last Update columns are never searched: "6d" is the
            # rendered Last Update of the parser issue (and part of the "16d"
            # age of the docs PR).
            await _submit_search(app, pilot, "6d")
            assert _listed_titles(app) == []

            await pilot.press("/")
            await pilot.pause()
            # The suggestion marker is decoration, not searchable content.
            await _submit_search(app, pilot, "*")
            assert _listed_titles(app) == []
            assert footer.render().plain == "Search matched 0 of 3 item(s)."

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_search_box_dismissed_without_text_leaves_the_list_unchanged() -> None:
    app = WorkdashApp(work_items=_search_work_items(), now_utc=_SEARCH_NOW_UTC)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("/")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert _listed_titles(app) == _ALL_SEARCH_TITLES

            await _submit_search(app, pilot, "")
            assert _listed_titles(app) == _ALL_SEARCH_TITLES

            await _submit_search(app, pilot, "   ")
            assert _listed_titles(app) == _ALL_SEARCH_TITLES

            _assert_no_modal_screens(app)
            assert app.query_one("#status-footer", Static).render().plain == "Ready."
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_actions_use_the_row_selected_in_the_filtered_list() -> None:
    work_items = _search_work_items()
    opened: list[WorkItem] = []
    app = WorkdashApp(
        work_items=work_items,
        open_callback=opened.append,
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            # Sorted by updated_at descending the parser issue is last; after
            # filtering the cursor must clamp onto it as the only row.
            await _submit_search(app, pilot, "parser")
            assert table.cursor_row == 0
            assert app._selected_item() is work_items[0]
            await pilot.press("o")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Opened issue owner/repo#11.",
                pilot,
            )
            assert opened == [work_items[0]]

            await pilot.press("/")
            await pilot.pause()
            await _submit_search(app, pilot, "no-such-item")
            assert app._selected_item() is None
            await pilot.press("o")
            await _wait_for_footer(app.query_one("#status-footer", Static), "Open skipped.", pilot)
            assert opened == [work_items[0]]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_capture_todo_clears_the_search_filter() -> None:
    work_items = _search_work_items()
    app = WorkdashApp(
        work_items=work_items,
        session=MagicMock(work_items=work_items, suggestion_markers={}),
        todo_callback=lambda _text, _target: {"item_id": "issue owner/todos#7"},
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "parser")
            assert _listed_titles(app) == ["Fix the parser"]

            # The captured todo joins the loaded list, so it must not stay hidden.
            await app._perform_todo("Write the changelog", "")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Captured todo issue owner/todos#7.",
                pilot,
            )
            assert _listed_titles(app) == _ALL_SEARCH_TITLES
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_include_of_an_already_tracked_item_clears_the_search_filter() -> None:
    work_items = _search_work_items()
    duplicate = work_items[1]
    app = WorkdashApp(
        work_items=work_items,
        include_callback=lambda _url, _identities: IncludeResult(
            duplicate_identity=(duplicate.item_type, duplicate.repo, duplicate.number)
        ),
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "parser")
            assert _listed_titles(app) == ["Fix the parser"]

            await app._perform_include(duplicate.url)
            await pilot.pause()
            assert _listed_titles(app) == _ALL_SEARCH_TITLES
            assert app._selected_item() is duplicate
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_failed_include_keeps_the_search_filter_active() -> None:
    app = WorkdashApp(
        work_items=_search_work_items(),
        include_callback=MagicMock(
            side_effect=[IncludeResult(invalid=True), IncludeResult(transient_failure=True)]
        ),
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            command_bar = app.query_one("#command-footer", Static)
            await _submit_search(app, pilot, "parser")

            # A failed include leaves the loaded list unchanged, so the filter stays on.
            for _ in range(2):
                await app._perform_include("https://github.com/owner/repo/issues/44")
                await pilot.pause()
                assert _listed_titles(app) == ["Fix the parser"]
                spans = command_bar.render().spans
                assert [(s.start, s.end, str(s.style)) for s in spans] == [(0, 9, "bold")]

            # The filter is still live, so "/" clears it instead of reopening search.
            await pilot.press("/")
            await pilot.pause()
            _assert_no_modal_screens(app)
            assert _listed_titles(app) == _ALL_SEARCH_TITLES
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_analyze_keeps_the_active_search_filter() -> None:
    app = WorkdashApp(
        work_items=_search_work_items(),
        analyze_callback=lambda _item, _choice="codex": None,
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "parser")
            assert _listed_titles(app) == ["Fix the parser"]

            # A fresh analysis re-renders the table to show the new suggestion
            # markers, but it does not change the loaded list, so the filter stays.
            await pilot.press("a")
            await pilot.pause()
            await pilot.press("1")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Analyzed issue owner/repo#11 with Claude.",
                pilot,
            )
            assert _listed_titles(app) == ["Fix the parser"]
            await pilot.press("q")

    asyncio.run(run_smoke())


def _cursor_anchor_work_items() -> list[WorkItem]:
    """Five items where the "parser" matches land on different rows once unfiltered."""

    def _item(number: int, title: str, updated_day: int) -> WorkItem:
        return WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=number,
            title=title,
            created_at=datetime(2026, 2, 1, 0, 0, 0, tzinfo=UTC),
            updated_at=datetime(2026, 2, updated_day, 0, 0, 0, tzinfo=UTC),
            url=f"https://github.com/owner/repo/issues/{number}",
        )

    # Listed newest first: epsilon, beta, gamma, delta, alpha.
    return [
        _item(1, "epsilon parser", 25),
        _item(2, "beta docs", 24),
        _item(3, "gamma parser", 23),
        _item(4, "delta chore", 22),
        _item(5, "alpha parser", 21),
    ]


# The unfiltered listing of _cursor_anchor_work_items(), newest update first.
_ALL_CURSOR_ANCHOR_TITLES = [
    "epsilon parser",
    "beta docs",
    "gamma parser",
    "delta chore",
    "alpha parser",
]


def test_workdash_app_clearing_the_search_filter_keeps_the_selected_item() -> None:
    work_items = _cursor_anchor_work_items()
    opened: list[WorkItem] = []
    app = WorkdashApp(
        work_items=work_items,
        open_callback=opened.append,
        now_utc=_SEARCH_NOW_UTC,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            await _submit_search(app, pilot, "parser")
            assert _listed_titles(app) == ["epsilon parser", "gamma parser", "alpha parser"]

            await pilot.press("down", "down")
            await pilot.pause()
            assert app._selected_item() is work_items[4]

            # Clearing the filter must keep the same item selected, not row 2 of
            # the full list ("gamma parser").
            await pilot.press("/")
            await pilot.pause()
            assert _listed_titles(app) == _ALL_CURSOR_ANCHOR_TITLES
            assert app._selected_item() is work_items[4]
            assert table.cursor_row == 4

            await pilot.press("o")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Opened issue owner/repo#5.",
                pilot,
            )
            assert opened == [work_items[4]]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_session_refresh_keeps_the_selected_item_while_filtered() -> None:
    work_items = _cursor_anchor_work_items()
    session = MagicMock(work_items=work_items, suggestion_markers={})
    app = WorkdashApp(work_items=work_items, session=session, now_utc=_SEARCH_NOW_UTC)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "parser")
            await pilot.press("down", "down")
            await pilot.pause()
            assert app._selected_item() is work_items[4]

            # A control-server push clears the filter and re-renders with no keypress.
            app.refresh_from_session()
            await pilot.pause()
            assert _listed_titles(app) == _ALL_CURSOR_ANCHOR_TITLES
            assert app._selected_item() is work_items[4]
            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_session_refresh_dropping_the_selected_item_clamps_the_cursor() -> None:
    """With no search involved, a shorter refreshed list must pull the cursor back in range."""

    work_items = _cursor_anchor_work_items()
    session = MagicMock(work_items=work_items, suggestion_markers={})
    app = WorkdashApp(work_items=work_items, session=session, now_utc=_SEARCH_NOW_UTC)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            await pilot.press("down", "down", "down", "down")
            await pilot.pause()
            assert app._selected_item() is work_items[4]

            session.work_items = work_items[:2]
            app.refresh_from_session()
            await pilot.pause()
            assert _listed_titles(app) == ["epsilon parser", "beta docs"]
            assert table.cursor_row == 1
            assert app._selected_item() is work_items[1]
            await pilot.press("q")

    asyncio.run(run_smoke())
