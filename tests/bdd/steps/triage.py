"""Step definitions for the triage domain feature files.

Covers list-work-items, list-command, recent-activity, refresh, suggested-item,
and include-item scenarios. Each step exercises real
``WorkdashApp``/``WorkdashBackend`` code — only the external GitHub and
browser-open boundaries are faked.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from rich.text import Text
from textual.widgets import DataTable, Input, Static

from workdash.backend import WorkdashBackend, compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashSession, _work_items_payload
from workdash.github_client import GitHubClient
from workdash.included_items import IncludedItemsStore
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.tui import IncludeDialog
from workdash.workdash import _print_work_items_result, format_work_item_id

from .common import (
    NOW_UTC,
    FakeApiBackend,
    api_config,
    ensure_api_session,
    install_config,
    install_valid_env,
    make_work_item,
    mock_backend,
    modal_screen_names,
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
    # The REVIEW label is applied by the TUI/list-command layer — verify both.
    import contextlib
    import io

    from workdash.workdash import _print_work_items

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_work_items(items, compute_suggestion_markers(items))
    output = buffer.getvalue()
    for review_item in review_items:
        expected_id = f"{review_item.repo}#REVIEW-{review_item.number}"
        assert any(
            line.startswith("REVIEW") and expected_id in line for line in output.splitlines()
        ), f"REVIEW label missing for {expected_id}: {output}"


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
    # When invoked via the triage S005 flow we run the CLI list path once
    # here so the assertion can consume stdout. If we already captured output
    # via a prior list-command step we reuse it.
    if "print_output" in scenario_state:
        assert "No work items found." in scenario_state["print_output"]
        return
    from workdash.workdash import _print_work_items

    _print_work_items(list(scenario_state.get("work_items", [])), {})
    assert "No work items found." in capsys.readouterr().out


# -- S006: repository auth failures ---------------------------------------


@given("one tracked repository requires additional GitHub authorization")
def _one_tracked_repository_requires_authorization(scenario_state: dict[str, Any]) -> None:
    scenario_state["unauthorized_repository"] = "owner/private"


@given("another tracked repository has open work")
def _another_tracked_repository_has_open_work(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unauthorized_repository = scenario_state["unauthorized_repository"]
    accessible_repository = "owner/public"
    scenario_state["accessible_repository"] = accessible_repository
    saml_error = (
        "GraphQL: Resource protected by organization SAML enforcement. "
        "You must grant your OAuth token access to this organization."
    )

    monkeypatch.setattr(GitHubClient, "list_open_authored_prs", lambda self, login: [])
    monkeypatch.setattr(
        GitHubClient,
        "list_open_review_requested_prs",
        lambda self, login, progress_callback=None: [],
    )
    monkeypatch.setattr(GitHubClient, "list_open_reviewed_prs", lambda self, login: [])
    monkeypatch.setattr(GitHubClient, "list_open_assigned_issues", lambda self, login: [])

    def fake_run(command, **kwargs):
        repositories = [
            command[index + 1] for index, token in enumerate(command) if token == "--repo"
        ]
        if repositories == [unauthorized_repository, accessible_repository]:
            raise subprocess.CalledProcessError(1, command, stderr=saml_error)
        if repositories == [unauthorized_repository]:
            raise subprocess.CalledProcessError(1, command, stderr=saml_error)
        if repositories == [accessible_repository]:
            payload = [
                {
                    "id": "TRACKED-ISSUE-42",
                    "number": 42,
                    "title": "Accessible issue",
                    "url": "https://github.com/owner/public/issues/42",
                    "createdAt": "2026-04-20T00:00:00Z",
                    "updatedAt": "2026-04-21T00:00:00Z",
                    "state": "OPEN",
                    "isPullRequest": False,
                    "repository": {"nameWithOwner": accessible_repository},
                }
            ]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"Unexpected gh command in auth scenario: {command}")

    monkeypatch.setattr("workdash.github_client.subprocess.run", fake_run)

    def hook(state: dict[str, Any], items: list[WorkItem], _mp: pytest.MonkeyPatch) -> None:
        progress_messages: list[str] = []
        config = WorkdashConfig(
            github_username="testuser",
            claude=AgentConfig(analyze="claude -p", launch="claude"),
            codex=AgentConfig(analyze="codex exec", launch="codex"),
            pi=AgentConfig(launch="pi"),
            repositories=(unauthorized_repository, accessible_repository),
            workdir=str(tmp_path / "wrk"),
        )
        backend = WorkdashBackend(
            cache_root=tmp_path / "cache",
            config=config,
            included_items_store=IncludedItemsStore(tmp_path / "included.json"),
        )
        fetched, markers = backend.load_items(progress_callback=progress_messages.append)
        items[:] = list(fetched)
        state["work_items"] = list(fetched)
        state["suggestion_markers"] = dict(markers)
        state["progress_messages"] = progress_messages

    scenario_state["_open_dashboard_hook"] = hook


@then("the accessible repository's work items appear")
def _accessible_repository_items_appear(scenario_state: dict[str, Any]) -> None:
    accessible_repository = scenario_state["accessible_repository"]
    assert any(
        item.repo == accessible_repository
        and item.number == 42
        and item.kind == WorkItemKind.TRACKED_ISSUE
        for item in scenario_state["work_items"]
    )


@then("the system warns that the unauthorized repository was skipped")
def _warns_unauthorized_repository_skipped(scenario_state: dict[str, Any]) -> None:
    unauthorized_repository = scenario_state["unauthorized_repository"]
    assert any(
        "Warning: skipped repository" in message
        and unauthorized_repository in message
        and "SAML enforcement" in message
        for message in scenario_state["progress_messages"]
    )


# -- S007: review-request metadata auth failures --------------------------


@given("one review-requested pull request requires additional GitHub authorization")
def _one_review_requested_pr_requires_authorization(scenario_state: dict[str, Any]) -> None:
    scenario_state["unauthorized_review_repo"] = "protected-org/protected-repo"
    scenario_state["unauthorized_review_number"] = 860


@given("another review-requested pull request has requested the user directly")
def _another_review_requested_pr_has_direct_request(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    unauthorized_repo = scenario_state["unauthorized_review_repo"]
    unauthorized_number = scenario_state["unauthorized_review_number"]
    authorized_repo = "owner/repo"
    authorized_number = 42
    scenario_state["authorized_review_repo"] = authorized_repo
    scenario_state["authorized_review_number"] = authorized_number
    saml_error = (
        "GraphQL: Resource protected by organization SAML enforcement. "
        "You must grant your OAuth token access to this organization."
    )

    monkeypatch.setattr(GitHubClient, "list_open_authored_prs", lambda self, login: [])
    monkeypatch.setattr(GitHubClient, "list_open_reviewed_prs", lambda self, login: [])
    monkeypatch.setattr(GitHubClient, "list_open_assigned_issues", lambda self, login: [])
    monkeypatch.setattr(
        GitHubClient,
        "list_recent_tracked_items",
        lambda self, repositories, progress_callback=None: [],
    )

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "search", "prs"] and "--review-requested" in command:
            payload = [
                {
                    "id": "UNAUTHORIZED-REVIEW",
                    "number": unauthorized_number,
                    "title": "Unauthorized review request",
                    "url": f"https://github.com/{unauthorized_repo}/pull/{unauthorized_number}",
                    "createdAt": "2026-04-20T00:00:00Z",
                    "updatedAt": "2026-04-21T00:00:00Z",
                    "isDraft": False,
                    "repository": {"nameWithOwner": unauthorized_repo},
                },
                {
                    "id": "AUTHORIZED-REVIEW",
                    "number": authorized_number,
                    "title": "Authorized review request",
                    "url": f"https://github.com/{authorized_repo}/pull/{authorized_number}",
                    "createdAt": "2026-04-22T00:00:00Z",
                    "updatedAt": "2026-04-23T00:00:00Z",
                    "isDraft": False,
                    "repository": {"nameWithOwner": authorized_repo},
                },
            ]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        if command[:3] == ["gh", "pr", "view"] and command[3] == str(unauthorized_number):
            raise subprocess.CalledProcessError(1, command, stderr=saml_error)
        if command[:3] == ["gh", "pr", "view"] and command[3] == str(authorized_number):
            payload = {"reviewRequests": [{"__typename": "User", "login": "testuser"}]}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        raise AssertionError(f"Unexpected gh command in review auth scenario: {command}")

    monkeypatch.setattr("workdash.github_client.subprocess.run", fake_run)

    def hook(state: dict[str, Any], items: list[WorkItem], _mp: pytest.MonkeyPatch) -> None:
        progress_messages: list[str] = []
        config = WorkdashConfig(
            github_username="testuser",
            claude=AgentConfig(analyze="claude -p", launch="claude"),
            codex=AgentConfig(analyze="codex exec", launch="codex"),
            pi=AgentConfig(launch="pi"),
            repositories=("owner/repo",),
            workdir=str(tmp_path / "wrk"),
        )
        backend = WorkdashBackend(
            cache_root=tmp_path / "cache",
            config=config,
            included_items_store=IncludedItemsStore(tmp_path / "included.json"),
        )
        fetched, markers = backend.load_items(progress_callback=progress_messages.append)
        items[:] = list(fetched)
        state["work_items"] = list(fetched)
        state["suggestion_markers"] = dict(markers)
        state["progress_messages"] = progress_messages

    scenario_state["_open_dashboard_hook"] = hook


@then("the authorized review-requested pull request appears")
def _authorized_review_requested_pr_appears(scenario_state: dict[str, Any]) -> None:
    assert any(
        item.repo == scenario_state["authorized_review_repo"]
        and item.number == scenario_state["authorized_review_number"]
        and item.kind == WorkItemKind.REVIEW_REQUESTED_PR
        for item in scenario_state["work_items"]
    )


@then("the system warns that the unauthorized review-requested pull request was skipped")
def _warns_unauthorized_review_requested_pr_skipped(
    scenario_state: dict[str, Any],
) -> None:
    item_label = (
        f"{scenario_state['unauthorized_review_repo']}"
        f"#{scenario_state['unauthorized_review_number']}"
    )
    assert any(
        "Warning: skipped review-requested pull request" in message
        and item_label in message
        and "SAML enforcement" in message
        for message in scenario_state["progress_messages"]
    )


# --------------------------------------------------------------------------
# F-TRIAGE-LIST-COMMAND
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


@given("a server-backed Workdash session has no open work items")
def _server_session_has_no_items(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    work_items.clear()
    scenario_state["work_items"] = []
    backend = FakeApiBackend(scenario_state, tmp_path)
    session = WorkdashSession(
        config=api_config(tmp_path),
        backend=backend,  # type: ignore[arg-type]
        work_items=[],
        suggestion_markers={},
        zellij_session=scenario_state.get("zellij_session", "workdash-main"),
    )
    scenario_state["api_session"] = session
    scenario_state["api_backend"] = backend


@given("a server-backed Workdash session has issue, pull request, and review work items")
def _server_session_has_item_types(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
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
    ensure_api_session(scenario_state, work_items, tmp_path)


@given("a server-backed Workdash session has work items")
def _server_session_has_work_items(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _server_session_has_item_types(scenario_state, work_items, tmp_path)


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
    if flag == "--configure":
        # Delegate to the setup helper so wizard inputs and detection
        # fakes run against real workdash.config.configure().
        from . import setup as setup_mod

        config_path = scenario_state.get("config_path") or (tmp_path / "config.json")
        scenario_state["config_path"] = config_path
        setup_mod._run_configure_with_fakes(scenario_state, config_path, monkeypatch, capsys)
        return

    _run_list_command([flag], scenario_state, work_items, monkeypatch, capsys, valid_config)


@when("the user runs `workdash list`")
@when("the user lists work items with `workdash list`")
def _list_work_items(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid_config: WorkdashConfig,
) -> None:
    _run_list_command(["list"], scenario_state, work_items, monkeypatch, capsys, valid_config)


@when("the user lists work items with `workdash list --json`")
def _list_work_items_json(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid_config: WorkdashConfig,
) -> None:
    _run_list_command(
        ["list", "--json"], scenario_state, work_items, monkeypatch, capsys, valid_config
    )


@when("the user runs `workdash list --refresh`")
def _list_work_items_refresh(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import workdash.workdash as workdash_module

    scenario_state["github_fetches"] = 0
    scenario_state["refreshed_items"] = [
        make_work_item(
            item_type=WorkItemType.PR,
            kind=WorkItemKind.TRACKED_PR,
            number=2,
            title="Fresh item",
            created_at=NOW_UTC,
            updated_at=NOW_UTC,
        )
    ]

    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            scenario_state.setdefault("client_requests", []).append((endpoint, payload or {}))
            assert endpoint == "list"
            return scenario_state["api_session"].list_items(
                refresh=bool((payload or {}).get("refresh", False))
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    scenario_state["exit_code"] = workdash_module.main(["list", "--refresh"])
    captured = capsys.readouterr()
    scenario_state["print_output"] = captured.out
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err


def _run_list_command(
    argv: list[str],
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    valid_config: WorkdashConfig,
) -> None:
    import workdash.workdash as workdash_module

    install_valid_env(monkeypatch, which_succeeds=True)
    install_config(monkeypatch, valid_config)
    mock_backend(monkeypatch, items=list(work_items))

    class UnreachableApp:
        def __init__(self, **kwargs) -> None:  # pragma: no cover - sanity
            raise AssertionError("TUI should not be constructed for list command")

        def run(self) -> None:  # pragma: no cover - sanity
            raise AssertionError("TUI should not run for list command")

    monkeypatch.setattr(workdash_module, "WorkdashApp", UnreachableApp)

    if scenario_state.get("api_session") is not None:

        class FakeControlClient:
            def request(
                self, endpoint: str, payload: dict[str, object] | None = None
            ) -> dict[str, object]:
                payload = payload or {}
                scenario_state.setdefault("control_requests", []).append(
                    {"endpoint": endpoint, "payload": dict(payload)}
                )
                if endpoint == "list":
                    return scenario_state["api_session"].list_items(
                        refresh=bool(payload.get("refresh", False))
                    )
                raise AssertionError(f"Unexpected control endpoint: {endpoint}")

        monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    scenario_state["exit_code"] = workdash_module.main(argv)
    captured = capsys.readouterr()
    scenario_state["print_output"] = captured.out
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err


@then("the command asks the local Workdash server to refresh dashboard items")
def _command_requests_refresh(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("client_requests") == [("list", {"refresh": True})]
    assert scenario_state.get("github_fetches") == 1


@then("the system emits the refreshed work items")
def _system_emits_refreshed_items(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["print_output"]
    for item in scenario_state["refreshed_items"]:
        assert format_work_item_id(item) in output
        assert item.title in output


@then("the refreshed work items become the shared dashboard state used by the TUI and API")
def _refreshed_items_become_shared_tui_api_state(scenario_state: dict[str, Any]) -> None:
    assert [format_work_item_id(item) for item in scenario_state["api_session"].work_items] == [
        format_work_item_id(item) for item in scenario_state["refreshed_items"]
    ]
    assert scenario_state.get("tui_refresh_callbacks") == ["refresh_from_session"]
    assert [format_work_item_id(item) for item in scenario_state["tui_work_items"]] == [
        format_work_item_id(item) for item in scenario_state["refreshed_items"]
    ]


@then("the command requests the current item list from the local Workdash server")
def _command_requests_current_items(scenario_state: dict[str, Any]) -> None:
    assert scenario_state.get("control_requests") == [
        {"endpoint": "list", "payload": {"refresh": False}}
    ]


@then("the system emits one line per work item to standard output")
def _print_one_line_per_item(scenario_state: dict[str, Any]) -> None:
    output_lines = [line for line in scenario_state["print_output"].splitlines() if line.strip()]
    expected_items = scenario_state["api_session"].list_items(refresh=False)["items"]
    assert len(output_lines) == len(expected_items), (
        scenario_state["print_output"],
        expected_items,
    )
    assert "No work items found." not in scenario_state["print_output"]
    for line, item in zip(output_lines, expected_items, strict=True):
        assert item["id"] in line
        assert item["title"] in line
        assert str(item["updated_at"])[:10] in line


@then("the TUI is not started")
@then("the TUI is not started by the command")
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


@then("each row includes a Workdash item ID")
def _each_row_has_workdash_id(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    lines = [line for line in scenario_state["print_output"].splitlines() if line.strip()]
    assert len(lines) == len(scenario_state["work_items"])
    for item in scenario_state["work_items"]:
        assert any(format_work_item_id(item) in line for line in lines), lines


@then("the issue row can be copied as `owner/repo#ISSUE-1`")
def _issue_row_copy_id(scenario_state: dict[str, Any]) -> None:
    assert "owner/repo#ISSUE-1" in scenario_state["print_output"]


@then("the pull request row can be copied as `owner/repo#PR-2`")
def _pr_row_copy_id(scenario_state: dict[str, Any]) -> None:
    assert "owner/repo#PR-2" in scenario_state["print_output"]


@then("the review row can be copied as `owner/repo#REVIEW-3`")
def _review_row_copy_id(scenario_state: dict[str, Any]) -> None:
    assert "owner/repo#REVIEW-3" in scenario_state["print_output"]


@then("the system returns JSON work item records")
def _returns_json_work_items(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    payload = json.loads(scenario_state["print_output"])
    scenario_state["json_payload"] = payload
    assert isinstance(payload.get("items"), list)
    assert payload["items"]


@then(
    "each record includes the Workdash item ID, type, kind, repository, number, title, URL, "
    "timestamps, and suggested status"
)
def _json_work_items_have_contract(scenario_state: dict[str, Any]) -> None:
    required = {
        "id",
        "type",
        "kind",
        "repo",
        "number",
        "title",
        "url",
        "created_at",
        "updated_at",
        "suggested",
    }
    for record in scenario_state["json_payload"]["items"]:
        assert required <= set(record), record
        assert record["id"].startswith(f"{record['repo']}#"), record


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
    elif key == "d":
        from . import branchdiff as branchdiff_mod

        branchdiff_mod.run_branchdiff_tui_scenario(
            scenario_state,
            work_items,
            monkeypatch,
            tmp_path,
        )
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
        for _ in range(20):
            await pilot.pause()
            status = app.query_one("#status-footer", Static).render().plain
            if status.startswith("Refreshed") or "failed" in status.lower():
                break
        footer = app.query_one("#status-footer", Static)
        table = app.query_one("#work-items", DataTable)
        captured["status"] = footer.render().plain
        captured["modal_screen_names"] = modal_screen_names(app)
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
    scenario_state["modal_screen_names"] = captured["modal_screen_names"]


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
    if "refresh_status" in scenario_state:
        assert "simulated refresh failure" in status
    if "analyze_status" in scenario_state:
        assert "analysis failed" in status


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


# --------------------------------------------------------------------------
# F-TRIAGE-INCLUDE
# --------------------------------------------------------------------------

_INCLUDE_PR_URL = "https://github.com/owner/repo/pull/111"
_INCLUDE_ISSUE_URL = "https://github.com/owner/repo/issues/222"


def _fake_gh_view_response(item_type: str, number: int, state: str = "OPEN") -> str:
    kind_segment = "pull" if item_type == "pr" else "issues"
    return json.dumps(
        {
            "number": number,
            "title": f"Fetched {item_type} {number}",
            "url": f"https://github.com/owner/repo/{kind_segment}/{number}",
            "createdAt": "2026-04-20T10:00:00Z",
            "updatedAt": "2026-04-28T10:00:00Z",
            "state": state,
        }
    )


def _install_gh_fetch_fake(
    monkeypatch: pytest.MonkeyPatch,
    *,
    responses: dict[tuple[str, str], subprocess.CompletedProcess | Exception],
) -> list[list[str]]:
    """Patch ``subprocess.run`` so ``gh pr/issue view`` returns canned data."""

    calls: list[list[str]] = []
    import workdash.github_client as gc

    def fake_run(command, **kwargs):
        calls.append(list(command))
        # gh {pr|issue} view N --repo owner/repo --json ...
        if (
            len(command) >= 6
            and command[:3] in (["gh", "pr", "view"], ["gh", "issue", "view"])
            and command[4] == "--repo"
        ):
            key = (command[1], command[3])
            result = responses.get(key)
            if isinstance(result, Exception):
                raise result
            if result is not None:
                return result
        raise AssertionError(f"Unexpected gh command in include scenario: {command}")

    monkeypatch.setattr(gc.subprocess, "run", fake_run)
    monkeypatch.setattr(gc.time, "sleep", lambda _: None)
    return calls


def _install_empty_github_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub every non-include GitHub client method to return empty lists."""

    monkeypatch.setattr(GitHubClient, "list_open_authored_prs", lambda self, login: [])
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


