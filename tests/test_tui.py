import asyncio
import threading
from datetime import UTC, datetime

import pytest

pytest.importorskip("textual")

from textual.screen import ModalScreen
from textual.widgets import DataTable, Static

from workdash.backend import IncludeResult
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import AnalyzeDialog, CodeDialog, WorkdashApp

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
                "PR",
                "owner/repo",
                "Implement renderer",
                "1d",
                "1d",
            ]
            assert table.get_row_at(1) == [
                "ISSUE",
                "owner/repo",
                "* Fix parser",
                "6d",
                "6d",
            ]

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
                "PR",
                "owner/repo",
                "Implement renderer",
                "6d",
                "6d",
            ]

            work_item.included = True
            app._render_table()

            assert [key.value for key in table.rows] == [row_key]
            assert [str(c) for c in table.get_row(row_key)] == [
                "PR+",
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
                "REVIEW",
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
                "PR",
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
                "PR",
                "owner/repo",
                "* Ship refresh",
                "0d",
                "0d",
            ]

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
                "ISSUE",
                "owner/repo",
                "Fix parser",
                "6d",
                "6d",
            ]

            session.work_items = [refreshed_item]
            session.suggestion_markers = {(WorkItemType.PR, "owner/repo", 33): "*"}
            app._refresh_from_session()

            assert table.row_count == 1
            assert [str(c) for c in table.get_row_at(0)] == [
                "PR",
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
                "ISSUE",
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
            assert (
                command_bar.render().plain
                == "(o)pen (r)efresh (a)nalyze (c)ode (t)erminal (i)nclude (q)uit"
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
