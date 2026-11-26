"""Step definitions for the triage domain feature files.

Covers dashboard listing and search, the list command,
refresh, and inclusion. Steps exercise real
``WorkdashApp``/``WorkdashBackend`` code — only the external GitHub and
browser-open boundaries are faked.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import DataTable, Input, Static

from workdash.backend import WorkdashBackend
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession
from workdash.github_client import GitHubClient
from workdash.included_items import IncludedItemsStore
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import IncludeDialog, SearchDialog

from .common import (
    NOW_UTC,
    api_config,
    ensure_api_session,
    make_work_item,
    modal_screen_names,
    run_app,
    seed_dashboard_item,
)


def linked_issues_response(
    command: list[str], *, closed_issues: dict[str, tuple[tuple[str, int], ...]] | None = None
) -> subprocess.CompletedProcess[str]:
    """Answer the linked-issue GraphQL batch for a fake ``gh api graphql`` call.

    Aliases are read back from the query the production code built, so a
    scenario only has to name the pull requests that close an issue.

    :param dict closed_issues: ``owner/repo#number`` -> the closed ``(repo, number)`` issues.
    """

    closed = closed_issues or {}
    data: dict[str, Any] = {}
    for alias, owner, name, number in re.findall(
        r'\b(p\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\) '
        r"{ pullRequest\(number: (\d+)\)",
        command[4],
    ):
        issues = closed.get(f"{owner}/{name}#{number}", ())
        data[alias] = {
            "pullRequest": {
                "closingIssuesReferences": {
                    "nodes": [
                        {"number": issue_number, "repository": {"nameWithOwner": repo}}
                        for repo, issue_number in issues
                    ]
                }
            }
        }
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"data": data}), stderr="")


def is_linked_issues_query(command: list[str]) -> bool:
    """Tell the linked-issue batch apart from other queries that also select closing issues."""
    return command[:3] == ["gh", "api", "graphql"] and bool(
        re.search(r"pullRequest\(number: \d+\) \{ closingIssuesReferences", command[4])
    )


# --------------------------------------------------------------------------
# F-TRIAGE-LIST
# --------------------------------------------------------------------------


@given("work items in `team/old`, `team/current`, and `archive/project`")
def _items_across_repositories(work_items: list[WorkItem]) -> None:
    work_items[:] = [
        make_work_item(repo=repo) for repo in ("team/old", "team/current", "archive/project")
    ]
    work_items[0].created_at -= timedelta(days=1)


@given("the configuration excludes `Team/OLD` and `Archive/*`")
def _exclude_repositories(scenario_state, work_items, tmp_path) -> None:
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    session.config = replace(session.config, excluded_repositories=["Team/OLD", "Archive/*"])


@when("the user opens the dashboard with these exclusions")
def _open_dashboard_with_exclusions(scenario_state, work_items) -> None:
    async def inspect_table(app, pilot):
        table = app.query_one("#work-items", DataTable)
        scenario_state["listed_repos"] = [
            str(table.get_row_at(index)[1]) for index in range(table.row_count)
        ]
        scenario_state["listed_titles"] = [
            str(table.get_row_at(index)[2]) for index in range(table.row_count)
        ]

    run_app(
        work_items=work_items, session=scenario_state["api_session"], interactions=inspect_table
    )


@then("only `team/current` appears in the work table")
def _only_current_repo_in_table(scenario_state) -> None:
    assert scenario_state["listed_repos"] == ["team/current"]


@then("no work item is marked as suggested")
def _no_work_is_suggested_after_exclusions(scenario_state) -> None:
    assert not any(title.startswith("* ") for title in scenario_state["listed_titles"])


@then("only `team/current` appears in the command output")
def _only_current_repo_in_list(scenario_state) -> None:
    lines = scenario_state["print_output"].splitlines()
    assert len(lines) == 1
    assert "team/current#ISSUE-1" in lines[0]


@given(
    "the user has an authored pull request, a review request, an assigned issue, and other tracked work"
)
def _has_open_work_all_sources(work_items: list[WorkItem]) -> None:
    # Build the list through the real normalization + merge pipeline so the
    # Then steps assert on classifications produced by production code rather
    # than on hand-built fixture tuples.
    from workdash.github_client import (
        merge_normalized_work_items,
        normalize_assigned_issues,
        normalize_authored_pull_requests,
        normalize_recent_tracked_items,
        normalize_review_requested_pull_requests,
    )

    authored_raw = [
        {
            "id": "PR-AUTHORED-10",
            "repo": "owner/repo",
            "number": 10,
            "title": "Authored PR",
            "url": "https://github.com/owner/repo/pull/10",
            "created_at": "2026-04-26T12:00:00Z",
            "updated_at": "2026-04-28T12:00:00Z",
            "is_draft": False,
            "ci_state": None,
            "review_decision": None,
            "review_requested": False,
            "has_unresolved_comments": False,
        }
    ]
    review_raw = [
        {
            "id": "PR-REVIEW-12",
            "repo": "owner/repo",
            "number": 12,
            "title": "Please review",
            "url": "https://github.com/owner/repo/pull/12",
            "created_at": "2026-04-25T12:00:00Z",
            "updated_at": "2026-04-27T12:00:00Z",
            "is_draft": False,
        }
    ]
    assigned_raw = [
        {
            "id": "ISSUE-ASSIGNED-11",
            "repo": "owner/repo",
            "number": 11,
            "title": "Assigned issue",
            "url": "https://github.com/owner/repo/issues/11",
            "created_at": "2026-04-24T12:00:00Z",
            "updated_at": "2026-04-26T12:00:00Z",
        }
    ]
    tracked_raw = [
        {
            "id": "TRACKED-ISSUE-13",
            "repo": "owner/repo",
            "number": 13,
            "title": "Tracked issue",
            "url": "https://github.com/owner/repo/issues/13",
            "created_at": "2026-04-23T12:00:00Z",
            "updated_at": "2026-04-25T12:00:00Z",
            "is_pull_request": False,
        },
        {
            "id": "TRACKED-PR-14",
            "repo": "owner/repo",
            "number": 14,
            "title": "Tracked PR",
            "url": "https://github.com/owner/repo/pull/14",
            "created_at": "2026-04-22T12:00:00Z",
            "updated_at": "2026-04-24T12:00:00Z",
            "is_pull_request": True,
        },
    ]

    merged = merge_normalized_work_items(
        normalize_authored_pull_requests(authored_raw),
        normalize_review_requested_pull_requests(review_raw),
    )
    merged = merge_normalized_work_items(merged, normalize_assigned_issues(assigned_raw))
    merged = merge_normalized_work_items(merged, normalize_recent_tracked_items(tracked_raw))
    work_items.extend(merged)


@when('the user opens the dashboard and uses "v" then "o" on the starred row')
def _open_starred_row(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    opened = []

    async def interactions(app, pilot):
        table = app.query_one("#work-items", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        scenario_state["triage_rows"] = [
            (type_column_label(row[0]), str(row[2]).removeprefix("* ")) for row in rows
        ]
        [index] = [i for i, row in enumerate(rows) if str(row[2]).startswith("* ")]
        scenario_state["starred_title"] = str(rows[index][2]).removeprefix("* ")
        await pilot.press(*(["down"] * index), "v", "o")

    run_app(work_items=work_items, interactions=interactions, open_callback=opened.append)
    scenario_state["opened_urls"] = [item.url for item in opened]


@then("all of that work is visible together")
def _all_work_visible(scenario_state: dict[str, Any]) -> None:
    assert sorted(scenario_state["triage_rows"]) == [
        ("IS 11", "Assigned issue"),
        ("IS 13", "Tracked issue"),
        ("PR 10", "Authored PR"),
        ("RV 12", "Please review"),
        ("VR 14", "Tracked PR"),
    ]


@then("the starred item's GitHub page opens")
def _starred_page_opens(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    item = next(item for item in work_items if item.title == scenario_state["starred_title"])
    assert scenario_state["opened_urls"] == [item.url]


def type_column_label(cell: object) -> str:
    """Return a Type cell without its leading CI and review symbols.

    Every Type cell reserves its first two characters for the CI and review
    symbols, so a step about the type label itself has to look past them.

    :param object cell: the rendered Type cell.
    """

    return str(cell)[2:]


_CHECK_PR_NUMBER = 909
_CHECK_PR_RECORD = {
    "id": "TRACKED-909",
    "repo": "owner/repo",
    "number": _CHECK_PR_NUMBER,
    "title": "Somebody else's pull request",
    "url": f"https://github.com/owner/repo/pull/{_CHECK_PR_NUMBER}",
    "created_at": "2026-04-20T10:00:00Z",
    "updated_at": "2026-04-28T10:00:00Z",
    "is_pull_request": True,
}


@given("the dashboard lists a pull request as VR")
def _dashboard_lists_a_check_item(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_empty_github_fakes(monkeypatch)
    monkeypatch.setattr(
        GitHubClient,
        "list_recent_tracked_items",
        lambda self, repositories, progress_callback=None: [dict(_CHECK_PR_RECORD)],
    )
    monkeypatch.setattr(GitHubClient, "fetch_linked_issues", lambda *args, **kwargs: {})
    backend = _make_backend(tmp_path)
    scenario_state["review_backend"] = backend
    scenario_state["initial_review_items"], _ = backend.load_items()


@given("the user has since reviewed that pull request on GitHub")
def _user_has_reviewed_that_pull_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # `gh search prs --reviewed-by` starts answering for the same pull request,
    # and the merge order lets that stronger relationship win.
    monkeypatch.setattr(
        GitHubClient,
        "list_open_reviewed_prs",
        lambda self, login: [
            {
                "id": "REVIEWED-909",
                "repo": "owner/repo",
                "number": _CHECK_PR_NUMBER,
                "title": "Somebody else's pull request",
                "url": f"https://github.com/owner/repo/pull/{_CHECK_PR_NUMBER}",
                "created_at": "2026-04-20T10:00:00Z",
                "updated_at": "2026-04-28T10:00:00Z",
                "is_draft": False,
            }
        ],
    )


@when('the user presses "r" to reload the reviewed pull request')
def _refresh_reviewed_pr(scenario_state: dict[str, Any]) -> None:
    async def interactions(app, pilot):
        table = app.query_one("#work-items", DataTable)
        assert table.row_count == 1
        assert type_column_label(table.get_row_at(0)[0]) == "VR 909"
        await pilot.press("r")
        footer = app.query_one("#status-footer", Static)
        for _ in range(100):
            if footer.render().plain == "Refreshed 1 item(s).":
                break
            await pilot.pause(0.01)
        scenario_state["reviewed_rows"] = [
            type_column_label(table.get_row_at(i)[0]) for i in range(table.row_count)
        ]

    run_app(
        work_items=scenario_state["initial_review_items"],
        refresh_callback=scenario_state["review_backend"].load_items,
        interactions=interactions,
    )


@then("the same pull request is listed as RV")
def _reviewed_pr_is_rv(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["reviewed_rows"] == ["RV 909"]


# ----- S013: a pull request replaces the issue it closes -----


@given("an open pull request closes an assigned issue")
def _pull_request_closes_an_assigned_issue(scenario_state: dict[str, Any]) -> None:
    scenario_state["closing_pr_number"] = 42149
    scenario_state["closed_issue_number"] = 41830


@given("both the pull request and that issue are open work for the user")
def _pull_request_and_issue_are_open_work(
    scenario_state: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    pr_number = scenario_state["closing_pr_number"]
    issue_number = scenario_state["closed_issue_number"]
    _install_empty_github_fakes(monkeypatch)
    monkeypatch.setattr(
        GitHubClient,
        "list_open_authored_prs",
        lambda self, login, progress_callback=None: [
            {
                "id": f"PR-{pr_number}",
                "repo": "owner/repo",
                "number": pr_number,
                "title": "Implement the assigned issue",
                "url": f"https://github.com/owner/repo/pull/{pr_number}",
                "created_at": "2026-04-25T10:00:00Z",
                "updated_at": "2026-04-28T10:00:00Z",
                "is_draft": False,
                "ci_state": None,
                "review_decision": None,
                "review_requested": False,
                "has_unresolved_comments": False,
            }
        ],
    )
    monkeypatch.setattr(
        GitHubClient,
        "list_open_assigned_issues",
        lambda self, login, progress_callback=None: [
            {
                "id": f"ISSUE-{issue_number}",
                "repo": "owner/repo",
                "number": issue_number,
                "title": "The issue the pull request closes",
                "url": f"https://github.com/owner/repo/issues/{issue_number}",
                "created_at": "2026-04-01T10:00:00Z",
                "updated_at": "2026-04-27T10:00:00Z",
            }
        ],
    )
    # The linked issue is discovered by the real GraphQL batch, so the query and
    # its per-alias parsing run for this scenario.
    monkeypatch.setattr(
        "workdash.github_client.subprocess.run",
        lambda command, **kwargs: (
            linked_issues_response(
                command,
                closed_issues={f"owner/repo#{pr_number}": (("owner/repo", issue_number),)},
            )
            if is_linked_issues_query(command)
            else (_ for _ in ()).throw(AssertionError(f"Unexpected gh command: {command}"))
        ),
    )


@when("the user returns to the dashboard")
def _return_to_closing_pr(scenario_state: dict[str, Any], tmp_path: Path) -> None:
    items, markers = _make_backend(tmp_path).load_items()

    async def interactions(app, pilot):
        table = app.query_one("#work-items", DataTable)
        scenario_state["returning_labels"] = [
            type_column_label(table.get_row_at(i)[0]) for i in range(table.row_count)
        ]

    run_app(work_items=items, suggestion_markers=markers, interactions=interactions)


@then("the pull request appears on the dashboard")
def _the_pull_request_appears(scenario_state: dict[str, Any]) -> None:
    assert f"PR {scenario_state['closing_pr_number']}" in scenario_state["returning_labels"]


@then("the issue it closes does not appear on the dashboard")
def _the_closed_issue_does_not_appear(scenario_state: dict[str, Any]) -> None:
    assert f"IS {scenario_state['closed_issue_number']}" not in scenario_state["returning_labels"]


@given("the user has open work items")
def _user_has_open_items(work_items: list[WorkItem]) -> None:
    if not work_items:
        work_items.extend(
            [
                make_work_item(
                    number=1,
                    title="First item",
                    updated_at=NOW_UTC - timedelta(days=2),
                    created_at=NOW_UTC - timedelta(days=8),
                ),
                make_work_item(
                    number=2,
                    title="Second item",
                    updated_at=NOW_UTC - timedelta(days=1),
                    created_at=NOW_UTC - timedelta(days=5),
                ),
            ]
        )


@given("the user has open work items and a suggested item exists")
def _user_has_items_with_suggestion(work_items: list[WorkItem]) -> None:
    work_items.extend(
        [
            make_work_item(
                item_type=WorkItemType.ISSUE,
                kind=WorkItemKind.TRACKED_ISSUE,
                number=77,
                title="Oldest suggestion target",
                created_at=NOW_UTC - timedelta(days=30),
                updated_at=NOW_UTC - timedelta(days=1),
            ),
            make_work_item(
                item_type=WorkItemType.PR,
                kind=WorkItemKind.TRACKED_PR,
                number=78,
                title="Fresh PR",
                created_at=NOW_UTC - timedelta(days=2),
                updated_at=NOW_UTC - timedelta(days=0),
            ),
        ]
    )


@given("the dashboard has issue, pull request, and review work items")
def _dashboard_has_item_types(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    work_items[:] = [
        make_work_item(
            item_type=WorkItemType.ISSUE,
            kind=WorkItemKind.ASSIGNED_ISSUE,
            number=1,
            title="Issue",
            created_at=NOW_UTC,
            updated_at=NOW_UTC,
        ),
        make_work_item(
            item_type=WorkItemType.PR,
            kind=WorkItemKind.AUTHORED_PR,
            number=2,
            title="Pull request",
            created_at=NOW_UTC,
            updated_at=NOW_UTC,
        ),
        make_work_item(
            item_type=WorkItemType.PR,
            kind=WorkItemKind.REVIEW_REQUESTED_PR,
            number=3,
            title="Review",
            created_at=NOW_UTC,
            updated_at=NOW_UTC,
        ),
    ]
    scenario_state["work_items"] = list(work_items)


@given("the dashboard has work items")
def _dashboard_has_work_items(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    _dashboard_has_item_types(scenario_state, work_items)


@given(parsers.parse('a new issue titled "{title}" is available on GitHub'))
def _new_github_issue(
    title: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    scenario_state["refreshed_items"] = [
        *work_items,
        make_work_item(number=99, title=title, created_at=NOW_UTC, updated_at=NOW_UTC),
    ]


@when(parsers.re(r"the user runs `workdash (?P<command>list(?: --refresh)?)`"))
def _list_work_items(
    command: str,
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.workdash as workdash_module

    session = scenario_state["api_session"]

    def request_list(endpoint, payload):
        assert endpoint == "list"
        return session.list_items(refresh=payload["refresh"])

    monkeypatch.setattr(
        workdash_module, "WorkdashControlClient", lambda _host: MagicMock(request=request_list)
    )
    assert workdash_module.main(command.split()) == 0
    scenario_state["print_output"] = capsys.readouterr().out


@given("the dashboard is running on the custom address set in `WORKDASH_HOST`")
def _dashboard_on_custom_address(scenario_state, work_items, tmp_path, monkeypatch, request):
    import socket

    from workdash.control import WorkdashControlServer

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    seed_dashboard_item(work_items, scenario_state)
    server = WorkdashControlServer(ensure_api_session(scenario_state, work_items, tmp_path))
    server.start()
    request.addfinalizer(server.stop)


@when("the user runs `workdash list` from another terminal")
def _list_work_from_custom_address(scenario_state, capsys):
    from workdash.workdash import main

    assert main(["list"]) == 0
    scenario_state["print_output"] = capsys.readouterr().out


@then(parsers.parse('the printed list includes "{title}" with the ID `{item_id}`'))
def _printed_list_includes(title: str, item_id: str, scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["print_output"].splitlines()
    assert any(item_id in line.split() and line.endswith(title) for line in lines), lines


# --------------------------------------------------------------------------
# F-TRIAGE-REFRESH
# --------------------------------------------------------------------------


@given("the TUI is open with a list of work items")
def _tui_open_with_items(work_items: list[WorkItem]) -> None:
    if not work_items:
        work_items.extend(
            [
                make_work_item(
                    number=1,
                    title="Preexisting one",
                    updated_at=NOW_UTC - timedelta(days=2),
                    created_at=NOW_UTC - timedelta(days=5),
                ),
                make_work_item(
                    number=2,
                    title="Fix the parser",
                    updated_at=NOW_UTC - timedelta(days=3),
                    created_at=NOW_UTC - timedelta(days=6),
                ),
                make_work_item(
                    number=33,
                    title="Polish the docs",
                    updated_at=NOW_UTC - timedelta(days=4),
                    created_at=NOW_UTC - timedelta(days=7),
                ),
            ]
        )


@when(parsers.parse('the user presses "{key}"'))
def _user_presses_key(
    key: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
) -> None:
    # Dispatch on the key to the correct domain-specific handler.
    if key == "r":
        _run_refresh_scenario(scenario_state, work_items, tmp_path)
    elif key == "/":
        _run_search_clear_scenario(scenario_state, work_items)
    else:
        raise AssertionError(f"Unhandled key press in BDD step: {key!r}")


def _run_refresh_scenario(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    expected_status = f"Refreshed {len(scenario_state['refreshed_items'])} item(s)."

    async def interactions(app, pilot) -> None:
        footer = app.query_one("#status-footer", Static)
        await pilot.press("r")
        for _ in range(20):
            if footer.render().plain == expected_status:
                break
            await pilot.pause()
        table = app.query_one("#work-items", DataTable)
        scenario_state["refresh_status"] = footer.render().plain
        scenario_state["refresh_titles"] = [
            str(table.get_row_at(i)[2]).removeprefix("* ") for i in range(table.row_count)
        ]

    run_app(
        work_items=session.work_items,
        session=session,
        refresh_callback=lambda: (
            session.list_items(refresh=True) and (session.work_items, session.suggestion_markers)
        ),
        interactions=interactions,
    )


@then(parsers.parse('the dashboard lists "{title}"'))
def _dashboard_lists_refreshed_title(
    title: str, scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    assert sorted(scenario_state["refresh_titles"]) == sorted(
        [title, *(item.title for item in work_items)]
    )


@then("the system reports how many work items are now shown")
def _reports_count(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    assert scenario_state["refresh_status"] == f"Refreshed {len(work_items) + 1} item(s)."


# --------------------------------------------------------------------------
# F-TRIAGE-SUGGESTED
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# F-TRIAGE-INCLUDE
# --------------------------------------------------------------------------


def _install_empty_github_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every non-include GitHub client method to return empty lists."""

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
    monkeypatch.setattr(
        GitHubClient,
        "list_open_todo_issues",
        lambda self, todo_repository, progress_callback=None: [],
    )