def _make_tmp_store(scenario_state: dict[str, Any], tmp_path: Path) -> IncludedItemsStore:
    store_path = scenario_state.get("included_store_path")
    if store_path is None:
        store_path = tmp_path / "included.json"
        scenario_state["included_store_path"] = store_path
    return IncludedItemsStore(store_path)


def _make_backend(
    scenario_state: dict[str, Any],
    tmp_path: Path,
) -> WorkdashBackend:
    store = _make_tmp_store(scenario_state, tmp_path)
    scenario_state["included_store"] = store
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir=str(tmp_path / "wrk"),
    )
    return WorkdashBackend(
        cache_root=tmp_path / "cache",
        config=config,
        included_items_store=store,
    )


def _make_open_dashboard_hook(tmp_path: Path):
    """Build the `_open_dashboard_hook` that drives a real WorkdashBackend.

    The hook is invoked by the shared "When the user opens the dashboard"
    step and captures items + markers into the scenario state for Then-step
    assertions. Extracted because the Given steps that seed the store share
    this exact wiring.
    """

    def hook(state: dict[str, Any], items: list[WorkItem], _mp: pytest.MonkeyPatch) -> None:
        backend = _make_backend(state, tmp_path)
        fetched, markers = backend.load_items()
        items[:] = list(fetched)
        state["work_items"] = list(fetched)
        state["suggestion_markers"] = dict(markers)

    return hook


