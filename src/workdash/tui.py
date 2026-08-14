"""Textual list view for work items."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from .backend import IncludeResult, compute_suggestion_markers
from .config import WorkdashAgentChoice
from .launcher import launch_branchdiff_context, open_markdown
from .models import WorkItem, WorkItemType, display_repo, format_type_label

SuggestionMarkers = dict[tuple[WorkItemType, str, int], str]
RefreshCallbackResult = Sequence[WorkItem] | tuple[Sequence[WorkItem], SuggestionMarkers]
AnalyzeCallbackResult = str | None
_CallbackResult = TypeVar("_CallbackResult")
# The Repo column is capped at this reference name so the title keeps more room.
_MAX_REPO_WIDTH = len("posit-dev/rsconnect-python")
# One-character CI symbols, keyed by the GraphQL status check rollup states.
_CI_SYMBOLS = {
    "SUCCESS": ("✓", "green"),
    "FAILURE": ("✗", "red"),
    "ERROR": ("✗", "red"),
    "PENDING": ("●", "yellow"),
    "EXPECTED": ("●", "yellow"),
}

if TYPE_CHECKING:
    from .control import WorkdashSession


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC)


def _type_column(item: WorkItem, *, bold: bool) -> Text:
    """Return the Type column cell, prefixed with the item's CI symbol.

    An item without a CI result keeps a blank prefix so every Type label stays
    aligned under the ones that carry a symbol. A passing, approved authored
    pull request gets a double checkmark instead of the single passing symbol.

    :param WorkItem item: the work item whose type is shown.
    :param bool bold: whether the whole row is highlighted as recently updated.
    """

    if item.ci_state == "SUCCESS" and item.review_decision == "APPROVED":
        symbol, color = "✓✓", "green"
    else:
        symbol, color = _CI_SYMBOLS.get(item.ci_state or "", (" ", None))
    cell = Text(
        f"{symbol}{format_type_label(item)}#{item.number}",
        style="bold" if bold else "",
    )
    if color is not None:
        cell.stylize(color, 0, len(symbol))
    return cell


def _repo_column(item: WorkItem) -> str:
    """Return the Repo column text, cutting a long owner off the left.

    :param WorkItem item: the work item whose repository is shown.
    """

    repo = display_repo(item)
    if len(repo) <= _MAX_REPO_WIDTH:
        return repo
    return f"…{repo[-(_MAX_REPO_WIDTH - 1) :]}"


class BusyScreen(ModalScreen[None]):
    """Modal status view shown while long-running callbacks execute."""

    DEFAULT_CSS = """
        #busy-shell {
            align: center middle;
            width: 40;
            height: 7;
            border: solid $accent;
            background: $surface;
        }
        #busy-message {
            width: 100%;
            content-align: center middle;
            text-style: bold;
        }
        """

    def __init__(self, *, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="busy-shell"):
            yield Static(self._message, id="busy-message")


class AnalyzeDialog(ModalScreen[str | None]):
    """Modal dialog for choosing an analysis action."""

    DEFAULT_CSS = """
        #analyze-shell {
            align: center middle;
            width: 50;
            height: auto;
            max-height: 14;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        .analyze-line {
            width: 100%;
        }
        """
    BINDINGS = [
        ("a", "open_analysis", "Open"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        *,
        item: WorkItem,
        now_utc: datetime,
        choices: Sequence[WorkdashAgentChoice],
    ) -> None:
        super().__init__()
        self._item = item
        self._now_utc = now_utc
        self._choices = tuple(choices)
        self._choices_by_key = {choice.key: choice for choice in self._choices}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="analyze-shell"):
            if self._item.analyzed_at is not None:
                age_days = max(0, (self._now_utc - self._item.analyzed_at).days)
                yield Static(
                    f"Last analyzed: {age_days}d ago",
                    classes="analyze-line",
                )
                yield Static("")
                yield Static("(a) Open analysis", classes="analyze-line")
            else:
                yield Static("No previous analysis.", classes="analyze-line")
            yield Static("")
            for choice in self._choices:
                yield Static(f"({choice.key}) {choice.label}", classes="analyze-line")
            if not self._choices:
                yield Static("No analysis agents configured.", classes="analyze-line")
            yield Static("")
            yield Static("(Esc) Cancel", classes="analyze-line")

    def action_open_analysis(self) -> None:
        if self._item.analyzed_at is not None:
            self.dismiss("cached")

    def on_key(self, event) -> None:
        choice = self._choices_by_key.get(event.key)
        if choice is not None:
            event.stop()
            self.dismiss(choice.agent)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CodeDialog(ModalScreen[str | None]):
    """Modal dialog for choosing a coding tool to launch or focusing an active agent."""

    DEFAULT_CSS = """
        #code-shell {
            align: center middle;
            width: 50;
            height: auto;
            max-height: 12;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        .code-line {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        *,
        choices: Sequence[WorkdashAgentChoice],
        active_agent_panes: Sequence[dict[str, object]] | None = None,
    ) -> None:
        super().__init__()
        self._choices = tuple(choices)
        self._choices_by_key = {choice.key: choice for choice in self._choices}
        self._active_agent_panes = list(active_agent_panes or [])

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="code-shell"):
            yield Static("Launch coding session:", classes="code-line")
            yield Static("")
            # Show focus option if there are active agent panes
            if self._active_agent_panes:
                yield Static("(0) Focus active agent", classes="code-line")
            for choice in self._choices:
                yield Static(f"({choice.key}) {choice.label}", classes="code-line")
            if not self._choices:
                yield Static("No coding agents configured.", classes="code-line")
            yield Static("")
            yield Static("(Esc) Cancel", classes="code-line")

    def on_key(self, event) -> None:
        # Handle focus active agent (key "0")
        if event.key == "0" and self._active_agent_panes:
            event.stop()
            self.dismiss("__focus_active_agent__")
        choice = self._choices_by_key.get(event.key)
        if choice is not None:
            event.stop()
            self.dismiss(choice.agent)

    def action_cancel(self) -> None:
        self.dismiss(None)


