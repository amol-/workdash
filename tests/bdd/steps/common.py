"""Shared fixtures and step definitions used across multiple BDD domains.

This module concentrates steps whose wording recurs across feature files
(e.g. "Given the TUI has a work item selected") so there is exactly one binding
per unique phrase, avoiding `pytest_bdd` duplicate-step errors.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, then
from textual.screen import ModalScreen

import workdash.control as control_module
import workdash.github_client as github_client_module
from pane_launch_fake import install_pane_launch
from workdash.backend import compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession, format_work_item_id
from workdash.github_client import GitHubClient
from workdash.included_items import IncludedItemsStore
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
    ci_state: str | None = None,
    review_decision: str | None = None,
    review_requested: bool = False,
    has_unresolved_comments: bool = False,
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
        ci_state=ci_state,
        review_decision=review_decision,
        review_requested=review_requested,
        has_unresolved_comments=has_unresolved_comments,
    )


# -- Shared fixtures --------------------------------------------------------


@pytest.fixture
def scenario_state() -> dict[str, Any]:
    """Generic bucket for per-scenario state across step functions."""

    return {}


@pytest.fixture
def work_items() -> list[WorkItem]:
    return []


def make_valid_config(agents: dict[str, AgentConfig] | None = None) -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        open_command="xdg-open",
        analysis_viewer={
            "command": "workdash view",
            "format": "markdown",
            "needs_pane": True,
        },
        agents=agents
        or {
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "pi": AgentConfig(launch="pi"),
        },
        repositories=("owner/repo",),
        workdir="/tmp/workdash-bdd",
        todo_repository="testuser/todos",
    ).require_valid()


@pytest.fixture
def valid_config() -> WorkdashConfig:
    return make_valid_config()


def _configure_agents(scenario_state: dict[str, Any], agents: dict[str, AgentConfig]) -> None:
    scenario_state["agents"] = agents
    session = scenario_state.get("api_session")
    if session is not None:
        session.config = replace(session.config, agents=agents)


# -- Reusable TUI harness ---------------------------------------------------


def modal_screen_names(app) -> list[str]:
    return [
        screen.__class__.__name__ for screen in app.screen_stack if isinstance(screen, ModalScreen)
    ]


def run_app(
    *,
    work_items: list[WorkItem],
    interactions,
    suggestion_markers=None,
    now_utc: datetime = NOW_UTC,
    open_callback=None,
    open_url_callback=None,
    open_markdown_callback=None,
    refresh_callback=None,
    launch_callback=None,
    review_callback=None,
    worktree_callback=None,
    terminal_callback=None,
    include_callback=None,
    todo_callback=None,
    session: WorkdashSession | None = None,
    config: WorkdashConfig | None = None,
    focus_callback=None,
) -> None:
    """Drive ``WorkdashApp`` through an async pilot routine for BDD tests."""

    from workdash.tui import WorkdashApp

    markers = (
        dict(suggestion_markers)
        if suggestion_markers is not None
        else compute_suggestion_markers(list(work_items))
    )
    config = config or make_valid_config()
    app = WorkdashApp(
        work_items=list(work_items),
        suggestion_markers=markers,
        open_callback=open_callback,
        open_url_callback=open_url_callback,
        open_markdown_callback=open_markdown_callback,
        refresh_callback=refresh_callback,
        launch_callback=launch_callback,
        review_callback=review_callback,
        worktree_callback=worktree_callback,
        analyze_choices=config.tui_analyze_choices(),
        code_choices=config.tui_code_choices(),
        terminal_callback=terminal_callback,
        include_callback=include_callback,
        todo_callback=todo_callback,
        session=session,
        now_utc=now_utc,
        focus_callback=focus_callback,
    )

    async def _driver() -> None:
        async with app.run_test() as pilot:
            await pilot.pause()
            await interactions(app, pilot)
            await pilot.press("q")

    asyncio.run(_driver())


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


@given("the Workdash server is not attached to a Zellij session")
def _server_without_zellij_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["no_zellij_session"] = True


@given("the system is running inside a Zellij session")
def _system_running_inside_zellij(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZELLIJ", "0")


@given("no server-backed Workdash session is running")
def _no_server_backed_workdash_session(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import workdash.control as control_module

    scenario_state.pop("api_session", None)

    def fail_urlopen(request, timeout=None):
        scenario_state.setdefault("urlopen_requests", []).append(request)
        raise ConnectionRefusedError("connection refused")

    monkeypatch.setattr(control_module.urllib.request, "urlopen", fail_urlopen)


# -- Shared server-backed session setup --------------------------------------


def api_config(tmp_path: Path) -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        open_command="xdg-open",
        analysis_viewer={
            "command": "workdash view",
            "format": "markdown",
            "needs_pane": True,
        },
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(launch="pi"),
        },
        repositories=("owner/repo",),
        workdir=str(tmp_path / "wrk"),
        todo_repository="testuser/todos",
    ).require_valid()


def set_session_items(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    scenario_state["work_items"] = list(work_items)
    markers = compute_suggestion_markers(list(work_items))
    scenario_state["suggestion_markers"] = markers
    session = scenario_state.get("api_session")
    if session is not None:
        session.work_items = list(work_items)
        session.suggestion_markers = dict(markers)


def seed_dashboard_item(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> WorkItem:
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        number=1,
        title="Fix the issue",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    work_items[:] = [item]
    set_session_items(scenario_state, work_items)
    return item


def ensure_api_session(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> WorkdashSession:
    session = scenario_state.get("api_session")
    if session is not None:
        return session
    if not work_items:
        seed_dashboard_item(work_items, scenario_state)
    backend = FakeApiBackend(scenario_state, tmp_path)
    session = WorkdashSession(
        config=api_config(tmp_path),
        backend=backend,  # type: ignore[arg-type]
        work_items=list(work_items),
        suggestion_markers=compute_suggestion_markers(list(work_items)),
        zellij_session=scenario_state.get("zellij_session", "workdash-main"),
    )
    scenario_state["api_session"] = session
    scenario_state["api_backend"] = backend
    return session


class FakeApiBackend:
    def __init__(self, scenario_state: dict[str, Any], tmp_path: Path) -> None:
        self._state = scenario_state
        self.github_client = GitHubClient()
        self.included_items_store = IncludedItemsStore(tmp_path / "included.json")
        self.analysis_cache = SimpleNamespace(
            build_analysis_path=lambda _item: tmp_path / "cached-analysis.md",
            load_analysis=lambda _item: None,
            load_analysis_date=lambda _item: None,
        )

    def load_items(self, progress_callback=None):
        assert progress_callback is None
        items = list(self._state.get("refreshed_items", self._state.get("work_items", [])))
        self._state["work_items"] = items
        return items, compute_suggestion_markers(items)

    def analyze_item(
        self, item: WorkItem, tool: str = "codex", *, cwd=None, zellij_session=None, group=None
    ) -> str | None:
        self._state.setdefault("analyze_calls", []).append((format_work_item_id(item), tool))
        if tool == "cached":
            return None
        if "analysis_path" not in self._state:
            raise AssertionError("scenario must set analysis_path from tmp_path")
        analysis_path = Path(self._state["analysis_path"])
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text("analysis body\n", encoding="utf-8")
        return str(analysis_path)


def install_pane(scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """Run each pane command in its cwd, as Zellij does, recording the launch."""

    scenario_state["pane_launches"] = install_pane_launch(monkeypatch)


def install_zellij_agent_fakes(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake the Zellij boundary of coding agent launches with ``scenario_state["panes"]``.

    Each launch is recorded in ``agent_launches`` and adds its live agent pane,
    unless the scenario makes Zellij unable to list panes or launched panes invisible.
    """

    panes = scenario_state.setdefault("panes", [])

    def load_panes(_session: str) -> list[dict[str, object]]:
        if scenario_state.get("zellij_unavailable"):
            raise RuntimeError("Failed to list Zellij panes for workdash-main: session not found")
        return [dict(pane) for pane in panes]

    def launch_agent(
        repo_path, prompt, agent_command_tokens=None, *, zellij_session=None, group=None
    ):
        launches = scenario_state.setdefault("agent_launches", [])
        launches.append((repo_path, prompt, agent_command_tokens, zellij_session))
        pane_id = 22 + len(launches)
        pane_title = f"code_{Path(repo_path).name}"
        if not scenario_state.get("launched_panes_invisible"):
            panes.append(
                {"id": pane_id, "title": pane_title, "pane_cwd": repo_path, "tab_name": group}
            )
        return SimpleNamespace(
            session=zellij_session,
            pane_id=f"terminal_{pane_id}",
            pane_title=pane_title,
            cwd=repo_path,
        )

    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", load_panes)
    monkeypatch.setattr(control_module.agent_execution.executor, "launch", launch_agent)
    monkeypatch.setattr(control_module, "_LAUNCH_VISIBILITY_INTERVAL_SECONDS", 0)