# ----- F-TRIAGE-INCLUDE: Given steps -----


@given("the TUI is open")
def _tui_is_open(scenario_state: dict[str, Any]) -> None:
    scenario_state.setdefault("_tui_open", True)


@given("the user pastes a pull request URL into the include dialog")
def _paste_pr_url(scenario_state: dict[str, Any]) -> None:
    scenario_state["include_url"] = _INCLUDE_PR_URL
    scenario_state["_include_kind"] = "pr"


@given("the user pastes an issue URL into the include dialog")
def _paste_issue_url(scenario_state: dict[str, Any]) -> None:
    scenario_state["include_url"] = _INCLUDE_ISSUE_URL
    scenario_state["_include_kind"] = "issue"


@given("the TUI is open with a work item already visible")
def _tui_open_with_visible_item(scenario_state: dict[str, Any], work_items: list[WorkItem]) -> None:
    if not work_items:
        work_items.append(
            make_work_item(
                item_type=WorkItemType.PR,
                kind=WorkItemKind.AUTHORED_PR,
                repo="owner/repo",
                number=111,
                title="Visible PR",
                updated_at=NOW_UTC - timedelta(days=2),
                created_at=NOW_UTC - timedelta(days=5),
                url=_INCLUDE_PR_URL,
            )
        )
    scenario_state["_visible_item"] = work_items[0]


