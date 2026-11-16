import asyncio
import threading
from dataclasses import replace
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
from workdash.tui import (
    AnalyzeDialog,
    BusyScreen,
    CodeDialog,
    ReviewDialog,
    SearchDialog,
    WorkdashApp,
)

_DEFAULT_TUI_CONFIG = WorkdashConfig(
    agents={
        "claude": AgentConfig(analyze="claude -p", launch="claude"),
        "codex": AgentConfig(analyze="codex exec", launch="codex"),
        "pi": AgentConfig(launch="pi"),
    },
)


def test_dashboard_todo_type_and_search() -> None:
    from workdash.control import format_work_item_id

    now = datetime.now(UTC)
    base = WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="Owner/Todos",
        number=21,
        title="Todo",
        created_at=now,
        updated_at=now,
        url="https://github.com/Owner/Todos/issues/21",
    )
    items = [
        base,
        replace(base, number=22, title="Targeted", todo_target="owner/project", included=True),
        replace(base, repo="owner/project", number=23, title="Issue"),
        replace(
            base,
            repo="owner/project",
            number=24,
            title="Target is todo repo",
            todo_target="owner/todos",
        ),
        replace(
            base, kind=WorkItemKind.AUTHORED_PR, item_type=WorkItemType.PR, number=25, title="PR"
        ),
    ]
    session = MagicMock()
    session.config = WorkdashConfig(todo_repository="owner/todos")
    session.work_items = items
    session.suggestion_markers = {}
    session.autodispatch_sessions.return_value = []
    app = WorkdashApp(session=session)

    async def exercise() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            labels = {
                str(row[2]): str(row[0]).strip()
                for row in (table.get_row_at(i) for i in range(table.row_count))
            }
            assert labels == {
                "Todo": "TD 21",
                "Targeted": "TD+ 22",
                "Issue": "IS 23",
                "Target is todo repo": "IS 24",
                "PR": "PR 25",
            }
            assert format_work_item_id(items[0]) == "Owner/Todos#ISSUE-21"
            assert format_work_item_id(items[1]) == "owner/project#ISSUE-WT22"
            await _submit_search(app, pilot, "TD+ 22")
            assert _listed_titles(app) == ["Targeted"]
            await pilot.press("/")
            await _submit_search(app, pilot, "IS 23")
            assert _listed_titles(app) == ["Issue"]

    asyncio.run(exercise())


def test_dashboard_initial_layout_and_shortcuts() -> None:
    items = _search_work_items()
    items[0] = replace(items[0], title="Long title " * 15)
    session = MagicMock()
    session.work_items = items
    session.suggestion_markers = {}
    session.autodispatch_sessions.return_value = []
    session.take_completed_autodispatch_items.return_value = []
    app = WorkdashApp(work_items=items[:1], session=session)

    async def exercise() -> None:
        async with app.run_test(size=(80, 10)) as pilot:
            table = app.query_one("#work-items", DataTable)
            command = app.query_one("#command-footer", Static)
            status = app.query_one("#status-footer", Static)
            assert status.render().plain == "Loaded 3 item(s)."
            assert table.region.bottom <= command.region.y < status.region.y
            for width in (80, 60, 100):
                await pilot.resize_terminal(width, 10)
                await pilot.pause()
                rows = [strip.text for strip in app.screen._compositor.render_strips()]
                assert "Age" in rows[0] and "Last Update" in rows[0], rows
                long_row = next(row for row in rows if "Long title" in row)
                assert len([word for word in long_row.split() if word.endswith("d")]) >= 2, rows
            assert not table.show_horizontal_scrollbar
            await pilot.press("right", "right", "down")
            await pilot.pause()
            assert table.scroll_x == 0
            assert table.cursor_row == 1
            hint = command.render()
            for key in "/oracdiwq":
                index = hint.plain.index(f"({key})") + 1
                assert any(
                    span.start <= index < span.end and str(span.style) == "bold"
                    for span in hint.spans
                )
            assert not any(span.start <= 3 < span.end for span in hint.spans)
            await _submit_search(app, pilot, "docs")
            hint = command.render()
            assert any(
                span.start == 0 and span.end == len(app.SEARCH_HINT_TEXT) for span in hint.spans
            )

    asyncio.run(exercise())