def github_item_node(
    item_type: WorkItemType, repo: str, number: int, *, body: str = "", closes: tuple[int, ...] = ()
) -> dict[str, object]:
    """Build the GraphQL node GitHub returns for one issue or pull request."""

    node: dict[str, object] = {
        "__typename": "Issue" if item_type == WorkItemType.ISSUE else "PullRequest",
        "title": f"GitHub {item_type.value} {repo}#{number}",
        "url": f"https://github.com/{repo}/"
        f"{'issues' if item_type == WorkItemType.ISSUE else 'pull'}/{number}",
        "createdAt": "2020-01-01T00:00:00Z",
        "updatedAt": "2020-01-02T00:00:00Z",
        "author": {"login": "someone-else"},
    }
    if item_type == WorkItemType.ISSUE:
        node["body"] = body
        node["subIssuesSummary"] = {"total": 0}
    else:
        node["closingIssuesReferences"] = {
            "nodes": [{"number": issue, "repository": {"nameWithOwner": repo}} for issue in closes]
        }
    return node


def install_github_item_lookup(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fake GitHub's single item lookup at the ``gh`` boundary.

    ``github_items`` maps ``owner/repo#N`` to a GraphQL node; any other item gets
    the ``github_answer`` default: no item, an issue, a pull request, or an outage.
    Other GitHub queries fail the scenario; other commands keep reaching whatever
    ``subprocess.run`` was installed before.
    """

    previous_run = subprocess.run

    def fake_run(command, *args, **kwargs):
        if command[:3] != ["gh", "api", "graphql"]:
            return previous_run(command, *args, **kwargs)
        match = re.search(
            r'repository\(owner: "([^"]+)", name: "([^"]+)"\) '
            r"\{ nameWithOwner issueOrPullRequest\(number: (\d+)\)",
            command[-1],
        )
        tree = re.search(
            r'query ReadyIssueTree \{ repository\(owner: "([^"]+)", name: "([^"]+)"\) '
            r"\{ issue\(number: (\d+)\)",
            command[-1],
        )
        if tree is not None:
            # An autostarted code request reads the work state of the issue it codes on.
            issue = {
                "number": int(tree[3]),
                "title": "Fix the issue",
                "url": f"https://github.com/{tree[1]}/{tree[2]}/issues/{tree[3]}",
                "createdAt": "2020-01-01T00:00:00Z",
                "updatedAt": "2020-01-02T00:00:00Z",
                "state": "OPEN",
                "body": "",
                "repository": {"nameWithOwner": f"{tree[1]}/{tree[2]}"},
                "subIssuesSummary": {"total": 0},
                "subIssues": {"nodes": []},
                "issueDependenciesSummary": {"blockedBy": 0},
                "closedByPullRequestsReferences": {"nodes": []},
            }
            payload = {"data": {"repository": {"issue": issue}}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if re.search(r"\{ issue\(number: \d+\) \{ closedByPullRequestsReferences", command[-1]):
            connection = {"nodes": [], "pageInfo": {"hasNextPage": False}}
            issue = {"closedByPullRequestsReferences": connection}
            payload = {"data": {"repository": {"issue": issue}}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if match is None:
            raise AssertionError(f"Unexpected GitHub query: {command}")
        repo, number = f"{match[1]}/{match[2]}", int(match[3])
        scenario_state.setdefault("github_lookups", []).append(f"{repo}#{number}")
        answer = scenario_state.get("github_answer", "no item")
        node = scenario_state.get("github_items", {}).get(f"{repo}#{number}")
        if node is None and answer == "an outage":
            raise subprocess.CalledProcessError(
                1,
                command,
                output="",
                stderr="HTTP 502: Bad Gateway (https://api.github.com/graphql)",
            )
        if node is None and answer in {"an issue", "a pull request"}:
            item_type = WorkItemType.ISSUE if answer == "an issue" else WorkItemType.PR
            node = github_item_node(item_type, repo, number)
        if node is None:
            message = f"Could not resolve to an issue or pull request with the number of {number}."
            payload = {
                "data": {"repository": {"nameWithOwner": repo, "issueOrPullRequest": None}},
                "errors": [{"type": "NOT_FOUND", "message": message}],
            }
            raise subprocess.CalledProcessError(
                1, command, output=json.dumps(payload), stderr=f"GraphQL: {message}"
            )
        payload = {"data": {"repository": {"nameWithOwner": repo, "issueOrPullRequest": node}}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(github_client_module, "_TRANSIENT_RETRY_DELAY_SECONDS", 0)
    scenario_state["github_lookup_installed"] = True


@given("a server-backed Workdash session has loaded dashboard items")
def _server_session_has_loaded_dashboard_items(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_dashboard_item(work_items, scenario_state)
    ensure_api_session(scenario_state, work_items, tmp_path)
    install_github_item_lookup(scenario_state, monkeypatch)


@given("a server-backed Workdash session is running")
def _server_session_running(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    seed_dashboard_item(work_items, scenario_state)
    ensure_api_session(scenario_state, work_items, tmp_path)


# -- Exit-code assertions ----------------------------------------------------


@then("the system exits with a zero status")
def _system_exits_zero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state


@then("the system exits with a non-zero status")
def _system_exits_nonzero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0, scenario_state


@then("the command reports that `workdash` must be running")
def _reports_server_must_be_running(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("urlopen_requests"), scenario_state
    assert (
        "No Workdash server is reachable at 127.0.0.1:8765. Start one with `workdash`."
    ) in scenario_state["stderr"]


# -- Auxiliary helpers shared across domains --------------------------------


def ensure_workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "wrk"
    workdir.mkdir(exist_ok=True)
    return workdir
