"""Step definitions for launch-coding-session scenarios."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, then, when
from textual.widgets import Static

from workdash.analysis_cache import AnalysisCache
from workdash.models import WorkItem
from workdash.tui import CodeDialog

from .common import make_work_item, modal_screen_names


async def _open_code_dialog(app, pilot) -> CodeDialog:
    await pilot.press("c")
    for _ in range(20):
        await pilot.pause()
        for screen in app.screen_stack:
            if isinstance(screen, CodeDialog):
                return screen
    raise AssertionError("CodeDialog did not appear after pressing 'c'")


@given("the next coding session launch will fail")
def _next_coding_session_launch_will_fail(scenario_state: dict[str, Any]) -> None:
    scenario_state["coding_launch_fails"] = True


@given("the coding dialog is open")
def _coding_dialog_open(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    if not work_items:
        work_items.append(make_work_item(number=60, title="Coding cancel target"))
    scenario_state["selected_item"] = work_items[0]
    scenario_state["dialog_kind"] = "coding"


def run_code_dialog_scenario(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Drive pressing 'c' in the TUI and then choosing the first configured agent.

    The ``launch_callback`` mirrors ``workdash.workdash._launch`` so the
    production prompt builder runs end-to-end. External boundaries (``gh``
    context collection, terminal/VSCode launchers) are patched so the test
    stays in-process; the assembled prompt is recorded for the Then steps.
    """

    import workdash.launcher as launcher_module

    worktree_calls: list[WorkItem] = []
    launch_calls: list[tuple[WorkItem, str]] = []
    launched_prompts: list[str] = []
    launched_tools: list[str] = []

    worktree_path = tmp_path / "wt"
    worktree_path.mkdir(parents=True, exist_ok=True)

    def worktree_callback(item: WorkItem) -> str:
        if scenario_state.get("worktree_fails"):
            raise RuntimeError("worktree failed")
        worktree_calls.append(item)
        return str(worktree_path)

    fake_github_context = {
        "number": 0,
        "title": "",
        "body": "body-from-gh: this is context the agent must see",
        "state": "OPEN",
        "url": "",
    }

    def fake_collect(item: WorkItem):
        fake_github_context["number"] = item.number
        fake_github_context["title"] = item.title
        fake_github_context["url"] = item.url
        return dict(fake_github_context)

    monkeypatch.setattr(launcher_module, "collect_launch_github_context", fake_collect)
    monkeypatch.setattr(
        launcher_module,
        "launch_agent_context",
        lambda repo_path, prompt, agent_command_tokens=None: (
            launched_prompts.append(prompt),
            launched_tools.append("codex-like"),
        ),
    )
    monkeypatch.setattr(
        launcher_module,
        "launch_vscode_context",
        lambda repo_path, prompt: (
            launched_prompts.append(prompt),
            launched_tools.append("vscode"),
        ),
    )

    cache: AnalysisCache | None = scenario_state.get("cache")

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item, tool))
        if scenario_state.get("coding_launch_fails"):
            raise RuntimeError("coding launch failed")
        analysis_path = (
            str(cache.build_analysis_path(item))
            if cache is not None and item.analysis is not None
            else None
        )
        prompt = launcher_module.prepare_launch_agent_prompt(
            item,
            str(worktree_path),
            analysis_path=analysis_path,
        )
        if tool == "vscode":
            launcher_module.launch_vscode_context(str(worktree_path), prompt)
        else:
            launcher_module.launch_agent_context(
                str(worktree_path),
                prompt,
                agent_command_tokens=[tool],
            )

    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await _open_code_dialog(app, pilot)
        await pilot.press("1")
        for _ in range(40):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if launch_calls or "Worktree setup failed" in status or "Launch failed" in status:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain
        captured["modal_screen_names"] = modal_screen_names(app)

    from .common import run_app

    run_app(
        work_items=list(work_items),
        worktree_callback=worktree_callback,
        launch_callback=launch_callback,
        interactions=interactions,
    )
    scenario_state["worktree_calls"] = worktree_calls
    scenario_state["launch_calls"] = launch_calls
    scenario_state["launched_prompts"] = launched_prompts
    scenario_state["launched_tools"] = launched_tools
    scenario_state["github_context"] = fake_github_context
    scenario_state["coding_status"] = captured["status"]
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


