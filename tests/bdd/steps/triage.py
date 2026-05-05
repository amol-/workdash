"""Step definitions for the triage domain feature files.

Covers list-work-items, print-mode, recent-activity, refresh and
suggested-item feature scenarios. Each step exercises real
``WorkdashApp``/``WorkdashBackend`` code — only the external GitHub and
xdg-open boundaries are faked.
"""

from __future__ import annotations

import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from rich.text import Text
from textual.widgets import DataTable, Static

from workdash.backend import compute_suggestion_markers
from workdash.config import WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType

from .common import (
    NOW_UTC,
    install_config,
    install_valid_env,
    make_work_item,
    mock_backend,
    run_app,
)

# --------------------------------------------------------------------------
# F-TRIAGE-LIST
# --------------------------------------------------------------------------


@given("the user has open work across all supported sources")
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


@then("authored pull requests appear as PR items")
def _authored_prs_are_pr(scenario_state: dict[str, Any]) -> None:
    items = scenario_state["work_items"]
    authored = [i for i in items if i.kind == WorkItemKind.AUTHORED_PR]
    assert authored, "No authored PR items present"
    for item in authored:
        assert item.item_type == WorkItemType.PR


@then("pull requests requiring the user's review appear as REVIEW items")
def _review_prs_render_as_review(scenario_state: dict[str, Any]) -> None:
    items = scenario_state["work_items"]
    review_items = [i for i in items if i.kind == WorkItemKind.REVIEW_REQUESTED_PR]
    assert review_items, "No review-requested PR items present"
    # The REVIEW label is applied by the TUI/print-mode layer — verify both.
    import contextlib
    import io

    from workdash.workdash import _print_work_items

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_work_items(items, compute_suggestion_markers(items))
    output = buffer.getvalue()
    for review_item in review_items:
        assert any(
            line.startswith("REVIEW") and f"#{review_item.number}" in line
            for line in output.splitlines()
        ), f"REVIEW label missing for PR #{review_item.number}: {output}"


@then("issues assigned to the user appear as ISSUE items")
def _assigned_issues_are_issue(scenario_state: dict[str, Any]) -> None:
    assigned = [i for i in scenario_state["work_items"] if i.kind == WorkItemKind.ASSIGNED_ISSUE]
    assert assigned, "No assigned issue present"
    for item in assigned:
        assert item.item_type == WorkItemType.ISSUE


@then("other open issues and pull requests in tracked repositories appear as ISSUE or PR items")
def _tracked_items_carry_expected_type(scenario_state: dict[str, Any]) -> None:
    tracked = [
        i
        for i in scenario_state["work_items"]
        if i.kind in {WorkItemKind.TRACKED_ISSUE, WorkItemKind.TRACKED_PR}
    ]
    assert tracked, "No tracked items present"
    for item in tracked:
        expected = (
            WorkItemType.ISSUE if item.kind == WorkItemKind.TRACKED_ISSUE else WorkItemType.PR
        )
        assert item.item_type == expected


# -- S002: direct vs team review requests ---------------------------------


@given("a pull request has requested only a team the user belongs to")
def _pr_team_only_request(scenario_state: dict[str, Any]) -> None:
    scenario_state["team_only_pr_number"] = 201


@given("a separate pull request has requested the user directly")
def _pr_direct_request(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    scenario_state["direct_pr_number"] = 202
    # Run the github_client against patched subprocess so only the direct
    # review request survives the team-only filter.
    from workdash import github_client as gc

    team_only_search = [
        {
            "id": "PR-TEAM",
            "number": 201,
            "title": "Team only review request",
            "url": "https://github.com/owner/repo/pull/201",
            "createdAt": "2026-04-20T00:00:00Z",
            "updatedAt": "2026-04-21T00:00:00Z",
            "isDraft": False,
            "repository": {"nameWithOwner": "owner/repo"},
        },
        {
            "id": "PR-DIRECT",
            "number": 202,
            "title": "Direct review request",
            "url": "https://github.com/owner/repo/pull/202",
            "createdAt": "2026-04-22T00:00:00Z",
            "updatedAt": "2026-04-23T00:00:00Z",
            "isDraft": False,
            "repository": {"nameWithOwner": "owner/repo"},
        },
    ]
    team_only_review_requests = {
        "reviewRequests": [{"__typename": "Team", "name": "team-reviewers"}]
    }
    direct_review_requests = {"reviewRequests": [{"__typename": "User", "login": "testuser"}]}

    def fake_run(command, **kwargs):
        import json

        if command[:3] == ["gh", "search", "prs"] and "--review-requested" in command:
            return subprocess.CompletedProcess(
                command, 0, stdout=json.dumps(team_only_search), stderr=""
            )
        if command[:3] == ["gh", "pr", "view"]:
            number = command[3]
            payload = team_only_review_requests if number == "201" else direct_review_requests
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"Unexpected gh command in scenario: {command}")

    scenario_state["_subprocess_patch"] = fake_run
    scenario_state["_github_client"] = gc.GitHubClient()


