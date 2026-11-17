"""Textual list view for work items."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

from .backend import IncludeResult, compute_suggestion_markers
from .config import WorkdashAgentChoice
from .github_client import parse_github_item_url
from .launcher import (
    ANSI_ESCAPE_RE,
    launch_branchdiff_context,
    open_in_browser,
    open_markdown,
)
from .models import (
    WorkItem,
    WorkItemKind,
    WorkItemType,
    ci_status_symbol,
    display_repo,
    format_type_label,
)

SuggestionMarkers = dict[tuple[WorkItemType, str, int], str]
RefreshCallbackResult = Sequence[WorkItem] | tuple[Sequence[WorkItem], SuggestionMarkers]
AnalyzeCallbackResult = str | None
_CallbackResult = TypeVar("_CallbackResult")
# The Repo column is capped at this reference name so the title keeps more room.
_MAX_REPO_WIDTH = 15
# A failed session's error is external text, so one progress row shows at most this much.
_MAX_PROGRESS_ERROR_WIDTH = 60
_TYPE_CODES = {"ISSUE": "IS", "PR": "PR", "REVIEW": "RV", "CHECK": "VR"}

if TYPE_CHECKING:
    from .control import WorkdashSession


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC)


def _type_column(item: WorkItem, *, bold: bool, todo_repository: str = "") -> Text:
    """Return the Type column cell, prefixed with the item's CI and review symbols.

    An item without a CI result or review state keeps a blank prefix so every
    Type label stays aligned under the ones that carry a symbol.

    :param WorkItem item: the work item whose type is shown.
    :param bool bold: whether the whole row is highlighted as recently updated.
    """

    (ci_symbol, ci_color), (review_symbol, review_color) = ci_status_symbol(
        item.ci_state, item.review_decision, item.review_requested, item.has_unresolved_comments
    )
    prefix = f"{ci_symbol}{review_symbol}"
    label = format_type_label(item)
    code = (
        "TD"
        if item.item_type == WorkItemType.ISSUE and item.repo.lower() == todo_repository.lower()
        else _TYPE_CODES[label.removesuffix("+")]
    ) + ("+" if item.included else "")
    cell = Text(
        f"{prefix}{code} {item.number}",
        style="bold" if bold else "",
    )
    if ci_color is not None:
        cell.stylize(ci_color, 0, len(ci_symbol))
    if review_color is not None:
        cell.stylize(review_color, len(ci_symbol), len(prefix))
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
            yield Static(self._message, id="busy-message", markup=False)


class AnalyzeDialog(ModalScreen[str | None]):
    """Modal dialog for choosing an analysis action."""

    DEFAULT_CSS = """
        #analyze-shell {
            align: center middle;
            width: 50;
            height: auto;
            max-height: 19;
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
                yield Static(f"({choice.key}) {choice.label}", classes="analyze-line", markup=False)
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


class ReviewDialog(ModalScreen[str | None]):
    """Modal dialog for choosing which analysis agent reviews the selected item."""

    DEFAULT_CSS = """
        #review-shell {
            align: center middle;
            width: 50;
            height: auto;
            max-height: 17;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        .review-line {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, choices: Sequence[WorkdashAgentChoice]) -> None:
        super().__init__()
        self._choices = tuple(choices)
        self._choices_by_key = {choice.key: choice for choice in self._choices}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="review-shell"):
            yield Static("Review with:", classes="review-line")
            yield Static("")
            for choice in self._choices:
                yield Static(f"({choice.key}) {choice.agent}", classes="review-line", markup=False)
            if not self._choices:
                yield Static("No analysis agents configured.", classes="review-line")
            yield Static("")
            yield Static("(Esc) Cancel", classes="review-line")

    def on_key(self, event) -> None:
        choice = self._choices_by_key.get(event.key)
        if choice is not None:
            event.stop()
            self.dismiss(choice.agent)

    def action_cancel(self) -> None:
        self.dismiss(None)


class CodeDialog(ModalScreen[str | None]):
    """Modal dialog for choosing a coding action for the selected item."""

    DEFAULT_CSS = """
        #code-shell {
            align: center middle;
            width: 50;
            height: auto;
            max-height: 21;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        .code-line {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, *, choices: Sequence[WorkdashAgentChoice]) -> None:
        super().__init__()
        self._choices = tuple(choices)
        self._choices_by_key = {choice.key: choice for choice in self._choices}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="code-shell"):
            yield Static("Code menu:", classes="code-line")
            yield Static("")
            for choice in self._choices:
                yield Static(f"({choice.key}) {choice.label}", classes="code-line", markup=False)
            if not self._choices:
                yield Static("No coding agents configured.", classes="code-line")
            yield Static("")
            yield Static("(e) edit (VSCode Copilot)", classes="code-line")
            yield Static("(d) branchdiff", classes="code-line")
            yield Static("(t) terminal", classes="code-line")
            yield Static("(r) auto review", classes="code-line")
            yield Static("(Esc) Cancel", classes="code-line")

    def on_key(self, event) -> None:
        if event.key in {"e", "d", "t", "r"}:
            event.stop()
            self.dismiss({"e": ":edit", "d": ":diff", "t": ":terminal", "r": ":review"}[event.key])
            return
        choice = self._choices_by_key.get(event.key)
        if choice is not None:
            event.stop()
            self.dismiss(choice.agent)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ViewDialog(ModalScreen[str | None]):
    """Modal dialog for choosing how to open the selected item."""

    DEFAULT_CSS = """
        #view-shell {
            align: center middle;
            width: 40;
            height: auto;
            max-height: 9;
            border: solid $accent;
            background: $surface;
            padding: 1 2;
        }
        .view-line {
            width: 100%;
        }
        """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="view-shell"):
            yield Static("View menu:", classes="view-line")
            yield Static("")
            yield Static("(v) view", classes="view-line")
            yield Static("(o) open", classes="view-line")
            yield Static("")
            yield Static("(Esc) Cancel", classes="view-line")

    def on_key(self, event) -> None:
        if event.key in {"v", "o"}:
            event.stop()
            self.dismiss(event.key)

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