def test_workdash_app_renders_type_repo_title_age_last_update_and_analysis_columns() -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=22,
            title="Implement renderer",
            has_sub_issues=True,
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
            assert table.get_row_at(0)[2].style == "bold"
            # Sorted by updated_at descending; PR #22 (updated 2/25) before issue #11 (updated 2/20)
            # PR #22 is within 24h of now_utc so cells are bold Text objects
            assert [str(c) for c in table.get_row_at(0)] == [
                "  VR 22",
                "owner/repo",
                "Implement renderer",
                "1d",
                "1d",
            ]
            assert [str(c) for c in table.get_row_at(1)] == [
                "  IS 11",
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
            repo="owner/123456789",  # exactly the column width
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
            assert str(table.get_row_at(0)[1]) == "owner/123456789"
            # The owner is cut, the repository name itself stays readable.
            assert str(table.get_row_at(1)[1]) == "…-python-longer"
            assert len(str(table.get_row_at(1)[1])) == 15

    asyncio.run(run_smoke())


@pytest.mark.parametrize(
    ("ci_state", "review_decision", "review_requested", "expected_prefix", "expected_spans"),
    [
        ("SUCCESS", None, False, "✓ ", [(0, 1, "green")]),
        ("FAILURE", None, False, "✗ ", [(0, 1, "red")]),
        ("ERROR", None, False, "✗ ", [(0, 1, "red")]),
        ("PENDING", None, False, "● ", [(0, 1, "yellow")]),
        ("EXPECTED", None, False, "● ", [(0, 1, "yellow")]),
        # A pull request with no checks, and a state GitHub may add later, both
        # fall back to the blank CI symbol rather than inventing one.
        (None, None, False, "  ", []),
        ("SOMETHING_NEW", None, False, "  ", []),
        # The review symbol is independent of the CI symbol.
        (None, "APPROVED", False, " ✓", [(1, 2, "green")]),
        (None, "CHANGES_REQUESTED", False, " ✗", [(1, 2, "red")]),
        (None, None, True, " ?", [(1, 2, "yellow")]),
        # A pull request with no reviewer requested carries no review symbol.
        (None, None, False, "  ", []),
        ("SUCCESS", "APPROVED", False, "✓✓", [(0, 1, "green"), (1, 2, "green")]),
    ],
)
def test_workdash_app_colors_the_ci_and_review_symbols_in_the_type_column(
    ci_state: str | None,
    review_decision: str | None,
    review_requested: bool,
    expected_prefix: str,
    expected_spans: list[tuple[int, int, str]],
) -> None:
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    work_item = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        # Older than the 24h bold cutoff, so only the CI/review symbols carry a style.
        created_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 20, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
        ci_state=ci_state,
        review_decision=review_decision,
        review_requested=review_requested,
    )
    app = WorkdashApp(work_items=[work_item], now_utc=now_utc)

    async def run_smoke() -> None:
        async with app.run_test() as _:
            table = app.query_one("#work-items", DataTable)
            cell = table.get_row_at(0)[0]

            assert cell.plain == f"{expected_prefix}PR 22"
            assert [(s.start, s.end, s.style) for s in cell.spans] == expected_spans

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
                "  VR 22",
                "owner/repo",
                "Implement renderer",
                "6d",
                "6d",
            ]

            work_item.included = True
            app._render_table()

            assert [key.value for key in table.rows] == [row_key]
            assert [str(c) for c in table.get_row(row_key)] == [
                "  VR+ 22",
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
                "  RV 22",
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

            # Press "c" to open CodeDialog, then "3" to choose pi
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            # Press "c" to open CodeDialog, then "e" to choose VSCode
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()

            await pilot.press("c", "t")
            await pilot.pause()

            await pilot.press("q")

            assert open_calls == [(WorkItemType.PR, 22)]
            assert analyze_calls == [(WorkItemType.PR, 22, "claude")]
            assert launch_calls == [
                (WorkItemType.PR, 22, "codex"),
                (WorkItemType.PR, 22, "pi"),
                (WorkItemType.PR, 22, "vscode"),
            ]
            assert terminal_calls == [(WorkItemType.PR, 22)]
            assert [str(c) for c in table.get_row_at(0)] == [
                "  VR 22",
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
                "  VR 33",
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
                "  VR 33",
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
        config = WorkdashConfig()
        work_items = [initial_item]
        suggestion_markers = {}

        def autodispatch_sessions(self):
            return []

        def take_completed_autodispatch_items(self):
            return set()

    session = Session()
    app = WorkdashApp(
        work_items=[initial_item],
        session=session,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert [str(c) for c in table.get_row_at(0)] == [
                "  IS 11",
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
                "  VR 33",
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
        launch_callback=lambda _, __tool="codex": {"status": "launched"},
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        terminal_callback=lambda _: None,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)
            assert footer.render().plain == "Loaded 1 item(s)."

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
            assert footer.render().plain == "Analyzed pr owner/repo#22 with claude."

            # Press "c" to open CodeDialog, then "2" to choose Codex
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            assert footer.render().plain == "Launched codex for pr owner/repo#22."

            # Press "c" to open CodeDialog, then "3" to choose pi
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            assert footer.render().plain == "Launched pi for pr owner/repo#22."

            # Press "c" to open CodeDialog, then "e" to choose VSCode
            await pilot.press("c")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert footer.render().plain == "Launched VSCode for pr owner/repo#22."

            await pilot.press("c", "t")
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
            assert [str(c) for c in table.get_row_at(0)] == [
                "  IS 11",
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
                "(/)search (o)pen (r)efresh (a)nalyze (c)ode (d)ispatch (i)nclude (w)todo (q)uit"
            )
            await pilot.press("q")

    asyncio.run(run_smoke())


def _assert_no_modal_screens(app: WorkdashApp) -> None:
    lingering = [
        screen.__class__.__name__ for screen in app.screen_stack if isinstance(screen, ModalScreen)
    ]
    assert lingering == []


async def _wait_for_busy_message(expected: str, pilot) -> None:
    for _ in range(20):
        await pilot.pause()
        if isinstance(pilot.app.screen, BusyScreen):
            message = pilot.app.screen.query_one("#busy-message", Static).render().plain
            assert message == expected
            return
    raise AssertionError(f"no busy screen shown; expected {expected!r}")


async def _wait_for_footer(footer: Static, expected: str, pilot) -> None:
    for _ in range(20):
        await pilot.pause()
        if footer.render().plain == expected:
            return
    assert footer.render().plain == expected


def test_analyze_dialog_shows_nine_agents_and_cached_analysis_without_scrolling() -> None:
    work_item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
        analyzed_at=datetime(2026, 2, 24, 0, 0, 0, tzinfo=UTC),
    )
    config = WorkdashConfig(
        agents={f"agent{number}": AgentConfig(analyze="agent -p") for number in range(1, 10)}
    )
    app = WorkdashApp(
        work_items=[work_item],
        analyze_callback=lambda item, tool: None,
        analyze_choices=config.tui_analyze_choices(),
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("a")
            await pilot.pause()
            assert isinstance(app.screen, AnalyzeDialog)
            lines = [str(line.render()) for line in app.screen.query(".analyze-line")]
            assert lines[-2:] == ["(9) Analyze with agent9", "(Esc) Cancel"]
            assert app.screen.query_one("#analyze-shell").max_scroll_y == 0

    asyncio.run(run_smoke())


def test_code_dialog_shows_every_choice_without_scrolling() -> None:
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
    config = WorkdashConfig(
        agents={f"agent{number}": AgentConfig(launch="agent") for number in range(1, 10)}
    )
    app = WorkdashApp(
        work_items=[work_item],
        code_choices=config.tui_code_choices(),
        launch_callback=lambda item, tool: {"status": "launched"},
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, CodeDialog)
            lines = [str(line.render()) for line in app.screen.query(".code-line")]
            assert lines[1] == "(1) agent1"
            assert lines[-5:] == [
                "(e) edit (VSCode Copilot)",
                "(d) branchdiff",
                "(t) terminal",
                "(r) auto review",
                "(Esc) Cancel",
            ]
            assert app.screen.query_one("#code-shell").max_scroll_y == 0

    asyncio.run(run_smoke())


def test_code_menu_auto_review_waits_with_a_progress_dialog_then_reports_completion() -> None:
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
    review_calls: list[tuple[WorkItem, str]] = []
    release_review = threading.Event()

    def review_callback(item: WorkItem, agent: str) -> dict[str, object]:
        review_calls.append((item, agent))
        release_review.wait(5)
        return {"status": "reviewed", "agent": agent}

    app = WorkdashApp(
        work_items=[work_item],
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        review_callback=review_callback,
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, CodeDialog)
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(app.screen, ReviewDialog)
            lines = [str(line.render()) for line in app.screen.query(".review-line")]
            assert lines == ["Review with:", "(1) claude", "(2) codex", "(Esc) Cancel"]

            await pilot.press("2")
            await _wait_for_busy_message("Reviewing with codex...", pilot)
            assert review_calls == [(work_item, "codex")]
            release_review.set()
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Reviewed pr owner/repo#22 with codex.",
                pilot,
            )
            _assert_no_modal_screens(app)

    asyncio.run(run_smoke())


def test_code_menu_auto_review_reports_the_agent_that_reviewed() -> None:
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
    review_calls: list[tuple[WorkItem, str]] = []
    app = WorkdashApp(
        work_items=[work_item],
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        review_callback=lambda item, agent: (
            review_calls.append((item, agent)) or {"status": "reviewed", "agent": "claude"}
        ),
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c", "r", "2")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Reviewed pr owner/repo#22 with claude.",
                pilot,
            )
            assert review_calls == [(work_item, "codex")]

    asyncio.run(run_smoke())


def test_code_menu_auto_review_reports_nothing_to_review() -> None:
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
    app = WorkdashApp(
        work_items=[work_item],
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        review_callback=lambda item, agent: {"status": "nothing_to_review"},
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c", "r", "1")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Nothing to review for issue owner/repo#11.",
                pilot,
            )

    asyncio.run(run_smoke())


def test_code_menu_auto_review_failure_reports_the_error() -> None:
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

    def failing_review(item: WorkItem, agent: str) -> dict[str, object]:
        raise RuntimeError("review failed")

    app = WorkdashApp(
        work_items=[work_item],
        analyze_choices=_DEFAULT_TUI_CONFIG.tui_analyze_choices(),
        review_callback=failing_review,
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )
    notifications: list[tuple[str, str | None]] = []
    app.notify = lambda message, **kwargs: notifications.append(  # type: ignore[method-assign]
        (message, kwargs.get("severity"))
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c", "r", "1")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Review failed: review failed",
                pilot,
            )
            assert notifications == [("Review failed: review failed", "error")]
            _assert_no_modal_screens(app)

    asyncio.run(run_smoke())


def test_code_dialog_actions_target_the_item_selected_when_the_dialog_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("workdash.tui.launch_branchdiff_context", MagicMock())
    now_utc = datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC)
    selected = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=22,
        title="Implement renderer",
        created_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/22",
    )
    other = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=23,
        title="Fix parser",
        created_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 25, 0, 0, 0, tzinfo=UTC),
        url="https://example.com/pull/23",
    )
    session = MagicMock()
    session.work_items = [selected, other]
    session.suggestion_markers = {}
    session.get_terminal_panes_for_item.return_value = []
    worktree_calls: list[WorkItem] = []
    terminal_calls: list[WorkItem] = []
    app = WorkdashApp(
        session=session,
        worktree_callback=lambda item: worktree_calls.append(item) or "/tmp/worktree",
        terminal_callback=lambda item: terminal_calls.append(item) or {"status": "opened"},
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()

            # A server refresh drops the selected item while the dialog is open.
            await pilot.press("c")
            await pilot.pause()
            session.work_items = [other]
            app.refresh_from_session()
            await pilot.press("d")
            for _ in range(20):
                await pilot.pause()
                if worktree_calls:
                    break
            assert worktree_calls == [selected]

            await pilot.press("c")
            await pilot.pause()
            session.work_items = [selected]
            app.refresh_from_session()
            await pilot.press("t")
            for _ in range(20):
                await pilot.pause()
                if terminal_calls:
                    break
            assert terminal_calls == [other]

    asyncio.run(run_smoke())


def test_agent_names_with_markup_characters_are_shown_literally() -> None:
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
    config = WorkdashConfig(agents={"a[/b]": AgentConfig(launch="agent")})
    app = WorkdashApp(
        work_items=[work_item],
        code_choices=config.tui_code_choices(),
        worktree_callback=lambda item: "/tmp/worktree",
        launch_callback=lambda item, tool: {"status": "launched"},
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, CodeDialog)
            lines = [str(line.render()) for line in app.screen.query(".code-line")]
            assert "(1) a[/b]" in lines
            await pilot.press("1")
            await _wait_for_footer(
                app.query_one("#status-footer", Static),
                "Launched a[/b] for pr owner/repo#22.",
                pilot,
            )

    asyncio.run(run_smoke())


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
        launch_callback=lambda item, tool="codex": (
            launch_calls.append((item, tool)) or {"status": "launched"}
        ),
        analyze_choices=WorkdashConfig(
            agents={"codex": AgentConfig(analyze="codex exec")}
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
            await pilot.press("e")
            await pilot.pause()
            assert worktree_attempts == []
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

            await pilot.press("c", "t")
            await _wait_for_footer(footer, "Terminal failed: zellij pane failed", pilot)
            _assert_no_modal_screens(app)

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_code_reports_a_failed_focus_of_the_running_agent_pane() -> None:
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
    launch_callback = MagicMock(return_value={"status": "already_running", "pane_id": "terminal_7"})

    app = WorkdashApp(
        work_items=[work_item],
        launch_callback=launch_callback,
        code_choices=_DEFAULT_TUI_CONFIG.tui_code_choices(),
        focus_callback=MagicMock(side_effect=RuntimeError("pane is gone")),
        now_utc=datetime(2026, 2, 26, 0, 0, 0, tzinfo=UTC),
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            footer = app.query_one("#status-footer", Static)

            await pilot.press("c", "1")
            await _wait_for_footer(footer, "Failed to focus pane: pane is gone", pilot)
            _assert_no_modal_screens(app)
            launch_callback.assert_called_once()

            await pilot.press("q")

    asyncio.run(run_smoke())


def test_workdash_app_include_recomputes_suggestion_marker(monkeypatch, tmp_path) -> None:
    """A successful include must reshuffle the suggestion marker to the new oldest item.

    The v1 suggestion heuristic picks the item with the earliest ``created_at``.
    If ``_perform_include`` appends an item without recomputing the markers,
    the render would still star the previously-suggested row even though the
    freshly fetched item is now the oldest (and thus the true suggestion).
    """
    from workdash.backend import WorkdashBackend
    from workdash.included_items import IncludedItemsStore

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
        has_sub_issues=True,
        sub_issue_total=1,
    )
    backend = WorkdashBackend(
        config=WorkdashConfig(github_username="user"),
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )
    monkeypatch.setattr(
        backend.github_client, "fetch_item_by_url", lambda *args: newly_fetched_item
    )

    app = WorkdashApp(
        work_items=[existing_item],
        suggestion_markers={(WorkItemType.PR, "owner/repo", 22): "*"},
        include_callback=backend.include_item_by_url,
        now_utc=now_utc,
    )

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._perform_include("https://github.com/owner/repo/issues/77")
            await pilot.pause()
            # Marker must now point at the freshly included item's identity.
            assert app._suggestion_markers == {(WorkItemType.ISSUE, "owner/repo", 77): "*"}
            title = app.query_one("#work-items", DataTable).get_row("issue:owner/repo#77")[2]
            assert title.style == "bold cyan"
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


def test_type_width_tracks_widest_visible_item_after_search() -> None:
    work_items = _search_work_items()
    work_items[0].number = 123456789
    work_items[0].included = True
    app = WorkdashApp(work_items=work_items, now_utc=_SEARCH_NOW_UTC)

    async def run_smoke() -> None:
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert table.columns["type"].width == len("  IS+ 123456789")
            await _submit_search(app, pilot, "ship the docs")
            assert table.columns["type"].width == len("  VR 22")
            await pilot.press("/")
            await pilot.pause()
            assert table.columns["type"].width == len("  IS+ 123456789")

    asyncio.run(run_smoke())


def test_workdash_app_search_matches_displayed_state_suffix() -> None:
    work_items = _search_work_items()
    work_items[0].state = "CLOSED"
    app = WorkdashApp(work_items=work_items, now_utc=_SEARCH_NOW_UTC)

    async def run() -> None:
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "parser (closed)")
            assert _listed_titles(app) == ["Fix the parser (closed)"]

    asyncio.run(run())


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
            assert not any(s.start == 0 and s.end == 9 for s in command_bar.render().spans)

            await _submit_search(app, pilot, "OWNER/OTHER")
            assert _listed_titles(app) == ["Ship the docs"]

            await pilot.press("/")
            await pilot.pause()
            await _submit_search(app, pilot, "issue#33")
            assert _listed_titles(app) == ["Unrelated chore"]

            await pilot.press("/")
            await pilot.pause()
            await _submit_search(app, pilot, "is 33")
            assert _listed_titles(app) == ["Unrelated chore"]

            await pilot.press("/")
            await pilot.pause()
            await _submit_search(app, pilot, "vr 22")
            assert _listed_titles(app) == ["Ship the docs"]

            await pilot.press("/")
            await pilot.pause()
            await _submit_search(app, pilot, "check#22")
            assert _listed_titles(app) == ["Ship the docs"]

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
            assert app.query_one("#status-footer", Static).render().plain == "Loaded 3 item(s)."
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
                assert (0, 9, "bold") in [(s.start, s.end, str(s.style)) for s in spans]

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
                "Analyzed issue owner/repo#11 with claude.",
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


def test_refresh_renders_sub_issue_parents_from_github(monkeypatch, tmp_path) -> None:
    import json
    import subprocess

    from workdash.backend import WorkdashBackend
    from workdash.included_items import IncludedItemsStore

    children = {("alpha", "one", "41"): ["CLOSED", "CLOSED"]}
    nodes = []
    for typename, repo, number, title in [
        ("Issue", "alpha/one", 41, "Parent with closed children"),
        ("Issue", "beta/two", 41, "Leaf"),
        ("PullRequest", "alpha/one", 42, "Pull request"),
    ]:
        nodes.append(
            {
                "__typename": typename,
                "id": f"{repo}-{number}",
                "number": number,
                "title": title,
                "url": f"https://github.com/{repo}/issues/{number}",
                "createdAt": "2026-02-20T00:00:00Z",
                "updatedAt": "2026-02-20T00:00:00Z",
                "state": "OPEN",
                "repository": {"nameWithOwner": repo},
                **(
                    {
                        "subIssuesSummary": {
                            "total": len(children.get((*repo.split("/"), str(number)), []))
                        }
                    }
                    if typename == "Issue"
                    else {}
                ),
            }
        )

    def fake_gh(command, **kwargs):
        if command[:3] in (
            ["gh", "search", "prs"],
            ["gh", "search", "issues"],
            ["gh", "issue", "list"],
        ):
            payload = []
        elif "assigned: search" in command[4]:
            payload = {"data": {"assigned": {"nodes": []}}}
        elif "tracked: search" in command[4]:
            payload = {"data": {"tracked": {"nodes": nodes}}}
        elif "issues(first:" in command[4]:
            payload = {"data": {"repository": {"issues": {"nodes": []}}}}
        elif "closingIssuesReferences" in command[4]:
            payload = {"data": {"p0": {"pullRequest": {"closingIssuesReferences": {"nodes": []}}}}}
        else:
            payload = {"data": {"tracked": {"nodes": nodes}}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_gh)
    backend = WorkdashBackend(
        config=WorkdashConfig(github_username="user", repositories=("alpha/one", "beta/two")),
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )
    app = WorkdashApp(
        work_items=[],
        refresh_callback=backend.load_items,
        now_utc=datetime(2026, 2, 26, tzinfo=UTC),
    )

    async def run() -> None:
        async with app.run_test() as pilot:
            await app.action_refresh_items()
            await pilot.pause()
            table = app.query_one("#work-items", DataTable)
            parent = table.get_row("issue:alpha/one#41")
            leaf = table.get_row("issue:beta/two#41")
            pr = table.get_row("pr:alpha/one#42")
            assert str(parent[0]).endswith("IS 41") and str(parent[1]) == "alpha/one"
            assert (
                str(parent[2]).endswith("Parent with closed children")
                and parent[2].style == "bold cyan"
            )
            assert str(leaf[0]).endswith("IS 41") and str(leaf[1]) == "beta/two"
            assert str(leaf[2]).endswith("Leaf") and not leaf[2].style
            assert (
                str(pr[0]).endswith("VR 42")
                and str(pr[2]).endswith("Pull request")
                and not pr[2].style
            )

    asyncio.run(run())


def test_autodispatch_refresh_coalesces_transitions_without_blocking() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    first = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="First",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
        has_sub_issues=True,
    )
    second = replace(first, number=2, title="Second", has_sub_issues=False)
    started, release = threading.Event(), threading.Event()
    backend = MagicMock()

    def fetch(repo, number, **kwargs):
        if number == 1:
            started.set()
            assert release.wait(5)
            return replace(first, title="Finished")
        return replace(second, title="Also finished")

    backend.github_client.fetch_item.side_effect = fetch
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[first, second],
        suggestion_markers={},
        zellij_session=None,
    )
    entry = {
        "id": "s",
        "status": "running",
        "scope": "owner/repo#ISSUE-2",
        "current_item": "owner/repo#ISSUE-1",
        "selected_group": None,
        "stage": "developing",
        "launch_reason": None,
        "iteration": 1,
        "elapsed_seconds": 0,
        "error": None,
    }
    session.autodispatch_sessions = MagicMock(return_value=[entry])
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert table.get_row("issue:owner/repo#1")[2].style == "bold magenta"
            assert table.get_row("issue:owner/repo#2")[2].style == "bold"
            session.completed_autodispatch_item(entry["current_item"])
            entry["current_item"] = "owner/repo#ISSUE-2"
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.01)
                if started.is_set():
                    break
            assert started.is_set()
            await pilot.press("down")
            assert table.cursor_row == 1
            assert table.get_row("issue:owner/repo#1")[2].style == "bold cyan"
            assert table.get_row("issue:owner/repo#2")[2].style == "bold magenta"
            session.completed_autodispatch_item(entry["current_item"])
            entry["status"] = "stopped"
            app._update_autodispatch_progress()
            release.set()
            for _ in range(150):
                await pilot.pause(0.02)
                if str(table.get_row("issue:owner/repo#2")[2]) == "Also finished":
                    break
            assert str(table.get_row("issue:owner/repo#1")[2]) == "Finished"
            assert str(table.get_row("issue:owner/repo#2")[2]) == "Also finished"
            assert table.cursor_row == 1

    try:
        asyncio.run(run())
    finally:
        release.set()