@then("the team-only pull request does not appear as a REVIEW item")
def _team_only_filtered_out(scenario_state: dict[str, Any]) -> None:
    reviewed = scenario_state["reviewed_prs"]
    assert all(item["number"] != scenario_state["team_only_pr_number"] for item in reviewed)


@then("the directly requested pull request appears as a REVIEW item")
def _direct_pr_present(scenario_state: dict[str, Any]) -> None:
    reviewed = scenario_state["reviewed_prs"]
    assert any(item["number"] == scenario_state["direct_pr_number"] for item in reviewed), reviewed


# -- S003: sort by most recently updated ----------------------------------


@given("the user has several open work items with different last update times")
def _several_items_different_updates(work_items: list[WorkItem]) -> None:
    work_items.extend(
        [
            make_work_item(
                number=1,
                title="Oldest update",
                updated_at=NOW_UTC - timedelta(days=7),
                created_at=NOW_UTC - timedelta(days=10),
            ),
            make_work_item(
                number=2,
                title="Middle update",
                updated_at=NOW_UTC - timedelta(days=3),
                created_at=NOW_UTC - timedelta(days=9),
            ),
            make_work_item(
                number=3,
                title="Latest update",
                updated_at=NOW_UTC - timedelta(days=1),
                created_at=NOW_UTC - timedelta(days=8),
            ),
        ]
    )


@then("the most recently updated item is listed first")
def _most_recent_first(work_items: list[WorkItem]) -> None:
    _ensure_sorted_matches_expected(work_items)


@then("older items follow in decreasing order of last update")
def _older_items_descending(work_items: list[WorkItem]) -> None:
    _ensure_sorted_matches_expected(work_items)


def _ensure_sorted_matches_expected(work_items: list[WorkItem]) -> None:
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        table = app.query_one("#work-items", DataTable)
        captured["rows"] = [
            [str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)
        ]

    run_app(work_items=list(work_items), interactions=interactions)
    visible_titles = [row[2].lstrip("* ") for row in captured["rows"]]
    expected = [item.title for item in sorted(work_items, key=lambda i: i.updated_at, reverse=True)]
    assert visible_titles == expected


# -- S004: dedupe authored vs tracked -------------------------------------


@given("a pull request the user authored also lives in a tracked repository")
def _authored_also_tracked(scenario_state: dict[str, Any]) -> None:
    from workdash.github_client import (
        merge_normalized_work_items,
        normalize_authored_pull_requests,
        normalize_recent_tracked_items,
    )

    authored_raw = [
        {
            "id": "PR-DUPLICATE",
            "repo": "owner/repo",
            "number": 77,
            "title": "Ship feature",
            "url": "https://github.com/owner/repo/pull/77",
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-20T00:00:00Z",
            "is_draft": False,
        }
    ]
    tracked_raw = [
        {
            "id": "TRACKED-DUP",
            "repo": "owner/repo",
            "number": 77,
            "title": "Ship feature (tracked view)",
            "url": "https://github.com/owner/repo/pull/77",
            "created_at": "2026-04-01T00:00:00Z",
            "updated_at": "2026-04-20T00:00:00Z",
            "is_pull_request": True,
        }
    ]
    merged = merge_normalized_work_items(
        normalize_authored_pull_requests(authored_raw),
        normalize_recent_tracked_items(tracked_raw),
    )
    scenario_state["merged_items"] = merged
    scenario_state["_dedupe_scenario"] = True


@then("the pull request appears exactly once")
def _pr_appears_once(scenario_state: dict[str, Any]) -> None:
    merged = scenario_state["merged_items"]
    matches = [i for i in merged if i.repo == "owner/repo" and i.number == 77]
    assert len(matches) == 1, matches


