"""Step definitions for the analysis domain scenarios.

Covers analyze-dialog, cache-invalidation, and generate-analysis features.
The analyze path is exercised through the TUI where relevant, and through
the WorkdashBackend + AnalysisCache layer for cache-freshness scenarios.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, then, when
from textual.widgets import Static

from workdash.analysis_cache import AnalysisCache
from workdash.backend import WorkdashBackend
from workdash.config import WorkdashConfig
from workdash.models import WorkItem
from workdash.tui import AnalyzeDialog, WorkdashApp

from .common import NOW_UTC, make_valid_config, make_work_item, modal_screen_names

# --------------------------------------------------------------------------
# Shared helpers for analysis scenarios
# --------------------------------------------------------------------------


def _dialog_static_lines(dialog: AnalyzeDialog) -> list[str]:
    return [widget.render().plain for widget in dialog.query(Static)]


async def _open_analyze_dialog(app: WorkdashApp, pilot, timeout: float = 1.0) -> AnalyzeDialog:
    await pilot.press("a")
    for _ in range(20):
        await pilot.pause()
        for screen in app.screen_stack:
            if isinstance(screen, AnalyzeDialog):
                return screen
    raise AssertionError("AnalyzeDialog did not appear after pressing 'a'")


def _build_cached_item(
    tmp_path: Path,
    *,
    analyzed_recently: bool = True,
) -> tuple[WorkItem, AnalysisCache]:
    cache = AnalysisCache(tmp_path / "cache")
    item = make_work_item(
        number=77,
        title="Cached analysis target",
        updated_at=NOW_UTC - timedelta(days=1),
        created_at=NOW_UTC - timedelta(days=5),
    )
    cache.save(item, "# cached analysis\nbody")
    item.analysis = cache.load_analysis(item)
    if analyzed_recently:
        item.analyzed_at = cache.load_analysis_date(item) or (NOW_UTC - timedelta(days=2))
    return item, cache


# --------------------------------------------------------------------------
# F-ANALYSIS-DIALOG
# --------------------------------------------------------------------------


@given("the selected work item has a cached analysis")
def _selected_has_cached_analysis(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    item, cache = _build_cached_item(tmp_path)
    work_items.clear()
    work_items.append(item)
    scenario_state["selected_item"] = item
    scenario_state["cache"] = cache


@given("the selected work item has no cached analysis")
def _selected_has_no_cached_analysis(
    scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    item = make_work_item(
        number=88,
        title="Uncached target",
        updated_at=NOW_UTC - timedelta(days=1),
        created_at=NOW_UTC - timedelta(days=4),
    )
    item.analysis = None
    item.analyzed_at = None
    work_items.clear()
    work_items.append(item)
    scenario_state["selected_item"] = item


@given("the analyze dialog is open")
def _analyze_dialog_open(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    # The cancel scenario needs any selectable item.
    if not work_items:
        work_items.append(
            make_work_item(
                number=44,
                title="Cancel target",
                updated_at=NOW_UTC - timedelta(days=1),
                created_at=NOW_UTC - timedelta(days=3),
            )
        )
    scenario_state["selected_item"] = work_items[0]


def run_analyze_dialog_scenario(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    """Handle the shared "When the user presses 'a'" step for analyze flows."""

    analyze_calls: list[tuple[WorkItem, str]] = []

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str | None:
        analyze_calls.append((item, tool))
        return None

    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        dialog = await _open_analyze_dialog(app, pilot)
        captured["dialog_lines"] = _dialog_static_lines(dialog)
        # Dismiss without acting so we can assert dialog contents only.
        dialog.dismiss(None)
        await pilot.pause()

    from .common import run_app

    run_app(
        work_items=list(work_items),
        interactions=interactions,
        analyze_callback=analyze_callback,
    )
    scenario_state["dialog_lines"] = captured["dialog_lines"]
    scenario_state["analyze_calls"] = analyze_calls


@then("the dialog shows how long ago the analysis was produced")
def _dialog_shows_age(scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["dialog_lines"]
    assert any("Last analyzed:" in line and "d ago" in line for line in lines), lines


@then("the dialog offers to open the cached analysis")
def _dialog_offers_open_cached(scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["dialog_lines"]
    assert any("Open analysis" in line for line in lines), lines


@then("the dialog offers to generate a fresh analysis")
def _dialog_offers_fresh(scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["dialog_lines"]
    for choice in make_valid_config().tui_analyze_choices():
        assert any(choice.label in line for line in lines), lines


@then("the dialog tells the user there is no previous analysis")
def _dialog_says_no_analysis(scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["dialog_lines"]
    assert any("No previous analysis" in line for line in lines), lines


@when("the user cancels the dialog")
def _user_cancels(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    analyze_calls: list[tuple[WorkItem, str]] = []
    launch_calls: list[tuple[WorkItem, str]] = []
    worktree_calls: list[WorkItem] = []

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str | None:
        analyze_calls.append((item, tool))
        return None

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item, tool))

    def worktree_callback(item: WorkItem) -> str:
        worktree_calls.append(item)
        return "/tmp/bogus"

    dialog_key = "a" if scenario_state.get("dialog_kind", "analyze") == "analyze" else "c"

    async def interactions(app, pilot) -> None:
        await pilot.press(dialog_key)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    from .common import run_app

    run_app(
        work_items=list(work_items),
        analyze_callback=analyze_callback,
        launch_callback=launch_callback,
        worktree_callback=worktree_callback,
        interactions=interactions,
    )
    scenario_state["analyze_calls"] = analyze_calls
    scenario_state["launch_calls"] = launch_calls
    scenario_state["worktree_calls"] = worktree_calls


@then("no analysis is generated")
def _no_analysis_generated(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["analyze_calls"] == []


@then("no analysis is opened")
def _no_analysis_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["analyze_calls"] == []


# --------------------------------------------------------------------------
# F-ANALYSIS-CACHE
# --------------------------------------------------------------------------


@given("a work item has a cached analysis produced before the item's last update on GitHub")
def _cached_before_update(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    cache = AnalysisCache(tmp_path / "cache")
    stale_item = make_work_item(
        number=55,
        title="Stale cache",
        updated_at=NOW_UTC - timedelta(days=10),
        created_at=NOW_UTC - timedelta(days=20),
    )
    cache.save(stale_item, "old analysis")
    # Simulate GitHub updating the item — its new updated_at differs.
    fresh_item = make_work_item(
        number=55,
        title="Stale cache",
        updated_at=NOW_UTC - timedelta(days=1),
        created_at=stale_item.created_at,
    )
    work_items.clear()
    work_items.append(fresh_item)
    scenario_state["selected_item"] = fresh_item
    scenario_state["cache"] = cache


@given("a work item has a cached analysis that matches its last update on GitHub")
def _cache_matches_update(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    item, cache = _build_cached_item(tmp_path)
    work_items.clear()
    work_items.append(item)
    scenario_state["selected_item"] = item
    scenario_state["cache"] = cache


@when("the user opens the analyze dialog on that work item")
def _open_analyze_on_item(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    cache: AnalysisCache = scenario_state["cache"]
    item = scenario_state["selected_item"]
    # Re-populate analysis/analyzed_at as the backend would — the stale case
    # must correctly return None now that the item's updated_at changed.
    item.analysis = cache.load_analysis(item)
    item.analyzed_at = cache.load_analysis_date(item)

    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        dialog = await _open_analyze_dialog(app, pilot)
        captured["lines"] = _dialog_static_lines(dialog)
        dialog.dismiss(None)
        await pilot.pause()

    from .common import run_app

    run_app(
        work_items=list(work_items),
        interactions=interactions,
        analyze_callback=lambda _item, tool="codex": None,
    )
    scenario_state["dialog_lines"] = captured["lines"]


# --------------------------------------------------------------------------
# F-ANALYSIS-GENERATE
# --------------------------------------------------------------------------


@given("the user has the analyze dialog open on a work item")
def _have_analyze_dialog_open(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    if not work_items:
        work_items.append(
            make_work_item(
                number=200,
                title="Fresh analysis target",
                updated_at=NOW_UTC - timedelta(days=1),
                created_at=NOW_UTC - timedelta(days=3),
            )
        )
    scenario_state["selected_item"] = work_items[0]


@given("the user has the analyze dialog open on a work item with a cached analysis")
def _have_analyze_dialog_on_cached(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    item, cache = _build_cached_item(tmp_path)
    work_items.clear()
    work_items.append(item)
    scenario_state["selected_item"] = item
    scenario_state["cache"] = cache


@given("the next fresh analysis will fail")
def _next_fresh_fails(scenario_state: dict[str, Any]) -> None:
    scenario_state["_fresh_fails"] = True


@when("the user chooses to generate a fresh analysis with a supported coding agent")
def _choose_generate_fresh(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _run_generate_fresh(scenario_state, work_items, monkeypatch, tmp_path)


@when("the user chooses to generate a fresh analysis")
def _choose_generate_fresh_short(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _run_generate_fresh(scenario_state, work_items, monkeypatch, tmp_path)


def _run_generate_fresh(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Drive the fresh-analysis flow through a real backend + cache.

    The scenario patches the ``Analyzer`` boundary so no subprocess is
    spawned, but the generated markdown flows through the real
    ``AnalysisCache.save`` so the Then steps can read the cached file off
    disk to prove persistence.
    """

    from workdash.config import AgentConfig

    worktree_calls: list[WorkItem] = []
    analyze_calls: list[tuple[WorkItem, str]] = []
    opened_paths: list[str] = []
    fresh_body = "# fresh analysis\nproduction-generated body"

    cache = AnalysisCache(tmp_path / "cache")
    analyzer = MagicMock()
    if scenario_state.get("_fresh_fails"):
        analyzer.analyze.side_effect = RuntimeError("analysis failed")
    else:
        analyzer.analyze.return_value = fresh_body

    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir=str(tmp_path / "wrk"),
    )
    backend = WorkdashBackend(
        analysis_cache=cache,
        analyzer=analyzer,
        config=config,
    )

    def worktree_callback(item: WorkItem) -> str:
        worktree_calls.append(item)
        return str(tmp_path / "wt")

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str | None:
        analyze_calls.append((item, tool))
        if tool != "cached":
            backend.resolve_analyze_command_tokens(tool)
            worktree_callback(item)
        return backend.analyze_item(item, tool=tool)

    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: opened_paths.append(path))
    busy_messages: list[str] = []

    async def interactions(app, pilot) -> None:
        await _open_analyze_dialog(app, pilot)
        await pilot.press("2")
        for _ in range(40):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if opened_paths or "Analyze failed" in status:
                break
        scenario_state["analyze_status"] = app.query_one("#status-footer", Static).render().plain
        scenario_state["modal_screen_names"] = modal_screen_names(app)

    from .common import run_app

    run_app(
        work_items=list(work_items),
        worktree_callback=worktree_callback,
        analyze_callback=analyze_callback,
        interactions=interactions,
        busy_messages=busy_messages,
    )
    scenario_state["worktree_calls"] = worktree_calls
    scenario_state["analyze_calls"] = analyze_calls
    scenario_state["opened_paths"] = opened_paths
    scenario_state["busy_messages"] = busy_messages
    scenario_state["cache"] = cache
    scenario_state["fresh_body"] = fresh_body