def test_autodispatch_refresh_retries_after_failure_without_blocking() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="Before",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
    )
    backend = MagicMock()
    backend.github_client.fetch_item.side_effect = [
        RuntimeError("unavailable"),
        replace(item, title="After"),
    ]
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[item, replace(item, number=2, title="Other")],
        suggestion_markers={},
        zellij_session=None,
    )
    session.autodispatch_sessions = MagicMock(return_value=[])
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            original_set_timer = app.set_timer
            delays = []

            def shorten_retry(delay, callback):
                delays.append(delay)
                return original_set_timer(0.1, callback)

            app.set_timer = shorten_retry
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.01)
                if backend.github_client.fetch_item.call_count:
                    break
            assert backend.github_client.fetch_item.call_count == 1
            assert delays == [30]
            await pilot.press("down")
            table = app.query_one("#work-items", DataTable)
            assert table.cursor_row == 1
            for _ in range(150):
                await pilot.pause(0.02)
                if str(table.get_row("issue:owner/repo#1")[2]) == "After":
                    break
            assert str(table.get_row("issue:owner/repo#1")[2]) == "After"
            assert backend.github_client.fetch_item.call_count == 2

    asyncio.run(run())


def test_autodispatch_updates_known_linked_pull_request_status() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    pr = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=9,
        title="Pull request",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/9",
        closing_issue_numbers=(1,),
        ci_state="SUCCESS",
    )
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(pr, ci_state="FAILURE")
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[pr],
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert str(table.get_row("pr:owner/repo#9")[0]).startswith("✓")
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.02)
                if str(table.get_row("pr:owner/repo#9")[0]).startswith("✗"):
                    break
            assert str(table.get_row("pr:owner/repo#9")[0]).startswith("✗")
            backend.load_items.assert_not_called()
            assert backend.github_client.fetch_item.call_args.args[:2] == ("owner/repo", 9)

    asyncio.run(run())