@then("it is classified as an authored pull request")
def _pr_classified_authored(scenario_state: dict[str, Any]) -> None:
    merged = scenario_state["merged_items"]
    match = next(i for i in merged if i.repo == "owner/repo" and i.number == 77)
    assert match.kind == WorkItemKind.AUTHORED_PR


# -- S005 / empty result listing ------------------------------------------


@given("the user has no open work items matching any source")
def _no_work_items_for_triage(work_items: list[WorkItem]) -> None:
    work_items.clear()


@given("the user has no open work items")
def _no_work_items(work_items: list[WorkItem]) -> None:
    work_items.clear()


@then("the system reports that no work items were found")
def _system_reports_empty_from_open(scenario_state: dict[str, Any], capsys) -> None:
    # When invoked via the triage S005 flow we run the CLI --print path once
    # here so the assertion can consume stdout. If we already captured output
    # via a prior print-mode step we reuse it.
    if "print_output" in scenario_state:
        assert "No work items found." in scenario_state["print_output"]
        return
    from workdash.workdash import _print_work_items

    _print_work_items(list(scenario_state.get("work_items", [])), {})
    assert "No work items found." in capsys.readouterr().out


# --------------------------------------------------------------------------
# F-TRIAGE-PRINT
# --------------------------------------------------------------------------


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


@when(parsers.parse('the user runs the system with "{flag}"'))
def _run_system_with_flag(
    flag: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid_config: WorkdashConfig,
    tmp_path: Path,
) -> None:
    import workdash.workdash as workdash_module

    if flag == "--configure":
        # Delegate to the setup helper so wizard inputs and detection
        # fakes run against real workdash.config.configure().
        from . import setup as setup_mod

        config_path = scenario_state.get("config_path") or (tmp_path / "config.json")
        scenario_state["config_path"] = config_path
        setup_mod._run_configure_with_fakes(scenario_state, config_path, monkeypatch, capsys)
        return

    install_valid_env(monkeypatch, which_succeeds=True)
    install_config(monkeypatch, valid_config)
    mock_backend(monkeypatch, items=list(work_items))
    # Assert TUI is never constructed in print-mode scenarios.

    class UnreachableApp:
        def __init__(self, **kwargs) -> None:  # pragma: no cover - sanity
            raise AssertionError("TUI should not be constructed for print mode")

        def run(self) -> None:  # pragma: no cover - sanity
            raise AssertionError("TUI should not run for print mode")

    if flag == "--print":
        monkeypatch.setattr(workdash_module, "WorkdashApp", UnreachableApp)

    exit_code = workdash_module.main([flag])
    scenario_state["exit_code"] = exit_code
    scenario_state["print_output"] = capsys.readouterr().out


