"""Step definitions for the analysis domain scenarios.

Covers analyze-dialog, cache-invalidation, and generate-analysis features.
The analyze path is exercised through the TUI where relevant, and through
the WorkdashBackend + AnalysisCache layer for cache-freshness scenarios.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import Static

from workdash.analysis_cache import AnalysisCache
from workdash.analyzer import Analyzer
from workdash.backend import WorkdashBackend
from workdash.config import WorkdashConfig
from workdash.control import WorkdashSession, format_work_item_id
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
    """Drive fresh analysis through the TUI, session, analyzer and cache."""

    from workdash.config import AgentConfig

    worktree_calls: list[WorkItem] = []
    analyze_calls: list[tuple[WorkItem, str]] = []
    opened_paths: list[str] = []
    fresh_body = "# fresh analysis\nproduction-generated body"

    cache = AnalysisCache(tmp_path / "cache")
    analyzer = Analyzer()
    scenario_state["target_source"] = {"src/cards.py": "value = 42\n"}
    _install_analysis_agent(scenario_state, monkeypatch, output=fresh_body)

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
        root = tmp_path / "wt"
        for name, content in scenario_state["target_source"].items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        return str(root)

    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=list(work_items),
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(
        "workdash.control.ensure_worktree", lambda workdir, item: worktree_callback(item)
    )

    def analyze_callback(item: WorkItem, tool: str = "codex") -> str | None:
        analyze_calls.append((item, tool))
        result = session.analyze(target=format_work_item_id(item), agent=tool, prefer_cache=False)
        return result["path"]

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


def _install_analysis_agent(
    scenario_state, monkeypatch, *, output="# Analysis\nSource inspection complete."
):
    """Fake only GitHub and the external agent, inspecting its cwd and prompt."""
    previous_run = subprocess.run

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            scenario_state["discussion_command"] = command
            discussion = scenario_state.get(
                "discussion", {"title": "Issue title", "body": "Issue discussion"}
            )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(discussion), stderr="")
        if len(command) > 1 and command[1] == "-ic":
            if scenario_state.get("_fresh_fails"):
                raise subprocess.CalledProcessError(1, command, stderr="analysis failed")
            root = Path(kwargs["cwd"])
            prompt = shlex.split(command[2])[-1]
            scenario_state["agent_cwd"] = root
            scenario_state["agent_prompt"] = prompt
            scenario_state["agent_sources"] = {
                path: (root / path).read_text() for path in scenario_state["target_source"]
            }
            assert "Inspect the local source code" in prompt
            assert "file path and line number without a URL" in prompt
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")
        return previous_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


def run_source_analysis(scenario_state, monkeypatch, tmp_path):
    from .worktrees import _install_fake_git

    session = scenario_state["api_session"]
    _install_fake_git(scenario_state, monkeypatch, workdir=Path(session.config.workdir))
    _install_analysis_agent(scenario_state, monkeypatch)
    session.backend = WorkdashBackend(config=session.config, cache_root=tmp_path / "analysis-cache")
    session.analyze(target=format_work_item_id(scenario_state["todo_item"]), prefer_cache=False)
    scenario_state["worktree_path"] = str(scenario_state["agent_cwd"])


@then(
    "the analysis agent starts inside that worktree with access to the code and the work item's discussion"
)
def _fresh_agent_has_source_and_discussion(scenario_state, tmp_path):
    assert scenario_state["agent_cwd"] == tmp_path / "wt"
    assert scenario_state["agent_sources"]["src/cards.py"] == "value = 42\n"
    assert "Issue discussion" in scenario_state["agent_prompt"]


@given(parsers.parse("the selected work item's code is hosted at `{repository}`"))
def _code_hosted_at(repository, scenario_state, work_items, tmp_path):
    from .todo import _seed_todo_item

    _seed_todo_item(scenario_state, work_items, tmp_path, number=110, target=repository)


@given(parsers.parse("the code relevant to the work item is in `{path}` at line {line:d}"))
def _source_at_line(path, line, scenario_state):
    scenario_state["target_source"] = {path: "\n" * (line - 1) + "def cards(): return 42\n"}


@when("the user generates a fresh analysis")
def _generate_source_analysis(scenario_state, monkeypatch, tmp_path):
    run_source_analysis(scenario_state, monkeypatch, tmp_path)


@then(
    parsers.parse(
        "the analysis cites `{path}` at line {line:d} using a link on `{hosting_service}`"
    )
)
def _citation_on_host(path, line, hosting_service, scenario_state):
    """Verify citation guidance delivered to the agent, not model output compliance."""
    prompt = " ".join(scenario_state["agent_prompt"].split())
    target = scenario_state["todo_item"].todo_target
    assert f"The code repository is {target}" in prompt
    assert "code repository's own hosting service" in prompt
    assert "Inspect the local Git checkout to determine the default branch" in prompt
    assert "Use the actual repository and branch, not the example values" in prompt
    assert "file path and line number without a URL" in prompt
    prefix = {
        "GitHub": "https://github.com/owner/repo/blob/main/",
        "GitLab": "https://gitlab.com/owner/repo/-/blob/main/",
        "Codeberg": "https://codeberg.org/owner/repo/src/branch/main/",
    }[hosting_service]
    assert f"- {hosting_service}: [`{path}:{line}`]({prefix}{path}#L{line})" in prompt
    assert scenario_state["agent_sources"][path].splitlines()[line - 1] == "def cards(): return 42"
    pytest.skip(
        "Generated citations require a live analysis agent with working source access; "
        "prompt checks are not output evidence."
    )