@when("the user chooses to open the cached analysis")
def _choose_open_cached(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache: AnalysisCache = scenario_state["cache"]
    item = scenario_state["selected_item"]
    analysis_md_path = cache.build_analysis_path(item)

    opened_paths: list[str] = []
    monkeypatch.setattr("workdash.tui.open_markdown", lambda path: opened_paths.append(path))

    def analyze_callback(item_: WorkItem, tool: str = "codex") -> str | None:
        assert tool == "cached", tool
        return str(analysis_md_path)

    async def interactions(app, pilot) -> None:
        dialog = await _open_analyze_dialog(app, pilot)
        dialog.action_open_analysis()
        for _ in range(40):
            await pilot.pause()
            if opened_paths:
                break

    from .common import run_app

    run_app(
        work_items=list(work_items),
        analyze_callback=analyze_callback,
        interactions=interactions,
    )
    scenario_state["opened_paths"] = opened_paths
    scenario_state["analysis_md_path"] = str(analysis_md_path)


@then("the system prepares the work item's worktree")
def _system_prepares_worktree_analysis(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["worktree_calls"] == [scenario_state["selected_item"]]


@then("the system shows that the analysis is in progress")
def _shows_in_progress(scenario_state: dict[str, Any]) -> None:
    # Prove the TUI pushed the busy screen with an analysis label while the
    # Codex backend was still running, not only that a post-completion
    # footer string contains the word "analyz".
    busy_messages = scenario_state.get("busy_messages", [])
    assert any(message.startswith("Analyzing with ") for message in busy_messages), busy_messages


@then("the generated analysis is cached")
def _generated_cached(scenario_state: dict[str, Any]) -> None:
    # Read the analysis off disk to prove WorkdashBackend.analyze_item drove
    # AnalysisCache.save through the full pipeline.
    cache: AnalysisCache = scenario_state["cache"]
    item: WorkItem = scenario_state["selected_item"]
    analysis_path = cache.build_analysis_path(item)
    assert analysis_path.exists(), analysis_path
    assert analysis_path.read_text(encoding="utf-8") == scenario_state["fresh_body"]
    assert cache.load_analysis(item) == scenario_state["fresh_body"]


@then("the rendered analysis is opened in the user's default browser")
def _rendered_opened(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["opened_paths"], "open_markdown should have been called"


@then("the cached analysis is rendered")
def _cached_rendered(scenario_state: dict[str, Any]) -> None:
    # The launcher.open_markdown helper converts md -> html; in TUI the path
    # passed through is the .md file, so we assert the handler was invoked
    # with the cached markdown path.
    assert scenario_state["opened_paths"] == [scenario_state["analysis_md_path"]]


@then("the previously cached analysis is preserved")
def _previous_cache_preserved(scenario_state: dict[str, Any]) -> None:
    cache: AnalysisCache = scenario_state["cache"]
    item = scenario_state["selected_item"]
    assert cache.load_analysis(item) == "# cached analysis\nbody"