def test_autodispatch_refresh_updates_filtered_linked_pr_status() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    issue = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="Issue",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
    )
    pr = replace(
        issue,
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        number=9,
        title="Pull request",
        closing_issue_numbers=(1,),
        ci_state="SUCCESS",
    )
    backend = MagicMock()
    backend.github_client.fetch_item.side_effect = lambda repo, number, **kwargs: (
        replace(issue, title="Updated issue") if number == 1 else replace(pr, ci_state="FAILURE")
    )
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session=None,
    )
    entry = {
        "id": "s",
        "status": "running",
        "scope": "owner/repo",
        "current_item": "owner/repo#ISSUE-1",
        "selected_group": None,
        "stage": "developing",
        "launch_reason": None,
        "iteration": 1,
        "elapsed_seconds": 0,
        "error": None,
    }
    session.autodispatch_sessions = MagicMock(return_value=[entry])
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            await _submit_search(app, pilot, "Pull request")
            table = app.query_one("#work-items", DataTable)
            assert table.row_count == 1
            assert table.get_row("pr:owner/repo#9")[2].style == "bold magenta"
            assert str(table.get_row("pr:owner/repo#9")[0]).startswith("✓")
            entry["status"] = "stopped"
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.02)
                if str(table.get_row("pr:owner/repo#9")[0]).startswith("✗"):
                    break
            assert str(table.get_row("pr:owner/repo#9")[0]).startswith("✗")
            assert table.get_row("pr:owner/repo#9")[2].style == "bold"
            assert issue.title == "Updated issue"
            assert app._selected_item() is pr

    asyncio.run(run())


