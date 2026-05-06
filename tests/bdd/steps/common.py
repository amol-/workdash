"""Shared fixtures and step definitions used across multiple BDD domains.

This module concentrates steps whose wording recurs across feature files
(e.g. "When the user opens the dashboard", "Given the TUI has a work item
selected") so there is exactly one binding per unique phrase, avoiding
`pytest_bdd` duplicate-step errors.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when

from workdash.backend import IncludeResult, compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType

# -- Datetime helpers -------------------------------------------------------

NOW_UTC = datetime(2026, 4, 29, 12, 0, 0, tzinfo=UTC)


def make_work_item(
    *,
    item_type: WorkItemType = WorkItemType.ISSUE,
    kind: WorkItemKind = WorkItemKind.TRACKED_ISSUE,
    repo: str = "owner/repo",
    number: int = 1,
    title: str = "Test item",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
    url: str | None = None,
) -> WorkItem:
    """Build a ``WorkItem`` with sensible defaults for BDD scenarios."""

    created = created_at or (NOW_UTC - timedelta(days=10))
    updated = updated_at or created
    return WorkItem(
        kind=kind,
        item_type=item_type,
        repo=repo,
        number=number,
        title=title,
        created_at=created,
        updated_at=updated,
        url=url or f"https://github.com/{repo}/{item_type.value}/{number}",
    )


# -- Shared fixtures --------------------------------------------------------


@pytest.fixture
def scenario_state() -> dict[str, Any]:
    """Generic bucket for per-scenario state across step functions."""

    return {}


@pytest.fixture
def work_items() -> list[WorkItem]:
    return []


@pytest.fixture
def valid_config() -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        repositories=("owner/repo",),
        workdir="/tmp/workdash-bdd",
    )


# -- Reusable TUI harness ---------------------------------------------------


def run_app(
    *,
    work_items: list[WorkItem],
    interactions,
    suggestion_markers=None,
    now_utc: datetime = NOW_UTC,
    open_callback=None,
    refresh_callback=None,
    analyze_callback=None,
    launch_callback=None,
    worktree_callback=None,
    terminal_callback=None,
    include_callback=None,
    busy_messages: list[str] | None = None,
) -> None:
    """Drive ``WorkdashApp`` through an async pilot routine for BDD tests.

    :param list[str] | None busy_messages: If supplied, every ``message``
        string passed to ``WorkdashApp._run_with_busy_screen`` during this
        run is appended to it. Lets step definitions assert that the busy
        screen was pushed with the production-expected label without
        racing against its transient pop.
    """

    from workdash.tui import WorkdashApp

    markers = (
        dict(suggestion_markers)
        if suggestion_markers is not None
        else compute_suggestion_markers(list(work_items))
    )
    app = WorkdashApp(
        work_items=list(work_items),
        suggestion_markers=markers,
        open_callback=open_callback,
        refresh_callback=refresh_callback,
        analyze_callback=analyze_callback,
        launch_callback=launch_callback,
        worktree_callback=worktree_callback,
        terminal_callback=terminal_callback,
        include_callback=include_callback,
        now_utc=now_utc,
    )
    if busy_messages is not None:
        original_run_with_busy_screen = WorkdashApp._run_with_busy_screen

        async def _recording_run_with_busy_screen(self, *, message, callback):
            busy_messages.append(message)
            return await original_run_with_busy_screen(self, message=message, callback=callback)

        app._run_with_busy_screen = _recording_run_with_busy_screen.__get__(app)

    async def _driver() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await interactions(app, pilot)
            await pilot.press("q")

    asyncio.run(_driver())


# -- Generic dashboard open steps -------------------------------------------


@when("the user opens the dashboard")
def _open_dashboard(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capture the items/markers the dashboard would see on startup.

    Some scenarios (e.g. the review-filter case) set ``_subprocess_patch`` +
    ``_github_client`` so we actually run the real github_client and filter
    out the team-only review requests. Include-store scenarios wire an
    ``_open_dashboard_hook`` that drives the real ``WorkdashBackend``
    through a patched ``gh`` subprocess boundary. Others simply compute
    suggestion markers over the pre-populated ``work_items`` list.
    """

    hook = scenario_state.get("_open_dashboard_hook")
    if hook is not None:
        hook(scenario_state, work_items, monkeypatch)
        return
    if "_subprocess_patch" in scenario_state:
        import workdash.github_client as gc

        monkeypatch.setattr(gc.subprocess, "run", scenario_state["_subprocess_patch"])
        scenario_state["reviewed_prs"] = scenario_state[
            "_github_client"
        ].list_open_review_requested_prs("testuser")
    scenario_state["work_items"] = list(work_items)
    scenario_state["suggestion_markers"] = compute_suggestion_markers(list(work_items))


# -- Generic selection state for TUI keybinding scenarios --------------------


@given("the TUI has a work item selected")
def _tui_has_work_item_selected(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    if not work_items:
        work_items.append(
            make_work_item(
                item_type=WorkItemType.ISSUE,
                kind=WorkItemKind.ASSIGNED_ISSUE,
                number=42,
                title="Picked item",
                updated_at=NOW_UTC - timedelta(days=2),
                created_at=NOW_UTC - timedelta(days=5),
                url="https://github.com/owner/repo/issues/42",
            )
        )
    scenario_state.setdefault("selected_item", work_items[0])


# -- Exit-code assertions ----------------------------------------------------


@then("the system exits with a zero status")
def _system_exits_zero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state


@then("the system exits with a non-zero status")
def _system_exits_nonzero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0, scenario_state


# -- Auxiliary helpers shared across domains --------------------------------


def install_valid_env(monkeypatch: pytest.MonkeyPatch, *, which_succeeds: bool = True) -> None:
    """Patch the workdash module so preflight passes (or fails on gh)."""

    import workdash.workdash as workdash_module

    monkeypatch.setattr(
        workdash_module.shutil,
        "which",
        lambda cmd: "/usr/bin/gh" if which_succeeds and cmd == "gh" else None,
    )


def mock_backend(monkeypatch: pytest.MonkeyPatch, *, items: list[WorkItem]) -> None:
    """Swap ``WorkdashBackend`` with a fake that returns ``items``."""

    import workdash.workdash as workdash_module

    markers = compute_suggestion_markers(items)

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.config = config

        def load_items(self, progress_callback=None):
            if progress_callback is not None:
                progress_callback("loading...")
            return list(items), dict(markers)

        def analyze_item(self, _item, tool="codex"):
            return None

        def include_item_by_url(self, _url, _existing_identities):
            return IncludeResult(invalid=True)

    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)


def install_config(monkeypatch: pytest.MonkeyPatch, config: WorkdashConfig) -> None:
    import workdash.workdash as workdash_module

    monkeypatch.setattr(workdash_module, "load_config", lambda: config)


def ensure_workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "wrk"
    workdir.mkdir(exist_ok=True)
    return workdir
