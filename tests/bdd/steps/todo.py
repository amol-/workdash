"""Step definitions for todo capture and targeted todo work actions.

Real behavior under test lives in ``workdash.todo``, ``workdash.control``,
``workdash.github_client`` and ``workdash.repo_worktree``; only the ``gh``
and ``git`` subprocess boundaries are faked.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import DataTable, Input, Static

from gh_todo_fake import (
    LABEL_EXISTS_STDERR,
    LABEL_REPOSITORY_MISSING_STDERR,
    TODO_ISSUE_NUMBER,
    TODO_ISSUE_URL,
    TODO_REPOSITORY,
    fake_gh_todo_run,
    gh_todo_failure,
)
from workdash.backend import WorkdashBackend
from workdash.config import load_config
from workdash.control import format_work_item_id
from workdash.git import GitHelper
from workdash.github_client import GitHubClient
from workdash.included_items import IncludedItemsStore
from workdash.models import WorkItem, WorkItemKind, WorkItemType, display_repo
from workdash.repo_worktree import ensure_worktree
from workdash.todo import TODO_LABEL, todo_target_from_body
from workdash.tui import TodoDialog

from .api import _api_result, _call_api
from .common import NOW_UTC, api_config, ensure_api_session, make_work_item, run_app
from .triage import type_column_label
from .worktrees import _install_fake_git

TODO_TEXT = "Fix the flaky test"


def _install_gh_todo_fake(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    label_stderr: str | None = LABEL_EXISTS_STDERR,
) -> list[list[str]]:
    """Fake the ``gh`` boundary used to create todos and record every command.

    The default world is the steady state after the first capture, where the
    todo label is already there and gh refuses to create it again.

    :param str | None label_stderr: Error gh reports for ``gh label create``,
        or ``None`` when the label is created successfully.
    """

    import workdash.todo as todo_module

    commands: list[list[str]] = []
    failures: list[list[str]] = []
    run_gh = fake_gh_todo_run(
        commands,
        label_error=gh_todo_failure(label_stderr) if label_stderr is not None else None,
    )

    def fake_run(command, **kwargs):
        """Run the shared fake, remembering which gh commands gh rejected."""

        try:
            return run_gh(command, **kwargs)
        except subprocess.CalledProcessError:
            failures.append(list(command))
            raise

    monkeypatch.setattr(todo_module.subprocess, "run", fake_run)
    scenario_state["todo_gh_commands"] = commands
    scenario_state["todo_gh_failures"] = failures
    return commands


def _gh_commands(scenario_state: dict[str, Any]) -> list[list[str]]:
    return scenario_state["todo_gh_commands"]


def _gh_command(scenario_state: dict[str, Any], prefix: list[str]) -> list[str]:
    command = next(
        (cmd for cmd in _gh_commands(scenario_state) if cmd[: len(prefix)] == prefix), None
    )
    assert command is not None, _gh_commands(scenario_state)
    return command


def _created_issue_command(scenario_state: dict[str, Any]) -> list[str]:
    return _gh_command(scenario_state, ["gh", "issue", "create"])


def _flag_value(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def _listed_todo(scenario_state: dict[str, Any], number: int) -> dict[str, Any]:
    items = scenario_state["api_session"].list_items()["items"]
    match = next((item for item in items if item["number"] == number), None)
    assert match is not None, items
    return match


def _capture_todo(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    *,
    text: str,
    target: str | None,
    body: str | None = None,
) -> None:
    """Capture a todo through the shared session and record success or failure."""

    session = ensure_api_session(scenario_state, work_items, tmp_path)
    scenario_state["items_before_todo"] = list(session.work_items)
    try:
        scenario_state["todo_result"] = session.todo(text=text, target=target, body=body)
        scenario_state["todo_error"] = None
    except RuntimeError as error:
        scenario_state["todo_result"] = None
        scenario_state["todo_error"] = error


def _seed_todo_item(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    *,
    number: int,
    target: str | None,
) -> None:
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        repo=TODO_REPOSITORY,
        number=number,
        title=TODO_TEXT,
        url=f"https://github.com/{TODO_REPOSITORY}/issues/{number}",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    item.todo_target = target
    work_items[:] = [item]
    scenario_state["todo_item"] = item
    scenario_state["work_items"] = list(work_items)
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    session.work_items = list(work_items)


def _static_text(widget: Static) -> str:
    renderable = widget.render()
    return getattr(renderable, "plain", str(renderable))


# ----- F-TODO-CREATE -----


@given("the user has a configured todo repository")
def _user_has_configured_todo_repository(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert api_config(tmp_path).todo_repository == TODO_REPOSITORY
    ensure_api_session(scenario_state, work_items, tmp_path)
    _install_gh_todo_fake(scenario_state, monkeypatch)


@given(parsers.parse("the todo repository has no `{label}` label"))
def _todo_repository_has_no_label(
    label: str, scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert label == TODO_LABEL
    _install_gh_todo_fake(scenario_state, monkeypatch, label_stderr=None)


@given("the configured todo repository does not exist on GitHub")
def _todo_repository_does_not_exist(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_api_session(scenario_state, work_items, tmp_path)
    _install_gh_todo_fake(scenario_state, monkeypatch, label_stderr=LABEL_REPOSITORY_MISSING_STDERR)


@given(parsers.parse("the user captured a todo with target `{target}` in an earlier session"))
def _todo_captured_in_earlier_session(
    target: str,
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import workdash.github_client as github_client_module

    monkeypatch.setattr(
        GitHubClient, "list_open_authored_prs", lambda self, login, progress_callback=None: []
    )
    monkeypatch.setattr(
        GitHubClient,
        "list_open_review_requested_prs",
        lambda self, login, progress_callback=None: [],
    )
    monkeypatch.setattr(GitHubClient, "list_open_reviewed_prs", lambda self, login: [])
    monkeypatch.setattr(GitHubClient, "list_open_assigned_issues", lambda self, login: [])
    monkeypatch.setattr(
        GitHubClient,
        "list_recent_tracked_items",
        lambda self, repositories, progress_callback=None: [],
    )
    stored_issues = [
        {
            "id": "I110",
            "number": TODO_ISSUE_NUMBER,
            "title": TODO_TEXT,
            "url": TODO_ISSUE_URL,
            "createdAt": "2026-04-01T00:00:00Z",
            "updatedAt": "2026-04-02T00:00:00Z",
            "body": scenario_state.get(
                "captured_todo_body",
                f'```json\n{{"workdash_todo_version": 1, "target": "{target}"}}\n```\n',
            ),
        },
        {
            "id": "I111",
            "number": 111,
            "title": "Buy milk",
            "url": f"https://github.com/{TODO_REPOSITORY}/issues/111",
            "createdAt": "2026-04-03T00:00:00Z",
            "updatedAt": "2026-04-03T00:00:00Z",
            "body": '```json\n{"workdash_todo_version": 1}\n```\n',
        },
    ]

    def fake_run(command, **kwargs):
        assert command[:3] == ["gh", "issue", "list"], command
        scenario_state["todo_list_command"] = list(command)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(stored_issues), stderr="")

    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)
    # Keyed by issue number: the list order is owned by the most-recently-updated
    # rule, not by this scenario.
    scenario_state["expected_todo_targets"] = {TODO_ISSUE_NUMBER: target, 111: None}


@when("the user asks the TUI to capture a todo")
def _user_asks_tui_to_capture_todo(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await pilot.press("w")
        dialog = None
        for _ in range(20):
            await pilot.pause()
            dialog = next(
                (screen for screen in app.screen_stack if isinstance(screen, TodoDialog)), None
            )
            if dialog is not None:
                break
        assert dialog is not None, "TodoDialog did not appear after pressing 'w'"
        captured["labels"] = [_static_text(widget) for widget in dialog.query(Static)]
        captured["input_ids"] = [widget.id for widget in dialog.query(Input)]
        captured["target_value"] = dialog.query_one("#todo-target", Input).value
        dialog.query_one("#todo-text", Input).value = TODO_TEXT
        await pilot.press("enter")
        for _ in range(60):
            await pilot.pause()
            if scenario_state["todo_gh_commands"] and not any(
                isinstance(screen, TodoDialog) for screen in app.screen_stack
            ):
                break
        table = app.query_one("#work-items", DataTable)
        captured["rows"] = [
            [str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)
        ]

    run_app(
        work_items=list(session.work_items),
        session=session,
        todo_callback=lambda text, target: session.todo(text=text, target=target),
        interactions=interactions,
    )
    scenario_state["todo_dialog"] = captured
    scenario_state["todo_rows"] = captured["rows"]


@when("the user captures a todo")
def _user_captures_todo(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _capture_todo(scenario_state, work_items, tmp_path, text=TODO_TEXT, target=None)


@when("the user captures a todo with empty text")
def _user_captures_todo_with_empty_text(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _capture_todo(scenario_state, work_items, tmp_path, text="   ", target=None)


@when(parsers.parse("the user captures a todo with target `{target}`"))
def _user_captures_todo_with_target(
    target: str, scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _capture_todo(scenario_state, work_items, tmp_path, text=TODO_TEXT, target=target)


@when("the system refreshes dashboard items")
def _system_refreshes_dashboard_items(scenario_state: dict[str, Any], tmp_path: Path) -> None:
    backend = WorkdashBackend(
        cache_root=tmp_path / "cache",
        config=api_config(tmp_path),
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )
    items, _markers = backend.load_items()
    scenario_state["refreshed_todo_items"] = items


@then("the system asks for the todo text and an optional target repository")
def _system_asks_for_text_and_optional_target(scenario_state: dict[str, Any]) -> None:
    dialog = scenario_state["todo_dialog"]
    assert dialog["input_ids"] == ["todo-text", "todo-target"], dialog
    assert any("optional" in label.lower() for label in dialog["labels"]), dialog
    assert dialog["target_value"] == ""


@then("submitting text with an empty target creates an open issue in the todo repository")
@then("the system creates the issue in the todo repository")
@then("the server creates the todo issue in the configured todo repository")
def _creates_issue_in_todo_repository(scenario_state: dict[str, Any]) -> None:
    command = _created_issue_command(scenario_state)
    assert _flag_value(command, "--repo") == TODO_REPOSITORY, command


@then("that issue is titled with the todo text")
def _issue_titled_with_todo_text(scenario_state: dict[str, Any]) -> None:
    command = _created_issue_command(scenario_state)
    assert _flag_value(command, "--title") == TODO_TEXT
    assert _flag_value(command, "--body") == ('```json\n{\n  "workdash_todo_version": 1\n}\n```\n')


@then("that issue is assigned to the user")
def _issue_assigned_to_user(scenario_state: dict[str, Any]) -> None:
    assert _flag_value(_created_issue_command(scenario_state), "--assignee") == "@me"


@then(parsers.parse("that issue is labeled `{label}`"))
@then(parsers.parse("the created issue is labeled `{label}`"))
def _issue_labeled(label: str, scenario_state: dict[str, Any]) -> None:
    assert _flag_value(_created_issue_command(scenario_state), "--label") == label


@then("the new todo appears in the dashboard as an item of the todo repository")
def _new_todo_appears_as_item_of_todo_repository(scenario_state: dict[str, Any]) -> None:
    rows = scenario_state["todo_rows"]
    row = next((entry for entry in rows if entry[1] == TODO_REPOSITORY), None)
    assert row is not None, rows
    assert type_column_label(row[0]) == f"ISSUE#{TODO_ISSUE_NUMBER}", row
    assert TODO_TEXT in row[2], row
    assert (
        _listed_todo(scenario_state, TODO_ISSUE_NUMBER)["id"]
        == f"{TODO_REPOSITORY}#ISSUE-{TODO_ISSUE_NUMBER}"
    )


@then(parsers.parse("the issue body metadata records `{target}` as the todo target"))
def _issue_body_records_target(target: str, scenario_state: dict[str, Any]) -> None:
    body = _flag_value(_created_issue_command(scenario_state), "--body")
    assert todo_target_from_body(body) == target, body


@then(parsers.parse("the new todo appears in the dashboard as an item of `{target}`"))
def _new_todo_appears_as_item_of_target(target: str, scenario_state: dict[str, Any]) -> None:
    listed = _listed_todo(scenario_state, TODO_ISSUE_NUMBER)
    assert listed["repo"] == target, listed
    assert listed["id"] == f"{target}#ISSUE-WT{TODO_ISSUE_NUMBER}", listed


@then(parsers.parse("the system creates the `{label}` label in the todo repository"))
def _system_creates_label(label: str, scenario_state: dict[str, Any]) -> None:
    command = _gh_command(scenario_state, ["gh", "label", "create"])
    assert command[3] == label, command
    assert _flag_value(command, "--repo") == TODO_REPOSITORY, command
    # First use means gh really creates the label; in the steady state the same
    # command fails with "already exists" and is only tolerated.
    assert command not in scenario_state["todo_gh_failures"], scenario_state["todo_gh_failures"]


@then("the system reports the GitHub failure")
def _system_reports_github_failure(scenario_state: dict[str, Any]) -> None:
    error = scenario_state["todo_error"]
    assert error is not None, scenario_state
    assert LABEL_REPOSITORY_MISSING_STDERR in str(error), error


@then("the system tells the user to create the todo repository")
def _system_tells_user_to_create_todo_repository(scenario_state: dict[str, Any]) -> None:
    assert f"Create the todo repository {TODO_REPOSITORY}" in str(scenario_state["todo_error"])


@then("no dashboard item is added")
def _no_dashboard_item_is_added(scenario_state: dict[str, Any]) -> None:
    session = scenario_state["api_session"]
    assert session.work_items == scenario_state["items_before_todo"], session.work_items


@then("the system reports that the todo text is required")
def _system_reports_todo_text_required(scenario_state: dict[str, Any]) -> None:
    error = scenario_state["todo_error"]
    assert error is not None, scenario_state
    assert "todo text is required" in str(error), error


@then("the system reports that the target must be a GitHub `owner/repo` or a supported git URL")
def _system_reports_target_form(scenario_state: dict[str, Any]) -> None:
    error = scenario_state["todo_error"]
    assert error is not None, scenario_state
    assert "must be a GitHub owner/repo or a supported git URL" in str(error), error


@then("no issue is created")
@then("the server does not create a GitHub issue")
def _no_issue_is_created(scenario_state: dict[str, Any]) -> None:
    assert _gh_commands(scenario_state) == []


@then(
    parsers.parse("the open `{label}` issues of the todo repository are recognized as todo items")
)
def _open_todo_issues_recognized(label: str, scenario_state: dict[str, Any]) -> None:
    command = scenario_state["todo_list_command"]
    assert _flag_value(command, "--repo") == TODO_REPOSITORY, command
    assert _flag_value(command, "--label") == label, command
    items = scenario_state["refreshed_todo_items"]
    assert {(item.repo, item.number) for item in items} == {
        (TODO_REPOSITORY, TODO_ISSUE_NUMBER),
        (TODO_REPOSITORY, 111),
    }, items


@then("each todo item's target is read from its issue body metadata")
def _todo_targets_read_from_body(scenario_state: dict[str, Any]) -> None:
    items = scenario_state["refreshed_todo_items"]
    assert {item.number: item.todo_target for item in items} == scenario_state[
        "expected_todo_targets"
    ]


# ----- F-TODO-TARGET -----


@given(
    parsers.parse("the dashboard includes a todo issue number {number:d} with target `{target}`")
)
def _dashboard_includes_targeted_todo(
    number: int,
    target: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    _seed_todo_item(scenario_state, work_items, tmp_path, number=number, target=target)


@given(parsers.parse("the dashboard includes a todo issue number {number:d} with no target"))
def _dashboard_includes_untargeted_todo(
    number: int,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    _seed_todo_item(scenario_state, work_items, tmp_path, number=number, target=None)


@when("the user lists work items")
def _user_lists_work_items(scenario_state: dict[str, Any]) -> None:
    scenario_state["listed_items"] = scenario_state["api_session"].list_items()["items"]


@when("the user opens the item in the browser")
def _user_opens_item_in_browser(scenario_state: dict[str, Any]) -> None:
    opened: list[str] = []

    async def interactions(app, pilot) -> None:
        await pilot.press("o")
        for _ in range(20):
            await pilot.pause()
            if opened:
                break

    run_app(
        work_items=[scenario_state["todo_item"]],
        open_callback=lambda item: opened.append(item.url),
        interactions=interactions,
    )
    scenario_state["opened_urls"] = opened


@when("the system prepares the item's worktree")
def _system_prepares_items_worktree(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = scenario_state["todo_item"]
    scenario_state["work_item"] = item
    workdir = scenario_state.get("workdir", tmp_path)
    scenario_state["workdir"] = workdir
    _install_fake_git(scenario_state, monkeypatch, workdir=workdir)
    scenario_state["worktree_path"] = ensure_worktree(str(workdir), item)


@when("the user launches a coding session for that item")
def _user_launches_coding_session_for_item(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import workdash.control as control_module
    import workdash.launcher as launcher_module

    session = scenario_state["api_session"]
    item = scenario_state["todo_item"]
    workdir = Path(session.config.workdir)
    scenario_state["work_item"] = item
    scenario_state["workdir"] = workdir
    _install_fake_git(scenario_state, monkeypatch, workdir=workdir)

    scenario_state["todo_github_body"] = "body-from-gh: what the todo issue says"
    monkeypatch.setattr(
        launcher_module,
        "collect_launch_github_context",
        lambda launched_item: {
            "number": launched_item.number,
            "title": launched_item.title,
            "body": scenario_state["todo_github_body"],
            "state": "OPEN",
            "url": launched_item.url,
        },
    )
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path: None)

    launches: list[tuple[str, str]] = []

    def fake_launch(repo_path, prompt, agent_command_tokens=None, *, zellij_session=None):
        launches.append((repo_path, prompt))
        return SimpleNamespace(
            session=zellij_session,
            pane_id="terminal_23",
            pane_title="code_todo",
            cwd=repo_path,
        )

    monkeypatch.setattr(control_module, "launch_agent_context", fake_launch)

    session.code(target=format_work_item_id(item), agent="codex")
    scenario_state["todo_launches"] = launches


@then(parsers.parse("the item is shown for repository `{repo}`"))
def _item_shown_for_repository(repo: str, scenario_state: dict[str, Any]) -> None:
    assert [item["repo"] for item in scenario_state["listed_items"]] == [repo]


@then("the item is shown for the todo repository")
def _item_shown_for_todo_repository(scenario_state: dict[str, Any]) -> None:
    assert [item["repo"] for item in scenario_state["listed_items"]] == [TODO_REPOSITORY]


@then(parsers.parse("the item's Workdash item ID is `{item_id}`"))
def _item_id_is(item_id: str, scenario_state: dict[str, Any]) -> None:
    assert [item["id"] for item in scenario_state["listed_items"]] == [item_id]


@then("the item's Workdash item ID is the ordinary issue ID for that repository and number")
def _item_id_is_ordinary_issue_id(scenario_state: dict[str, Any]) -> None:
    item = scenario_state["todo_item"]
    assert [entry["id"] for entry in scenario_state["listed_items"]] == [
        f"{item.repo}#ISSUE-{item.number}"
    ]


@then("the system opens the todo issue in the todo repository")
def _system_opens_todo_issue(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["opened_urls"] == [scenario_state["todo_item"].url]
    assert TODO_REPOSITORY in scenario_state["todo_item"].url


@then(parsers.parse("the worktree belongs to the `{target}` main clone"))
def _worktree_belongs_to_target_main_clone(target: str, scenario_state: dict[str, Any]) -> None:
    worktree = Path(scenario_state["worktree_path"])
    owner, _, name = GitHelper.repo_from_remote_url(target).partition("/")
    number = scenario_state["todo_item"].number
    assert worktree.name == f"{owner}_{name}_todo_{number}", worktree
    command = ["gh", "repo", "clone"] if target == f"{owner}/{name}" else ["git", "clone"]
    assert [*command, target, str(worktree.parent / f"{owner}_{name}")] in scenario_state[
        "_recorded_git_calls"
    ]
    if command == ["git", "clone"]:
        assert not any(cmd[0] == "gh" for cmd in scenario_state["_recorded_git_calls"])


@then("that branch was created from the target repository's current default branch")
def _branch_created_from_target_default_branch(scenario_state: dict[str, Any]) -> None:
    recorded = scenario_state["_recorded_git_calls"]
    add_cmd = next(cmd for cmd in recorded if cmd[:3] == ["git", "worktree", "add"])
    assert "origin/HEAD" in add_cmd, add_cmd
    assert ["git", "fetch", "--prune", "origin"] in recorded, recorded


@then("the session starts in the target repository's todo worktree")
def _session_starts_in_target_todo_worktree(scenario_state: dict[str, Any]) -> None:
    launches = scenario_state["todo_launches"]
    assert launches, scenario_state
    item = scenario_state["todo_item"]
    owner, _, name = display_repo(item).partition("/")
    assert launches[0][0] == str(
        Path(scenario_state["workdir"]) / f"{owner}_{name}_todo_{item.number}"
    )


@then("the session context describes the todo issue")
def _session_context_describes_todo_issue(scenario_state: dict[str, Any]) -> None:
    item = scenario_state["todo_item"]
    prompt = scenario_state["todo_launches"][0][1]
    # The identity lines must name the todo issue, not the target repository the
    # session works in; matching whole lines keeps the gh context dump (which
    # repeats number and url) from making this check vacuous.
    assert f"- repo: {TODO_REPOSITORY}" in prompt, prompt
    assert f"- number: {item.number}" in prompt, prompt
    assert f"- url: {item.url}" in prompt, prompt
    assert f"- repo: {item.todo_target}" not in prompt, prompt
    assert scenario_state["todo_github_body"] in prompt, prompt


# ----- F-SETUP-CONFIGURE-S009 -----


@given("the configuration has no todo repository")
def _configuration_has_no_todo_repository(config_path: Path) -> None:
    assert load_config(config_path).todo_repository == ""


@then(parsers.parse('the system prompts for the todo repository with the default "{template}"'))
def _prompts_for_todo_repository(template: str, scenario_state: dict[str, Any]) -> None:
    expected = template.replace("<username>", scenario_state["provided_username"])
    prompts = scenario_state["prompts"]
    assert any(f"Todo repository [{expected}]" in prompt for prompt in prompts), prompts


@then(parsers.parse('submitting an empty response stores "{template}"'))
def _empty_response_stores_todo_repository(template: str, scenario_state: dict[str, Any]) -> None:
    expected = template.replace("<username>", scenario_state["provided_username"])
    assert scenario_state["written_config"].todo_repository == expected


# ----- F-CLI-ORCHESTRATION-S024/S025 -----


@when(parsers.parse('the user runs `workdash todo "{text}" --target {target:S} --host {host}`'))
@when(parsers.parse('the user runs `workdash todo "{text}" --target {target:S}`'))
def _run_todo_cli_with_target(
    text: str,
    target: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    host: str = "127.0.0.1",
    body: str | None = None,
) -> None:
    import workdash.workdash as workdash_module

    session = ensure_api_session(scenario_state, work_items, tmp_path)
    _install_gh_todo_fake(scenario_state, monkeypatch)

    class FakeControlClient:
        def __init__(self, selected_host="127.0.0.1") -> None:
            assert selected_host == host
            scenario_state["client_host"] = selected_host

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            payload = payload or {}
            scenario_state.setdefault("control_requests", []).append(
                {"endpoint": endpoint, "payload": dict(payload)}
            )
            assert endpoint == "todo"
            return session.todo(
                text=payload["text"], target=payload.get("target"), body=payload.get("body")
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    scenario_state["requested_target"] = target
    scenario_state["exit_code"] = workdash_module.main(
        ["todo", text, "--target", target, "--host", host]
        + (["--body", body] if body is not None else [])
    )
    captured = capsys.readouterr()
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err


@when(parsers.parse('the user runs `workdash todo "{text}"`'))
def _run_todo_cli_without_server(
    text: str,
    scenario_state: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.workdash as workdash_module

    scenario_state["exit_code"] = workdash_module.main(["todo", text])
    captured = capsys.readouterr()
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err


@then("the command captures the todo through the local Workdash server")
def _command_captures_todo_through_server(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    payload = {"text": TODO_TEXT, "target": scenario_state["requested_target"]}
    if "todo_body" in scenario_state:
        payload["body"] = scenario_state["todo_body"]
    assert scenario_state["control_requests"] == [{"endpoint": "todo", "payload": payload}]
    assert _created_issue_command(scenario_state)


@then("the system reports the created Workdash item ID and issue URL")
def _system_reports_created_item_id_and_url(scenario_state: dict[str, Any]) -> None:
    stdout = scenario_state["stdout"]
    assert f"owner/repo#ISSUE-WT{TODO_ISSUE_NUMBER}" in stdout, stdout
    assert TODO_ISSUE_URL in stdout, stdout


# ----- F-API-JSON-CONTROL-S015/S016 -----


@when(parsers.parse("a client requests a todo with text `{text}` and target `{target}`"))
def _client_requests_todo(
    text: str,
    target: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: str | None = None,
) -> None:
    _install_gh_todo_fake(scenario_state, monkeypatch)
    scenario_state["requested_target"] = target
    payload = {"text": text, "target": target}
    if body is not None:
        payload["body"] = body
    _call_api(scenario_state, work_items, tmp_path, "todo", payload)


@when("a client requests a todo with an invalid target")
def _client_requests_todo_with_invalid_target(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_gh_todo_fake(scenario_state, monkeypatch)
    _call_api(
        scenario_state, work_items, tmp_path, "todo", {"text": TODO_TEXT, "target": "not-a-repo"}
    )


@then("the new todo becomes part of the shared dashboard state")
def _new_todo_becomes_shared_state(scenario_state: dict[str, Any]) -> None:
    session = scenario_state["api_session"]
    assert format_work_item_id(session.work_items[-1]) == (
        f"owner/repo#ISSUE-WT{TODO_ISSUE_NUMBER}"
    )


@then("the API returns the Workdash item ID, todo repository, target, issue number, and issue URL")
def _api_returns_todo_result(scenario_state: dict[str, Any]) -> None:
    assert _api_result(scenario_state) == {
        "item_id": f"owner/repo#ISSUE-WT{TODO_ISSUE_NUMBER}",
        "todo_repository": TODO_REPOSITORY,
        "target": scenario_state["requested_target"],
        "number": TODO_ISSUE_NUMBER,
        "url": TODO_ISSUE_URL,
    }


@then(
    "the API returns an error saying the target must be a GitHub `owner/repo` or a supported git URL"
)
def _api_returns_invalid_target_error(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["api_status"] == 400, scenario_state["api_payload"]
    payload = scenario_state["api_payload"]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "bad_request"
    assert "must be a GitHub owner/repo or a supported git URL" in payload["error"]["message"]


@given("the user has a multiline todo body longer than 256 characters")
def _long_todo_body(scenario_state: dict[str, Any]) -> None:
    scenario_state["todo_body"] = (
        "  Research for wrong/repository.\n\n"
        + "Keep the full migration findings in the body.\n" * 8
        + '```json\n{"workdash_todo_version": 1, "target": "wrong/repository"}\n```\n  '
    )
    assert len(scenario_state["todo_body"]) > 256


@when("the user captures a short todo title with that body using the CLI")
def _run_todo_cli_with_body(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_todo_cli_with_target(
        TODO_TEXT,
        "owner/repo",
        scenario_state,
        work_items,
        tmp_path,
        monkeypatch,
        capsys,
        body=scenario_state["todo_body"],
    )


@when("a client requests a short todo title with that body and target `owner/repo`")
def _request_todo_with_body(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client_requests_todo(
        TODO_TEXT,
        "owner/repo",
        scenario_state,
        work_items,
        tmp_path,
        monkeypatch,
        body=scenario_state["todo_body"],
    )


@then("the created issue keeps the short title and the complete body after its metadata")
def _issue_has_title_and_body(scenario_state: dict[str, Any]) -> None:
    command = _created_issue_command(scenario_state)
    assert _flag_value(command, "--title") == TODO_TEXT
    issue_body = _flag_value(command, "--body")
    assert issue_body == (
        '```json\n{\n  "workdash_todo_version": 1,\n  "target": "owner/repo"\n}\n```\n\n'
        + scenario_state["todo_body"]
    )
    assert todo_target_from_body(issue_body) == "owner/repo"


@when("the user captures a targeted todo with body details naming a different repository")
def _capture_conflicting_body(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _long_todo_body(scenario_state)
    _capture_todo(
        scenario_state,
        work_items,
        tmp_path,
        text=TODO_TEXT,
        target="owner/repo",
        body=scenario_state["todo_body"],
    )
    assert scenario_state["todo_error"] is None
    _issue_has_title_and_body(scenario_state)
    scenario_state["captured_todo_body"] = _flag_value(
        _created_issue_command(scenario_state), "--body"
    )
    scenario_state["todo_item"] = scenario_state["api_session"].work_items[-1]


@when("the system refreshes that captured todo from GitHub")
def _refresh_captured_todo(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _todo_captured_in_earlier_session("owner/repo", scenario_state, monkeypatch)
    _system_refreshes_dashboard_items(scenario_state, tmp_path)
    scenario_state["todo_item"] = next(
        item for item in scenario_state["refreshed_todo_items"] if item.number == TODO_ISSUE_NUMBER
    )
    # Prepare a fresh checkout to verify repository selection after loading from GitHub.
    scenario_state["workdir"] = tmp_path / "after-refresh"


@then(parsers.parse("the refreshed todo is listed under `{repository}`"))
def _refreshed_repository(repository, scenario_state):
    item = next(
        item for item in scenario_state["refreshed_todo_items"] if item.number == TODO_ISSUE_NUMBER
    )
    assert display_repo(item) == repository
    assert format_work_item_id(item) == f"{repository}#ISSUE-WT{TODO_ISSUE_NUMBER}"


@given("the target repository has no local clone or worktree")
def _target_not_prepared(scenario_state):
    session = scenario_state["api_session"]
    workdir = Path(session.config.workdir)
    assert not (workdir / "owner_repo").exists()
    assert not (workdir / "owner_repo_todo_110").exists()


@then(parsers.parse("the worktree directory is named `{directory}`"))
def _target_worktree_directory(directory, scenario_state):
    assert Path(scenario_state["worktree_path"]).name == directory


@when("the user opens a terminal for that item")
def _open_target_terminal(scenario_state, monkeypatch):
    import workdash.control as control_module

    session = scenario_state["api_session"]
    workdir = Path(session.config.workdir)
    scenario_state["workdir"] = workdir
    _install_fake_git(scenario_state, monkeypatch, workdir=workdir)
    launches = []

    def launch(repo_path, *, zellij_session=None):
        launches.append(repo_path)
        return SimpleNamespace(
            session=zellij_session, pane_id="terminal_23", pane_title="todo", cwd=repo_path
        )

    monkeypatch.setattr(control_module, "launch_terminal_context", launch)
    try:
        result = session.terminal(target=format_work_item_id(scenario_state["todo_item"]))
        scenario_state["worktree_path"] = result["cwd"]
    except RuntimeError as error:
        scenario_state["terminal_error"] = error
    scenario_state["terminal_calls"] = launches


@then("the terminal starts in the target repository's todo worktree")
def _terminal_in_target(scenario_state):
    expected = str(scenario_state["workdir"] / "owner_repo_todo_110")
    assert scenario_state["terminal_calls"] == [expected]


@given(parsers.parse("the local clone directory `{directory}` belongs to `{origin}`"))
def _conflicting_clone(directory, origin, scenario_state):
    path = Path(scenario_state["api_session"].config.workdir) / directory
    path.mkdir(parents=True)
    (path / "work.txt").write_text("local changes")
    scenario_state["origin_url"] = origin
    scenario_state["conflicting_clone"] = path


@then("the system reports that the local directory belongs to a different repository")
def _reports_clone_conflict(scenario_state):
    assert "belongs to a different repository" in str(scenario_state["terminal_error"])


@then("the existing clone and its worktrees remain untouched")
def _conflicting_clone_untouched(scenario_state):
    assert (scenario_state["conflicting_clone"] / "work.txt").read_text() == "local changes"
    assert all(
        cmd[:4] == ["git", "config", "--local", "--get"]
        or cmd == ["git", "rev-parse", "--show-toplevel"]
        for cmd in scenario_state["_recorded_git_calls"]
    )


@given(parsers.parse("the target repository contains a source file `{path}`"))
def _target_has_source(path, scenario_state):
    scenario_state["target_source"] = {path: "\n" * 11 + "def cards(): return 42\n"}


@given("the todo issue has a description and comments on GitHub")
def _todo_has_discussion(scenario_state):
    item = scenario_state["todo_item"]
    scenario_state["discussion"] = {
        "title": item.title,
        "body": "Todo description from GitHub",
        "comments": [{"body": "Todo comment from GitHub", "author": {"login": "owner"}}],
    }


@when("the user generates a fresh analysis for that item")
def _analyze_targeted_todo(scenario_state, monkeypatch, tmp_path):
    from .analysis import run_source_analysis

    run_source_analysis(scenario_state, monkeypatch, tmp_path)


@then("the analysis agent starts inside the target repository's todo worktree")
def _analysis_in_target_worktree(scenario_state):
    assert (
        scenario_state["agent_cwd"]
        == Path(scenario_state["api_session"].config.workdir) / "owner_repo_todo_110"
    )


@then(parsers.parse("the agent can inspect `{path}` in the target repository"))
def _agent_inspects_target_source(path, scenario_state):
    assert scenario_state["agent_sources"][path] == scenario_state["target_source"][path]


@then("the agent receives the todo issue's title, body, and comments from GitHub")
def _agent_receives_todo_discussion(scenario_state):
    discussion = scenario_state["discussion"]
    prompt = scenario_state["agent_prompt"]
    assert discussion["title"] in prompt
    assert discussion["body"] in prompt
    assert discussion["comments"][0]["body"] in prompt
    assert "testuser/todos" in scenario_state["discussion_command"]