def test_autodispatch_progress_preserves_scroll_away_from_selection() -> None:
    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title=f"Issue {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in range(1, 51)
    ]
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=MagicMock(),
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    entries = []
    session.autodispatch_sessions = MagicMock(return_value=entries)
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            table = app.query_one("#work-items", DataTable)
            await pilot.pause()
            table.scroll_to(y=25, animate=False, force=True)
            await pilot.pause()
            assert table.cursor_row == 0 and table.scroll_y == 25
            entries.append(
                {
                    "id": "s",
                    "status": "running",
                    "scope": "owner/repo#1",
                    "current_item": None,
                    "selected_group": None,
                    "stage": "developing",
                    "launch_reason": None,
                    "iteration": 1,
                    "elapsed_seconds": 0,
                    "error": None,
                }
            )
            app._update_autodispatch_progress()
            await pilot.pause()
            assert table.cursor_row == 0 and table.scroll_y == 25
            entries.clear()
            app._update_autodispatch_progress()
            await pilot.pause()
            assert table.cursor_row == 0 and table.scroll_y == 25
            await pilot.resize_terminal(80, 10)
            await pilot.pause()
            assert table.scroll_y == 0
            await pilot.press(*(["down"] * 49))
            assert table.cursor_row == 49 and table.scroll_y > 0

    asyncio.run(run())


def test_autodispatch_refresh_preserves_scroll_away_from_selection() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title=f"Issue {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in range(1, 51)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(items[0], title="Updated issue")
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            table = app.query_one("#work-items", DataTable)
            await pilot.pause()
            selected = app._selected_item()
            table.scroll_to(y=25, animate=False, force=True)
            await pilot.pause()
            assert table.scroll_y == 25
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._pending_autodispatch.add("owner/repo#ISSUE-1")
            await app._refresh_autodispatch_items()
            await pilot.pause()
            assert str(table.get_row("issue:owner/repo#1")[2]) == "Updated issue"
            assert app._selected_item().number == selected.number
            assert table.scroll_y == 25

    asyncio.run(run())


def test_autodispatch_refresh_keeps_visible_search_selection_on_screen() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title="Hidden" if n == 1 else f"Match {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in range(1, 31)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(items[0], title="Match added")
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.press("/", "m", "a", "t", "c", "h", "enter")
            table = app.query_one("#work-items", DataTable)
            table.move_cursor(row=15)
            await pilot.pause()
            selected = app._selected_item()
            scroll = table.scroll_y
            assert table._get_row_region(table.cursor_row).y - scroll == table.size.height - 1
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._pending_autodispatch.add("owner/repo#ISSUE-1")
            await app._refresh_autodispatch_items()
            await pilot.pause()
            assert app._selected_item() == selected
            assert table.scroll_y == scroll + 1
            assert table._get_row_region(table.cursor_row).y - table.scroll_y < table.size.height

    asyncio.run(run())


def test_autodispatch_refresh_updates_search_membership() -> None:
    from dataclasses import replace
    from datetime import timedelta

    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title=f"Match {n}" if n != 21 else "Hidden",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in range(1, 22)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.side_effect = lambda repo, number, **kwargs: replace(
        items[number - 1],
        title="Hidden" if number == 1 else "Match added",
        updated_at=now + timedelta(days=1) if number == 21 else now,
    )
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            await pilot.press("/", "m", "a", "t", "c", "h", "enter")
            await pilot.pause()
            table = app.query_one("#work-items", DataTable)
            table.move_cursor(row=10)
            await pilot.pause()
            selected = app._selected_item()
            scroll = table.scroll_y
            assert scroll > 0
            for number in (1, 21):
                session.completed_autodispatch_item(f"owner/repo#ISSUE-{number}")
                app._pending_autodispatch.add(f"owner/repo#ISSUE-{number}")
                await app._refresh_autodispatch_items()
                await pilot.pause()
                assert app._selected_item() == selected
                assert table.scroll_y == scroll
            assert table.row_count == 20
            assert all(str(table.get_row_at(i)[2]).startswith("Match") for i in range(20))
            assert str(table.get_row_at(19)[2]) == "Match added"
            await pilot.press("/")
            assert table.row_count == 21
            assert str(table.get_row("issue:owner/repo#1")[2]) == "Hidden"

    asyncio.run(run())


def test_autodispatch_refresh_updates_all_displayed_linked_pull_requests() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=n,
            title=f"Old {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/pull/{n}",
            closing_issue_numbers=(1,),
        )
        for n in (2, 3)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.side_effect = lambda repo, number, **kwargs: replace(
        items[number - 2], title=f"Updated {number}"
    )
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            app._pending_autodispatch.add("owner/repo#ISSUE-1")
            await app._refresh_autodispatch_items()
            await pilot.pause()
            table = app.query_one("#work-items", DataTable)
            assert {str(table.get_row_at(i)[2]) for i in range(2)} == {"Updated 2", "Updated 3"}
            assert {item["title"] for item in session.list_items()["items"]} == {
                "Updated 2",
                "Updated 3",
            }

    asyncio.run(run())