def _make_backend(tmp_path: Path) -> WorkdashBackend:
    config = WorkdashConfig(
        github_username="testuser",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(launch="pi"),
        },
        repositories=("owner/repo",),
        workdir=str(tmp_path / "wrk"),
        todo_repository="testuser/todos",
    )
    return WorkdashBackend(
        cache_root=tmp_path / "cache",
        config=config,
        included_items_store=IncludedItemsStore(tmp_path / "included.json"),
    )


_INCLUDE_PR_URL = "https://github.com/owner/other/pull/111"
_INCLUDE_PR_LABEL = "VR+ 111"


@pytest.fixture
def include_backend(monkeypatch, tmp_path):
    _install_empty_github_fakes(monkeypatch)
    monkeypatch.setattr(GitHubClient, "fetch_linked_issues", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        GitHubClient, "fetch_linked_issues_for_pull_request", lambda *args, **kwargs: []
    )
    payload = {
        "number": 111,
        "title": "Off-dashboard pull request",
        "url": _INCLUDE_PR_URL,
        "createdAt": "2026-04-20T10:00:00Z",
        "updatedAt": "2026-04-28T10:00:00Z",
        "state": "OPEN",
    }

    def run(command, **kwargs):
        assert command[:6] == ["gh", "pr", "view", "111", "--repo", "owner/other"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    return _make_backend(tmp_path), payload


@given("the dashboard is filtered to an unrelated issue")
def _seed_dashboard_issue(work_items):
    work_items.extend(
        [
            make_work_item(title="Fix the parser", updated_at=NOW_UTC),
            make_work_item(number=2, title="Polish the docs"),
        ]
    )


@given("the dashboard shows a pull request below the selected issue")
def _seed_listed_pull_request(work_items):
    work_items.append(make_work_item(title="Fix the parser", updated_at=NOW_UTC))
    work_items.append(
        make_work_item(
            item_type=WorkItemType.PR,
            kind=WorkItemKind.TRACKED_PR,
            repo="owner/other",
            number=111,
            title="Listed pull request",
            url=_INCLUDE_PR_URL,
        )
    )


@when('the user presses "i", pastes the off-dashboard pull request\'s URL, and confirms')
@when('the user presses "i", pastes the listed pull request\'s URL, and confirms')
def _submit_include(work_items, scenario_state, include_backend):
    backend, _ = include_backend
    already_listed = any(item.url == _INCLUDE_PR_URL for item in work_items)

    async def interactions(app, pilot):
        table = app.query_one("#work-items", DataTable)
        if not already_listed:
            await _apply_search_filter(app, pilot, "parser")
            assert table.row_count == 1
        assert str(table.get_row_at(table.cursor_row)[2]).removeprefix("* ") == "Fix the parser"
        await pilot.press("i")
        assert isinstance(app.screen, IncludeDialog)
        app.screen.query_one("#include-url", Input).value = _INCLUDE_PR_URL
        await pilot.press("enter")
        footer = app.query_one("#status-footer", Static)
        for _ in range(100):
            if footer.render().plain.startswith(("Included ", "Already tracking ")):
                break
            await pilot.pause(0.01)
        else:
            raise AssertionError(footer.render().plain)
        selected = table.get_row_at(table.cursor_row)
        scenario_state["selected_include_label"] = type_column_label(selected[0])
        scenario_state["selected_include_title"] = str(selected[2]).removeprefix("* ")

    run_app(
        work_items=work_items,
        include_callback=backend.include_item_by_url,
        interactions=interactions,
    )


@then("the dashboard selects the pull request and marks it as included")
def _included_pr_selected(scenario_state):
    assert scenario_state["selected_include_label"] == _INCLUDE_PR_LABEL
    assert scenario_state["selected_include_title"] == "Off-dashboard pull request"


@then("the dashboard selects the listed pull request")
def _listed_pr_selected(scenario_state):
    assert scenario_state["selected_include_title"] == "Listed pull request"


@given("the user included an off-dashboard pull request in a previous dashboard session")
def _previous_inclusion(include_backend):
    backend, _ = include_backend
    assert backend.include_item_by_url(_INCLUDE_PR_URL, set()).fetched_item is not None


@when("the user reopens the dashboard")
def _reopen_included_work(scenario_state, tmp_path):
    items, markers = _make_backend(tmp_path).load_items()

    async def interactions(app, pilot):
        table = app.query_one("#work-items", DataTable)
        scenario_state["reopened_labels"] = [
            type_column_label(table.get_row_at(i)[0]) for i in range(table.row_count)
        ]

    run_app(work_items=items, suggestion_markers=markers, interactions=interactions)


@then("the included pull request is still listed")
def _included_work_reappears(scenario_state):
    assert _INCLUDE_PR_LABEL in scenario_state["reopened_labels"]


@given("the dashboard shows an included pull request that has since closed on GitHub")
def _close_included_pr(scenario_state, include_backend):
    backend, payload = include_backend
    _previous_inclusion(include_backend)
    scenario_state["before_close_items"], markers = backend.load_items()
    scenario_state["before_close_markers"] = markers
    payload["state"] = "CLOSED"


@when('the user presses "r" to refresh the dashboard')
def _refresh_closed_included_work(scenario_state, include_backend):
    backend, _ = include_backend

    async def interactions(app, pilot):
        table = app.query_one("#work-items", DataTable)
        assert type_column_label(table.get_row_at(0)[0]) == _INCLUDE_PR_LABEL
        await pilot.press("r")
        footer = app.query_one("#status-footer", Static)
        for _ in range(100):
            if footer.render().plain == "Refreshed 0 item(s).":
                break
            await pilot.pause(0.01)
        else:
            raise AssertionError(footer.render().plain)
        scenario_state["refreshed_labels"] = [
            type_column_label(table.get_row_at(i)[0]) for i in range(table.row_count)
        ]

    run_app(
        work_items=scenario_state["before_close_items"],
        suggestion_markers=scenario_state["before_close_markers"],
        refresh_callback=backend.load_items,
        interactions=interactions,
    )


@then("the closed pull request is no longer listed")
def _closed_included_work_disappears(scenario_state):
    assert not any(label.endswith(" 111") for label in scenario_state["refreshed_labels"])


# --------------------------------------------------------------------------
# F-TRIAGE-SEARCH
# --------------------------------------------------------------------------

_SEARCH_TITLE_FRAGMENT = "parser"


async def _apply_search_filter(app, pilot, query: str) -> None:
    """Press "/" and submit ``query`` through the real search dialog."""

    await pilot.press("/")
    dialog = app.screen
    assert isinstance(dialog, SearchDialog)
    dialog.query_one("#search-text", Input).value = query
    await pilot.press("enter")


def _capture_listing(app, captured: dict[str, Any]) -> None:
    table = app.query_one("#work-items", DataTable)
    captured["search_rows"] = [
        [str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)
    ]
    captured["search_status"] = app.query_one("#status-footer", Static).render().plain
    captured["search_modal_names"] = modal_screen_names(app)


def _listed_titles(scenario_state: dict[str, Any]) -> list[str]:
    return sorted(row[2].lstrip("* ") for row in scenario_state["search_rows"])


def _run_search_scenario(
    scenario_state: dict[str, Any], work_items: list[WorkItem], query: str
) -> None:
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await _apply_search_filter(app, pilot, query)
        _capture_listing(app, captured)

    run_app(work_items=list(work_items), interactions=interactions)
    scenario_state["search_query"] = query
    scenario_state["loaded_items"] = list(work_items)
    scenario_state.update(captured)


def _run_search_clear_scenario(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await _apply_search_filter(app, pilot, scenario_state["search_query"])
        table = app.query_one("#work-items", DataTable)
        assert table.row_count == 0
        await pilot.press("/")
        _capture_listing(app, captured)

    run_app(work_items=list(work_items), interactions=interactions)
    scenario_state["loaded_items"] = list(work_items)
    scenario_state.update(captured)


# ----- F-TRIAGE-SEARCH: Given steps -----


@given("the dashboard has no matches for the active search")
def _tui_open_with_no_search_matches(
    work_items: list[WorkItem], scenario_state: dict[str, Any]
) -> None:
    _tui_open_with_items(work_items)
    scenario_state["search_query"] = "nothing-here-matches-this"


@when('the user presses "/" and searches for part of an item\'s title')
def _search_for_title_fragment(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    _run_search_scenario(scenario_state, work_items, _SEARCH_TITLE_FRAGMENT)


@then("only the work items whose title contains that fragment are listed")
def _only_matching_titles_listed(scenario_state: dict[str, Any]) -> None:
    fragment = scenario_state["search_query"].lower()
    expected = sorted(
        item.title for item in scenario_state["loaded_items"] if fragment in item.title.lower()
    )
    assert expected, scenario_state["loaded_items"]
    assert len(expected) < len(scenario_state["loaded_items"]), "fixture cannot prove filtering"
    assert _listed_titles(scenario_state) == expected


@then("the system reports how many of the loaded work items matched")
def _reports_matched_count(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    # The expected count comes from the seeded items and the query, never from the
    # rendered rows, so the report is proved even if the list were not filtered.
    fragment = scenario_state["search_query"].lower()
    matched = sum(1 for item in work_items if fragment in item.title.lower())
    assert 0 < matched < len(work_items), "fixture cannot prove the reported count"
    assert scenario_state["search_status"] == (
        f"Search matched {matched} of {len(work_items)} item(s)."
    ), scenario_state["search_status"]


@then("no search box is shown")
def _no_search_box_shown(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["search_modal_names"] == [], scenario_state["search_modal_names"]


@then("all loaded work items are listed again")
def _all_loaded_items_listed_again(
    scenario_state: dict[str, Any], work_items: list[WorkItem]
) -> None:
    expected = sorted(item.title for item in work_items)
    assert len(expected) > 1, "fixture cannot prove the filter was cleared"
    assert _listed_titles(scenario_state) == expected


@given("the dashboard shows a leaf issue being worked on by autodispatch")
def _autodispatch_rows(work_items: list[WorkItem]) -> None:
    work_items.append(make_work_item(number=2, title="Leaf"))


@when("autodispatch stops working on the leaf issue after its title changes")
def _autodispatch_stops(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    from dataclasses import replace

    backend = MagicMock()
    updated = replace(work_items[0], title="Finished leaf", state="CLOSED")
    backend.github_client.fetch_item.return_value = updated
    backend.github_client.fetch_item_by_url.return_value = None
    session = WorkdashSession(
        config=api_config(Path("/tmp")),
        backend=backend,
        work_items=work_items,
        suggestion_markers={},
        zellij_session=None,
    )
    session.prepare_autodispatch = MagicMock()
    session.dispatch_once = MagicMock(
        return_value={
            "status": "launched",
            "item_id": "owner/repo#ISSUE-2",
            "pane_id": "terminal_1",
            "session": "test",
        }
    )

    async def interactions(app, pilot):
        entry = session.start_autodispatch(scope="owner/repo#ISSUE-1")
        table = app.query_one("#work-items", DataTable)
        for _ in range(150):
            await pilot.pause(0.02)
            if "magenta" in str(getattr(table.get_row("issue:owner/repo#2")[2], "style", "")):
                break
        assert "magenta" in table.get_row("issue:owner/repo#2")[2].style
        session.stop_autodispatch(str(entry["id"]))
        for _ in range(150):
            await pilot.pause(0.02)
            row = table.get_row("issue:owner/repo#2")
            if "Finished leaf" in str(row[2]) and "magenta" not in str(row[2].style):
                break
        cell = table.get_row("issue:owner/repo#2")[2]
        scenario_state["updated_title"] = str(cell)
        scenario_state["working_highlight"] = "magenta" in cell.style

    run_app(work_items=work_items, interactions=interactions, session=session)


@then("the leaf issue shows its updated title and closed status without the working highlight")
def _autodispatch_updated(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["updated_title"] == "Finished leaf (closed)"
    assert not scenario_state["working_highlight"]
