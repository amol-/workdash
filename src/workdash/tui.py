"""Textual list view for work items."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, TypeVar

from .backend import IncludeResult, compute_suggestion_markers
from .config import WorkdashAgentChoice
from .launcher import launch_branchdiff_context, open_markdown
from .models import WorkItem, WorkItemType, format_type_label

SuggestionMarkers = dict[tuple[WorkItemType, str, int], str]
RefreshCallbackResult = Sequence[WorkItem] | tuple[Sequence[WorkItem], SuggestionMarkers]
AnalyzeCallbackResult = str | None
_CallbackResult = TypeVar("_CallbackResult")

if TYPE_CHECKING:
    from .control import WorkdashSession


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC)


try:
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Input, Static
except ModuleNotFoundError:  # pragma: no cover - allows import before deps are installed

    class WorkdashApp:  # type: ignore[no-redef]
        """Fallback app shell used only when textual is unavailable."""

        TITLE = "workdash"

        def __init__(
            self,
            work_items: Sequence[WorkItem] | None = None,
            suggestion_markers: SuggestionMarkers | None = None,
            open_callback: Callable[[WorkItem], None] | None = None,
            refresh_callback: Callable[[], RefreshCallbackResult] | None = None,
            analyze_callback: Callable[[WorkItem, str], AnalyzeCallbackResult] | None = None,
            launch_callback: Callable[[WorkItem, str], None] | None = None,
            analyze_choices: Sequence[WorkdashAgentChoice] | None = None,
            code_choices: Sequence[WorkdashAgentChoice] | None = None,
            terminal_callback: Callable[[WorkItem], None] | None = None,
            include_callback: (
                Callable[[str, set[tuple[WorkItemType, str, int]]], IncludeResult] | None
            ) = None,
            session: WorkdashSession | None = None,
            now_utc: datetime | None = None,
        ) -> None:
            self.work_items = tuple(work_items or ())
            self.suggestion_markers = dict(suggestion_markers or {})
            self.open_callback = open_callback
            self.refresh_callback = refresh_callback
            self.analyze_callback = analyze_callback
            self.launch_callback = launch_callback
            self.analyze_choices = tuple(analyze_choices or ())
            self.code_choices = tuple(code_choices or ())
            self.terminal_callback = terminal_callback
            self.include_callback = include_callback
            self.session = session
            self.now_utc = now_utc or datetime.now(UTC)
            self.status_message = "Ready."

        def refresh_from_session(self) -> None:
            if self.session is None:
                return
            self.work_items = tuple(self.session.work_items)
            self.suggestion_markers = dict(self.session.suggestion_markers)
else:

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
        """Modal dialog for choosing a coding tool to launch."""

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

        def __init__(self, *, choices: Sequence[WorkdashAgentChoice]) -> None:
            super().__init__()
            self._choices = tuple(choices)
            self._choices_by_key = {choice.key: choice for choice in self._choices}

        def compose(self) -> ComposeResult:
            with VerticalScroll(id="code-shell"):
                yield Static("Launch coding session:", classes="code-line")
                yield Static("")
                for choice in self._choices:
                    yield Static(f"({choice.key}) {choice.label}", classes="code-line")
                if not self._choices:
                    yield Static("No coding agents configured.", classes="code-line")
                yield Static("")
                yield Static("(Esc) Cancel", classes="code-line")

        def on_key(self, event) -> None:
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
        COMMAND_HINT_TEXT = "(o)pen (r)efresh (a)nalyze (c)ode (d)iff (t)erminal (i)nclude (q)uit"
        BINDINGS = [
            ("o", "open_link", "Open"),
            ("r", "refresh_items", "Refresh"),
            ("a", "analyze_item", "Analyze"),
            ("c", "launch_code", "Code"),
            ("d", "show_branchdiff", "Diff"),
            ("t", "open_terminal", "Terminal"),
            ("i", "include_item", "Include"),
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
            session: WorkdashSession | None = None,
            now_utc: datetime | None = None,
        ) -> None:
            super().__init__()
            self._session = session
            self._work_items = list(work_items or ())
            self._sorted_work_items: list[WorkItem] = []
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
            self._now_utc = now_utc or datetime.now(UTC)
            self._status_message = "Ready."

        def compose(self) -> ComposeResult:
            yield DataTable(id="work-items")
            yield Static(self._status_message, id="status-footer")
            yield Static(self.COMMAND_HINT_TEXT, id="command-footer")

        def on_mount(self) -> None:
            if self._session is not None:
                self.refresh_from_session()
            else:
                self._render_table()

        def refresh_from_session(self) -> None:
            if self._session is None:
                return
            self._work_items = list(self._session.work_items)
            self._suggestion_markers = dict(self._session.suggestion_markers)
            self._render_table()

        def _update_status(self, message: str) -> None:
            self._status_message = message
            self.query_one("#status-footer", Static).update(message)

        def _title_with_suggestion_marker(self, item: WorkItem) -> str:
            marker = self._suggestion_markers.get((item.item_type, item.repo, item.number))
            return f"* {item.title}" if marker else item.title

        def _render_table(self, *, focus_item: WorkItem | None = None) -> None:
            table = self.query_one("#work-items", DataTable)
            table.cursor_type = "row"
            previous_row = table.cursor_row if table.cursor_row is not None else 0
            if table.columns:
                table.clear()
            else:
                table.add_column("Type", key="type")
                table.add_column("Repo", key="repo")
                table.add_column("Title", key="title")
                table.add_column("Age", key="age")
                table.add_column("Last Update", key="last_update")
            self._sorted_work_items = sorted(
                self._work_items,
                key=lambda entry: entry.updated_at,
                reverse=True,
            )
            cutoff = self._now_utc - timedelta(hours=24)
            for item in self._sorted_work_items:
                age_days = max(0, (self._now_utc - _to_utc(item.created_at)).days)
                update_days = max(0, (self._now_utc - _to_utc(item.updated_at)).days)
                type_label = format_type_label(item)
                title = self._title_with_suggestion_marker(item)
                row_key = f"{item.item_type.value}:{item.repo}#{item.number}"
                if _to_utc(item.updated_at) >= cutoff:
                    table.add_row(
                        Text(type_label, style="bold"),
                        Text(item.repo, style="bold"),
                        Text(title, style="bold"),
                        Text(f"{age_days}d", style="bold"),
                        Text(f"{update_days}d", style="bold"),
                        key=row_key,
                    )
                else:
                    table.add_row(
                        type_label,
                        item.repo,
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
                    callback=lambda: launch_branchdiff_context(repo_path),
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
            dialog = CodeDialog(choices=self._code_choices)

            def _on_dialog_result(choice: str | None) -> None:
                if choice is not None:
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
            self._render_table(focus_item=existing)
            self._update_status(
                f"Included {fetched_item.item_type.value} "
                f"{fetched_item.repo}#{fetched_item.number}."
            )

        def action_quit_app(self) -> None:
            self.exit()