def test_autodispatch_refresh_preserves_order_selection_and_scroll() -> None:
    from dataclasses import replace
    from datetime import timedelta

    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title=f"Issue {n}",
            created_at=now,
            updated_at=now - timedelta(days=n),
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in range(1, 21)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(items[-1], updated_at=now)
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            table = app.query_one("#work-items", DataTable)
            table.move_cursor(row=10)
            await pilot.pause()
            table.move_cursor(row=9)
            await pilot.pause()
            selected = app._selected_item()
            scroll = table.scroll_y
            assert scroll > 0
            session.completed_autodispatch_item("owner/repo#ISSUE-20")
            app._pending_autodispatch.add("owner/repo#ISSUE-20")
            await app._refresh_autodispatch_items()
            await pilot.pause()
            assert [str(table.get_row_at(i)[2]) for i in range(20)] == [
                f"Issue {n}" for n in range(1, 21)
            ]
            assert app._selected_item() == selected
            assert table.scroll_y == scroll
            app.refresh_from_session()
            assert str(table.get_row_at(0)[2]) == "Issue 20"

    asyncio.run(run())


def test_autodispatch_refresh_preserves_scrolled_selection_when_timestamp_changes() -> None:
    from dataclasses import replace
    from datetime import timedelta

    now = datetime(2026, 2, 26, tzinfo=UTC)
    items = [
        WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=n,
            title=f"Issue {n}",
            created_at=now,
            updated_at=now - timedelta(days=n),
            url=f"https://github.com/owner/repo/issues/{n}",
        )
        for n in range(1, 21)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(items[-1], updated_at=now)
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            table = app.query_one("#work-items", DataTable)
            table.move_cursor(row=19)
            await pilot.pause()
            scroll = table.scroll_y
            assert scroll > 0
            session.completed_autodispatch_item("owner/repo#ISSUE-20")
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.02)
                if app._selected_item().updated_at == now:
                    break
            assert app._selected_item().number == 20
            assert app._selected_item().updated_at == now
            assert table.cursor_row == 19
            assert table.scroll_y == scroll

    asyncio.run(run())


def test_autodispatch_lifecycle_refreshes_completed_issue_and_highlights_next(monkeypatch) -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    first = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="First",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
    )
    second = replace(first, number=2, title="Second", url="https://github.com/owner/repo/issues/2")
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(first, state="CLOSED")
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[first, second],
        suggestion_markers={},
        zellij_session="workdash",
    )
    session.prepare_autodispatch = MagicMock()
    session.dispatch_once = MagicMock(
        side_effect=[
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "pane_id": "terminal_1",
                "session": "workdash",
            },
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-2",
                "pane_id": "terminal_2",
                "session": "workdash",
            },
        ]
    )
    session.review = MagicMock(return_value={"status": "nothing_to_review"})
    finished = threading.Event()
    monkeypatch.setattr("workdash.autodispatch._AUTODISPATCH_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        "workdash.launcher.zellij.load_zellij_panes",
        lambda _: [{"id": 2} if finished.is_set() else {"id": 1}],
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            session.start_autodispatch(scope="owner/repo", agent="claude")
            for _ in range(100):
                await pilot.pause(0.02)
                if table_style(1) == "bold magenta":
                    break
            assert table_style(1) == "bold magenta"
            finished.set()
            for _ in range(150):
                await pilot.pause(0.02)
                table = app.query_one("#work-items", DataTable)
                if (
                    str(table.get_row("issue:owner/repo#1")[2]) == "First (closed)"
                    and table_style(2) == "bold magenta"
                ):
                    break
            assert str(table.get_row("issue:owner/repo#1")[2]) == "First (closed)"
            assert table_style(1) == "bold"
            assert table_style(2) == "bold magenta"

    def table_style(number):
        return (
            app.query_one("#work-items", DataTable).get_row(f"issue:owner/repo#{number}")[2].style
        )

    try:
        asyncio.run(run())
    finally:
        for entry in session._autodispatch.values():
            entry.stop()
            entry.thread.join(timeout=2)


def test_autodispatch_highlights_linked_prs_when_issue_row_is_absent() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    pr = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=9,
        title="Same repo",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/9",
        closing_issue_numbers=(1,),
    )
    cross = replace(
        pr,
        repo="other/repo",
        number=10,
        title="Cross repo",
        closing_issue_numbers=(),
        linked_issues=(("OWNER/repo", 1),),
    )
    scope = replace(pr, number=11, title="Scope", closing_issue_numbers=(2,))
    issue = replace(
        pr,
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        number=1,
        title="Issue",
        closing_issue_numbers=(),
    )
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=MagicMock(),
        work_items=[pr, cross, scope],
        suggestion_markers={},
        zellij_session=None,
    )
    entry = {
        "id": "s",
        "status": "running",
        "scope": "owner/repo#ISSUE-2",
        "current_item": "owner/repo#ISSUE-1",
        "selected_group": None,
        "stage": "developing",
        "launch_reason": None,
        "iteration": 1,
        "elapsed_seconds": 0,
        "error": None,
    }
    session.autodispatch_sessions = MagicMock(return_value=[entry])
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert table.get_row("pr:owner/repo#9")[2].style == "bold magenta"
            assert table.get_row("pr:other/repo#10")[2].style == "bold magenta"
            assert table.get_row("pr:owner/repo#11")[2].style == "bold"
            session.work_items.append(issue)
            app.refresh_from_session()
            assert table.get_row("issue:owner/repo#1")[2].style == "bold magenta"
            assert table.get_row("pr:owner/repo#9")[2].style == "bold"
            await _submit_search(app, pilot, "Same repo")
            assert table.get_row("pr:owner/repo#9")[2].style == "bold magenta"
            entry["status"] = "stopped"
            app._update_autodispatch_progress()
            assert table.get_row("pr:owner/repo#9")[2].style == "bold"
            entry["status"] = "running"
            app._update_autodispatch_progress()
            assert table.get_row("pr:owner/repo#9")[2].style == "bold magenta"
            await pilot.press("/")
            assert table.get_row("pr:owner/repo#9")[2].style == "bold"
            session.work_items.remove(issue)
            app.refresh_from_session()
            entry["status"] = "stopped"
            app._update_autodispatch_progress()
            assert table.get_row("pr:owner/repo#9")[2].style == "bold"
            assert table.get_row("pr:other/repo#10")[2].style == "bold"

    asyncio.run(run())


def test_autodispatch_retries_all_pr_rows_after_fetch_failure() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    prs = [
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=n,
            title=f"Old {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/pull/{n}",
            closing_issue_numbers=(1,),
        )
        for n in (2, 3)
    ]
    backend = MagicMock()
    backend.github_client.fetch_item.side_effect = [
        replace(prs[0], title="New 2"),
        RuntimeError("unavailable"),
        replace(prs[1], title="New 3"),
    ]
    backend.github_client.fetch_linked_issues_for_pull_request.return_value = []
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=prs,
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            original_set_timer = app.set_timer
            delays = []

            def shorten_retry(delay, callback):
                delays.append(delay)
                return original_set_timer(0.5, callback)

            app.set_timer = shorten_retry
            table = app.query_one("#work-items", DataTable)
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.01)
                if delays:
                    break
            assert delays == [30]
            assert [pr.title for pr in session.work_items] == ["New 2", "Old 3"]
            assert [str(table.get_row(f"pr:owner/repo#{n}")[2]) for n in (2, 3)] == [
                "New 2",
                "Old 3",
            ]
            for _ in range(150):
                await pilot.pause(0.02)
                if str(table.get_row("pr:owner/repo#3")[2]) == "New 3":
                    break
            assert [pr.title for pr in session.work_items] == ["New 2", "New 3"]
            assert [str(table.get_row(f"pr:owner/repo#{n}")[2]) for n in (2, 3)] == [
                "New 2",
                "New 3",
            ]

    asyncio.run(run())