@when("the user picks a supported coding agent from the dialog")
def _picks_coding_agent(scenario_state: dict[str, Any]) -> None:
    # Already performed inside run_code_dialog_scenario when the user pressed 'c'.
    if scenario_state.get("worktree_fails"):
        assert "Worktree setup failed" in scenario_state["coding_status"]
    else:
        assert scenario_state["launch_calls"], "Expected the launch callback to have fired"


@then("the system prepares the worktree for the selected work item")
def _worktree_prepared_for_code(scenario_state: dict[str, Any]) -> None:
    calls = scenario_state["worktree_calls"]
    assert calls, "Expected worktree callback invocation"
    assert calls[0] == scenario_state["selected_item"]


@then("a coding session with the chosen agent opens inside that worktree")
def _coding_session_opens(scenario_state: dict[str, Any]) -> None:
    calls = scenario_state["launch_calls"]
    assert calls, "Expected the launch callback to have been invoked"
    item, tool = calls[0]
    assert item == scenario_state["selected_item"]
    assert tool == "claude"


@then("the agent is preloaded with the work item's GitHub context")
def _agent_preloaded_context(scenario_state: dict[str, Any]) -> None:
    # Assert on the prompt the TUI's launch path actually constructed and
    # passed to the production terminal/VSCode launchers, not on a prompt
    # we independently recompute here.
    item: WorkItem = scenario_state["selected_item"]
    launched_prompts = scenario_state["launched_prompts"]
    assert launched_prompts, "Expected launch_agent_context/launch_vscode_context to be invoked"
    prompt = launched_prompts[0]
    assert item.repo in prompt
    assert str(item.number) in prompt
    assert item.url in prompt
    assert scenario_state["github_context"]["body"] in prompt


@then("the TUI reports that the session was launched")
def _tui_reports_session_launched(
    scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    # Rerun the flow just to observe the status footer directly — the
    # previous helper discarded the app before we could inspect it.
    worktree_calls: list[WorkItem] = []
    launch_calls: list[tuple[WorkItem, str]] = []
    captured: dict[str, Any] = {}

    def worktree_callback(item: WorkItem) -> str:
        worktree_calls.append(item)
        return "/tmp/wt"

    def launch_callback(item: WorkItem, tool: str = "codex") -> None:
        launch_calls.append((item, tool))

    async def interactions(app, pilot) -> None:
        await _open_code_dialog(app, pilot)
        await pilot.press("1")
        for _ in range(40):
            await pilot.pause()
            if launch_calls:
                break
        captured["status"] = app.query_one("#status-footer", Static).render().plain

    from .common import run_app

    run_app(
        work_items=list(work_items),
        worktree_callback=worktree_callback,
        launch_callback=launch_callback,
        interactions=interactions,
    )
    assert "Launched" in captured["status"], captured


@when("the user launches a coding session with a supported coding agent")
def _launches_coding_session_with_cache(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_code_dialog_scenario(scenario_state, work_items, monkeypatch, tmp_path)


@then("the agent is preloaded with the cached analysis alongside the GitHub context")
def _preloaded_with_cache(scenario_state: dict[str, Any]) -> None:
    # Assert on the prompt the TUI's launch path actually built so the
    # analysis_path branch in workdash._launch (AnalysisCache.build_analysis_path)
    # is exercised end-to-end rather than recomputed locally.
    item: WorkItem = scenario_state["selected_item"]
    cache: AnalysisCache = scenario_state["cache"]
    expected_analysis_path = cache.build_analysis_path(item)
    launched_prompts = scenario_state["launched_prompts"]
    assert launched_prompts, "Expected the launch path to produce a prompt"
    prompt = launched_prompts[0]
    assert "PREVIOUS ANALYSIS:" in prompt
    assert str(expected_analysis_path) in prompt
    assert item.url in prompt
    assert scenario_state["github_context"]["body"] in prompt


@then("the system reports the coding launch error details to the user")
def _reports_coding_launch_error_details(scenario_state: dict[str, Any]) -> None:
    assert "Launch failed: coding launch failed" in scenario_state["coding_status"]


@then("no coding session is launched")
def _no_coding_session(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["launch_calls"] == []


@then("no worktree is prepared")
def _no_worktree_prepared(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["worktree_calls"] == []