@given("the user pastes that same work item's URL into the include dialog")
def _paste_existing_url(scenario_state: dict[str, Any]) -> None:
    scenario_state["include_url"] = scenario_state["_visible_item"].url
    scenario_state["_include_kind"] = "duplicate"


@given(
    "the user pastes a URL that is not a GitHub issue or pull request URL into the include dialog"
)
def _paste_invalid_url(scenario_state: dict[str, Any]) -> None:
    scenario_state["include_url"] = "https://example.com/not-a-github-url"
    scenario_state["_include_kind"] = "invalid"


@given(
    "the user has an included pull request, an included issue, and an included review-requested pull request"
)
def _seed_three_included_items(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _make_tmp_store(scenario_state, tmp_path)
    store.save(
        [
            "https://github.com/owner/repo/pull/111",
            "https://github.com/owner/repo/issues/222",
            "https://github.com/owner/repo/pull/333",
        ]
    )
    # PR #333 is surfaced by the review-requested source too; the backend
    # merges the included payload into the existing REVIEW row, keeping the
    # REVIEW classification while flipping `included` to True.
    monkeypatch.setattr(GitHubClient, "list_open_authored_prs", lambda self, login: [])
    monkeypatch.setattr(
        GitHubClient,
        "list_open_review_requested_prs",
        lambda self, login, progress_callback=None: [
            {
                "id": "REVIEW-333",
                "repo": "owner/repo",
                "number": 333,
                "title": "Fetched pr 333",
                "url": "https://github.com/owner/repo/pull/333",
                "created_at": "2026-04-20T10:00:00Z",
                "updated_at": "2026-04-28T10:00:00Z",
                "is_draft": False,
            }
        ],
    )
    monkeypatch.setattr(GitHubClient, "list_open_reviewed_prs", lambda self, login: [])
    monkeypatch.setattr(GitHubClient, "list_open_assigned_issues", lambda self, login: [])
    # Seed one non-included tracked PR so the list-command assertion can verify
    # that format_type_label does not spuriously append "+" to regular rows.
    monkeypatch.setattr(
        GitHubClient,
        "list_recent_tracked_items",
        lambda self, repositories, progress_callback=None: [
            {
                "id": "TRACKED-444",
                "repo": "owner/repo",
                "number": 444,
                "title": "Tracked non-included PR",
                "url": "https://github.com/owner/repo/pull/444",
                "created_at": "2026-04-20T10:00:00Z",
                "updated_at": "2026-04-27T10:00:00Z",
                "is_pull_request": True,
            }
        ],
    )
    _install_gh_fetch_fake(
        monkeypatch,
        responses={
            ("pr", "111"): subprocess.CompletedProcess(
                [], 0, stdout=_fake_gh_view_response("pr", 111), stderr=""
            ),
            ("issue", "222"): subprocess.CompletedProcess(
                [], 0, stdout=_fake_gh_view_response("issue", 222), stderr=""
            ),
            ("pr", "333"): subprocess.CompletedProcess(
                [], 0, stdout=_fake_gh_view_response("pr", 333), stderr=""
            ),
        },
    )
    scenario_state["_open_dashboard_hook"] = _make_open_dashboard_hook(tmp_path)


@given("the included-items store contains a URL from a previous session")
def _store_has_prior_url(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _make_tmp_store(scenario_state, tmp_path)
    store.save([_INCLUDE_PR_URL])
    _install_empty_github_fakes(monkeypatch)
    _install_gh_fetch_fake(
        monkeypatch,
        responses={
            ("pr", "111"): subprocess.CompletedProcess(
                [], 0, stdout=_fake_gh_view_response("pr", 111), stderr=""
            ),
        },
    )
    scenario_state["_open_dashboard_hook"] = _make_open_dashboard_hook(tmp_path)


@given("the included-items store contains a URL for an item that has since closed")
def _store_has_closed_url(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    store = _make_tmp_store(scenario_state, tmp_path)
    store.save([_INCLUDE_PR_URL])
    _install_empty_github_fakes(monkeypatch)
    _install_gh_fetch_fake(
        monkeypatch,
        responses={
            ("pr", "111"): subprocess.CompletedProcess(
                [], 0, stdout=_fake_gh_view_response("pr", 111, state="CLOSED"), stderr=""
            ),
        },
    )
    scenario_state["_open_dashboard_hook"] = _make_open_dashboard_hook(tmp_path)


@given("the included-items store contains a URL")
def _store_has_url(
    scenario_state: dict[str, Any],
    tmp_path: Path,
) -> None:
    store = _make_tmp_store(scenario_state, tmp_path)
    store.save([_INCLUDE_PR_URL])


@given("the next fetch for that URL will fail transiently")
def _next_fetch_transient(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_empty_github_fakes(monkeypatch)
    _install_gh_fetch_fake(
        monkeypatch,
        responses={
            ("pr", "111"): subprocess.CalledProcessError(
                returncode=1,
                cmd=["gh", "pr", "view", "111"],
                stderr="HTTP 503: Service Unavailable",
            ),
        },
    )
    scenario_state["_open_dashboard_hook"] = _make_open_dashboard_hook(tmp_path)


@given("the included-items store does not exist")
def _store_missing(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    scenario_state["included_store_path"] = tmp_path / "does-not-exist" / "included.json"
    scenario_state["included_store"] = IncludedItemsStore(scenario_state["included_store_path"])
    _install_empty_github_fakes(monkeypatch)
    scenario_state["_open_dashboard_hook"] = _make_open_dashboard_hook(tmp_path)


# ----- F-TRIAGE-INCLUDE: When steps -----


@when("the user confirms the include dialog")
def _confirm_include_dialog(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    url = scenario_state["include_url"]
    kind = scenario_state["_include_kind"]

    if kind == "pr":
        _install_gh_fetch_fake(
            monkeypatch,
            responses={
                ("pr", "111"): subprocess.CompletedProcess(
                    [], 0, stdout=_fake_gh_view_response("pr", 111), stderr=""
                ),
            },
        )
    elif kind == "issue":
        _install_gh_fetch_fake(
            monkeypatch,
            responses={
                ("issue", "222"): subprocess.CompletedProcess(
                    [], 0, stdout=_fake_gh_view_response("issue", 222), stderr=""
                ),
            },
        )
    # For "duplicate" and "invalid" we do not expect any gh call; omit the patch.

    # Drive the real WorkdashBackend entry points so the TUI exercises
    # production code (URL parsing, fetch, canonical persistence, duplicate
    # handling) rather than a hand-rolled mirror.
    backend = _make_backend(scenario_state, tmp_path)
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        await pilot.press("i")
        for _ in range(20):
            await pilot.pause()
            for screen in app.screen_stack:
                if isinstance(screen, IncludeDialog):
                    screen.query_one("#include-url", Input).value = url
                    await pilot.press("enter")
                    break
            else:
                continue
            break
        for _ in range(40):
            await pilot.pause()
            if not any(isinstance(screen, IncludeDialog) for screen in app.screen_stack):
                break
        table = app.query_one("#work-items", DataTable)
        captured["rows"] = [
            [str(cell) for cell in table.get_row_at(index)] for index in range(table.row_count)
        ]
        captured["cursor_row"] = table.cursor_row
        captured["status"] = app.query_one("#status-footer", Static).render().plain
        captured["sorted_items"] = list(app._sorted_work_items)

    run_app(
        work_items=list(work_items),
        include_callback=backend.include_item_by_url,
        interactions=interactions,
    )
    scenario_state["include_rows"] = captured["rows"]
    scenario_state["include_cursor_row"] = captured["cursor_row"]
    scenario_state["include_status"] = captured["status"]
    scenario_state["include_sorted_items"] = captured["sorted_items"]


# ----- F-TRIAGE-INCLUDE: Then steps -----


def _cursor_item(scenario_state: dict[str, Any]) -> WorkItem:
    index = scenario_state["include_cursor_row"]
    assert index is not None, scenario_state
    return scenario_state["include_sorted_items"][index]


@then("the pull request appears on the dashboard as an included item")
def _pr_appears_included(scenario_state: dict[str, Any]) -> None:
    sorted_items = scenario_state["include_sorted_items"]
    match = next(
        (item for item in sorted_items if item.url == scenario_state["include_url"]),
        None,
    )
    assert match is not None, sorted_items
    assert match.item_type == WorkItemType.PR
    assert match.included is True


@then("the issue appears on the dashboard as an included item")
def _issue_appears_included(scenario_state: dict[str, Any]) -> None:
    sorted_items = scenario_state["include_sorted_items"]
    match = next(
        (item for item in sorted_items if item.url == scenario_state["include_url"]),
        None,
    )
    assert match is not None, sorted_items
    assert match.item_type == WorkItemType.ISSUE
    assert match.included is True


@then("the cursor is positioned on that pull request")
def _cursor_on_pr(scenario_state: dict[str, Any]) -> None:
    item = _cursor_item(scenario_state)
    assert item.url == scenario_state["include_url"], (item, scenario_state)


@then("the cursor is positioned on that issue")
def _cursor_on_issue(scenario_state: dict[str, Any]) -> None:
    item = _cursor_item(scenario_state)
    assert item.url == scenario_state["include_url"], (item, scenario_state)


@then("the cursor is positioned on that work item")
def _cursor_on_work_item(scenario_state: dict[str, Any]) -> None:
    item = _cursor_item(scenario_state)
    assert item.url == scenario_state["include_url"], (item, scenario_state)


@then("the URL is persisted in the included-items store")
def _url_persisted(scenario_state: dict[str, Any]) -> None:
    store: IncludedItemsStore = scenario_state["included_store"]
    urls = store.load()
    assert scenario_state["include_url"] in urls, urls


@then("the work item appears exactly once on the dashboard")
def _work_item_appears_once(scenario_state: dict[str, Any]) -> None:
    sorted_items = scenario_state["include_sorted_items"]
    matches = [item for item in sorted_items if item.url == scenario_state["include_url"]]
    assert len(matches) == 1, matches


@then('the included pull request\'s type column reads "PR+"')
def _pr_plus_label(scenario_state: dict[str, Any]) -> None:
    _assert_type_column(scenario_state, number=111, expected="PR+")


@then('the included issue\'s type column reads "ISSUE+"')
def _issue_plus_label(scenario_state: dict[str, Any]) -> None:
    _assert_type_column(scenario_state, number=222, expected="ISSUE+")


@then('the included review-requested pull request\'s type column reads "REVIEW+"')
def _review_plus_label(scenario_state: dict[str, Any]) -> None:
    _assert_type_column(scenario_state, number=333, expected="REVIEW+")
    # The merged row must keep the REVIEW_REQUESTED_PR kind so the "+" suffix
    # genuinely comes from the `included` flag layered onto a REVIEW row,
    # not a fallback that infers "REVIEW" from some other signal.
    merged = next(
        (item for item in scenario_state["work_items"] if item.number == 333),
        None,
    )
    assert merged is not None, scenario_state["work_items"]
    assert merged.kind == WorkItemKind.REVIEW_REQUESTED_PR
    assert merged.included is True


def _assert_type_column(scenario_state: dict[str, Any], *, number: int, expected: str) -> None:
    work_items = scenario_state["work_items"]
    captured: dict[str, Any] = {}

    async def interactions(app, pilot) -> None:
        table = app.query_one("#work-items", DataTable)
        captured["rows"] = [
            [str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)
        ]
        captured["sorted_items"] = list(app._sorted_work_items)

    run_app(
        work_items=list(work_items),
        suggestion_markers=compute_suggestion_markers(list(work_items)),
        interactions=interactions,
    )
    for row, item in zip(captured["rows"], captured["sorted_items"], strict=True):
        if item.number == number:
            assert row[0] == expected, (row, item)
            return
    raise AssertionError(f"No row for number {number}: {captured['rows']}")


@then("the same suffixes appear when the user runs `workdash list`")
def _list_command_suffixes(scenario_state: dict[str, Any]) -> None:
    import contextlib
    import io

    work_items = scenario_state["work_items"]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        _print_work_items_result(
            _work_items_payload(work_items, compute_suggestion_markers(work_items))
        )
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    # Each seeded item's row identity is the copy/paste Workdash item ID;
    # per-row assertions guarantee the "+" suffix is anchored to the type
    # column of the correct row rather than appearing somewhere else.
    expected_by_id = {
        "owner/repo#PR-111": "PR+",
        "owner/repo#ISSUE-222": "ISSUE+",
        "owner/repo#REVIEW-333": "REVIEW+",
        # The tracked non-included PR must NOT carry the "+" suffix; this
        # guards against a regression where format_type_label always
        # appends "+".
        "owner/repo#PR-444": "PR",
    }
    found: dict[str, str] = {}
    for line in lines:
        for item_id, expected_label in expected_by_id.items():
            if item_id in line:
                assert line.startswith(f"{expected_label:7} "), (line, expected_label)
                found[item_id] = expected_label
                break
    assert set(found) == set(expected_by_id), (found, lines)


@then("the included item appears on the dashboard")
def _included_item_appears(scenario_state: dict[str, Any]) -> None:
    items = scenario_state["work_items"]
    assert any(item.included for item in items), items


@then("the item does not appear on the dashboard")
def _item_does_not_appear(scenario_state: dict[str, Any]) -> None:
    items = scenario_state["work_items"]
    assert not any(item.url == _INCLUDE_PR_URL for item in items), items


@then("the URL is no longer persisted in the included-items store")
def _url_no_longer_persisted(scenario_state: dict[str, Any]) -> None:
    store: IncludedItemsStore = scenario_state["included_store"]
    assert _INCLUDE_PR_URL not in store.load()


@then("the system reports that the URL is not valid")
def _reports_invalid(scenario_state: dict[str, Any]) -> None:
    status = scenario_state["include_status"]
    assert "Invalid" in status or "not valid" in status.lower(), status


@then("no URL is persisted in the included-items store")
def _no_url_persisted(scenario_state: dict[str, Any]) -> None:
    store: IncludedItemsStore = scenario_state["included_store"]
    assert store.load() == []


@then("the system retries the included item on the next refresh")
def _system_retries_included_item_on_next_refresh(scenario_state: dict[str, Any]) -> None:
    store: IncludedItemsStore = scenario_state["included_store"]
    assert _INCLUDE_PR_URL in store.load()


@then("the dashboard loads without error")
def _dashboard_loads(scenario_state: dict[str, Any]) -> None:
    assert "work_items" in scenario_state


@then("no included items appear on the dashboard")
def _no_included_items(scenario_state: dict[str, Any]) -> None:
    items = scenario_state["work_items"]
    assert not any(item.included for item in items), items