def test_autodispatch_refresh_removes_selected_nonmatching_search_result() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="Match first",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
    )
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(item, title="Changed")
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[item, replace(item, number=2, title="Match second")],
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test() as pilot:
            await pilot.press("/", "m", "a", "t", "c", "h", "enter")
            assert app._selected_item().number == 1
            session.completed_autodispatch_item("owner/repo#ISSUE-1")
            app._update_autodispatch_progress()
            for _ in range(100):
                await pilot.pause(0.02)
                if app.query_one("#work-items", DataTable).row_count == 1:
                    break
            table = app.query_one("#work-items", DataTable)
            assert table.row_count == 1
            assert app._selected_item().number == 2
            assert str(table.get_row_at(0)[2]) == "Match second"

    asyncio.run(run())


def test_loaded_linked_pr_tracks_autodispatch_stop() -> None:
    from dataclasses import replace
    from datetime import timedelta

    from workdash.backend import WorkdashBackend

    now = datetime(2026, 2, 26, tzinfo=UTC)
    github = MagicMock()
    raw = {
        "repo": "owner/repo",
        "number": 1,
        "title": "Issue",
        "url": "https://github.com/owner/repo/issues/1",
        "created_at": now.isoformat(),
        "updated_at": now.isoformat(),
    }
    github.list_open_assigned_issues.return_value = [
        {
            **raw,
            "number": n,
            "title": f"Issue {n}",
            "updated_at": (now - timedelta(days=n)).isoformat(),
        }
        for n in range(1, 22)
    ]
    github.list_open_authored_prs.return_value = [
        {
            **raw,
            "number": 99,
            "title": "Linked PR",
            "url": "https://github.com/owner/repo/pull/99",
            "ci_state": "SUCCESS",
            "review_decision": None,
            "review_requested": False,
            "has_unresolved_comments": False,
        }
    ]
    for method in (
        "list_open_review_requested_prs",
        "list_open_reviewed_prs",
        "list_recent_tracked_items",
        "list_open_todo_issues",
    ):
        getattr(github, method).return_value = []
    github.fetch_linked_issues.return_value = {("owner/repo", 99): [("owner/repo", 1)]}
    github.fetch_linked_issues_for_pull_request.return_value = [("owner/repo", 1)]
    cache, store = MagicMock(), MagicMock()
    cache.load_analysis.return_value = cache.load_analysis_date.return_value = None
    store.load.return_value = []
    backend = WorkdashBackend(
        github_client=github,
        analysis_cache=cache,
        included_items_store=store,
        config=_DEFAULT_TUI_CONFIG,
    )
    items, markers = backend.load_items()
    assert not any(item.item_type == WorkItemType.ISSUE and item.number == 1 for item in items)
    pr = next(item for item in items if item.item_type == WorkItemType.PR)
    github.fetch_item.return_value = replace(pr, ci_state="FAILURE")
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=items,
        suggestion_markers=markers,
        zellij_session="workdash",
    )
    session.prepare_autodispatch = MagicMock()
    session.dispatch_once = MagicMock(
        return_value={
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "pane_id": "terminal_1",
            "session": "workdash",
        }
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        async with app.run_test(size=(80, 12)) as pilot:
            entry = session.start_autodispatch(scope="owner/repo", agent="claude")
            table = app.query_one("#work-items", DataTable)
            for _ in range(100):
                await pilot.pause(0.02)
                if table.get_row("pr:owner/repo#99")[2].style == "bold magenta":
                    break
            assert table.get_row("pr:owner/repo#99")[2].style == "bold magenta"
            assert str(table.get_row("pr:owner/repo#99")[0]).startswith("✓")
            table.move_cursor(row=10)
            for _ in range(100):
                await pilot.pause(0.02)
                if table.scroll_y == table.scroll_target_y and table.scroll_y > 0:
                    break
            assert table.scroll_y == table.scroll_target_y
            assert table._get_row_region(table.cursor_row).y - table.scroll_y < table.size.height
            selected, scroll = app._selected_item(), table.scroll_y
            assert scroll > 0
            session.stop_autodispatch(str(entry["id"]))
            for _ in range(100):
                await pilot.pause(0.02)
                if str(table.get_row("pr:owner/repo#99")[0]).startswith("✗"):
                    break
            assert str(table.get_row("pr:owner/repo#99")[0]).startswith("✗")
            assert table.get_row("pr:owner/repo#99")[2].style == "bold"
            assert app._selected_item() == selected
            assert table.scroll_y == scroll

    try:
        asyncio.run(run())
    finally:
        session.stop_autodispatch_sessions()
        for entry in session._autodispatch.values():
            entry.thread.join(timeout=2)


def test_autodispatch_refresh_displays_successes_and_retries_failed_rows() -> None:
    from dataclasses import replace

    now = datetime(2026, 2, 26, tzinfo=UTC)
    issue = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="Old issue",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/issues/1",
    )
    prs = [
        WorkItem(
            kind=WorkItemKind.TRACKED_PR,
            item_type=WorkItemType.PR,
            repo="owner/repo",
            number=n,
            title=f"Old {n}",
            created_at=now,
            updated_at=now,
            url=f"https://github.com/owner/repo/pull/{n}",
            closing_issue_numbers=(1,),
        )
        for n in (2, 3)
    ]
    backend = MagicMock()
    failed = True

    def fetch(repo, number, **kwargs):
        if number == 2 and failed:
            return None
        return replace({1: issue, 2: prs[0], 3: prs[1]}[number], title=f"Updated {number}")

    backend.github_client.fetch_item.side_effect = fetch
    backend.github_client.fetch_linked_issues_for_pull_request.return_value = [("owner/repo", 1)]
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[issue, *prs],
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session, now_utc=now)

    async def run():
        nonlocal failed
        async with app.run_test(size=(80, 12)) as pilot:
            app._pending_autodispatch.add("owner/repo#ISSUE-1")
            await app._refresh_autodispatch_items()
            await pilot.pause()
            table = app.query_one("#work-items", DataTable)
            assert {str(table.get_row_at(i)[2]) for i in range(3)} == {
                "Updated 1",
                "Old 2",
                "Updated 3",
            }
            assert "Cannot refresh owner/repo#2" in app._status_message
            failed = False
            app._pending_autodispatch.add("owner/repo#ISSUE-1")
            await app._refresh_autodispatch_items()
            await pilot.pause()
            assert {str(table.get_row_at(i)[2]) for i in range(3)} == {
                "Updated 1",
                "Updated 2",
                "Updated 3",
            }

    asyncio.run(run())