class WorkItemsTable(DataTable):
    def on_resize(self, event: events.Resize) -> None:
        old_height = getattr(self, "_previous_height", 0)
        old_width = getattr(self, "_previous_width", 0)
        self._previous_width = event.size.width
        if old_width != event.size.width and self.columns:
            self.app._render_table(preserve_view=True)
        old_app_size = getattr(self, "_previous_app_size", None)
        self._previous_height = event.size.height
        self._previous_app_size = self.app.size
        row_y = self._get_row_region(self.cursor_row).y - self.scroll_y if self.row_count else -1
        if old_app_size != self.app.size or self.header_height <= row_y < old_height:
            self.call_after_refresh(self.move_cursor)
        self.call_after_refresh(self.app._reserve_table_space)

    def watch_show_horizontal_scrollbar(self) -> None:
        self.call_after_refresh(self.app._reserve_table_space)


class WorkdashApp(App[None]):
    """Main Textual app shell."""

    TITLE = "workdash"
    DEFAULT_CSS = """
        #autodispatch-scroll {
            display: none;
            height: auto;
        }
        #autodispatch-progress {
            height: auto;
            padding: 0 1;
        }
        #work-items {
            height: 1fr;
            overflow-x: hidden;
        }
        #status-footer {
            height: 1;
            padding: 0 1;
        }
        #command-footer {
            height: 1;
            padding: 0 1;
        }
        """
    SEARCH_HINT_TEXT = "(/)search"
    COMMAND_HINT_TEXT = (
        f"{SEARCH_HINT_TEXT} (v)iew (r)efresh (a)nalyze (c)ode (d)ispatch (i)nclude (w)todo (q)uit"
    )
    BINDINGS = [
        ("/", "search_items", "Search"),
        ("v", "view_item", "View"),
        ("r", "refresh_items", "Refresh"),
        ("a", "analyze_item", "Analyze"),
        ("c", "launch_code", "Code"),
        ("d", "dispatch_item", "Dispatch"),
        ("m", "toggle_automerge", "Merge"),
        ("s", "stop_blocked", "Stop blocked"),
        ("i", "include_item", "Include"),
        ("w", "capture_todo", "Todo"),
        ("q", "quit_app", "Quit"),
    ]

    def on_resize(self, event: events.Resize) -> None:
        self._reserve_table_space()

    def _reserve_table_space(self) -> None:
        table = self.query_one("#work-items", DataTable)
        self.query_one("#autodispatch-scroll", VerticalScroll).styles.max_height = max(
            0, self.size.height - 4 - int(table.show_horizontal_scrollbar)
        )

    def __init__(
        self,
        work_items: Sequence[WorkItem] | None = None,
        suggestion_markers: SuggestionMarkers | None = None,
        open_callback: Callable[[WorkItem], None] | None = None,
        open_markdown_callback: Callable[[str], None] | None = None,
        open_url_callback: Callable[[str], None] | None = None,
        view_callback: Callable[[WorkItem], None] | None = None,
        refresh_callback: Callable[[], RefreshCallbackResult] | None = None,
        analyze_callback: Callable[[WorkItem, str], AnalyzeCallbackResult] | None = None,
        launch_callback: Callable[[WorkItem, str], dict[str, object]] | None = None,
        review_callback: Callable[[WorkItem, str], dict[str, object]] | None = None,
        worktree_callback: Callable[[WorkItem], str] | None = None,
        analyze_choices: Sequence[WorkdashAgentChoice] | None = None,
        code_choices: Sequence[WorkdashAgentChoice] | None = None,
        terminal_callback: Callable[[WorkItem], dict[str, object]] | None = None,
        include_callback: (
            Callable[[str, set[tuple[WorkItemType, str, int]]], IncludeResult] | None
        ) = None,
        todo_callback: Callable[[str, str | None], dict[str, object]] | None = None,
        session: WorkdashSession | None = None,
        now_utc: datetime | None = None,
        focus_callback: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self._session = session
        self._todo_repository = session.config.todo_repository if session is not None else ""
        self._work_items = sorted(work_items or (), key=lambda item: item.updated_at, reverse=True)
        self._sorted_work_items: list[WorkItem] = []
        # Cleared whenever the loaded list changes so new items are never hidden.
        self._search_filter = ""
        self._suggestion_markers = dict(suggestion_markers or {})
        self._open_callback = open_callback
        self._open_markdown_callback = open_markdown_callback or open_markdown
        self._open_url_callback = open_url_callback or open_in_browser
        self._view_callback = view_callback
        self._refresh_callback = refresh_callback
        self._analyze_callback = analyze_callback
        self._launch_callback = launch_callback
        self._review_callback = review_callback
        self._worktree_callback = worktree_callback
        self._analyze_choices = tuple(analyze_choices or ())
        self._code_choices = tuple(code_choices or ())
        self._terminal_callback = terminal_callback
        self._include_callback = include_callback
        self._todo_callback = todo_callback
        self._now_utc = now_utc or datetime.now(UTC)
        self._focus_callback = focus_callback
        self._dispatch_session_id: str | None = None
        self._status_message = "Ready."
        self._current_autodispatch: set[tuple[str, int] | str] = set()
        self._pending_autodispatch: set[str] = set()
        self._autodispatch_refreshing = False
        self._progress_index = 0
        self._progress_entries: list[dict[str, object]] = []
        self._row_keys: dict[tuple[WorkItemType, str, int], str] = {}

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="autodispatch-scroll"):
            yield Static("", id="autodispatch-progress", markup=False)
        yield WorkItemsTable(id="work-items")
        yield Static(self.COMMAND_HINT_TEXT, id="command-footer")
        yield Static(self._status_message, id="status-footer", markup=False)

    def on_mount(self) -> None:
        self.query_one("#work-items", DataTable).focus()
        if self._session is not None:
            self.set_interval(1, self._update_autodispatch_progress)
            self._update_autodispatch_progress()
            self.refresh_from_session()
        else:
            self._render_table()
        self._update_status(f"Loaded {len(self._work_items)} item(s).")

    def _update_autodispatch_progress(self) -> None:
        if self._session is None:
            return
        sessions = self._session.autodispatch_sessions()
        running = [entry for entry in sessions if entry["status"] in {"running", "blocked"}]
        current = {
            self._session._scope_identity(str(entry["current_item"]))
            for entry in running
            if entry["current_item"]
        }
        changed = current ^ self._current_autodispatch
        self._current_autodispatch = current
        if changed and self._sorted_work_items:
            table = self.query_one("#work-items", DataTable)
            issue_rows = {
                self._item_identity(item)
                for item in self._sorted_work_items
                if item.item_type == WorkItemType.ISSUE
            }
            current_without_rows = current - issue_rows
            for item in self._sorted_work_items:
                key = self._row_keys[(item.item_type, item.repo, item.number)]
                table.update_cell(key, "title", self._title_cell(item, current_without_rows))
        self._pending_autodispatch.update(self._session.take_completed_autodispatch_items())
        if self._pending_autodispatch and not self._autodispatch_refreshing:
            self._autodispatch_refreshing = True
            asyncio.create_task(self._refresh_autodispatch_items())
        failed = [entry for entry in sessions if entry["status"] == "failed"]
        started_failure = next(
            (entry for entry in failed if entry["id"] == self._dispatch_session_id), None
        )
        if started_failure is not None:
            self._dispatch_session_id = None
            self._update_status(
                f"Autodispatch {started_failure['id']} failed: {started_failure['error']}"
            )
        entries = running + failed[-3:]
        self._progress_entries = entries
        widget = self.query_one("#autodispatch-progress", Static)
        self.query_one("#autodispatch-scroll", VerticalScroll).display = bool(entries)

        def short(value: str) -> str:
            parsed = parse_github_item_url(value)
            if parsed and parsed.item_type == WorkItemType.ISSUE:
                return f"{parsed.repo}#{parsed.number}"
            return re.sub(
                r"#(?:ISSUE-)?(\d+)$",
                lambda match: f"#{int(match.group(1))}",
                value,
                flags=re.IGNORECASE,
            )

        self._progress_index = min(self._progress_index, max(0, len(entries) - 1))
        rows = []
        for index, entry in enumerate(entries):
            if entry["status"] == "failed":
                # Error text can span lines and carry escape sequences; the full text
                # stays available through `workdash info`.
                error = " ".join(ANSI_ESCAPE_RE.sub("", str(entry["error"])).split())
                line = (
                    f"Failed {short(entry['scope'])}: {error[:_MAX_PROGRESS_ERROR_WIDTH]}"
                    f"  ({entry['id']})"
                )
            else:
                scope = short(entry["scope"])
                item = short(entry["current_item"]) if entry["current_item"] else None
                todo = re.search(r"#ISSUE-WT(\d+)$", item or "", re.IGNORECASE)
                same_todo = todo and scope.lower() == (
                    f"{self._session.config.todo_repository}#{int(todo.group(1))}".lower()
                )
                target = (
                    f"scope: {scope}  item: {item}"
                    if item and item.lower() != scope.lower() and not same_todo
                    else scope
                )
                reason = (
                    f" ({entry['launch_reason'].replace('_', ' ')})"
                    if entry["stage"] == "developing" and entry["launch_reason"]
                    else ""
                )
                status = "blocked (s)top" if entry["status"] == "blocked" else entry["stage"]
                line = f"{entry['selected_group'] or 'automatic'}  {target}  {status}{reason}  {entry['iteration']}/{entry['elapsed_seconds']}s"
            if entry.get("automerge"):
                line = f"[automerge] {line}"
            marker = "> " if index == self._progress_index else "  "
            text = Text(marker + " ".join(line.splitlines()))
            text.truncate(max(0, self.size.width - 4), overflow="ellipsis")
            rows.append(text)
        widget.update(Text("\n").join(rows))
        selected_blocked = (
            self.query_one("#autodispatch-scroll", VerticalScroll).has_focus
            and bool(entries)
            and entries[self._progress_index]["status"] == "blocked"
        )
        hint = self._command_hint()
        if selected_blocked:
            hint = Text(hint.plain.replace("(d)ispatch", "(s)top"))
        self.query_one("#command-footer", Static).update(hint)

    def on_key(self, event: events.Key) -> None:
        scroll = self.query_one("#autodispatch-scroll", VerticalScroll)
        if not scroll.has_focus or event.key not in {"up", "down"}:
            return
        sessions = self._session.autodispatch_sessions() if self._session else []
        count = sum(entry["status"] in {"running", "blocked"} for entry in sessions)
        count += min(3, sum(entry["status"] == "failed" for entry in sessions))
        self._progress_index = max(
            0, min(count - 1, self._progress_index + (1 if event.key == "down" else -1))
        )
        scroll.scroll_to(y=self._progress_index, animate=False)
        self._update_autodispatch_progress()
        event.prevent_default()
        event.stop()

    def action_stop_blocked(self) -> None:
        scroll = self.query_one("#autodispatch-scroll", VerticalScroll)
        if self._session is None or not scroll.has_focus:
            return
        entries = self._progress_entries
        index = self._progress_index
        if index < len(entries) and entries[index]["status"] == "blocked":
            try:
                self._session.stop_autodispatch(str(entries[index]["id"]))
            except Exception as error:
                self._update_status(f"Could not stop autodispatch: {error}")
            self._update_autodispatch_progress()

    def _item_identity(self, item: WorkItem) -> tuple[str, int]:
        repo = self._session.config.todo_repository if item.todo_target else item.repo
        return repo.lower(), item.number

    def _matches_filter(self, item: WorkItem) -> bool:
        needle = self._search_filter.lower()
        return any(
            needle in value.lower()
            for value in (
                str(_type_column(item, bold=False, todo_repository=self._todo_repository))[2:],
                f"{format_type_label(item)}#{item.number}",
                display_repo(item),
                item.title if item.state == "OPEN" else f"{item.title} ({item.state.lower()})",
            )
        )

    def _title_cell(self, item: WorkItem, current_without_rows: set[tuple[str, int] | str]) -> Text:
        title = self._title_with_suggestion_marker(item)
        if item.state != "OPEN":
            title = f"{title} ({item.state.lower()})"
        bold = _to_utc(item.updated_at) >= self._now_utc - timedelta(hours=24)
        current = self._current_autodispatch
        linked = {(repo.lower(), number) for repo, number in item.linked_issues}
        linked.update((item.repo.lower(), number) for number in item.closing_issue_numbers)
        if self._session is not None and (
            (item.item_type == WorkItemType.ISSUE and self._item_identity(item) in current)
            or (item.item_type == WorkItemType.PR and bool(linked & current_without_rows))
        ):
            return Text(title, style="bold magenta" if bold or item.has_sub_issues else "magenta")
        if item.item_type == WorkItemType.ISSUE and item.has_sub_issues:
            return Text(title, style="bold cyan")
        return Text(title, style="bold" if bold else "")

    async def _refresh_autodispatch_items(self) -> None:
        try:
            while self._pending_autodispatch:
                target = self._pending_autodispatch.pop()
                try:
                    items, error = await asyncio.to_thread(
                        self._session.refresh_autodispatch_item, target
                    )
                    if not items:
                        if error:
                            raise error
                        continue
                    table = self.query_one("#work-items", DataTable)
                    selected_item = self._selected_item()
                    scroll_x, scroll_y = table.scroll_x, table.scroll_y
                    row_y = (
                        table._get_row_region(table.cursor_row).y - scroll_y
                        if selected_item
                        else -1
                    )
                    selection_visible = table.header_height <= row_y < table.size.height
                    for item in items:
                        for index, old in enumerate(self._work_items):
                            if (old.item_type, old.repo, old.number) == (
                                item.item_type,
                                item.repo,
                                item.number,
                            ):
                                self._work_items[index] = item
                                break
                    self._render_table(focus_item=selected_item, preserve_view=True)
                    table.scroll_to(x=scroll_x, y=scroll_y, animate=False, force=True)
                    table.call_after_refresh(
                        table.scroll_to, x=scroll_x, y=scroll_y, animate=False, force=True
                    )
                    table.call_after_refresh(table.move_cursor, scroll=selection_visible)
                    if error:
                        raise error
                except Exception as error:
                    self._update_status(f"Autodispatch refresh failed: {error}")
                    self.set_timer(30, lambda target=target: self._pending_autodispatch.add(target))
        finally:
            self._autodispatch_refreshing = False

    def refresh_from_session(self) -> None:
        if self._session is None:
            return
        self._search_filter = ""
        self._work_items = sorted(
            self._session.work_items, key=lambda item: item.updated_at, reverse=True
        )
        self._suggestion_markers = dict(self._session.suggestion_markers)
        self._render_table()

    def _update_status(self, message: str) -> None:
        self._status_message = message
        self.query_one("#status-footer", Static).update(message)

    def _title_with_suggestion_marker(self, item: WorkItem) -> str:
        marker = self._suggestion_markers.get((item.item_type, item.repo, item.number))
        return f"* {item.title}" if marker else item.title

    def _selected_autodispatch(self) -> list[dict[str, object]]:
        item = self._selected_item()
        if item is None or self._session is None:
            return []
        identities = {self._item_identity(item)}
        identities.update((repo.lower(), number) for repo, number in item.linked_issues)
        identities.update((item.repo.lower(), number) for number in item.closing_issue_numbers)
        return [
            entry
            for entry in self._session.autodispatch_sessions()
            if entry["status"] == "running"
            and any(
                value and self._session._scope_identity(str(value)) in identities
                for value in (entry["current_item"], entry["scope"])
            )
        ]

    def _command_hint(self) -> Text:
        entries = self._selected_autodispatch()
        action = "(m)erge off" if len(entries) == 1 and entries[0].get("automerge") else "(m)erge"
        hint = Text(
            self.COMMAND_HINT_TEXT.replace("(d)ispatch", action)
            if entries
            else self.COMMAND_HINT_TEXT
        )
        for match in re.finditer(r"\((.)\)", hint.plain):
            hint.stylize("bold", match.start() + 1, match.end() - 1)
        if self._search_filter:
            hint.stylize("bold", 0, len(self.SEARCH_HINT_TEXT))
        return hint

    def _render_table(
        self, *, focus_item: WorkItem | None = None, preserve_view: bool = False
    ) -> None:
        table = self.query_one("#work-items", DataTable)
        table.cursor_type = "row"
        previous_row = table.cursor_row if table.cursor_row is not None else 0
        # Anchor on the selected item by default: the row set changes wholesale
        # when a search filter is applied or cleared, so keeping the row index
        # would silently move the selection to a different work item.
        if focus_item is None:
            focus_item = self._selected_item()
        # Neither the suggestion marker nor the CI symbol is searchable.
        matched_items = [item for item in self._work_items if self._matches_filter(item)]
        table.clear(columns=True)
        self._row_keys.clear()
        type_width = max(
            (
                len(str(_type_column(item, bold=False, todo_repository=self._todo_repository)))
                for item in matched_items
            ),
            default=4,
        )
        table.add_column("Type", key="type", width=max(4, type_width))
        table.add_column("Repo", key="repo")
        repo_width = max((cell_len(_repo_column(item)) for item in matched_items), default=4)
        age_width = max(
            (
                len(f"{max(0, (self._now_utc - _to_utc(item.created_at)).days)}d")
                for item in matched_items
            ),
            default=3,
        )
        update_width = max(
            (
                len(f"{max(0, (self._now_utc - _to_utc(item.updated_at)).days)}d")
                for item in matched_items
            ),
            default=11,
        )
        title_width = max(
            1,
            (table.size.width or self.size.width)
            - 1
            - 10
            - max(4, type_width)
            - max(4, repo_width)
            - max(3, age_width)
            - max(11, update_width),
        )
        table.add_column("Title", key="title", width=title_width)
        table.add_column("Age", key="age")
        table.add_column("Last Update", key="last_update")
        self._sorted_work_items = matched_items
        cutoff = self._now_utc - timedelta(hours=24)
        issue_rows = (
            {
                self._item_identity(item)
                for item in matched_items
                if item.item_type == WorkItemType.ISSUE
            }
            if self._session is not None and self._current_autodispatch
            else set()
        )
        current_without_rows = self._current_autodispatch - issue_rows
        for item in self._sorted_work_items:
            age_days = max(0, (self._now_utc - _to_utc(item.created_at)).days)
            update_days = max(0, (self._now_utc - _to_utc(item.updated_at)).days)
            row_key = f"{item.item_type.value}:{item.repo}#{item.number}"
            self._row_keys[(item.item_type, item.repo, item.number)] = row_key
            if _to_utc(item.updated_at) >= cutoff:
                table.add_row(
                    _type_column(item, bold=True, todo_repository=self._todo_repository),
                    Text(_repo_column(item), style="bold"),
                    self._title_cell(item, current_without_rows),
                    Text(f"{age_days}d", style="bold"),
                    Text(f"{update_days}d", style="bold"),
                    key=row_key,
                )
            else:
                table.add_row(
                    _type_column(item, bold=False, todo_repository=self._todo_repository),
                    _repo_column(item),
                    self._title_cell(item, current_without_rows),
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
            table.move_cursor(
                row=min(max(target_row, 0), len(self._sorted_work_items) - 1),
                scroll=not preserve_view,
            )
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

    async def action_view_item(self) -> None:
        selected_item = self._selected_item()
        if selected_item is None:
            self._update_status("View skipped.")
            return

        def _on_dialog_result(choice: str | None) -> None:
            if choice == "v":
                self._run_after_dialog(lambda: self._perform_view(selected_item))
            elif choice == "o":
                self._run_after_dialog(self.action_open_link)

        await self.push_screen(ViewDialog(), callback=_on_dialog_result)

    async def _perform_view(self, item: WorkItem) -> None:
        """Show ``item`` as a document in its own viewer pane."""
        if self._view_callback is None:
            self._update_status("View skipped.")
            return
        try:
            await self._run_with_busy_screen(
                message="Opening viewer...",
                callback=lambda: self._view_callback(item),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"View failed: {error}")
            self.notify(f"View failed: {error}", severity="error", timeout=10, markup=False)
            return
        self._update_status(f"Opened viewer for {item.item_type.value} {item.repo}#{item.number}.")

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
            if self._session is not None:
                self.refresh_from_session()
            elif (
                isinstance(refreshed, tuple)
                and len(refreshed) == 2
                and isinstance(refreshed[1], dict)
            ):
                self._work_items = list(refreshed[0])
                self._suggestion_markers = dict(refreshed[1])
            else:
                self._work_items = list(refreshed)
            if self._session is None:
                self._work_items.sort(key=lambda item: item.updated_at, reverse=True)
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

    def _session_analysis_url(self, item: WorkItem) -> str | None:
        """Return the server analysis URL for ``item`` when the shared session is known."""

        session = self._session
        if session is None:
            return None
        return session.analysis_url(item)

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
            self.notify(f"Analyze failed: {error}", severity="error", timeout=10, markup=False)
            return
        if analysis_path is not None:
            try:
                self._open_markdown_callback(analysis_path)
            except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
                self._update_status(f"Failed to open analysis: {error}")
                self.notify(f"Failed to open analysis: {error}", severity="error", timeout=10)
                return
        item_label = f"{item.item_type.value} {item.repo}#{item.number}"
        if choice == "cached":
            self._update_status(f"Opened analysis for {item_label}.")
        else:
            self._update_status(f"Analyzed {item_label} with {tool_label}.")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self.query("#command-footer") and self.query("#work-items"):
            self.query_one("#command-footer", Static).update(self._command_hint())

    def action_toggle_automerge(self) -> None:
        entries = self._selected_autodispatch()
        if not entries:
            self._update_status("No autodispatch session for selected work.")
            return
        if len(entries) > 1:
            self._update_status(
                "Multiple sessions match; use workdash info and autodispatch-stop to select one."
            )
            return
        entry = entries[0]
        try:
            enabled = self._session.toggle_automerge(str(entry["id"]))
        except (KeyError, RuntimeError) as error:
            self._update_status(f"Automerge toggle failed: {error}")
            return
        self._update_status(
            f"Automerge {'enabled' if enabled else 'disabled'} for {entry['scope']}."
            + (" A merge already in progress may still complete." if not enabled else "")
        )
        self._update_autodispatch_progress()
        self.query_one("#command-footer", Static).update(self._command_hint())

    async def action_dispatch_item(self) -> None:
        if self.query_one("#autodispatch-scroll", VerticalScroll).has_focus:
            return
        item = self._selected_item()
        if item is None or self._session is None:
            self._update_status("Dispatch skipped.")
            return
        if item.kind != WorkItemKind.AUTHORED_PR and item.item_type != WorkItemType.ISSUE:
            self._update_status("Dispatch is only available for issues and authored pull requests.")
            return

        try:
            started = await self._run_with_busy_screen(
                message="Starting autodispatch...",
                callback=lambda: self._session.start_autodispatch_for_item(item),
            )
        except Exception as error:
            self._update_status(f"Dispatch failed: {error}")
            self.notify(f"Dispatch failed: {error}", severity="error", timeout=10)
            return
        if started is None:
            self._update_status("Cannot dispatch a pull request without a linked issue.")
            self.notify("Cannot dispatch a pull request without a linked issue.", severity="error")
            return
        self._dispatch_session_id = str(started["id"])
        self._update_status(f"Started autodispatch for {started['scope']}: {started['id']}.")

    async def action_show_branchdiff(self, item: WorkItem | None = None) -> None:
        """Launch branchdiff command in a new zellij pane for ``item``.

        Ensures the worktree exists, then spawns `workdash branchdiff`
        as a standalone command. Workdash doesn't handle the diff itself.

        :param WorkItem item: the target item, defaulting to the current selection.
        """
        selected_item = item if item is not None else self._selected_item()
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
                callback=lambda: launch_branchdiff_context(
                    repo_path,
                    selected_item,
                    github_client=self._session.backend.github_client if self._session else None,
                ),
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
        if selected_item is None:
            self._update_status("Code launch skipped.")
            return

        def _on_dialog_result(choice: str | None) -> None:
            if choice is None:
                return
            if choice == ":diff":
                self._run_after_dialog(lambda: self.action_show_branchdiff(selected_item))
            elif choice == ":terminal":
                self._run_after_dialog(lambda: self.action_open_terminal(selected_item))
            elif choice == ":review":
                self._run_after_dialog(lambda: self._choose_review_agent(selected_item))
            else:
                self._run_after_dialog(
                    lambda: self._perform_launch(
                        selected_item, "vscode" if choice == ":edit" else choice
                    )
                )

        await self.push_screen(CodeDialog(choices=self._code_choices), callback=_on_dialog_result)

    async def _perform_launch(self, item: WorkItem, choice: str) -> None:
        """Execute coding tool launch after the user picks from the dialog."""

        if self._launch_callback is None:
            self._update_status("Code launch skipped.")
            return
        tool_label = (
            "VSCode" if choice == "vscode" else self._choice_tool_label(self._code_choices, choice)
        )
        try:
            result = await self._run_with_busy_screen(
                message=f"Launching {tool_label}...",
                callback=lambda c=choice: self._launch_callback(item, c),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Launch failed: {error}")
            self.notify(f"Launch failed: {error}", severity="error", timeout=10, markup=False)
            return
        if result["status"] == "already_running":
            await self._focus_pane(str(result["pane_id"]))
            return
        self._update_status(
            f"Launched {tool_label} for {item.item_type.value} {item.repo}#{item.number}."
        )

    async def _choose_review_agent(self, item: WorkItem) -> None:
        """Ask which analysis agent reviews ``item``, then start the review."""

        def _on_dialog_result(choice: str | None) -> None:
            if choice is not None:
                self._run_after_dialog(lambda: self._perform_review(item, choice))

        await self.push_screen(
            ReviewDialog(choices=self._analyze_choices), callback=_on_dialog_result
        )

    async def _perform_review(self, item: WorkItem, choice: str) -> None:
        """Review the pull request after the user picks an analysis agent."""

        if self._review_callback is None:
            self._update_status("Review skipped.")
            return
        tool_label = self._choice_tool_label(self._analyze_choices, choice)
        item_label = f"{item.item_type.value} {item.repo}#{item.number}"
        try:
            result = await self._run_with_busy_screen(
                message=f"Reviewing with {tool_label}...",
                callback=lambda c=choice: self._review_callback(item, c),
            )
        except Exception as error:  # noqa: BLE001 - keep TUI alive on callback errors
            self._update_status(f"Review failed: {error}")
            self.notify(f"Review failed: {error}", severity="error", timeout=10, markup=False)
            return
        if result["status"] == "nothing_to_review":
            self._update_status(f"Nothing to review for {item_label}.")
            return
        # A repeated request waits on the running review, whose agent may differ.
        reviewed_label = self._choice_tool_label(self._analyze_choices, str(result["agent"]))
        self._update_status(f"Reviewed {item_label} with {reviewed_label}.")

    async def _focus_pane(self, pane_id: str) -> None:
        """Focus a Zellij pane by its ID."""
        if not pane_id or self._focus_callback is None:
            self._update_status("Cannot focus pane: no pane ID or callback.")
            return
        try:
            await self._run_with_busy_screen(
                message="Focusing pane...",
                callback=lambda: self._focus_callback(pane_id),
            )
            self._update_status(f"Focused pane {pane_id}.")
        except Exception as error:
            self._update_status(f"Failed to focus pane: {error}")
            self.notify(f"Failed to focus pane: {error}", severity="error", timeout=10)

    async def action_open_terminal(self, item: WorkItem | None = None) -> None:
        """Open a terminal for ``item``, reusing its live pane when one exists.

        :param WorkItem item: the target item, defaulting to the current selection.
        """
        selected_item = item if item is not None else self._selected_item()
        if selected_item is None or self._terminal_callback is None:
            self._update_status("Terminal launch skipped.")
            return
        if self._session is not None:
            try:
                panes = await self._run_with_busy_screen(
                    message="Checking terminal panes...",
                    callback=lambda: self._session.get_terminal_panes_for_item(selected_item),
                )
            except Exception:  # noqa: BLE001 - reusing a terminal is advisory
                panes = []
            if panes:
                await self._focus_pane(str(panes[0]["pane_id"]))
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
        if self._session is not None:
            self.refresh_from_session()
        if result.duplicate_identity is not None:
            item_type, repo, number = result.duplicate_identity
            existing = next(
                (
                    item
                    for item in self._work_items
                    if item.item_type == item_type and item.repo == repo and item.number == number
                ),
                None,
            )
            if existing is None:
                self._update_status("Included item is no longer on the dashboard.")
                return
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
        if existing is None and self._session is not None:
            self._update_status("Included item is no longer on the dashboard.")
            if result.linked_issues_unavailable:
                self.notify(
                    "Linked issues unavailable; refresh the dashboard to retry.",
                    severity="warning",
                    timeout=10,
                )
            return
        if existing is None:
            self._work_items.append(fetched_item)
            self._work_items.sort(key=lambda item: item.updated_at, reverse=True)
            # Suggestion markers depend on the full item set; recompute so any
            # newer/better candidate is highlighted correctly on the next render.
            self._suggestion_markers = compute_suggestion_markers(self._work_items)
            existing = fetched_item
        self._search_filter = ""
        self._render_table(focus_item=existing)
        self._update_status(
            f"Included {fetched_item.item_type.value} {fetched_item.repo}#{fetched_item.number}."
        )
        if result.linked_issues_unavailable:
            message = "Linked issues unavailable; refresh the dashboard to retry."
            self._update_status(message)
            self.notify(message, severity="warning", timeout=10)

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
