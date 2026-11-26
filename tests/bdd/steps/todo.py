"""Step definitions for capturing, acting on, and closing todos."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
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
from gh_work_state_fake import install_fake_github, todo_body
from workdash.backend import WorkdashBackend
from workdash.config import AgentConfig, load_config
from workdash.control import format_work_item_id
from workdash.github_client import GitHubClient, todo_target_from_body
from workdash.included_items import IncludedItemsStore
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import TodoDialog, WorkdashApp

from .common import (
    NOW_UTC,
    api_config,
    ensure_api_session,
    install_pane,
    install_zellij_agent_fakes,
    make_work_item,
    run_app,
)
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

    import workdash.github_client as todo_module

    commands: list[list[str]] = []
    run_gh = fake_gh_todo_run(
        commands,
        label_error=gh_todo_failure(label_stderr) if label_stderr is not None else None,
    )

    monkeypatch.setattr(todo_module.subprocess, "run", run_gh)
    scenario_state["todo_gh_commands"] = commands
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


def _todo_captured_in_earlier_session(
    target: str,
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
    monkeypatch.setattr(
        GitHubClient, "list_open_assigned_issues", lambda self, login, progress_callback=None: []
    )
    monkeypatch.setattr(
        GitHubClient,
        "list_recent_tracked_items",
        lambda self, repositories, progress_callback=None: [],
    )
    stored_issues = [
        {
            "subIssuesSummary": {"total": 0},
            "id": "I110",
            "number": TODO_ISSUE_NUMBER,
            "title": TODO_TEXT,
            "url": TODO_ISSUE_URL,
            "createdAt": "2026-04-01T00:00:00Z",
            "updatedAt": "2026-04-02T00:00:00Z",
            "body": todo_body(target),
        },
    ]

    def fake_run(command, **kwargs):
        assert command[:3] == ["gh", "api", "graphql"], command
        payload = {"data": {"repository": {"issues": {"nodes": stored_issues}}}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)


@when(parsers.parse('the user presses "w" and submits "{title}" with the target blank'))
@when(parsers.parse('the user captures "{title}" with "w" for target "{target}"'))
@when(
    parsers.parse(
        'the user submits "{title}" with "w" and follows the guidance to create "{repository}" on GitHub and retry'
    )
)
def _capture_in_dashboard(title, scenario_state, monkeypatch, target="", repository=None):
    session = scenario_state["api_session"]

    async def submit(app, pilot):
        footer = app.query_one("#status-footer", Static)
        previous_status = _static_text(footer)
        await pilot.press("w")
        dialog = app.screen
        assert isinstance(dialog, TodoDialog)
        scenario_state["todo_dialog"] = (
            [widget.id for widget in dialog.query(Input)],
            [_static_text(widget) for widget in dialog.query(Static)],
            dialog.query_one("#todo-target", Input).value,
        )
        dialog.query_one("#todo-text", Input).value = title
        if target:
            await pilot.press("tab")
            dialog.query_one("#todo-target", Input).value = target
        await pilot.press("enter")
        async with asyncio.timeout(3):
            while True:
                status = _static_text(footer)
                if status != previous_status and status.startswith(
                    ("Captured todo ", "Todo failed: ")
                ):
                    return status
                await pilot.pause(0.01)

    async def interactions(app, pilot):
        assert all(title not in str(row[2]) for row in _dashboard_rows(app))
        status = await submit(app, pilot)
        if repository:
            assert repository == TODO_REPOSITORY
            assert f"Create the todo repository {repository} on GitHub" in status
            scenario_state["rows_after_failed_capture"] = _dashboard_rows(app)
            _install_gh_todo_fake(scenario_state, monkeypatch, label_stderr=None)
            status = await submit(app, pilot)
        assert status.startswith("Captured todo "), status
        scenario_state["todo_rows"] = _dashboard_rows(app)

    run_app(
        work_items=list(session.work_items),
        session=session,
        todo_callback=lambda text, target: session.todo(text=text, target=target),
        interactions=interactions,
    )


def _dashboard_rows(app):
    table = app.query_one("#work-items", DataTable)
    return [table.get_row_at(index) for index in range(table.row_count)]


@then("the capture dialog has asked for a title and an optional target repository")
def _system_asks_for_text_and_optional_target(scenario_state):
    inputs, labels, initial_target = scenario_state["todo_dialog"]
    assert inputs == ["todo-text", "todo-target"]
    assert any("optional" in label.lower() for label in labels)
    assert initial_target == ""


@then(parsers.parse('the dashboard immediately shows todo "{title}" under "{repository}"'))
@then(parsers.parse('the dashboard shows todo "{title}" under "{repository}"'))
def _dashboard_shows_todo(title, repository, scenario_state):
    rows = scenario_state["todo_rows"]
    assert any(
        type_column_label(str(row[0])) == f"TD {TODO_ISSUE_NUMBER}"
        and str(row[1]) == repository
        and title in str(row[2])
        for row in rows
    ), rows


@then(parsers.parse('no todo titled "{title}" was added by the failed attempt'))
def _failed_capture_does_not_add_todo(title, scenario_state):
    assert all(title not in str(row[2]) for row in scenario_state["rows_after_failed_capture"])


@given(
    parsers.parse('an earlier todo titled "{title}" targets "{target}" but is not on the dashboard')
)
def _earlier_todo_not_on_dashboard(title, target, monkeypatch):
    assert title == TODO_TEXT
    _todo_captured_in_earlier_session(target, monkeypatch)


@when('the user refreshes the dashboard with "r"')
def _refresh_todos_in_dashboard(scenario_state, tmp_path, work_items):
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    session.backend = WorkdashBackend(
        cache_root=tmp_path / "cache",
        config=api_config(tmp_path),
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )

    async def interactions(app, pilot):
        assert all(TODO_TEXT not in str(row[2]) for row in _dashboard_rows(app))
        await pilot.press("r")
        footer = app.query_one("#status-footer", Static)
        async with asyncio.timeout(3):
            while not _static_text(footer).startswith("Refreshed "):
                await pilot.pause(0.01)
        scenario_state["todo_rows"] = _dashboard_rows(app)

    run_app(
        work_items=session.work_items,
        session=session,
        refresh_callback=lambda: session.list_items(refresh=True),
        interactions=interactions,
    )


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


@given(parsers.parse('the dashboard lists a todo "{title}" targeting `{target}`'))
def _select_targeted_todo(title, target, scenario_state, work_items, tmp_path):
    assert title == TODO_TEXT
    _seed_todo_item(scenario_state, work_items, tmp_path, number=110, target=target)


@given("the target repository has no local clone or worktree")
def _target_not_prepared(scenario_state):
    assert not Path(scenario_state["api_session"].config.workdir).exists()


@when("the user runs `workdash list` to find the todo's ID")
def _find_todo_id(scenario_state, monkeypatch, capsys):
    import workdash.workdash as workdash_module
    from workdash import control

    session = scenario_state["api_session"]
    workdir = Path(session.config.workdir)
    _install_fake_git(scenario_state, monkeypatch, workdir=workdir)

    def launch(cwd, *, zellij_session):
        assert Path(cwd).is_dir()
        scenario_state["terminal_cwd"] = cwd
        return SimpleNamespace(
            session=zellij_session, cwd=cwd, pane_id="terminal_23", pane_title="Todo shell"
        )

    class Client:
        def __init__(self, host):
            pass

        def request(self, endpoint, payload):
            return {"list": session.list_items, "terminal": session.terminal}[endpoint](**payload)

    monkeypatch.setattr(control, "launch_terminal_context", launch)
    monkeypatch.setattr(workdash_module, "WorkdashControlClient", Client)
    assert workdash_module.main(["list"]) == 0
    scenario_state["printout"] = capsys.readouterr().out


@then(parsers.parse("the printed list identifies the todo as `{item_id}`"))
def _copy_todo_id(item_id, scenario_state):
    [row] = [row for row in scenario_state["printout"].splitlines() if TODO_TEXT in row]
    scenario_state["copied_id"] = row.split()[1]
    assert scenario_state["copied_id"] == item_id


@when("the user passes that ID to `workdash terminal`")
def _open_todo_terminal(scenario_state, capsys):
    from workdash.workdash import main

    assert main(["terminal", scenario_state["copied_id"]]) == 0
    scenario_state["printout"] = capsys.readouterr().out


@then("Workdash reports a shell in a new worktree of the Codeberg repository")
def _reports_target_shell(scenario_state):
    output = scenario_state["printout"].splitlines()
    cwd = scenario_state["terminal_cwd"]
    assert f"Item: {scenario_state['copied_id']}" in output
    assert f"Cwd: {cwd}" in output
    assert "Pane id: terminal_23" in output
    add = next(
        cmd
        for cmd in scenario_state["_recorded_git_calls"]
        if cmd[:3] == ["git", "worktree", "add"]
    )
    assert cwd in add
    assert any(
        cmd[:3] == ["git", "clone", "https://codeberg.org/owner/repo.git"]
        for cmd in scenario_state["_recorded_git_calls"]
    )


@when('the user presses "v" then "o" on the todo')
def _open_todo_issue(scenario_state):
    opened = []

    async def interactions(app, pilot):
        [row] = _dashboard_rows(app)
        assert str(row[1]) == "owner/repo"
        await pilot.press("v", "o")
        async with asyncio.timeout(3):
            while not opened:
                await pilot.pause(0.01)

    run_app(
        work_items=[scenario_state["todo_item"]],
        open_callback=lambda item: opened.append(item.url),
        interactions=interactions,
    )
    scenario_state["opened_urls"] = opened


@then(parsers.parse("the browser opens `{url}`, not an issue in `{target}`"))
def _original_todo_url(url, target, scenario_state):
    assert scenario_state["opened_urls"] == [url]
    assert url != f"https://github.com/{target}/issues/110"


@when("the user launches a coding session for that item")
@when('the user presses "c" and chooses a coding agent')
def _choose_todo_coding_agent(scenario_state, monkeypatch):
    from workdash import control

    session = scenario_state["api_session"]
    _install_fake_git(scenario_state, monkeypatch, workdir=Path(session.config.workdir))
    github = install_fake_github(monkeypatch)
    github.add_issue(f"{TODO_REPOSITORY}#110", body=todo_body("owner/repo"))
    monkeypatch.setattr(
        session.backend.github_client,
        "fetch_launch_context",
        lambda item: {"title": item.title, "body": "Find what makes the test fail intermittently"},
    )
    monkeypatch.setattr(control, "get_merge_base", lambda _path, **kwargs: None)
    install_zellij_agent_fakes(scenario_state, monkeypatch)

    async def interactions(app, pilot):
        await pilot.press("c", "1")
        footer = app.query_one("#status-footer", Static)
        async with asyncio.timeout(3):
            while not _static_text(footer).startswith(("Launched ", "Launch failed: ")):
                await pilot.pause(0.01)
        assert _static_text(footer).startswith("Launched "), _static_text(footer)

    run_app(
        work_items=session.work_items,
        session=session,
        config=session.config,
        launch_callback=lambda item, agent: session.code(
            target=format_work_item_id(item), agent=agent
        ),
        interactions=interactions,
    )


@then("the coding agent opens in the target checkout with the todo as its task")
def _coding_agent_on_todo(scenario_state):
    [(cwd, prompt, *_)] = scenario_state["agent_launches"]
    assert Path(cwd).name == "owner_repo_todo_110"
    assert "Find what makes the test fail intermittently" in prompt


@given("the todo has discussion on GitHub and source code in its target repository")
def _todo_analysis_inputs(scenario_state):
    scenario_state["target_source"] = {"src/cards.py": "cards are stored by suit\n"}
    scenario_state["discussion"] = {"body": "find the missing ace"}


@when('the user presses "a" and chooses a fresh analysis')
def _choose_todo_analysis(scenario_state, monkeypatch, tmp_path):
    from .analysis import _install_github_discussion

    session = scenario_state["api_session"]
    _install_fake_git(scenario_state, monkeypatch, workdir=Path(session.config.workdir))
    _install_github_discussion(scenario_state, monkeypatch, issue=scenario_state["todo_item"])
    install_pane(scenario_state, monkeypatch)
    agent = tmp_path / "todo-analysis-agent"
    agent.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "print(Path('src/cards.py').read_text().strip())\n"
        "print(next(line.strip() for line in sys.argv[-1].splitlines() if 'find the missing ace' in line))\n"
    )
    agent.chmod(0o755)
    session.config = replace(session.config, agents={"test": AgentConfig(analyze=str(agent))})
    session.backend = WorkdashBackend(config=session.config, cache_root=tmp_path / "analysis-cache")
    opened = []

    app = WorkdashApp(
        work_items=session.work_items,
        session=session,
        analyze_choices=session.config.tui_analyze_choices(),
        analyze_callback=lambda item, agent: session.analyze(
            target=format_work_item_id(item), agent=agent, prefer_cache=False
        )["path"],
        open_markdown_callback=lambda path: opened.append(Path(path).read_text()),
    )

    async def interactions():
        async with app.run_test() as pilot:
            await pilot.press("a", "1")
            footer = app.query_one("#status-footer", Static)
            async with asyncio.timeout(3):
                while not opened and not _static_text(footer).startswith("Analyze failed: "):
                    await pilot.pause(0.01)
            assert opened, _static_text(footer)

    asyncio.run(interactions())
    [scenario_state["opened_analysis"]] = opened


@then("the opened analysis draws on both the target code and the todo discussion")
def _opened_target_analysis(scenario_state):
    assert "cards are stored by suit" in scenario_state["opened_analysis"]
    assert "find the missing ace" in scenario_state["opened_analysis"]


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


@given("the dashboard lists todo `owner/todos#ISSUE-42`")
def _close_todo_setup(scenario_state, work_items, tmp_path, monkeypatch):
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    session.config = replace(session.config, todo_repository="owner/todos")
    session.work_items.append(
        make_work_item(repo="owner/todos", number=42, title="Close this todo")
    )

    def run(command, **kwargs):
        assert command == ["gh", "issue", "close", "42", "--repo", "owner/todos"]
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(subprocess, "run", run)


@when(parsers.parse("the user runs `workdash todo --close {reference}`"))
def _close_todo_cli(reference, scenario_state, monkeypatch):
    import workdash.workdash as workdash_module

    session = scenario_state["api_session"]

    class Client:
        def __init__(self, *args):
            pass

        def request(self, endpoint, payload):
            assert endpoint == "todo_close"
            return session.close_todo(item=payload["item"])

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", Client)

    output = StringIO()

    def close_todo():
        with redirect_stdout(output):
            return workdash_module.main(["todo", "--close", reference])

    async def interactions(app, _pilot):
        assert any("Close this todo" in str(row[2]) for row in _dashboard_rows(app))
        session.items_changed_callback = lambda: app.call_from_thread(app.refresh_from_session)
        assert await asyncio.to_thread(close_todo) == 0
        scenario_state["todo_rows"] = _dashboard_rows(app)

    run_app(work_items=session.work_items, session=session, interactions=interactions)
    scenario_state["close_output"] = output.getvalue()


@then("the closed todo is removed from the dashboard")
def _closed_todo_removed(scenario_state):
    assert all("Close this todo" not in str(row[2]) for row in scenario_state["todo_rows"])


@then(parsers.parse("the command confirms that `{reference}` was closed"))
def _close_confirmation(reference, scenario_state):
    assert reference in scenario_state["close_output"]
    assert "closed" in scenario_state["close_output"].lower()