def test_included_pull_request_highlights_running_autodispatch() -> None:
    from workdash.backend import WorkdashBackend

    now = datetime(2026, 2, 26, tzinfo=UTC)
    pr = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=2,
        title="Included PR",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/2",
        included=True,
    )
    github = MagicMock()
    github.fetch_item_by_url.return_value = pr
    github.fetch_linked_issues_for_pull_request.return_value = [("owner/repo", 1)]
    backend = WorkdashBackend(
        github_client=github,
        analysis_cache=MagicMock(),
        included_items_store=MagicMock(),
        config=_DEFAULT_TUI_CONFIG,
    )
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    session.autodispatch_sessions = MagicMock(
        return_value=[
            {
                "id": "s",
                "status": "running",
                "scope": "owner/repo",
                "current_item": "owner/repo#ISSUE-1",
                "selected_group": None,
                "stage": "developing",
                "launch_reason": None,
                "iteration": 1,
                "elapsed_seconds": 0,
                "error": None,
            }
        ]
    )
    app = WorkdashApp(
        session=session,
        now_utc=now,
        include_callback=lambda url, _: session.include_item_by_url(url),
    )

    async def run():
        async with app.run_test() as pilot:
            await pilot.pause()
            await app._perform_include(pr.url)
            await pilot.pause()
            assert (
                app.query_one("#work-items", DataTable).get_row("pr:owner/repo#2")[2].style
                == "bold magenta"
            )

    asyncio.run(run())


def test_include_pull_request_warns_when_linked_issues_unavailable(tmp_path) -> None:
    from workdash.backend import WorkdashBackend
    from workdash.included_items import IncludedItemsStore

    now = datetime(2026, 2, 26, tzinfo=UTC)
    pr = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=2,
        title="Included PR",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/repo/pull/2",
        included=True,
    )
    github = MagicMock()
    github.fetch_item_by_url.return_value = pr
    github.fetch_linked_issues_for_pull_request.side_effect = RuntimeError("unavailable")
    store = IncludedItemsStore(tmp_path / "included.json")
    backend = WorkdashBackend(
        github_client=github,
        analysis_cache=MagicMock(),
        included_items_store=store,
        config=_DEFAULT_TUI_CONFIG,
    )
    app = WorkdashApp(
        work_items=[],
        suggestion_markers={},
        now_utc=now,
        include_callback=backend.include_item_by_url,
    )
    notifications = []
    app.notify = lambda message, **kwargs: notifications.append((message, kwargs.get("severity")))

    async def run():
        async with app.run_test() as pilot:
            await app._perform_include(pr.url)
            await pilot.pause()
            assert "Included PR" in str(app.query_one("#work-items", DataTable).get_row_at(0)[2])
            assert store.load() == [pr.url]
            assert any(
                "linked" in message.lower()
                and "refresh" in message.lower()
                and severity == "warning"
                for message, severity in notifications
            )

    asyncio.run(run())


def test_refresh_does_not_restore_todo_closed_after_capture() -> None:
    now = datetime.now(UTC)
    item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/todos",
        number=42,
        title="Close me",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/todos/issues/42",
    )
    backend = MagicMock()
    backend.load_items.return_value = ([item], {})
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="owner/todos"),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(
        session=session,
        refresh_callback=lambda: (
            session.list_items(refresh=True) and (session.work_items, session.suggestion_markers)
        ),
    )
    session.items_changed_callback = app.refresh_from_session

    async def run_test() -> None:
        async with app.run_test():

            async def interrupted_refresh(*, message, callback):
                captured = callback()
                session.close_todo(item="42")
                return captured

            app._run_with_busy_screen = interrupted_refresh
            await app.action_refresh_items()
            assert app.query_one("#work-items", DataTable).row_count == 0
            assert session.list_items()["items"] == []

    asyncio.run(run_test())


@pytest.mark.parametrize("duplicate", [False, True])
def test_include_closed_todo_is_pruned_on_refresh(tmp_path, duplicate: bool) -> None:
    from workdash.backend import WorkdashBackend
    from workdash.github_client import GitHubClient
    from workdash.included_items import IncludedItemsStore

    now = datetime.now(UTC)
    item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/todos",
        number=42,
        title="Close me",
        created_at=now,
        updated_at=now,
        url="https://github.com/owner/todos/issues/42",
    )
    github = MagicMock(spec=GitHubClient)
    for name in (
        "list_open_authored_prs",
        "list_open_review_requested_prs",
        "list_open_reviewed_prs",
        "list_open_assigned_issues",
        "list_recent_tracked_items",
        "list_open_todo_issues",
    ):
        getattr(github, name).return_value = []
    github.fetch_item_by_url.side_effect = [None] if duplicate else [item, None]
    store = IncludedItemsStore(tmp_path / "included.json")
    config = WorkdashConfig(todo_repository="owner/todos")
    backend = WorkdashBackend(
        config=config,
        github_client=github,
        included_items_store=store,
        cache_root=tmp_path / "cache",
    )
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[item] if duplicate else [],
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(
        session=session,
        include_callback=lambda url, _: session.include_item_by_url(url),
        refresh_callback=lambda: session.list_items(refresh=True),
    )
    session.items_changed_callback = app.refresh_from_session

    async def run_test() -> None:
        async with app.run_test():

            async def interrupted_include(*, message, callback):
                captured = callback()
                assert store.load() == [item.url]
                session.close_todo(item="42")
                return captured

            app._run_with_busy_screen = interrupted_include
            await app._perform_include(item.url)
            assert app.query_one("#work-items", DataTable).row_count == 0
            assert session.list_items()["items"] == []
            session.list_items(refresh=True)
            assert app.query_one("#work-items", DataTable).row_count == 0
            assert session.list_items()["items"] == []
            assert store.load() == []
            github.close_todo.assert_called_once_with(todo_repository="owner/todos", number=42)
            assert github.fetch_item_by_url.call_count == (1 if duplicate else 2)

    asyncio.run(run_test())


def test_progress_selects_and_stops_a_blocked_session():
    session = WorkdashSession(
        config=_DEFAULT_TUI_CONFIG,
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    entry = {
        "id": "first",
        "status": "running",
        "scope": "owner/repo#1",
        "current_item": "owner/repo#1",
        "selected_group": None,
        "stage": "developing",
        "launch_reason": None,
        "iteration": 1,
        "elapsed_seconds": 0,
        "error": None,
    }
    blocked = {**entry, "id": "blocked", "scope": "owner/repo#2", "status": "blocked"}
    session.autodispatch_sessions = MagicMock(return_value=[entry, blocked])
    session.stop_autodispatch = MagicMock()
    app = WorkdashApp(session=session)

    async def run():
        async with app.run_test() as pilot:
            await pilot.press("shift+tab")
            await pilot.press("down")
            footer = str(app.query_one("#command-footer").render())
            assert "(s)top" in footer
            assert "(d)ispatch" not in footer
            app._selected_item = MagicMock()
            await pilot.press("d")
            app._selected_item.assert_not_called()
            assert "blocked (s)top" in str(app.query_one("#autodispatch-progress").render())
            session.autodispatch_sessions.return_value = [entry]
            await pilot.press("s")
            session.stop_autodispatch.assert_called_once_with("blocked")

    asyncio.run(run())