@then("the system emits one line per work item to standard output")
def _print_one_line_per_item(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    output_lines = [line for line in scenario_state["print_output"].splitlines() if line.strip()]
    assert len(output_lines) == len(work_items), (
        scenario_state["print_output"],
        work_items,
    )


@then("the TUI is not started")
def _tui_not_started(scenario_state: dict[str, Any]) -> None:
    # UnreachableApp raises if instantiated — reaching this step means no TUI.
    assert "print_output" in scenario_state


@then('the suggested item\'s line has its title prefixed with "* "')
def _suggested_item_prefix(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    markers = compute_suggestion_markers(list(work_items))
    assert markers, "Expected a suggestion marker for this scenario"
    (_item_type, repo, number), _ = next(iter(markers.items()))
    suggested_item = next(
        item for item in work_items if item.repo == repo and item.number == number
    )
    marker_line = next(
        line
        for line in scenario_state["print_output"].splitlines()
        if f"* {suggested_item.title}" in line
    )
    assert marker_line  # present


@then("the system prints that no work items were found")
def _system_prints_empty(scenario_state: dict[str, Any]) -> None:
    assert "No work items found." in scenario_state["print_output"]


# --------------------------------------------------------------------------
# F-TRIAGE-RECENT
# --------------------------------------------------------------------------


@given("a work item was last updated within the last 24 hours")
def _item_updated_recently(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    item = make_work_item(
        number=100,
        title="Fresh activity",
        updated_at=NOW_UTC - timedelta(hours=2),
        created_at=NOW_UTC - timedelta(days=3),
    )
    work_items.append(item)
    scenario_state["target_item"] = item


@given("a work item was last updated more than 24 hours ago")
def _item_updated_long_ago(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    item = make_work_item(
        number=101,
        title="Stale activity",
        updated_at=NOW_UTC - timedelta(days=5),
        created_at=NOW_UTC - timedelta(days=10),
    )
    work_items.append(item)
    scenario_state["target_item"] = item


@then("that work item is rendered in bold")
def _rendered_bold(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    captured = _capture_rendered_row(work_items, scenario_state["target_item"])
    for cell in captured:
        assert isinstance(cell, Text), f"expected bold Text, got {type(cell)}: {cell}"
        assert "bold" in str(cell.style)


@then("that work item is rendered in the normal weight")
def _rendered_normal(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    captured = _capture_rendered_row(work_items, scenario_state["target_item"])
    for cell in captured:
        # Non-bold rows use plain str cells in the DataTable
        assert not isinstance(cell, Text), f"expected plain str, got Text: {cell}"


def _capture_rendered_row(work_items: list[WorkItem], target: WorkItem) -> list:
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        table = app.query_one("#work-items", DataTable)
        for index in range(table.row_count):
            # get_row_at returns the raw values (Text or str) used in the cell
            row = table.get_row_at(index)
            title_cell = row[2]
            title_plain = title_cell.plain if isinstance(title_cell, Text) else str(title_cell)
            if target.title in title_plain:
                captured["row"] = row
                return

    run_app(work_items=list(work_items), interactions=interactions)
    assert "row" in captured, "target item not rendered in table"
    return captured["row"]


# --------------------------------------------------------------------------
# F-TRIAGE-REFRESH
# --------------------------------------------------------------------------


@given("the TUI is open with a list of work items")
def _tui_open_with_items(work_items: list[WorkItem], scenario_state: dict[str, Any]) -> None:
    if not work_items:
        work_items.extend(
            [
                make_work_item(
                    number=1,
                    title="Preexisting one",
                    updated_at=NOW_UTC - timedelta(days=2),
                    created_at=NOW_UTC - timedelta(days=5),
                )
            ]
        )
    scenario_state["initial_items"] = list(work_items)


@given("the next refresh will fail")
def _refresh_will_fail(scenario_state: dict[str, Any]) -> None:
    scenario_state["refresh_fails"] = True


@when(parsers.parse('the user presses "{key}"'))
def _user_presses_key(
    key: str,
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dispatch on the key to the correct domain-specific handler.
    if key == "r":
        _run_refresh_scenario(scenario_state, work_items)
    elif key == "a":
        from . import analysis

        analysis.run_analyze_dialog_scenario(scenario_state, work_items)
    elif key == "o":
        from . import browse

        browse.run_open_scenario(scenario_state, work_items, monkeypatch)
    elif key == "c":
        from . import coding

        coding.run_code_dialog_scenario(scenario_state, work_items, monkeypatch, tmp_path)
    elif key == "t":
        from . import terminal as terminal_mod

        terminal_mod.run_terminal_scenario(scenario_state, work_items, monkeypatch, tmp_path)
    else:
        raise AssertionError(f"Unhandled key press in BDD step: {key!r}")


def _run_refresh_scenario(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    refresh_will_fail = scenario_state.get("refresh_fails", False)
    refreshed_items = [
        make_work_item(
            number=99,
            title="Newly refreshed",
            updated_at=NOW_UTC - timedelta(hours=1),
            created_at=NOW_UTC - timedelta(days=2),
        )
    ]

    def refresh_callback():
        if refresh_will_fail:
            raise RuntimeError("simulated refresh failure")
        return refreshed_items, compute_suggestion_markers(refreshed_items)

    captured: dict[str, Any] = {}
    busy_messages: list[str] = []

    async def interactions(app, pilot) -> None:
        await pilot.press("r")
        await pilot.pause()
        footer = app.query_one("#status-footer", Static)
        table = app.query_one("#work-items", DataTable)
        captured["status"] = footer.render().plain
        captured["rows"] = [
            [str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)
        ]

    run_app(
        work_items=list(work_items),
        refresh_callback=refresh_callback,
        interactions=interactions,
        busy_messages=busy_messages,
    )
    scenario_state["refresh_status"] = captured["status"]
    scenario_state["refresh_rows"] = captured["rows"]
    scenario_state["initial_items"] = list(work_items)
    scenario_state["refreshed_items"] = refreshed_items
    scenario_state["busy_messages"] = busy_messages


@then("the system shows that a refresh is in progress")
def _refresh_in_progress(scenario_state: dict[str, Any]) -> None:
    # Prove the TUI pushed the busy screen with the refresh label while the
    # callback was running, not just that a post-completion footer string
    # happens to contain "Refresh".
    busy_messages = scenario_state.get("busy_messages", [])
    assert "Refreshing work items..." in busy_messages, busy_messages


@then("the list updates to the latest state from GitHub")
def _list_updated(scenario_state: dict[str, Any]) -> None:
    rows = scenario_state["refresh_rows"]
    refreshed = scenario_state["refreshed_items"]
    assert len(rows) == len(refreshed)
    titles = {row[2].lstrip("* ") for row in rows}
    assert {item.title for item in refreshed} == titles


@then("the system reports how many work items are now shown")
def _reports_count(scenario_state: dict[str, Any]) -> None:
    status = scenario_state["refresh_status"]
    assert status.startswith("Refreshed") and "item" in status, status


@then("the system reports the failure to the user")
def _reports_failure(scenario_state: dict[str, Any]) -> None:
    # Shared across refresh and analysis failure scenarios; both record a
    # status message from the TUI footer that starts with "X failed:".
    status = scenario_state.get("refresh_status") or scenario_state.get("analyze_status")
    assert status is not None, "Expected either refresh_status or analyze_status to be recorded"
    assert "failed" in status.lower(), status


@then("the previously shown list remains visible")
def _previous_list_visible(scenario_state: dict[str, Any]) -> None:
    rows = scenario_state["refresh_rows"]
    initial_titles = {item.title for item in scenario_state["initial_items"]}
    rendered_titles = {row[2].lstrip("* ") for row in rows}
    assert rendered_titles == initial_titles, (rendered_titles, initial_titles)


# --------------------------------------------------------------------------
# F-TRIAGE-SUGGESTED
# --------------------------------------------------------------------------


@given("the user has several open work items with different creation dates")
def _several_items_different_creation(work_items: list[WorkItem]) -> None:
    work_items.extend(
        [
            make_work_item(
                number=1,
                title="Newest",
                created_at=NOW_UTC - timedelta(days=2),
                updated_at=NOW_UTC - timedelta(days=1),
            ),
            make_work_item(
                number=2,
                title="Middle",
                created_at=NOW_UTC - timedelta(days=10),
                updated_at=NOW_UTC - timedelta(days=1),
            ),
            make_work_item(
                number=3,
                title="Oldest creation",
                created_at=NOW_UTC - timedelta(days=30),
                updated_at=NOW_UTC - timedelta(days=1),
            ),
        ]
    )


@given("the oldest creation date is shared by a pull request and an issue")
def _tie_oldest_creation(work_items: list[WorkItem]) -> None:
    shared_creation = NOW_UTC - timedelta(days=30)
    work_items.extend(
        [
            make_work_item(
                item_type=WorkItemType.ISSUE,
                kind=WorkItemKind.TRACKED_ISSUE,
                number=1,
                title="Tied issue",
                created_at=shared_creation,
                updated_at=NOW_UTC - timedelta(days=1),
            ),
            make_work_item(
                item_type=WorkItemType.PR,
                kind=WorkItemKind.TRACKED_PR,
                number=2,
                title="Tied PR",
                created_at=shared_creation,
                updated_at=NOW_UTC - timedelta(days=1),
            ),
        ]
    )


@then("exactly one work item is marked as suggested")
def _exactly_one_suggested(scenario_state: dict[str, Any]) -> None:
    markers = scenario_state["suggestion_markers"]
    assert len(markers) == 1, markers


@then("the suggested item is the oldest by creation date")
def _suggested_is_oldest(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    markers = scenario_state["suggestion_markers"]
    (_, repo, number), _marker = next(iter(markers.items()))
    suggested = next(i for i in work_items if i.repo == repo and i.number == number)
    oldest = min(work_items, key=lambda i: i.created_at)
    assert suggested.created_at == oldest.created_at


@then("the pull request is marked as suggested")
def _pr_wins_suggestion(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    markers = scenario_state["suggestion_markers"]
    (item_type, _, _), _ = next(iter(markers.items()))
    assert item_type == WorkItemType.PR, markers


@then("no item is marked as suggested")
def _no_suggestion(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["suggestion_markers"] == {}