class IncludeDialog(ModalScreen[str | None]):
    """Modal dialog that accepts a GitHub issue or pull request URL."""

    DEFAULT_CSS = """
        #include-shell {
            align: center middle;
            width: 70;
            height: auto;
            max-height: 9;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        #include-url {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="include-shell"):
            yield Static("Paste a GitHub issue or pull request URL:")
            yield Input(placeholder="https://github.com/owner/repo/pull/123", id="include-url")
            yield Static("(Enter to confirm, Esc to cancel)")

    def on_mount(self) -> None:
        self.query_one("#include-url", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())  # single strip; caller trusts this value

    def action_cancel(self) -> None:
        self.dismiss(None)


class TodoDialog(ModalScreen[tuple[str, str] | None]):
    """Modal dialog that accepts a todo text and an optional target repository."""

    DEFAULT_CSS = """
        #todo-shell {
            align: center middle;
            width: 70;
            height: auto;
            max-height: 13;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        #todo-text, #todo-target {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="todo-shell"):
            yield Static("What do you want to remember?")
            yield Input(placeholder="Fix the flaky test", id="todo-text")
            yield Static("Target repository (optional):")
            yield Input(placeholder="owner/repo", id="todo-target")
            yield Static("(Enter to confirm, Esc to cancel)")

    def on_mount(self) -> None:
        self.query_one("#todo-text", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(
            (
                self.query_one("#todo-text", Input).value.strip(),
                self.query_one("#todo-target", Input).value.strip(),
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class SearchDialog(ModalScreen[str | None]):
    """Modal dialog that accepts text to narrow the listed work items."""

    DEFAULT_CSS = """
        #search-shell {
            align: center middle;
            width: 70;
            height: auto;
            max-height: 9;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        #search-text {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="search-shell"):
            yield Static("Filter items by type, repository, or title:")
            yield Input(placeholder="parser", id="search-text")
            yield Static("(Enter to confirm, Esc to cancel)")

    def on_mount(self) -> None:
        self.query_one("#search-text", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class WorkdashApp(App[None]):
    """Main Textual app shell."""

    TITLE = "workdash"
    DEFAULT_CSS = """
        #status-footer {
            dock: bottom;
            height: 1;
            padding: 0 1;
        }
        #command-footer {
            dock: bottom;
            height: 1;
            padding: 0 1;
        }
        """
    SEARCH_HINT_TEXT = "(/)search"
    COMMAND_HINT_TEXT = (
        f"{SEARCH_HINT_TEXT} (o)pen (r)efresh (a)nalyze (c)ode (d)iff "
        "(t)erminal (i)nclude (w)todo (q)uit"
    )
    BINDINGS = [
        ("/", "search_items", "Search"),
        ("o", "open_link", "Open"),
        ("r", "refresh_items", "Refresh"),
        ("a", "analyze_item", "Analyze"),
        ("c", "launch_code", "Code"),
        ("d", "show_branchdiff", "Diff"),
        ("t", "open_terminal", "Terminal"),
        ("i", "include_item", "Include"),
        ("w", "capture_todo", "Todo"),
        ("q", "quit_app", "Quit"),
    ]

    def __init__(
        self,
        work_items: Sequence[WorkItem] | None = None,
        suggestion_markers: SuggestionMarkers | None = None,
        open_callback: Callable[[WorkItem], None] | None = None,
        refresh_callback: Callable[[], RefreshCallbackResult] | None = None,
        analyze_callback: Callable[[WorkItem, str], AnalyzeCallbackResult] | None = None,
        launch_callback: Callable[[WorkItem, str], None] | None = None,
        worktree_callback: Callable[[WorkItem], str] | None = None,
        analyze_choices: Sequence[WorkdashAgentChoice] | None = None,
        code_choices: Sequence[WorkdashAgentChoice] | None = None,
        terminal_callback: Callable[[WorkItem], None] | None = None,
        include_callback: (
            Callable[[str, set[tuple[WorkItemType, str, int]]], IncludeResult] | None
        ) = None,
        todo_callback: Callable[[str, str | None], dict[str, object]] | None = None,
        session: WorkdashSession | None = None,
        now_utc: datetime | None = None,
        focus_callback: Callable[[str], None] | None = None,
        list_agent_panes_callback: Callable[[WorkItem], list[dict[str, object]]] | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        self._work_items = list(work_items or ())
        self._sorted_work_items: list[WorkItem] = []
        # Cleared whenever the loaded list changes so new items are never hidden.
        self._search_filter = ""
        self._suggestion_markers = dict(suggestion_markers or {})
        self._open_callback = open_callback
        self._refresh_callback = refresh_callback
        self._analyze_callback = analyze_callback
        self._launch_callback = launch_callback
        self._worktree_callback = worktree_callback
        self._analyze_choices = tuple(analyze_choices or ())
        self._code_choices = tuple(code_choices or ())
        self._terminal_callback = terminal_callback
        self._include_callback = include_callback
        self._todo_callback = todo_callback
        self._now_utc = now_utc or datetime.now(UTC)
        self._focus_callback = focus_callback
        self._list_agent_panes_callback = list_agent_panes_callback
        self._status_message = "Ready."

    def compose(self) -> ComposeResult:
        yield DataTable(id="work-items")
        yield Static(self._status_message, id="status-footer")
        yield Static(self._command_hint(), id="command-footer")

    def on_mount(self) -> None:
        if self._session is not None:
            self.refresh_from_session()
        else:
            self._render_table()

    def refresh_from_session(self) -> None:
        if self._session is None:
            return
        self._search_filter = ""
        self._work_items = list(self._session.work_items)
        self._suggestion_markers = dict(self._session.suggestion_markers)
        self._render_table()

    def _update_status(self, message: str) -> None:
        self._status_message = message
        self.query_one("#status-footer", Static).update(message)

    def _title_with_suggestion_marker(self, item: WorkItem) -> str:
        marker = self._suggestion_markers.get((item.item_type, item.repo, item.number))
        return f"* {item.title}" if marker else item.title

    def _command_hint(self) -> Text:
        hint = Text(self.COMMAND_HINT_TEXT)
        if self._search_filter:
            hint.stylize("bold", 0, len(self.SEARCH_HINT_TEXT))
        return hint

    def _render_table(self, *, focus_item: WorkItem | None = None) -> None:
        table = self.query_one("#work-items", DataTable)
        table.cursor_type = "row"
        previous_row = table.cursor_row if table.cursor_row is not None else 0
        # Anchor on the selected item by default: the row set changes wholesale
        # when a search filter is applied or cleared, so keeping the row index
        # would silently move the selection to a different work item.
        if focus_item is None:
            focus_item = self._selected_item()
        if table.columns:
            table.clear()
        else:
            table.add_column("Type", key="type")
            table.add_column("Repo", key="repo")
            table.add_column("Title", key="title")
            table.add_column("Age", key="age")
            table.add_column("Last Update", key="last_update")
        needle = self._search_filter.lower()
        # Match the rendered Type/Repo/Title text. The raw title and the
        # unprefixed type label are used so neither the "* " suggestion marker
        # nor the CI symbol is ever searchable.
        matched_items = [
            item
            for item in self._work_items
            if any(
                needle in value.lower()
                for value in (
                    f"{format_type_label(item)}#{item.number}",
                    display_repo(item),
                    item.title,
                )
            )
        ]
        self._sorted_work_items = sorted(
            matched_items,
            key=lambda entry: entry.updated_at,
            reverse=True,
        )
        cutoff = self._now_utc - timedelta(hours=24)
        for item in self._sorted_work_items:
            age_days = max(0, (self._now_utc - _to_utc(item.created_at)).days)
            update_days = max(0, (self._now_utc - _to_utc(item.updated_at)).days)
            title = self._title_with_suggestion_marker(item)
            row_key = f"{item.item_type.value}:{item.repo}#{item.number}"
            if _to_utc(item.updated_at) >= cutoff:
                table.add_row(
                    _type_column(item, bold=True),
                    Text(_repo_column(item), style="bold"),
                    Text(title, style="bold"),
                    Text(f"{age_days}d", style="bold"),
                    Text(f"{update_days}d", style="bold"),
                    key=row_key,
                )
            else:
                table.add_row(
                    _type_column(item, bold=False),
                    _repo_column(item),
                    title,
                    f"{age_days}d",
                    f"{update_days}d",
                    key=row_key,
                )
        if self._sorted_work_items:
            target_row = previous_row
            if focus_item is not None:
                for index, item in enumerate(self._sorted_work_items):
                    if (
                        item.item_type == focus_item.item_type
                        and item.repo == focus_item.repo
                        and item.number == focus_item.number
                    ):
                        target_row = index
                        break
            table.move_cursor(row=min(max(target_row, 0), len(self._sorted_work_items) - 1))
        self.query_one("#command-footer", Static).update(self._command_hint())

    def _selected_item(self) -> WorkItem | None:
        table = self.query_one("#work-items", DataTable)
        if (
            table.cursor_row is None
            or table.cursor_row < 0
            or table.cursor_row >= len(self._sorted_work_items)
        ):
            return None
        return self._sorted_work_items[table.cursor_row]

    def _run_after_dialog(self, callback: Callable[[], Awaitable[None]]) -> None:
        async def _runner() -> None:
            # Let Textual finish removing the choice/input modal before
            # starting the next progress/error flow. Without this handoff
            # a fast subprocess failure can report behind the still-visible
            # dialog the user just acted on.
            await asyncio.sleep(0)
            await callback()

        asyncio.create_task(_runner())

    async def _run_with_busy_screen(
        self,
        *,
        message: str,
        callback: Callable[[], _CallbackResult],
    ) -> _CallbackResult:
        busy_screen = BusyScreen(message=message)
        await self.push_screen(busy_screen)
        try:
            return await asyncio.to_thread(callback)
        finally:
            if self.screen is busy_screen:
                removal = self.pop_screen()
                if inspect.isawaitable(removal):
                    await removal
                else:
                    await asyncio.sleep(0)

    def _choice_tool_label(self, choices: Sequence[WorkdashAgentChoice], agent: str) -> str:
        return next((choice.tool_label for choice in choices if choice.agent == agent), agent)

    async def action_search_items(self) -> None:
        if self._search_filter:
            self._search_filter = ""
            self._render_table()
            self._update_status(f"Search cleared; {len(self._work_items)} item(s) shown.")
            return

        def _on_dialog_result(query: str | None) -> None:
            if not query:
                return
            self._search_filter = query
            self._render_table()
            self._update_status(
                f"Search matched {len(self._sorted_work_items)} of {len(self._work_items)} item(s)."
            )

        await self.push_screen(SearchDialog(), callback=_on_dialog_result)

    async def action_open_link(self) -> None:
        selected_item = self._selected_item()
        if selected_item is None or self._open_callback is None:
            self._update_status("Open skipped.")
            return
        try:
            await self._run_with_busy_screen(
                message="Opening link...",
                callback=lambda: self._open_callback(selected_item),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Open failed: {error}")
            self.notify(f"Open failed: {error}", severity="error", timeout=10)
            return
        self._update_status(
            f"Opened {selected_item.item_type.value} {selected_item.repo}#{selected_item.number}."
        )

    async def action_refresh_items(self) -> None:
        if self._refresh_callback is None:
            self._update_status("Refresh skipped.")
            return
        try:
            refreshed = await self._run_with_busy_screen(
                message="Refreshing work items...",
                callback=self._refresh_callback,
            )
            if (
                isinstance(refreshed, tuple)
                and len(refreshed) == 2
                and isinstance(refreshed[1], dict)
            ):
                self._work_items = list(refreshed[0])
                self._suggestion_markers = dict(refreshed[1])
            else:
                self._work_items = list(refreshed)
            self._search_filter = ""
            self._render_table()
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Refresh failed: {error}")
            self.notify(f"Refresh failed: {error}", severity="error", timeout=10)
            return
        self._update_status(f"Refreshed {len(self._work_items)} item(s).")

    async def action_analyze_item(self) -> None:
        selected_item = self._selected_item()
        if selected_item is None or self._analyze_callback is None:
            self._update_status("Analyze skipped.")
            return
        dialog = AnalyzeDialog(
            item=selected_item,
            now_utc=self._now_utc,
            choices=self._analyze_choices,
        )

        def _on_dialog_result(choice: str | None) -> None:
            if choice is not None:
                self._run_after_dialog(lambda: self._perform_analysis(selected_item, choice))

        await self.push_screen(dialog, callback=_on_dialog_result)

    async def _perform_analysis(self, item: WorkItem, choice: str) -> None:
        """Execute analysis after the user picks a tool from the dialog."""

        tool_label = (
            "cached"
            if choice == "cached"
            else self._choice_tool_label(self._analyze_choices, choice)
        )
        busy_message = (
            "Loading analysis..." if choice == "cached" else f"Analyzing with {tool_label}..."
        )
        try:
            analysis_path = await self._run_with_busy_screen(
                message=busy_message,
                callback=lambda c=choice: self._analyze_callback(item, c),
            )
            if choice != "cached":
                self._render_table()
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Analyze failed: {error}")
            self.notify(f"Analyze failed: {error}", severity="error", timeout=10)
            return
        if analysis_path is not None:
            try:
                open_markdown(analysis_path)
            except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
                self._update_status(f"Failed to open analysis: {error}")
                self.notify(f"Failed to open analysis: {error}", severity="error", timeout=10)
                return
        item_label = f"{item.item_type.value} {item.repo}#{item.number}"
        if choice == "cached":
            self._update_status(f"Opened analysis for {item_label}.")
        else:
            self._update_status(f"Analyzed {item_label} with {tool_label}.")

    async def action_show_branchdiff(self) -> None:
        """Launch branchdiff command in a new zellij pane for the selected item.

        Ensures the worktree exists, then spawns `workdash branchdiff`
        as a standalone command. Workdash doesn't handle the diff itself.
        """
        selected_item = self._selected_item()
        if selected_item is None:
            self._update_status("No item selected for diff.")
            return

        # Ensure worktree exists
        if self._worktree_callback is None:
            self._update_status("Worktree not configured.")
            return

        try:
            repo_path = await self._run_with_busy_screen(
                message="Preparing worktree for diff...",
                callback=lambda: self._worktree_callback(selected_item),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Worktree setup failed: {error}")
            self.notify(f"Worktree setup failed: {error}", severity="error", timeout=10)
            return

        # Launch branchdiff in a new zellij pane
        try:
            await self._run_with_busy_screen(
                message="Opening diff viewer...",
                callback=lambda: launch_branchdiff_context(repo_path, selected_item),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Failed to open diff: {error}")
            self.notify(f"Failed to open diff: {error}", severity="error", timeout=10)
            return

        self._update_status(
            f"Opened diff for {selected_item.item_type.value} "
            f"{selected_item.repo}#{selected_item.number}."
        )

    async def action_launch_code(self) -> None:
        selected_item = self._selected_item()
        if selected_item is None or self._launch_callback is None:
            self._update_status("Code launch skipped.")
            return
        # Get active agent panes for this item
        active_panes = []
        if self._list_agent_panes_callback is not None:
            try:
                active_panes = self._list_agent_panes_callback(selected_item)
            except Exception as error:
                self._update_status(f"Failed to check active panes: {error}")
                self.notify(f"Failed to check active panes: {error}", severity="error", timeout=10)
                return
        dialog = CodeDialog(
            choices=self._code_choices,
            active_agent_panes=active_panes,
        )

        def _on_dialog_result(choice: str | None) -> None:
            if choice is None:
                return
            if choice == "__focus_active_agent__":
                self._run_after_dialog(
                    lambda: self._handle_focus_active_agent(selected_item, active_panes)
                )
                return
            self._run_after_dialog(lambda: self._perform_launch(selected_item, choice))

        await self.push_screen(dialog, callback=_on_dialog_result)

    async def _perform_launch(self, item: WorkItem, choice: str) -> None:
        """Execute coding tool launch after the user picks from the dialog."""

        if self._worktree_callback is not None:
            try:
                await self._run_with_busy_screen(
                    message=f"Preparing worktree for {item.repo}#{item.number}...",
                    callback=lambda: self._worktree_callback(item),
                )
            except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
                self._update_status(f"Worktree setup failed: {error}")
                self.notify(f"Worktree setup failed: {error}", severity="error", timeout=10)
                return
        tool_label = self._choice_tool_label(self._code_choices, choice)
        try:
            await self._run_with_busy_screen(
                message=f"Launching {tool_label}...",
                callback=lambda c=choice: self._launch_callback(item, c),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Launch failed: {error}")
            self.notify(f"Launch failed: {error}", severity="error", timeout=10)
            return
        self._update_status(
            f"Launched {tool_label} for {item.item_type.value} {item.repo}#{item.number}."
        )

    async def _handle_focus_active_agent(
        self, item: WorkItem, active_panes: list[dict[str, object]]
    ) -> None:
        """Handle focusing an active agent pane - picks first if multiple exist."""
        if not active_panes:
            self._update_status("No active agent panes found.")
            return
        await self._focus_pane(active_panes[0].get("pane_id", ""))

    async def _focus_pane(self, pane_id: str) -> None:
        """Focus a Zellij pane by its ID."""
        if not pane_id or self._focus_callback is None:
            self._update_status("Cannot focus pane: no pane ID or callback.")
            return
        try:
            await self._run_with_busy_screen(
                message="Focusing agent pane...",
                callback=lambda: self._focus_callback(pane_id),
            )
            self._update_status(f"Focused pane {pane_id}.")
        except Exception as error:
            self._update_status(f"Failed to focus pane: {error}")
            self.notify(f"Failed to focus pane: {error}", severity="error", timeout=10)

    async def action_open_terminal(self) -> None:
        selected_item = self._selected_item()
        if selected_item is None or self._terminal_callback is None:
            self._update_status("Terminal launch skipped.")
            return
        if self._worktree_callback is not None:
            try:
                await self._run_with_busy_screen(
                    message=f"Preparing worktree for {selected_item.repo}#{selected_item.number}...",
                    callback=lambda: self._worktree_callback(selected_item),
                )
            except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
                self._update_status(f"Worktree setup failed: {error}")
                self.notify(f"Worktree setup failed: {error}", severity="error", timeout=10)
                return
        try:
            await self._run_with_busy_screen(
                message="Opening terminal...",
                callback=lambda: self._terminal_callback(selected_item),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Terminal failed: {error}")
            self.notify(f"Terminal failed: {error}", severity="error", timeout=10)
            return
        self._update_status(
            f"Opened terminal for {selected_item.item_type.value} {selected_item.repo}#{selected_item.number}."
        )

    async def action_include_item(self) -> None:
        if self._include_callback is None:
            self._update_status("Include skipped.")
            return
        dialog = IncludeDialog()

        def _on_dialog_result(normalized_url: str | None) -> None:
            if normalized_url is not None:
                self._run_after_dialog(lambda: self._perform_include(normalized_url))

        await self.push_screen(dialog, callback=_on_dialog_result)

    async def _perform_include(self, url: str) -> None:
        """Fetch the pasted URL and fold the item into the visible list."""

        if not url:
            self._update_status("No URL provided.")
            self.notify("No URL provided.", severity="warning", timeout=10)
            return
        existing_identities = {
            (item.item_type, item.repo, item.number) for item in self._work_items
        }
        try:
            result = await self._run_with_busy_screen(
                message="Including work item...",
                callback=lambda: self._include_callback(url, existing_identities),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            message = f"Failed to persist URL: {error}"
            self._update_status(message)
            self.notify(message, severity="error", timeout=10)
            return
        if result.invalid:
            self._update_status(f"Invalid URL: {url}")
            self.notify(f"Invalid URL: {url}", severity="error", timeout=10)
            return
        if result.transient_failure:
            message = "GitHub unreachable — URL not saved. Try again later."
            self._update_status(message)
            self.notify(message, severity="error", timeout=10)
            return
        if result.duplicate_identity is not None:
            item_type, repo, number = result.duplicate_identity
            existing = next(
                item
                for item in self._work_items
                if item.item_type == item_type and item.repo == repo and item.number == number
            )
            existing.included = True
            self._search_filter = ""
            self._render_table(focus_item=existing)
            self._update_status(
                f"Already tracking {existing.item_type.value} "
                f"{existing.repo}#{existing.number}; moved cursor."
            )
            return
        fetched_item = result.fetched_item
        assert fetched_item is not None  # IncludeResult invariant
        existing = next(
            (
                item
                for item in self._work_items
                if item.item_type == fetched_item.item_type
                and item.repo == fetched_item.repo
                and item.number == fetched_item.number
            ),
            None,
        )
        if existing is None:
            self._work_items.append(fetched_item)
            # Suggestion markers depend on the full item set; recompute so any
            # newer/better candidate is highlighted correctly on the next render.
            self._suggestion_markers = compute_suggestion_markers(self._work_items)
            existing = fetched_item
        self._search_filter = ""
        self._render_table(focus_item=existing)
        self._update_status(
            f"Included {fetched_item.item_type.value} {fetched_item.repo}#{fetched_item.number}."
        )

    async def action_capture_todo(self) -> None:
        if self._todo_callback is None:
            self._update_status("Todo skipped.")
            return
        dialog = TodoDialog()

        def _on_dialog_result(entry: tuple[str, str] | None) -> None:
            if entry is not None:
                self._run_after_dialog(lambda: self._perform_todo(entry[0], entry[1]))

        await self.push_screen(dialog, callback=_on_dialog_result)

    async def _perform_todo(self, text: str, target: str) -> None:
        """Capture a todo and fold the new item into the visible list."""

        try:
            result = await self._run_with_busy_screen(
                message="Capturing todo...",
                callback=lambda: self._todo_callback(text, target or None),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            message = f"Todo failed: {error}"
            self._update_status(message)
            self.notify(message, severity="error", timeout=10)
            return
        self.refresh_from_session()
        self._update_status(f"Captured todo {result['item_id']}.")

    def action_quit_app(self) -> None:
        self.exit()
