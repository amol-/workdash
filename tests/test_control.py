import base64
import json
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from importlib.metadata import version
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from webob import Request

import workdash.control as control_module
import workdash.launcher as launcher_module
import workdash.repo_worktree as repo_worktree
from gh_todo_fake import fake_gh_todo_run
from gh_work_state_fake import (
    AFTER_PUSH,
    HEAD_SHA,
    check_run,
    comment,
    failure,
    install_fake_github,
    review,
    thread,
    todo_body,
)
from pane_launch_fake import install_pane_launch
from workdash.analysis_cache import AnalysisCache
from workdash.backend import IncludeResult, WorkdashBackend
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import (
    WorkdashControlError,
    WorkdashSession,
    format_work_item_id,
    probe_dashboard_session,
)
from workdash.git import GitHelper
from workdash.github_client import GitHubClient
from workdash.included_items import IncludedItemsStore
from workdash.models import WorkItem, WorkItemKind, WorkItemType, accepted_worktree_numbers
from workdash.work_instruction import render_work_instruction
from zellij_pane_launch import mark_pane_started, pane_launch


def _work_item(
    *, number: int, title: str, item_type: WorkItemType = WorkItemType.ISSUE
) -> WorkItem:
    created_at = datetime(2026, 2, number, tzinfo=UTC)
    return WorkItem(
        kind=WorkItemKind.TRACKED_PR
        if item_type == WorkItemType.PR
        else WorkItemKind.TRACKED_ISSUE,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title=title,
        created_at=created_at,
        updated_at=created_at,
        url=f"https://example.com/{number}",
    )


def test_list_api_reads_cached_items_and_refreshes_shared_state(monkeypatch) -> None:
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8765")
    initial_item = replace(_work_item(number=1, title="Initial"), analysis="Cached analysis")
    refreshed_item = _work_item(number=2, title="Refreshed", item_type=WorkItemType.PR)
    authored_pr = replace(
        _work_item(number=3, title="Authored PR", item_type=WorkItemType.PR),
        kind=WorkItemKind.AUTHORED_PR,
    )
    review_pr = replace(
        _work_item(number=4, title="Requested review", item_type=WorkItemType.PR),
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
    )
    marker = {(WorkItemType.PR, "owner/repo", 2): "*"}
    backend = MagicMock()
    backend.load_items.return_value = [refreshed_item], marker
    repainted_item_ids: list[list[str]] = []
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[initial_item, authored_pr, review_pr],
        suggestion_markers={},
        zellij_session=None,
        items_changed_callback=lambda: repainted_item_ids.append(
            [format_work_item_id(item) for item in session.work_items]
        ),
    )
    app = control_module._make_turbogears_app(session)

    def list_items(*, refresh: bool):
        response = Request.blank(
            "/api/v0/list",
            method="POST",
            content_type="application/json",
            body=json.dumps({"refresh": refresh}).encode(),
        ).get_response(app)
        assert response.status_int == 200
        return response.json["result"]["items"]

    assert list_items(refresh=False) == [
        {
            "id": "owner/repo#REVIEW-4",
            "type": "pr",
            "display_type": "REVIEW",
            "kind": "review_requested_pr",
            "repo": "owner/repo",
            "number": 4,
            "title": "Requested review",
            "url": "https://example.com/4",
            "created_at": "2026-02-04T00:00:00+00:00",
            "updated_at": "2026-02-04T00:00:00+00:00",
            "suggested": False,
            "analysis_available": False,
            "analysis_url": None,
        },
        {
            "id": "owner/repo#PR-3",
            "type": "pr",
            "display_type": "PR",
            "kind": "authored_pr",
            "repo": "owner/repo",
            "number": 3,
            "title": "Authored PR",
            "url": "https://example.com/3",
            "created_at": "2026-02-03T00:00:00+00:00",
            "updated_at": "2026-02-03T00:00:00+00:00",
            "suggested": False,
            "analysis_available": False,
            "analysis_url": None,
        },
        {
            "id": "owner/repo#ISSUE-1",
            "type": "issue",
            "display_type": "ISSUE",
            "kind": "tracked_issue",
            "repo": "owner/repo",
            "number": 1,
            "title": "Initial",
            "url": "https://example.com/1",
            "created_at": "2026-02-01T00:00:00+00:00",
            "updated_at": "2026-02-01T00:00:00+00:00",
            "suggested": False,
            "analysis_available": True,
            "analysis_url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
        },
    ]
    backend.load_items.assert_not_called()
    assert repainted_item_ids == []

    result = list_items(refresh=True)

    assert [(item["id"], item["display_type"], item["suggested"]) for item in result] == [
        ("owner/repo#CHECK-2", "CHECK", True)
    ]
    assert result[0]["analysis_available"] is False
    assert result[0]["analysis_url"] is None
    assert session.suggestion_markers == marker
    assert repainted_item_ids == [["owner/repo#CHECK-2"]]
    assert list_items(refresh=False) == result
    backend.load_items.assert_called_once()


def test_autodispatch_refresh_updates_issue_and_linked_pr() -> None:
    issue = _work_item(number=1, title="Old issue")
    pr = replace(
        _work_item(number=2, title="Linked PR", item_type=WorkItemType.PR),
        closing_issue_numbers=(1,),
    )
    backend = MagicMock()
    backend.github_client.fetch_item.side_effect = lambda repo, number, **kwargs: replace(
        issue if number == 1 else pr, title="Updated issue" if number == 1 else "Updated PR"
    )
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[pr, issue],
        suggestion_markers={},
        zellij_session=None,
    )

    session.refresh_autodispatch_item("owner/repo#ISSUE-1")

    assert [item.title for item in session.work_items] == ["Updated PR", "Updated issue"]
    assert {call.args[:2] for call in backend.github_client.fetch_item.call_args_list} == {
        ("owner/repo", 1),
        ("owner/repo", 2),
    }


@pytest.mark.parametrize("target", ["new/repo", None])
def test_autodispatch_refresh_uses_changed_todo_target(target, monkeypatch) -> None:
    item = replace(_work_item(number=1, title="Todo"), repo="owner/todos", todo_target="old/repo")
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(item, todo_target=target)
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="owner/todos"),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    ensure = MagicMock(return_value=Path("/worktree"))
    monkeypatch.setattr(control_module, "ensure_worktree", ensure)
    monkeypatch.setattr(
        control_module,
        "launch_terminal_context",
        MagicMock(
            return_value=SimpleNamespace(
                session=None, cwd="/worktree", pane_title="term", pane_id=1
            )
        ),
    )

    session.refresh_autodispatch_item("old/repo#ISSUE-WT1")

    expected_id = "new/repo#ISSUE-WT1" if target else "owner/todos#ISSUE-1"
    assert session.list_items()["items"][0]["id"] == expected_id
    assert session.terminal(target=expected_id)["item_id"] == expected_id
    assert ensure.call_args.args[1].todo_target == target


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (WorkItemKind.AUTHORED_PR, "✓✓PR 2"),
        (WorkItemKind.REVIEW_REQUESTED_PR, "  RV 2"),
        (WorkItemKind.TRACKED_PR, "  VR 2"),
    ],
)
def test_autodispatch_refresh_keeps_status_symbols_for_authored_prs_only(kind, expected) -> None:
    from workdash.tui import _type_column

    pr = replace(
        _work_item(number=2, title="Linked PR", item_type=WorkItemType.PR),
        kind=kind,
        closing_issue_numbers=(1,),
    )
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(
        pr, ci_state="SUCCESS", review_decision="APPROVED", title="Updated"
    )
    backend.github_client.fetch_linked_issues_for_pull_request.return_value = [("owner/repo", 1)]
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[pr],
        suggestion_markers={},
        zellij_session=None,
    )

    session.refresh_autodispatch_item("owner/repo#ISSUE-1")

    assert pr.title == "Updated"
    assert _type_column(pr, bold=False).plain == expected


def test_autodispatch_refresh_clears_stale_analysis() -> None:
    item = _work_item(number=1, title="Old")
    item.analysis = "old analysis"
    item.analyzed_at = item.updated_at
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(item)
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )

    session.refresh_autodispatch_item("owner/repo#ISSUE-1")
    assert session.work_items[0].analysis == "old analysis"
    assert session.work_items[0].analyzed_at == item.updated_at

    backend.github_client.fetch_item.return_value = replace(
        item, title="New", updated_at=_work_item(number=2, title="").updated_at
    )
    session.refresh_autodispatch_item("owner/repo#ISSUE-1")

    assert session.work_items[0].analysis is None
    assert session.work_items[0].analyzed_at is None
    assert not session.list_items()["items"][0]["analysis_available"]


def test_analysis_survives_metadata_refresh_after_item_resolution(tmp_path, monkeypatch) -> None:
    item = _work_item(number=1, title="Old")
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(item, title="Updated")
    path = tmp_path / "analysis.md"
    path.write_text("Analysis", encoding="utf-8")

    def analyze(requested_item, *, tool, **kwargs):
        if tool == "cached":
            return None
        requested_item.analysis = "Analysis"
        return str(path)

    backend.analyze_item.side_effect = analyze
    monkeypatch.setattr(
        control_module, "analysis_worktree", lambda *args, **kwargs: nullcontext(tmp_path)
    )
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(analyze="pi")}),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="test",
    )
    require_item = session._require_item

    def resolve_then_refresh(target):
        resolved = require_item(target)
        session.refresh_autodispatch_item(target)
        return resolved

    monkeypatch.setattr(session, "_require_item", resolve_then_refresh)
    session.analyze(target="owner/repo#ISSUE-1")

    assert session.work_items[0].title == "Updated"
    assert session.work_items[0].analysis == "Analysis"
    assert session.list_items()["items"][0]["analysis_available"]


def test_autodispatch_refresh_finds_cross_repository_linked_pr() -> None:
    pr = _work_item(number=2, title="Old", item_type=WorkItemType.PR)
    pr.linked_issues = (("other/repo", 1),)
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(pr, title="New")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[pr],
        suggestion_markers={},
        zellij_session=None,
    )

    session.refresh_autodispatch_item("OTHER/repo#ISSUE-1")

    assert session.work_items[0].title == "New"
    assert backend.github_client.fetch_item.call_args.args[:2] == ("owner/repo", 2)


def test_autodispatch_refresh_updates_pr_links() -> None:
    pr = replace(
        _work_item(number=2, title="PR", item_type=WorkItemType.PR),
        kind=WorkItemKind.AUTHORED_PR,
        linked_issues=(("owner/repo", 1),),
        closing_issue_numbers=(1,),
    )
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = replace(pr)
    backend.github_client.fetch_linked_issues_for_pull_request.return_value = [
        ("owner/repo", 3),
        ("other/repo", 4),
    ]
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[pr],
        suggestion_markers={},
        zellij_session=None,
    )

    session.refresh_autodispatch_item("owner/repo#ISSUE-1")
    assert pr.linked_issues == (("owner/repo", 3), ("other/repo", 4))
    assert pr.closing_issue_numbers == (3,)
    assert accepted_worktree_numbers(pr) == (3, 2)
    assert session.refresh_autodispatch_item("owner/repo#ISSUE-1") == ([], None)
    backend.github_client.fetch_linked_issues_for_pull_request.return_value = []
    assert session.refresh_autodispatch_item("other/repo#ISSUE-4") == ([pr], None)
    assert pr.linked_issues == ()
    assert pr.closing_issue_numbers == ()
    assert accepted_worktree_numbers(pr) == (2,)


def test_analysis_lookup_does_not_block_dashboard_and_uses_current_item(tmp_path, monkeypatch):
    item = _work_item(number=1, title="Fetched")
    current = replace(item, title="Dashboard")
    entered, release = Event(), Event()
    backend = MagicMock()

    def fetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return item

    backend.github_client.fetch_item.side_effect = fetch
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(analyze="pi")}),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    path = tmp_path / "analysis.md"
    path.write_text("Analysis")

    def analyze(requested_item, **kwargs):
        requested_item.analysis = "Analysis"
        return str(path)

    monkeypatch.setattr(session, "_analyze_item", analyze)
    with ThreadPoolExecutor(max_workers=2) as pool:
        analysis = pool.submit(session.analyze, target="owner/repo#ISSUE-1")
        try:
            assert entered.wait(5)
            listing = pool.submit(session.list_items)
            assert listing.result(timeout=1)["items"] == []
            with session._lock:
                session.work_items.append(current)
        finally:
            release.set()
        analysis.result(timeout=5)
    assert current.analysis == "Analysis"
    assert item.analysis != "Analysis"


def test_autodispatch_refresh_retries_after_concurrent_full_refresh() -> None:
    item = _work_item(number=1, title="Old")
    backend = MagicMock()
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    backend.load_items.return_value = ([replace(item, title="Full refresh")], {})

    def fetch(*args, **kwargs):
        if backend.github_client.fetch_item.call_count == 1:
            session.list_items(refresh=True)
            return replace(item, title="Stale fetch")
        return replace(item, title="After completion")

    backend.github_client.fetch_item.side_effect = fetch
    session.refresh_autodispatch_item("owner/repo#ISSUE-1")

    assert session.list_items()["items"][0]["title"] == "After completion"
    assert backend.github_client.fetch_item.call_count == 2


def test_session_list_payload_keeps_copy_paste_id_plain_and_display_type_included() -> None:
    included_item = _work_item(number=2, title="Included", item_type=WorkItemType.PR)
    included_item.included = True

    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=object(),  # type: ignore[arg-type]
        work_items=[included_item],
        suggestion_markers={},
        zellij_session=None,
    )

    result = session.list_items()

    assert result["items"][0]["id"] == "owner/repo#CHECK-2"
    assert result["items"][0]["display_type"] == "CHECK+"


def test_session_include_notifies_items_changed_callback() -> None:
    initial_item = _work_item(number=1, title="Initial")
    included_item = _work_item(number=2, title="Included", item_type=WorkItemType.PR)
    included_item.included = True
    repainted_item_ids: list[list[str]] = []

    class FakeBackend:
        def include_item_by_url(self, url, existing_identities):
            assert url == "https://github.com/owner/repo/pull/2"
            assert existing_identities == {(WorkItemType.ISSUE, "owner/repo", 1)}
            return IncludeResult(fetched_item=included_item)

    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=FakeBackend(),  # type: ignore[arg-type]
        work_items=[initial_item],
        suggestion_markers={},
        zellij_session=None,
        items_changed_callback=lambda: repainted_item_ids.append(
            [format_work_item_id(item) for item in session.work_items]
        ),
    )

    result = session.include_item_by_url("https://github.com/owner/repo/pull/2")

    assert result.fetched_item is included_item
    assert [format_work_item_id(item) for item in session.work_items] == [
        "owner/repo#ISSUE-1",
        "owner/repo#CHECK-2",
    ]
    assert repainted_item_ids == [["owner/repo#ISSUE-1", "owner/repo#CHECK-2"]]


def test_agent_panes_map_an_authored_pr_to_the_worktree_of_the_issue_it_closes(
    tmp_path, monkeypatch
) -> None:
    # The agent works in the checkout opened from the linked issue, so the pane
    # must still be reported against the pull request that owns that work.
    worktree = tmp_path / "owner_repo_41830"
    worktree.mkdir()
    item = WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=42149,
        title="Implement the issue",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url="https://example.com/42149",
        closing_issue_numbers=(41830,),
    )

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{Path(kwargs['cwd']).resolve()}\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        control_module.launcher.zellij,
        "load_zellij_panes",
        lambda _session: [
            {
                "id": 1,
                "title": "code_owner_repo_41830",
                "pane_cwd": str(worktree),
                "pane_command": "pi",
                "tab_id": 7,
                "tab_name": "work",
                "state": "Running",
                "exited": False,
            }
        ],
    )

    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    assert [pane["item"] for pane in session.info()["panes"]] == ["owner/repo#PR-42149"]


@pytest.mark.parametrize("body", [None, "  Context\n\nMore details\n  "])
def test_todo_notifies_the_live_tui_after_capturing(body, monkeypatch) -> None:
    """A captured todo must reach a running TUI without waiting for the next refresh."""

    initial_item = _work_item(number=1, title="Initial")
    todo_item = _work_item(number=10, title="Fix the flaky test")
    todo_item.repo = "testuser/todos"
    todo_item.todo_target = "owner/repo"
    create_todo = MagicMock(return_value=todo_item)

    repainted_item_ids: list[list[str]] = []

    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=SimpleNamespace(github_client=SimpleNamespace(create_todo=create_todo)),
        work_items=[initial_item],
        suggestion_markers={},
        zellij_session=None,
        items_changed_callback=lambda: repainted_item_ids.append(
            [format_work_item_id(item) for item in session.work_items]
        ),
    )

    result = session.todo(text="  Fix the flaky test  ", target="owner/repo", body=body)

    create_todo.assert_called_once_with(
        todo_repository="testuser/todos", text="Fix the flaky test", target="owner/repo", body=body
    )
    assert result["item_id"] == "owner/repo#ISSUE-WT10"
    assert repainted_item_ids == [["owner/repo#ISSUE-1", "owner/repo#ISSUE-WT10"]]


@pytest.mark.parametrize(
    ("text", "target", "message"),
    [
        ("   ", None, "The todo text is required."),
        ("Fix the flaky test", "not-a-repo", "must be a GitHub owner/repo or a supported git URL"),
    ],
)
def test_todo_session_rejects_invalid_input_before_contacting_github(text, target, message):
    github = MagicMock(spec=GitHubClient)
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=github),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )

    with pytest.raises(WorkdashControlError, match=message):
        session.todo(text=text, target=target)

    assert github.mock_calls == []
    assert session.work_items == []


@pytest.mark.parametrize(
    ("fields", "expected_status", "message"),
    [
        ({}, 200, None),
        ({"body": None}, 200, None),
        ({"body": "  Details\n\n  "}, 200, None),
        ({"body": ""}, 400, "Optional field 'body' must be a non-empty string."),
        ({"body": 123}, 400, "Optional field 'body' must be a non-empty string."),
        ({"body": ["details"]}, 400, "Optional field 'body' must be a non-empty string."),
        pytest.param({"target": "owner/repo"}, 200, None, id="github-target"),
        pytest.param(
            {"target": "https://gitlab.com/owner/repo.git"}, 200, None, id="gitlab-target"
        ),
        pytest.param(
            {"target": "https://codeberg.org/owner/repo.git"}, 200, None, id="codeberg-target"
        ),
        pytest.param(
            {
                "target": "owner/repo",
                "body": (
                    "  Detailed context\n\n" * 16
                    + '```json\n{"workdash_todo_version": 1, "target": "wrong/repository"}\n```\n  '
                ),
            },
            200,
            None,
            id="long-details",
        ),
        pytest.param(
            {"target": "not-a-repo"},
            400,
            "must be a GitHub owner/repo or a supported git URL",
            id="invalid-target",
        ),
    ],
)
def test_todo_api_validates_input_and_captures_the_issue(
    fields, expected_status, message, monkeypatch
):
    recorded: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", fake_gh_todo_run(recorded))
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    request = Request.blank(
        "/api/v0/todo",
        method="POST",
        content_type="application/json",
        body=json.dumps({"text": "Short title", **fields}).encode(),
    )

    response = request.get_response(control_module._make_turbogears_app(session))

    assert response.status_int == expected_status
    payload = response.json
    if expected_status == 400:
        assert payload["ok"] is False
        assert payload["error"]["code"] == "bad_request"
        assert message in payload["error"]["message"]
        assert recorded == []
        assert session.work_items == []
        return

    command = recorded[-1]
    assert command[:3] == ["gh", "issue", "create"]
    assert command[command.index("--repo") + 1] == "testuser/todos"
    assert command[command.index("--title") + 1] == "Short title"
    target = fields.get("target")
    metadata = {"workdash_todo_version": 1}
    if target is not None:
        metadata["target"] = target
    expected_body = f"```json\n{json.dumps(metadata, indent=2)}\n```\n"
    if fields.get("body") is not None:
        expected_body += "\n" + fields["body"]
    assert command[command.index("--body") + 1] == expected_body
    item_id = "owner/repo#ISSUE-WT110" if target else "testuser/todos#ISSUE-110"
    assert payload == {
        "ok": True,
        "result": {
            "item_id": item_id,
            "todo_repository": "testuser/todos",
            "target": target,
            "number": 110,
            "url": "https://github.com/testuser/todos/issues/110",
        },
    }
    [entry] = session.list_items()["items"]
    assert entry["id"] == item_id
    assert entry["repo"] == ("owner/repo" if target else "testuser/todos")


def test_capture_and_refresh_use_todo_metadata_over_conflicting_body_details(tmp_path, monkeypatch):
    from gh_todo_fake import TODO_ISSUE_NUMBER, TODO_ISSUE_URL, TODO_REPOSITORY

    config = WorkdashConfig(
        github_username="testuser", todo_repository=TODO_REPOSITORY, workdir=str(tmp_path)
    )
    backend = WorkdashBackend(config=config, cache_root=tmp_path / "cache")
    github = backend.github_client
    for method in (
        "list_open_authored_prs",
        "list_open_review_requested_prs",
        "list_open_reviewed_prs",
        "list_open_assigned_issues",
        "list_recent_tracked_items",
    ):
        monkeypatch.setattr(github, method, lambda *args, **kwargs: [])
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    title = "Fix the flaky test"
    conflicting_metadata = todo_body("wrong/repository")
    details = f"Research for wrong/repository.\n\n{conflicting_metadata}"
    commands = []
    capture = fake_gh_todo_run(commands)

    def run(cmd, **kwargs):
        if cmd[:3] != ["gh", "api", "graphql"]:
            return capture(cmd, **kwargs)
        create = commands[-1]
        assert create[:3] == ["gh", "issue", "create"]
        body = create[create.index("--body") + 1]
        assert conflicting_metadata in body
        node = {
            "id": "I110",
            "number": TODO_ISSUE_NUMBER,
            "title": title,
            "url": TODO_ISSUE_URL,
            "createdAt": "2026-04-01T00:00:00Z",
            "updatedAt": "2026-04-02T00:00:00Z",
            "subIssuesSummary": {"total": 0},
            "body": body,
        }
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "data": {"repository": {"issues": {"nodes": [node]}}},
                }
            ),
        )

    monkeypatch.setattr(subprocess, "run", run)
    checkout = str(tmp_path / "owner_repo_todo_110")
    prepare = MagicMock(return_value=checkout)
    launch = MagicMock(
        return_value=SimpleNamespace(
            session=None, cwd=checkout, pane_title="Todo", pane_id="terminal_23"
        )
    )
    monkeypatch.setattr(control_module, "ensure_worktree", prepare)
    monkeypatch.setattr(control_module, "launch_terminal_context", launch)
    result = session.todo(text=title, target="owner/repo", body=details)
    assert result["item_id"] == "owner/repo#ISSUE-WT110"
    [captured] = session.work_items
    assert captured.todo_target == "owner/repo"
    assert session.terminal(target=result["item_id"])["cwd"] == checkout
    assert prepare.call_args.args[1] is captured

    [entry] = session.list_items(refresh=True)["items"]

    [refreshed] = session.work_items
    assert refreshed is not captured
    assert (entry["repo"], entry["id"], refreshed.todo_target) == (
        "owner/repo",
        "owner/repo#ISSUE-WT110",
        "owner/repo",
    )
    assert session.terminal(target=entry["id"])["cwd"] == checkout
    assert prepare.call_args.args[1] is refreshed


def _ready_session(tmp_path, *, repositories: tuple[str, ...] = ()) -> WorkdashSession:
    """A session resolving explicit targets through the real GitHub client."""

    backend = MagicMock(github_client=GitHubClient())
    backend.analysis_cache.load_analysis.return_value = None
    backend.analysis_cache.load_analysis_date.return_value = None
    return WorkdashSession(
        config=WorkdashConfig(
            github_username="testuser",
            repositories=repositories,
            todo_repository="testuser/todos",
            workdir=str(tmp_path),
        ),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash-main",
    )


def _post_ready(session: WorkdashSession, payload: dict[str, object]):
    request = Request.blank(
        "/api/v0/ready",
        method="POST",
        content_type="application/json",
        body=json.dumps(payload).encode(),
    )
    response = request.get_response(control_module._make_turbogears_app(session))
    return response.status_int, json.loads(response.text)


def test_ready_api_reports_ready_work(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#3")
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _session: [])

    status, body = _post_ready(_ready_session(tmp_path), {"scope": "todo"})

    assert status == 200
    assert body["result"]["scope"] == "todo"
    assert [item["id"] for item in body["result"]["items"]] == ["testuser/todos#ISSUE-3"]


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({}, 400, "bad_request"),
        ({"scope": "https://github.com/owner/repo/pull/5"}, 400, "unsupported_item_type"),
        ({"scope": "owner/down"}, 502, "github_error"),
    ],
)
def test_ready_api_reports_a_scope_error(payload, status, code, monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyRepositoryIssues"] = failure("GraphQL: Something went wrong")
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _session: [])

    status_code, body = _post_ready(_ready_session(tmp_path), payload)

    assert (status_code, body["error"]["code"]) == (status, code)


def test_ready_ids_resolve_to_the_same_items_code_acts_on(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#42")
    github.add_issue("testuser/todos#7", body=todo_body("owner/other"))
    github.add_pull_request(
        "owner/repo#52",
        author="testuser",
        closes=("owner/repo#42",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _session: [])
    session = _ready_session(tmp_path, repositories=("owner/repo",))

    ready = session.ready(scope="any")["items"]

    assert [item["id"] for item in ready] == ["owner/repo#ISSUE-42", "owner/other#ISSUE-WT7"]
    for item in ready:
        resolved = session._require_item(item["id"])
        assert (format_work_item_id(resolved), resolved.url) == (item["id"], item["url"])


def test_ready_leaves_out_the_pull_request_of_a_targeted_todo_an_agent_works_on(
    monkeypatch, tmp_path
) -> None:
    worktree = tmp_path / "owner_other_todo_7"
    worktree.mkdir()

    def fake_git(cmd, *args, **kwargs):
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=f"{kwargs['cwd']}\n", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/other.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_git)
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#7", body=todo_body("owner/other"))
    github.add_pull_request(
        "owner/other#51",
        author="testuser",
        closes=("testuser/todos#7",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    panes: list[dict[str, object]] = []
    monkeypatch.setattr(
        control_module.launcher.zellij, "load_zellij_panes", lambda _session: list(panes)
    )
    session = _ready_session(tmp_path)

    assert [item["id"] for item in session.ready(scope="owner/other")["items"]] == [
        "owner/other#ISSUE-WT7"
    ]
    panes.append(
        {
            "id": 1,
            "title": "code_owner_other_todo_7",
            "pane_cwd": str(worktree),
            "pane_command": "pi",
            "exited": False,
        }
    )
    assert session.ready(scope="owner/other")["items"] == []


def test_ready_reports_a_failed_issue_tree_lookup_without_widening_the_scope(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#99")
    github.failures["ReadyIssueTree"] = failure("HTTP 502: Bad Gateway")

    with pytest.raises(WorkdashControlError) as raised:
        _ready_session(tmp_path).ready(scope="owner/repo#99")

    assert (raised.value.code, raised.value.status) == ("github_error", HTTPStatus.BAD_GATEWAY)
    assert "ReadyRepositoryIssues" not in github.operations


def test_ready_reads_github_outside_the_session_lock_and_panes_inside_it(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session = _ready_session(tmp_path)
    lock_held: list[tuple[str, bool]] = []

    def gh(*args, **kwargs):
        lock_held.append(("gh", session._lock._is_owned()))
        return github.run(*args, **kwargs)

    def load_panes(_session):
        lock_held.append(("panes", session._lock._is_owned()))
        return []

    monkeypatch.setattr(subprocess, "run", gh)
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", load_panes)

    assert [item["id"] for item in session.ready(scope="owner/repo")["items"]] == [
        "owner/repo#ISSUE-1"
    ]
    assert {held for call, held in lock_held if call == "gh"} == {False}
    assert {held for call, held in lock_held if call == "panes"} == {True}


def test_session_reports_the_operation_blocking_the_dashboard(tmp_path) -> None:
    session = _ready_session(tmp_path)
    assert session.busy_message() is None

    with session._blocking("Checking ready work..."):
        assert session.busy_message() == "Checking ready work..."
        # Operations overlap, so the dialog names the one that started last.
        with session._blocking("Starting autodispatch..."):
            assert session.busy_message() == "Starting autodispatch..."
        assert session.busy_message() == "Checking ready work..."
    assert session.busy_message() is None

    with pytest.raises(RuntimeError):
        with session._blocking("Starting autodispatch..."):
            raise RuntimeError("Could not confirm existing coding pane was closed.")
    assert session.busy_message() is None


def test_busy_message_answers_while_an_operation_holds_the_session_lock(tmp_path) -> None:
    session = _ready_session(tmp_path)
    holding, release = Event(), Event()

    def hold_lock() -> None:
        with session._lock:
            holding.set()
            release.wait(5)

    with session._blocking("Starting autodispatch..."):
        holder = Thread(target=hold_lock)
        holder.start()
        assert holding.wait(5)
        answers: list[str | None] = []
        reader = Thread(target=lambda: answers.append(session.busy_message()))
        reader.start()
        reader.join(1)
        answered_without_the_lock = not reader.is_alive()
        release.set()
        holder.join(5)
        assert answered_without_the_lock
        assert answers == ["Starting autodispatch..."]


def test_session_analyze_returns_cached_analysis_without_configured_agents(
    tmp_path, monkeypatch
) -> None:
    item = _work_item(number=1, title="Cached")
    item.analysis = "# cached analysis"
    cached_path = tmp_path / "cached-analysis.md"
    cached_path.write_text("# cached analysis\n", encoding="utf-8")
    analyze_calls: list[tuple[WorkItem, str]] = []

    class FakeBackend:
        def analyze_item(self, requested_item, tool="codex"):
            analyze_calls.append((requested_item, tool))
            if tool == "cached":
                return str(cached_path)
            raise AssertionError(f"Unexpected fresh analysis with {tool}")

    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=FakeBackend(),  # type: ignore[arg-type]
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )

    monkeypatch.setattr(
        control_module,
        "analysis_worktree",
        MagicMock(side_effect=AssertionError("prepared worktree")),
    )
    monkeypatch.setattr(
        control_module,
        "ensure_worktree",
        MagicMock(side_effect=AssertionError("prepared worktree")),
    )
    result = session.analyze(target="owner/repo#ISSUE-1")

    assert not (tmp_path / "owner_repo_1").exists()
    assert result == {
        "item_id": "owner/repo#ISSUE-1",
        "path": str(cached_path),
        "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
        "agent": "cached",
        "cache_used": True,
        "status": "cached",
        "content_type": "text/markdown",
        "file_name": "cached-analysis.md",
        "file_content": base64.b64encode(b"# cached analysis\n").decode("ascii"),
    }
    assert analyze_calls == [(item, "cached")]


@pytest.mark.parametrize(
    ("action", "agent", "code", "message"),
    [
        ("analyze", "ghost", "unknown_agent", "Agent 'ghost' is not configured."),
        ("code", "ghost", "unknown_agent", "Agent 'ghost' is not configured."),
        ("code", "vscode", "unknown_agent", "Agent 'vscode' is not configured."),
        (
            "analyze",
            "pi",
            "unsupported_agent",
            "Agent 'pi' cannot analyze: it has no analyze command.",
        ),
        (
            "code",
            "aider",
            "unsupported_agent",
            "Agent 'aider' cannot code: it has no launch command.",
        ),
    ],
)
def test_session_rejects_agents_that_are_unknown_or_cannot_do_the_action(
    action, agent, code, message
) -> None:
    session = WorkdashSession(
        config=WorkdashConfig(
            agents={"pi": AgentConfig(launch="pi"), "aider": AgentConfig(analyze="aider")}
        ),
        backend=object(),  # type: ignore[arg-type]
        work_items=[_work_item(number=1, title="Issue")],
        suggestion_markers={},
        zellij_session=None,
    )

    with pytest.raises(control_module.WorkdashControlError) as error:
        getattr(session, action)(target="owner/repo#ISSUE-1", agent=agent)

    assert (error.value.code, error.value.status, str(error.value)) == (code, 400, message)


def test_session_without_agents_reports_no_agent_for_each_action() -> None:
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=object(),  # type: ignore[arg-type]
        work_items=[_work_item(number=1, title="Issue")],
        suggestion_markers={},
        zellij_session=None,
    )

    for action, message in [
        ("analyze", "No analysis agents are configured."),
        ("code", "No coding agents are configured."),
    ]:
        with pytest.raises(control_module.WorkdashControlError) as error:
            getattr(session, action)(target="owner/repo#ISSUE-1")
        assert (error.value.code, error.value.status, str(error.value)) == (
            "no_agent",
            409,
            message,
        )


def _coding_session(
    monkeypatch, tmp_path, panes, *, zellij_session="workdash-main"
) -> tuple[WorkdashSession, Path, MagicMock, MagicMock]:
    """A session whose item worktree exists, with Zellij reporting ``panes``."""

    worktree = tmp_path / "owner_repo_1"
    worktree.mkdir(exist_ok=True)
    ensure_worktree = MagicMock(return_value=str(worktree))

    def launch(repo, *_args, **_kwargs):
        title = f"code_{Path(repo).name}"
        panes.append({"id": 30, "title": title, "pane_cwd": repo})
        return SimpleNamespace(session=None, pane_id="terminal_30", pane_title=title, cwd=repo)

    launch_agent = MagicMock(side_effect=launch)
    monkeypatch.setattr(
        control_module.launcher.zellij, "load_zellij_panes", lambda _session: list(panes)
    )
    monkeypatch.setattr(
        control_module, "existing_worktree_path", lambda _wd, _item, **kwargs: worktree
    )
    monkeypatch.setattr(control_module, "ensure_worktree", ensure_worktree)
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path, **kwargs: None)
    monkeypatch.setattr(control_module, "prepare_launch_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(control_module.agent_execution.executor, "launch", launch_agent)
    monkeypatch.setattr(control_module, "_LAUNCH_VISIBILITY_INTERVAL_SECONDS", 0)
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi")}, workdir=str(tmp_path)),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[_work_item(number=1, title="Issue")],
        suggestion_markers={},
        zellij_session=zellij_session,
    )
    return session, worktree, ensure_worktree, launch_agent


def test_concurrent_code_requests_launch_one_agent(monkeypatch, tmp_path) -> None:
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    target = "owner/repo#ISSUE-1"
    launching, second_request = Event(), Event()
    launch = launch_agent.side_effect
    parse_target = session._item_target

    def launch_while_second_request_arrives(*args, **kwargs):
        launching.set()
        assert second_request.wait(5)
        return launch(*args, **kwargs)

    def signal_target(ref):
        if launching.is_set():
            second_request.set()
        return parse_target(ref)

    launch_agent.side_effect = launch_while_second_request_arrives
    monkeypatch.setattr(session, "_item_target", signal_target)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.code, target=target, agent="pi")
        assert launching.wait(5)
        second = pool.submit(session.code, target=target, agent="pi")
        launched, running = first.result(timeout=5), second.result(timeout=5)

    assert (launched["status"], launched["pane_id"]) == ("launched", "terminal_30")
    assert (running["status"], running["pane_id"]) == ("already_running", launched["pane_id"])
    launch_agent.assert_called_once()


@pytest.mark.parametrize(
    "panes",
    [
        [{"id": 3, "title": "code_owner_repo_1", "pane_cwd": "owner_repo_1", "exited": True}],
        [
            {"id": 4, "title": "terminal_owner_repo_1", "pane_cwd": "owner_repo_1"},
            {"id": 5, "title": "code_owner_repo_2", "pane_cwd": "owner_repo_2"},
        ],
    ],
    ids=["exited-agent", "terminal-and-other-worktree"],
)
def test_code_launches_in_existing_worktree_without_a_live_claim(
    monkeypatch, tmp_path, panes
) -> None:
    panes = [{**pane, "pane_cwd": str(tmp_path / pane["pane_cwd"])} for pane in panes]
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, panes)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert (result["status"], result["pane_id"], result["cwd"]) == (
        "launched",
        "terminal_30",
        str(worktree),
    )
    launch_agent.assert_called_once()
    assert launch_agent.call_args.args[0] == str(worktree)


def test_code_reports_the_lowest_live_agent_pane_tied_to_the_worktree(
    monkeypatch, tmp_path
) -> None:
    worktree = tmp_path / "owner_repo_1"
    panes = [
        # The lowest agent changed directory, but its title still names the worktree.
        {"id": 4, "title": "code_owner_repo_1", "pane_cwd": "/elsewhere"},
        {"id": 9, "title": "code_owner_repo_1", "pane_cwd": str(worktree / "src")},
        # An agent of another worktree that changed directory into this one.
        {"id": 3, "title": "code_owner_repo_2", "pane_cwd": str(worktree)},
        {"id": 1, "title": "code_owner_repo_1", "pane_cwd": str(worktree), "exited": True},
        {"id": 2, "title": "terminal_owner_repo_1", "pane_cwd": str(worktree)},
        {"id": 0, "title": "code_owner_repo_10", "pane_cwd": str(tmp_path / "owner_repo_10")},
    ]
    session, worktree, ensure_worktree, launch_agent = _coding_session(monkeypatch, tmp_path, panes)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert result == {
        "status": "already_running",
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": None,
        "cwd": "/elsewhere",
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_4",
        "tab_name": None,
    }
    ensure_worktree.assert_not_called()
    launch_agent.assert_not_called()


@pytest.mark.parametrize(
    ("zellij_session", "panes", "code"),
    [
        (None, [], "zellij_session_required"),
        ("workdash-main", [{"title": "code_owner_repo_1", "pane_cwd": "/x"}], "zellij_unavailable"),
    ],
)
def test_code_starts_nothing_when_live_agent_panes_cannot_be_checked(
    monkeypatch, tmp_path, zellij_session, panes, code
) -> None:
    session, _worktree, ensure_worktree, launch_agent = _coding_session(
        monkeypatch, tmp_path, panes, zellij_session=zellij_session
    )

    with pytest.raises(control_module.WorkdashControlError) as error:
        session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert error.value.code == code
    ensure_worktree.assert_not_called()
    launch_agent.assert_not_called()


def test_code_does_not_launch_when_pane_listing_fails(monkeypatch, tmp_path) -> None:
    session, _worktree, ensure_worktree, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    monkeypatch.setattr(
        control_module.launcher.zellij,
        "load_zellij_panes",
        MagicMock(side_effect=RuntimeError("pane listing failed")),
    )

    response = _post_item_action(session, "code", "owner/repo#ISSUE-1", agent="pi")

    assert response.status_int == 503
    assert response.json["error"]["code"] == "zellij_unavailable"
    assert "pane listing failed" in response.json["error"]["message"]

    ensure_worktree.assert_not_called()
    launch_agent.assert_not_called()


@pytest.mark.parametrize("unusable_count", [1, 2], ids=["first-read", "first-two-reads"])
def test_code_launches_after_unusable_pane_listings(monkeypatch, tmp_path, unusable_count):
    panes = []
    session, _worktree, _ensure, launch = _coding_session(monkeypatch, tmp_path, panes)
    unusable = [
        subprocess.CompletedProcess([], 0, stdout="Session 'workdash-main' not found.\n")
    ] * unusable_count

    def run(command, **_kwargs):
        assert command[3:5] == ["action", "list-panes"]
        if unusable:
            return unusable.pop()
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(panes))

    monkeypatch.setattr(launcher_module, "subprocess", SimpleNamespace(run=run))
    monkeypatch.setattr(launcher_module, "_resolve_zellij_binary", lambda: "zellij")
    monkeypatch.setattr(launcher_module, "_ZELLIJ_PANE_READ_RETRY_SECONDS", 0)
    monkeypatch.setattr(
        launcher_module.zellij, "load_zellij_panes", launcher_module.Zellij.load_zellij_panes
    )

    response = _post_item_action(session, "code", "owner/repo#ISSUE-1", agent="pi")

    assert response.status_int == HTTPStatus.OK
    result = response.json["result"]
    assert (result["status"], result["pane_id"]) == ("launched", "terminal_30")
    assert not unusable
    launch.assert_called_once()


def test_vscode_is_not_opened_while_an_agent_pane_is_live(monkeypatch, tmp_path) -> None:
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, _worktree, _ensure, _launch = _coding_session(monkeypatch, tmp_path, panes)
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module.agent_execution.executor, "launch_vscode", launch_vscode)

    result = session.code(target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True)

    assert (result["status"], result["pane_id"]) == ("already_running", "terminal_5")
    launch_vscode.assert_not_called()


def test_vscode_opens_without_a_zellij_session(monkeypatch, tmp_path) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    session, worktree, _ensure, _launch = _coding_session(
        monkeypatch, tmp_path, [], zellij_session=None
    )
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module.agent_execution.executor, "launch_vscode", launch_vscode)

    result = session.code(target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True)

    assert result["status"] == "launched"
    launch_vscode.assert_called_once_with(str(worktree), "PROMPT")


def test_code_retry_does_not_repeat_an_unconfirmed_launch(monkeypatch, tmp_path) -> None:
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    launch_agent.side_effect = None
    launch_agent.return_value = SimpleNamespace(
        pane_id="terminal_30", pane_title=f"code_{worktree.name}"
    )

    for _ in range(2):
        response = _post_item_action(session, "code", "owner/repo#ISSUE-1", agent="pi")
        assert response.status_int == 409
        assert response.json["error"]["code"] == "launch_unconfirmed"

    launch_agent.assert_called_once()


def test_an_unconfirmed_launch_whose_pane_shows_up_later_is_reported_as_running(
    monkeypatch, tmp_path
) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    panes: list[dict[str, object]] = []
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, panes)
    side_effect = launch_agent.side_effect
    launch_agent.side_effect = None

    with pytest.raises(control_module.WorkdashControlError) as error:
        session.code(target="owner/repo#ISSUE-1", agent="pi")
    assert error.value.code == "launch_unconfirmed"

    panes.append({"id": 8, "title": "code_owner_repo_1", "pane_cwd": str(worktree)})
    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert (result["status"], result["pane_id"]) == ("already_running", "terminal_8")
    assert launch_agent.call_count == 1

    # The pane settled the earlier launch, so once it exits a new agent may start.
    panes.clear()
    launch_agent.side_effect = side_effect
    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert (result["status"], result["pane_id"]) == ("launched", "terminal_30")
    assert launch_agent.call_count == 2


def test_an_agent_that_exits_right_after_launch_fails_without_blocking_a_retry(
    monkeypatch, tmp_path
) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    # An earlier agent pane for this worktree that exited long ago.
    panes = [{"id": 1, "title": "code_owner_repo_1", "exited": True}]
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, panes)
    side_effect = launch_agent.side_effect
    launch_agent.side_effect = lambda repo, *_args, **_kwargs: (
        panes.append({"id": 8, "title": "code_owner_repo_1", "pane_cwd": repo, "is_held": True})
        or SimpleNamespace(
            session=None, pane_id="terminal_8", pane_title="code_owner_repo_1", cwd=repo
        )
    )

    with pytest.raises(control_module.WorkdashControlError) as error:
        session.code(target="owner/repo#ISSUE-1", agent="pi")
    assert error.value.code == "agent_exited"
    assert "terminal_8" in str(error.value)

    launch_agent.side_effect = side_effect
    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert (result["status"], result["pane_id"]) == ("launched", "terminal_30")
    assert launch_agent.call_count == 2


@pytest.mark.parametrize(
    ("winner_state", "code"),
    [
        ("live", None),
        (None, "launch_unconfirmed"),
        ("held", "agent_exited"),
        ("wrong-title", "launch_unconfirmed"),
    ],
)
def test_code_reports_and_supervises_only_the_winning_retry(
    monkeypatch, tmp_path, winner_state, code
):
    panes = []
    session, _, _, launch_agent = _coding_session(monkeypatch, tmp_path, panes)
    supervise = MagicMock()
    monkeypatch.setattr(session.autoclose, "supervise", supervise)

    install_fake_github(monkeypatch).add_issue("owner/repo#1")

    def launch(repo, *_args, **_kwargs):
        title = "code_owner_repo_1"
        panes.append({"id": 29, "title": title, "pane_cwd": repo})
        if winner_state is not None:
            panes.append(
                {
                    "id": 30,
                    "title": "other" if winner_state == "wrong-title" else title,
                    "is_held": winner_state == "held",
                }
            )
        return SimpleNamespace(session=None, pane_id="terminal_30", pane_title=title, cwd=repo)

    launch_agent.side_effect = launch
    options = dict(target="owner/repo#ISSUE-1", agent="pi", autostart=True, autoclose=True)
    if code is None:
        result = session.code(**options)
        assert (result["status"], result["pane_id"], result["autoclose_enabled"]) == (
            "launched",
            "terminal_30",
            True,
        )
        supervise.assert_called_once()
        assert supervise.call_args.kwargs["pane_id"] == "terminal_30"
    else:
        with pytest.raises(control_module.WorkdashControlError) as error:
            session.code(**options)
        assert error.value.code == code
        supervise.assert_not_called()


def test_code_waits_for_a_launched_agent_pane_that_zellij_lists_late(monkeypatch, tmp_path) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    launch_agent.side_effect = None
    late_pane = {"id": 8, "title": "code_owner_repo_1", "pane_cwd": str(worktree)}
    launch_agent.return_value.pane_id = "terminal_8"
    launch_agent.return_value.pane_title = "code_owner_repo_1"
    # Listings: before launch, first check after launch, second check after launch.
    monkeypatch.setattr(
        control_module.launcher.zellij,
        "load_zellij_panes",
        MagicMock(side_effect=[[], [], [late_pane]]),
    )

    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert (result["status"], result["pane_id"]) == ("launched", "terminal_8")
    launch_agent.assert_called_once()


def test_an_expired_unconfirmed_launch_no_longer_blocks_the_worktree(monkeypatch, tmp_path) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    panes: list[dict[str, object]] = []
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, panes)
    side_effect = launch_agent.side_effect
    launch_agent.side_effect = None
    monkeypatch.setattr(control_module, "_UNCONFIRMED_LAUNCH_TTL_SECONDS", 0)
    with pytest.raises(control_module.WorkdashControlError):
        session.code(target="owner/repo#ISSUE-1", agent="pi")

    launch_agent.side_effect = side_effect
    result = session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert (result["status"], result["pane_id"]) == ("launched", "terminal_30")
    assert launch_agent.call_count == 2


@pytest.mark.parametrize(("ttl", "listed"), [(60, []), (0, ["owner/repo#ISSUE-1"])])
def test_ready_leaves_out_work_whose_agent_launch_is_still_unconfirmed(
    monkeypatch, tmp_path, ttl, listed
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    launch_agent.side_effect = None
    monkeypatch.setattr(control_module, "_UNCONFIRMED_LAUNCH_TTL_SECONDS", ttl)
    with pytest.raises(WorkdashControlError, match="did not appear"):
        session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert [item["id"] for item in session.ready(scope="owner/repo")["items"]] == listed


def _launched_prompt(launch_agent: MagicMock) -> str:
    return launch_agent.call_args.args[1]


def _authored_session(monkeypatch, tmp_path, panes=None):
    """A coding session whose user authored the pull requests of the fake GitHub."""

    session, worktree, ensure, launch_agent = _coding_session(
        monkeypatch, tmp_path, [] if panes is None else panes
    )
    session.config = replace(session.config, github_username="testuser")
    prepare = MagicMock(return_value="PROMPT")
    monkeypatch.setattr(control_module, "prepare_launch_agent_prompt", prepare)
    helper = MagicMock()
    helper.fetch_worktree_head.side_effect = lambda item: ("pr-branch", item.repo)
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )
    git = MagicMock()
    git.branch_tracks_repository.return_value = True
    session.git = git
    return session, worktree, ensure, launch_agent, prepare


def test_code_with_autostart_adds_the_implementation_appendix_once(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])

    session.work_items[0].url = "https://github.com/owner/repo/issues/1"

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    prompt = _launched_prompt(launch_agent)
    assert result["status"] == "launched"
    assert prompt.startswith("PROMPT\n\nAUTOSTART: IMPLEMENT, PUBLISH AND VERIFY")
    assert prompt.count("AUTOSTART:") == 1
    assert prompt.endswith(render_work_instruction("implement", session.work_items[0]))
    assert prompt.count("coordination workflows") == 1

    normalized = " ".join(prompt.split())
    for instruction in (
        "Disregard the interactive instruction above to stop at a plan",
        "start implementing without asking for routine confirmation",
        "existing commits and uncommitted changes",
        "continue valid partial work",
        "Replace or discard it only for a clear technical reason",
        "Preserve unrelated user changes",
        "committed, pushed, and represented by a GitHub pull request",
        "create a pull request that closes it with `Fixes #NUMBER`",
        "Commit and push are explicitly requested and permitted",
        "force-push only the pull request branch",
        "--force-with-lease",
        "CURRENT head has all applicable CI checks passing",
        "if pending, wait",
        'report "no checks required or reported", never "CI passed"',
        "report INCOMPLETE",
        "Use GitHub metadata only for this",
        "never read, search or parse repository files, workflow definitions or CI configuration",
        "mergeable against the CURRENT base branch",
        "A changed base SHA invalidates any earlier mergeability decision",
        "Only report INCOMPLETE and wait for instructions if blocked by missing credentials "
        "or access to development tools or external infrastructure, external service or "
        "platform outages, or genuinely impossible or contradictory requirements where "
        "essential specification is absent from both the repository and the issue.",
        "Application-under-test endpoint errors are not external access blockers",
        "tool or subagent disagreement",
        "failing tests, lint or formatting, timeouts, and trade-offs",
        "Inspect, resume, and finish partial uncommitted subagent work",
        "Never merge the pull request or close the issue",
        "stay in this interactive session without exiting the agent",
        "development practices, skills, and coordination workflows you have been configured",
        "do not open a duplicate pull request",
    ):
        assert instruction in normalized
    assert "anything else real" not in normalized
    assert "Continue the assigned issue https://github.com/owner/repo/issues/1." in normalized


@pytest.mark.parametrize("conflict", [False, True])
def test_autostart_full_briefing_publishes(monkeypatch, tmp_path, conflict):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    if conflict:
        github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), mergeable="CONFLICTING")
    session, _worktree, _ensure, launch_agent, _prepare = _authored_session(monkeypatch, tmp_path)
    session.config = replace(
        session.config, github_username="testuser", todo_repository="testuser/todos"
    )
    monkeypatch.setattr(
        control_module, "prepare_launch_agent_prompt", launcher_module.prepare_launch_agent_prompt
    )
    monkeypatch.setattr(session.backend.github_client, "fetch_launch_context", lambda _item: {})

    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    prompt = _launched_prompt(launch_agent)
    assert "TASK: IMPLEMENT" in prompt and "Do not start implementing code yet" in prompt
    assert "Commit and push are explicitly requested and permitted" in prompt
    assert "force-push only the pull request branch" in prompt
    assert "--force-with-lease" in prompt
    assert "repository" in prompt and "restrictions" in prompt
    assert "--add-assignee" not in prompt
    assert "Never merge the pull request or close the issue" in prompt
    if conflict:
        assert "cannot merge cleanly" in prompt
        assert "force-push unless the repository's explicit policy authorizes it" not in prompt


@pytest.mark.parametrize("autostart", [False, True])
def test_code_on_a_live_agent_sends_nothing_and_reads_no_github_state_even_when_it_is_down(
    monkeypatch, tmp_path, autostart
) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyIssueTree"] = failure("HTTP 502: Bad Gateway")
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, panes)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=autostart)

    assert result == {
        "status": "already_running",
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": None,
        "cwd": str(worktree),
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_5",
        "tab_name": None,
    }
    launch_agent.assert_not_called()
    assert github.operations == []


def test_code_on_a_live_authored_pr_reports_its_pane_during_github_outage(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyPullRequests"] = failure("HTTP 502: Bad Gateway")
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, _worktree, ensure, launch_agent, _prepare = _authored_session(
        monkeypatch, tmp_path, panes
    )
    session.work_items = [
        replace(
            _work_item(number=1, title="PR", item_type=WorkItemType.PR),
            kind=WorkItemKind.AUTHORED_PR,
        )
    ]

    result = session.code(target="owner/repo#PR-1", agent="pi", autostart=True)

    assert (result["status"], result["item_id"], result["pane_id"]) == (
        "already_running",
        "owner/repo#PR-1",
        "terminal_5",
    )
    ensure.assert_not_called()
    launch_agent.assert_not_called()
    assert github.operations == []


def test_autostart_appends_the_appendix_once_to_a_vscode_briefing(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, worktree, _ensure, _launch_agent = _coding_session(monkeypatch, tmp_path, [])
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module.agent_execution.executor, "launch_vscode", launch_vscode)

    result = session.code(
        target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True, autostart=True
    )

    [(path, prompt)] = [call.args for call in launch_vscode.call_args_list]
    assert (result["status"], path) == ("launched", str(worktree))
    assert prompt.startswith("PROMPT\n\nAUTOSTART: IMPLEMENT, PUBLISH AND VERIFY")
    assert prompt.count("AUTOSTART:") == 1
    assert prompt.endswith(render_work_instruction("implement", session.work_items[0]))


def test_autostart_does_not_launch_where_the_redirected_pull_requests_agent_lives(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    pull_request_worktree = tmp_path / "owner_repo_51"
    pull_request_worktree.mkdir()
    panes = [{"id": 6, "title": "code_owner_repo_51", "pane_cwd": str(pull_request_worktree)}]
    session, _worktree, _ensure, launch_agent, _prepare = _authored_session(
        monkeypatch, tmp_path, panes
    )
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda _wd, item, **kwargs: pull_request_worktree if item.number == 51 else None,
    )

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert (result["status"], result["item_id"], result["pane_id"]) == (
        "already_running",
        "owner/repo#ISSUE-1",
        "terminal_6",
    )
    launch_agent.assert_not_called()


@pytest.mark.parametrize(
    ("pull_request", "instruction"),
    [
        pytest.param(
            {"checks": {HEAD_SHA: [check_run("tests", "FAILURE")]}},
            "has failing checks: tests",
            id="failing_ci",
        ),
        pytest.param(
            {"mergeable": "CONFLICTING", "checks": {HEAD_SHA: [check_run("tests", "SUCCESS")]}},
            "cannot merge cleanly into main (base head base1)",
            id="merge_conflicts",
        ),
        pytest.param(
            {"threads": [thread(comment("reviewer", created=AFTER_PUSH))]},
            None,
            id="review_feedback",
        ),
    ],
)
def test_autostarting_an_issue_with_an_authored_pull_request_repairs_that_pull_request(
    pull_request, instruction, monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), **pull_request)
    session, worktree, ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    [state] = GitHubClient().evaluate_pull_requests(
        [("owner/repo", 51)], github_username="testuser", todo_repository="testuser/todos"
    )
    prompt = _launched_prompt(launch_agent)
    worked_on = prepare.call_args.args[0]
    assert (worked_on.item_type, worked_on.number) == (WorkItemType.PR, 51)
    assert ensure.call_args.args[1] == worked_on
    assert result["cwd"] == str(worktree)
    if instruction is not None:
        normalized = " ".join(prompt.split())
        assert instruction in normalized
        assert (
            "development practices, skills, and coordination workflows you have been configured"
            in normalized
        )
        assert "do not create another pull request" in normalized.lower()
    # The result still names the target that was asked for.
    assert result["item_id"] == "owner/repo#ISSUE-1"
    assert prompt.startswith("PROMPT\n\nAUTOSTART: IMPLEMENT")
    assert prompt.endswith(render_work_instruction(state.action, state))
    # The configured-practices rule belongs to the appendix, so repairs get it too.
    assert prompt.count("coordination workflows") == 1


@pytest.mark.parametrize(
    ("pr_fields", "instructions"),
    [
        pytest.param(
            {
                "mergeable": "CONFLICTING",
                "checks": {HEAD_SHA: [check_run("tests", "FAILURE")]},
                "threads": [thread(comment("reviewer", created=AFTER_PUSH))],
            },
            (
                "has actionable review feedback",
                "Also resolve the confirmed merge conflicts into main (base head base1)",
                "Also investigate and fix these failing checks: tests.",
            ),
            id="review-conflicts-ci",
        ),
        pytest.param(
            {
                "reviews": [
                    review(
                        "reviewer",
                        "CHANGES_REQUESTED",
                        created=AFTER_PUSH,
                        body="Please rework this.",
                    )
                ]
            },
            (
                "full body of each changes-requested review",
                "resolve every inline review thread",
                "request re-review from each reviewer whose changes-requested review you addressed",
                "If a re-review request fails, report the blocker",
            ),
            id="requested-changes",
        ),
    ],
)
def test_autostart_briefs_the_agent_on_pr_feedback_in_work_order(
    monkeypatch, tmp_path, pr_fields, instructions
):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), **pr_fields)
    session, _worktree, _ensure, launch, _prepare = _authored_session(monkeypatch, tmp_path)

    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    prompt = " ".join(_launched_prompt(launch).split())
    assert prompt.startswith("PROMPT AUTOSTART: IMPLEMENT")
    positions = [prompt.index(instruction) for instruction in instructions]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    ("ci", "status"),
    [
        ("pending", "without a final CI result (its current head's checks are still running)"),
        ("passing", "waiting for human review (its current head's checks pass)"),
        (
            "no_checks",
            "waiting for human review (GitHub reports no required or observed checks for its current head)",
        ),
        (
            "unknown",
            "without a final CI result (GitHub reports an unknown or unavailable CI status for its current head, which is neither passing nor proof that no checks exist)",
        ),
    ],
    ids=["pending", "passing", "no_checks", "unknown"],
)
def test_autostarting_a_pull_request_waiting_for_others_orders_no_work(
    monkeypatch, tmp_path, ci, status
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    checks = (
        [check_run("tests", None, status="IN_PROGRESS")]
        if ci == "pending"
        else []
        if ci == "no_checks"
        else [check_run("tests")]
    )
    if ci == "unknown":
        github.ruleset_checks[("owner/repo", "main")] = ["build"]
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), checks={HEAD_SHA: checks})
    session, worktree, ensure, launch_agent, _prepare = _authored_session(monkeypatch, tmp_path)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    checkout = ensure.call_args.args[1]
    assert (checkout.repo, checkout.item_type, checkout.number) == (
        "owner/repo",
        WorkItemType.PR,
        51,
    )
    assert result["cwd"] == str(worktree)
    prompt = " ".join(_launched_prompt(launch_agent).split())
    assert status in prompt
    assert "do not invent changes, make redundant commits, or create another pull request" in prompt
    assert "AUTOSTART" not in prompt
    assert "--add-assignee" not in prompt and "force-push" not in prompt


def test_autostarting_an_issue_closed_by_somebody_elses_pull_request_still_implements_it(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), author="someone-else")
    session, worktree, ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.url = "https://github.com/owner/repo/issues/1"

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert prepare.call_args.args[0] is issue
    assert ensure.call_args.args[1] is issue
    assert result["cwd"] == str(worktree)
    prompt = _launched_prompt(launch_agent)
    assert prompt.startswith("PROMPT\n\nAUTOSTART: IMPLEMENT, PUBLISH AND VERIFY")
    assert prompt.count("AUTOSTART:") == 1
    assert "Continue the assigned issue https://github.com/owner/repo/issues/1." in prompt
    assert "development practices, skills, and coordination workflows" in " ".join(prompt.split())


def test_autostart_prefers_the_users_own_pull_request_among_several(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#50", closes=("owner/repo#1",), author="someone-else")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _worktree, _ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)

    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert prepare.call_args.args[0].number == 51
    assert "has failing checks: tests" in _launched_prompt(launch_agent)


@pytest.mark.parametrize("autostart", [False, True])
@pytest.mark.parametrize("target", [None, "owner/other"])
def test_coding_a_todo_protects_its_identity(monkeypatch, tmp_path, autostart, target) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#7", body=todo_body("owner/other"))
    session, _worktree, _ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)
    session.config = replace(session.config, todo_repository="testuser/todos")
    todo = _work_item(number=7, title="Todo")
    todo.repo = session.config.todo_repository
    todo.todo_target = target
    session.work_items.append(todo)

    session.code(target=format_work_item_id(todo), agent="pi", autostart=autostart)

    assert prepare.call_args.args[0] is todo
    prompt = _launched_prompt(launch_agent)
    assert "This work originates from a todo" in prompt
    assert "never disclose or reference the todo in the pull request" in prompt
    assert "Fixes testuser/todos#7" not in prompt
    assert "Fixes owner/other#7" not in prompt


@pytest.mark.parametrize(
    "target",
    ["owner/repo", "https://gitlab.com/owner/repo.git", "https://codeberg.org/owner/repo.git"],
)
def test_coding_a_targeted_todo_launches_in_its_checkout_with_the_original_task(
    target, monkeypatch, tmp_path
):
    prepare_prompt = control_module.prepare_launch_agent_prompt
    session, _, ensure, launch = _coding_session(monkeypatch, tmp_path, [])
    monkeypatch.setattr(control_module, "prepare_launch_agent_prompt", prepare_prompt)
    session.config = replace(session.config, todo_repository="testuser/todos")
    todo = replace(
        _github_issue("testuser/todos", 1, todo_target=target),
        url="https://github.com/testuser/todos/issues/1",
    )
    session.work_items = [todo]
    checkout = str(tmp_path / "owner_repo_todo_1")
    ensure.return_value = checkout
    body = "Find what makes the test fail intermittently"
    context = MagicMock(return_value={"title": todo.title, "body": body})
    monkeypatch.setattr(session.backend.github_client, "fetch_launch_context", context)

    result = session.code(target="owner/repo#ISSUE-WT1", agent="pi")

    assert result["cwd"] == launch.call_args.args[0] == checkout
    assert ensure.call_args.args == (str(tmp_path), todo)
    context.assert_called_once_with(todo)
    prompt = _launched_prompt(launch)
    assert {"- repo: testuser/todos", "- number: 1", f"- url: {todo.url}"} <= set(
        prompt.splitlines()
    )
    assert f"- repo: {target}" not in prompt.splitlines()
    assert body in prompt


def test_autostarting_a_review_reads_no_github_state_and_stays_review_only(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    github.add_pull_request(
        "owner/repo#2", author="someone-else", checks={HEAD_SHA: [check_run("tests", "FAILURE")]}
    )
    session.work_items.append(_work_item(number=2, title="Review", item_type=WorkItemType.PR))

    session.code(target="owner/repo#CHECK-2", agent="pi", autostart=True)

    prompt = " ".join(_launched_prompt(launch_agent).split())
    assert prompt.startswith("PROMPT AUTOSTART: REVIEW")
    assert prompt.count("AUTOSTART:") == 1
    assert (
        "start reviewing the pull request right away without asking for routine confirmation"
        in prompt
    )
    assert "Never edit files, commit, push, or change the pull request." in prompt
    assert "IMPLEMENT" not in prompt and "closing keyword" not in prompt
    assert "coordination workflows" not in prompt
    assert "--add-assignee" not in prompt and "force-push" not in prompt
    assert github.operations == []


def test_autostart_reports_a_failed_work_state_lookup_without_launching(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyIssueTree"] = failure("HTTP 502: Bad Gateway")
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])

    response = _post_item_action(session, "code", "owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert response.status_int == HTTPStatus.BAD_GATEWAY
    assert response.json["error"]["code"] == "github_error"
    launch_agent.assert_not_called()


def test_autostart_reads_github_outside_the_session_lock(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _worktree, _ensure, _launch_agent = _coding_session(monkeypatch, tmp_path, [])
    lock_held: list[bool] = []

    def gh(*args, **kwargs):
        lock_held.append(session._lock._is_owned())
        return github.run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", gh)

    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert lock_held
    assert not any(lock_held)


@pytest.mark.parametrize(
    ("body", "autostart", "autoclose"),
    [
        ({}, False, False),
        ({"autostart": None}, False, False),
        ({"autostart": False}, False, False),
        ({"autostart": True}, True, False),
        ({"autostart": True, "autoclose": True}, True, True),
        ({"autostart": True, "autoclose": False}, True, False),
    ],
)
def test_code_api_forwards_boolean_autostart_and_autoclose_defaulting_to_false(
    body, autostart, autoclose
) -> None:
    session = MagicMock()
    session.code.return_value = {"status": "launched"}
    request = Request.blank(
        "/api/v0/code",
        method="POST",
        content_type="application/json",
        body=json.dumps({"target": "owner/repo#1", **body}).encode(),
    )

    response = request.get_response(control_module._make_turbogears_app(session))

    assert response.status_int == 200
    session.code.assert_called_once_with(
        target="owner/repo#1", agent=None, autostart=autostart, autoclose=autoclose
    )


@pytest.mark.parametrize("value", ["false", "true", 1, 0, [], {}])
@pytest.mark.parametrize("field", ["autostart", "autoclose"])
def test_code_api_rejects_a_non_boolean_flag(field, value) -> None:
    session = MagicMock()
    request = Request.blank(
        "/api/v0/code",
        method="POST",
        content_type="application/json",
        body=json.dumps({"target": "owner/repo#1", field: value}).encode(),
    )

    response = request.get_response(control_module._make_turbogears_app(session))

    assert (response.status_int, json.loads(response.text)["error"]["code"]) == (
        400,
        "bad_request",
    )
    session.code.assert_not_called()


def test_list_api_hides_repositories_without_restricting_explicit_targets(tmp_path) -> None:
    hidden = _github_issue("archive/private", 7)
    hidden_todo = _github_issue(
        "owner/todos", 9, todo_target="git@gitlab.example.com:Platform_Team/import*export.git"
    )
    visible = _github_issue("archives/public", 8)
    items = [hidden, hidden_todo, visible]
    markers = {(WorkItemType.ISSUE, hidden.repo, hidden.number): "*"}
    analysis_path = tmp_path / "analysis.md"
    analysis_path.write_text("# Cached\n")
    backend = MagicMock()
    backend.load_items.return_value = (items, markers)
    backend.analyze_item.return_value = str(analysis_path)
    session = WorkdashSession(
        config=WorkdashConfig(excluded_repositories=["Archive/*", "platform_team/*"]),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    app = control_module._make_turbogears_app(session)
    expected_item_ids = [format_work_item_id(item) for item in items]
    expected_markers = markers.copy()

    response = Request.blank(
        "/api/v0/list", method="POST", content_type="application/json", body=b'{"refresh":true}'
    ).get_response(app)
    assert response.status_int == 200
    listed = json.loads(response.text)["result"]["items"]
    assert [(row["id"], row["suggested"]) for row in listed] == [("archives/public#ISSUE-8", False)]
    assert [format_work_item_id(item) for item in session.work_items] == expected_item_ids
    assert session.suggestion_markers == expected_markers

    response = Request.blank(
        "/api/v0/analyze",
        method="POST",
        content_type="application/json",
        body=b'{"target":"archive/private#ISSUE-7","agent":"cached"}',
    ).get_response(app)
    assert response.status_int == 200
    assert json.loads(response.text)["result"]["item_id"] == "archive/private#ISSUE-7"
    backend.github_client.fetch_item.assert_not_called()
    backend.analyze_item.assert_called_once_with(hidden, tool="cached")


def _post_item_action(session, action, target, **fields):
    request = Request.blank(
        f"/api/v0/{action}",
        method="POST",
        content_type="application/json",
        body=json.dumps({"target": target, **fields}).encode(),
    )
    return request.get_response(control_module._make_turbogears_app(session))


@pytest.mark.parametrize(
    ("fields", "agent", "group"),
    [
        pytest.param({"agent": "codex"}, "codex", "repo", id="selected-agent"),
        pytest.param({}, "pi", "repo", id="first-agent"),
        pytest.param({"group": "epic"}, "pi", "epic", id="requested-tab"),
    ],
)
def test_analyze_api_runs_the_configured_agent(monkeypatch, tmp_path, fields, agent, group):
    item = _work_item(number=1, title="Issue")
    analysis = tmp_path / "analysis.md"
    analysis.write_bytes(b"analysis body\n")
    worktree = str(tmp_path / "owner_repo_1")
    monkeypatch.setattr(control_module, "analysis_worktree", lambda *a, **k: nullcontext(worktree))
    analyze_item = MagicMock(side_effect=[None, str(analysis)])
    session = WorkdashSession(
        config=WorkdashConfig(
            agents={
                "pi": AgentConfig(analyze="pi"),
                "claude": AgentConfig(analyze="claude -p"),
                "codex": AgentConfig(analyze="codex exec"),
            }
        ),
        backend=SimpleNamespace(analyze_item=analyze_item, github_client=MagicMock()),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    response = _post_item_action(session, "analyze", "owner/repo#ISSUE-1", **fields)

    assert response.status_int == 200
    url = "http://localhost:80/api/items/owner/repo%23ISSUE-1/analysis"
    assert response.json == {
        "ok": True,
        "result": {
            "item_id": "owner/repo#ISSUE-1",
            "agent": agent,
            "path": str(analysis),
            "url": url,
            "markdown_url": url,
            "html_url": url + "/html",
            "cache_used": False,
            "status": "generated",
            "content_type": "text/markdown",
            "file_name": "analysis.md",
            "file_content": base64.b64encode(b"analysis body\n").decode(),
        },
    }
    assert analyze_item.call_args_list == [
        call(item, tool="cached"),
        call(item, tool=agent, cwd=worktree, zellij_session="workdash-main", group=group),
    ]


def test_code_api_launches_the_selected_agent(monkeypatch, tmp_path):
    session, worktree, _, launch_agent = _coding_session(monkeypatch, tmp_path, [])

    response = _post_item_action(session, "code", "owner/repo#ISSUE-1", agent="pi")

    assert response.status_int == 200
    assert response.json == {
        "ok": True,
        "result": {
            "status": "launched",
            "item_id": "owner/repo#ISSUE-1",
            "session": "workdash-main",
            "agent": "pi",
            "cwd": str(worktree),
            "pane_title": "code_owner_repo_1",
            "pane_id": "terminal_30",
            "tab_name": None,
        },
    }
    launch_agent.assert_called_once_with(
        str(worktree), "PROMPT", agent_command_tokens=["pi"], zellij_session="workdash-main"
    )


@pytest.mark.parametrize(
    ("action", "target", "repo", "number", "item_type", "item_id", "worktree_name"),
    [
        pytest.param(
            "analyze",
            "other/tool#7",
            "other/tool",
            7,
            WorkItemType.ISSUE,
            "other/tool#ISSUE-7",
            "other_tool_7",
            id="analyze-issue",
        ),
        pytest.param(
            "code",
            "owner/repo#ISSUE-99",
            "owner/repo",
            99,
            WorkItemType.ISSUE,
            "owner/repo#ISSUE-99",
            "owner_repo_99",
            id="code-issue",
        ),
        pytest.param(
            "terminal",
            "https://github.com/owner/repo/issues/42",
            "owner/repo",
            42,
            WorkItemType.ISSUE,
            "owner/repo#ISSUE-42",
            "owner_repo_42",
            id="terminal-issue",
        ),
        pytest.param(
            "code",
            "owner/repo#REVIEW-5",
            "owner/repo",
            5,
            WorkItemType.PR,
            "owner/repo#CHECK-5",
            "owner_repo_5",
            id="code-pr-id",
        ),
        pytest.param(
            "code",
            "https://github.com/owner/repo/pull/5",
            "owner/repo",
            5,
            WorkItemType.PR,
            "owner/repo#CHECK-5",
            "owner_repo_5",
            id="code-pr-url",
        ),
    ],
)
def test_actions_use_items_outside_the_dashboard_without_including_them(
    monkeypatch, tmp_path, action, target, repo, number, item_type, item_id, worktree_name
):
    visible_item = _work_item(number=1, title="Visible issue")
    item = replace(
        _work_item(number=5, title="Other work", item_type=item_type), repo=repo, number=number
    )
    config = WorkdashConfig(
        workdir=str(tmp_path),
        agents={"pi": AgentConfig(launch="pi"), "codex": AgentConfig(analyze="codex exec")},
    )
    github = MagicMock()
    github.fetch_item.return_value = item
    backend = WorkdashBackend(
        config=config,
        github_client=github,
        cache_root=tmp_path / "cache",
        included_items_store=IncludedItemsStore(tmp_path / "included-items.json"),
    )
    worktree = str(tmp_path / worktree_name)
    prepare = MagicMock(return_value=worktree)
    monkeypatch.setattr(control_module, "ensure_worktree", prepare)
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *a, **k: None)
    monkeypatch.setattr(control_module, "get_merge_base", lambda *a, **k: None)
    monkeypatch.setattr(control_module, "prepare_launch_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(
        control_module, "analysis_worktree", lambda *a, **k: nullcontext(prepare(*a, **k))
    )
    analysis = tmp_path / "analysis.md"
    analysis.write_text("# Analysis\n")
    analyze = MagicMock(side_effect=[None, str(analysis)])
    monkeypatch.setattr(backend, "analyze_item", analyze)
    terminal = MagicMock(
        return_value=SimpleNamespace(
            session="workdash-main",
            cwd=worktree,
            pane_title=f"terminal_{worktree_name}",
            pane_id="terminal_23",
        )
    )
    monkeypatch.setattr(control_module, "launch_terminal_context", terminal)
    panes = []

    def launch(cwd, *_args, **_kwargs):
        title = f"code_{worktree_name}"
        panes.append({"id": 30, "title": title, "pane_cwd": cwd})
        return SimpleNamespace(pane_id="terminal_30", pane_title=title, cwd=cwd, session=None)

    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: panes)
    monkeypatch.setattr(control_module.agent_execution.executor, "launch", launch)
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[visible_item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    fields = {"analyze": {"agent": "codex"}, "code": {"agent": "pi"}}
    response = _post_item_action(session, action, target, **fields.get(action, {}))

    assert response.status_int == 200, response.text
    assert response.json["ok"] is True
    result = response.json["result"]
    assert result["item_id"] == item_id
    if action == "analyze":
        analyze.assert_called_with(
            item, tool="codex", cwd=worktree, zellij_session="workdash-main", group="tool"
        )
    elif action == "code":
        assert result["status"] == "launched"
        assert [pane["pane_cwd"] for pane in panes] == [worktree]
    else:
        terminal.assert_called_once_with(worktree, zellij_session="workdash-main")
    github.fetch_item.assert_called_once_with(
        repo, number, github_username=config.github_username, todo_repository=config.todo_repository
    )
    assert prepare.call_count == 1
    assert prepare.call_args.args == (str(tmp_path), item)
    assert [row["id"] for row in session.list_items()["items"]] == ["owner/repo#ISSUE-1"]
    assert backend.included_items_store.load() == []


@pytest.mark.parametrize(
    ("target", "hidden"),
    [
        pytest.param("owner/repo#42", False, id="issue-number"),
        pytest.param("owner/repo#ISSUE-42", False, id="issue-id"),
        pytest.param("owner/repo#42", True, id="hidden-by-closing-pr"),
    ],
)
def test_terminal_resolves_issue_references(monkeypatch, tmp_path, target, hidden):
    item = replace(
        _work_item(number=1, title="Issue"),
        number=42,
        url="https://github.com/owner/repo/issues/42",
    )
    pull_request = replace(
        _work_item(number=5, title="Fix it", item_type=WorkItemType.PR),
        kind=WorkItemKind.AUTHORED_PR,
        closing_issue_numbers=(42,),
    )
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = item
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=backend,
        work_items=[pull_request] if hidden else [],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    worktree = str(tmp_path / "owner_repo_42")
    prepare = MagicMock(return_value=worktree)
    launch = MagicMock(
        return_value=SimpleNamespace(
            session="workdash-main",
            cwd=worktree,
            pane_title="terminal_owner_repo_42",
            pane_id="terminal_23",
        )
    )
    monkeypatch.setattr(control_module, "ensure_worktree", prepare)
    monkeypatch.setattr(control_module, "launch_terminal_context", launch)

    response = _post_item_action(session, "terminal", target)

    assert response.status_int == 200, response.text
    assert response.json["ok"] is True
    assert response.json["result"]["item_id"] == "owner/repo#ISSUE-42"
    assert backend.github_client.fetch_item.call_args.args == ("owner/repo", 42)
    assert prepare.call_args.args == (str(tmp_path), item)
    assert prepare.call_count == 1
    launch.assert_called_once_with(worktree, zellij_session="workdash-main")


def test_terminal_resolves_an_unlisted_targeted_todo_not_the_targets_issue(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#42", body=todo_body("owner/repo"))
    github.add_issue("owner/repo#42")
    session = _ready_session(tmp_path)
    lookup = MagicMock(wraps=session.backend.github_client.fetch_item)
    monkeypatch.setattr(session.backend.github_client, "fetch_item", lookup)
    worktree = str(tmp_path / "owner_repo_todo_42")
    prepare = MagicMock(return_value=worktree)
    launch = MagicMock(
        return_value=SimpleNamespace(
            session="workdash-main", cwd=worktree, pane_title="Todo", pane_id="terminal_23"
        )
    )
    monkeypatch.setattr(control_module, "ensure_worktree", prepare)
    monkeypatch.setattr(control_module, "launch_terminal_context", launch)

    response = _post_item_action(session, "terminal", "owner/repo#ISSUE-WT42")

    assert response.status_int == 200, response.text
    result = response.json["result"]
    assert (result["item_id"], result["cwd"], result["pane_id"]) == (
        "owner/repo#ISSUE-WT42",
        worktree,
        "terminal_23",
    )
    assert [called.args for called in lookup.call_args_list] == [("testuser/todos", 42)]
    assert prepare.call_count == 1
    workdir, item = prepare.call_args.args
    assert workdir == str(tmp_path)
    assert (item.repo, item.number, item.todo_target) == ("testuser/todos", 42, "owner/repo")
    launch.assert_called_once_with(worktree, zellij_session="workdash-main")
    assert session.work_items == []


def test_terminal_does_not_launch_when_todo_checkout_conflicts(monkeypatch, tmp_path):
    session = _ready_session(tmp_path)
    todo = _github_issue("testuser/todos", 1, todo_target="https://codeberg.org/owner/repo.git")
    session.work_items.append(todo)
    clone = tmp_path / "owner_repo"
    clone.mkdir()
    marker = clone / "work.txt"
    marker.write_text("local changes")

    def run(cmd, *, cwd, **kwargs):
        assert cwd == clone
        match cmd:
            case ["git", "rev-parse", "--show-toplevel"]:
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{clone}\n")
            case ["git", "config", "--local", "--get", "remote.origin.url"]:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="https://github.com/owner/repo.git\n"
                )
            case _:
                pytest.fail(f"Unexpected command against the unrelated clone: {cmd}")

    monkeypatch.setattr(subprocess, "run", run)
    launch = MagicMock()
    monkeypatch.setattr(control_module, "launch_terminal_context", launch)

    with pytest.raises(RuntimeError, match="belongs to a different repository"):
        session.terminal(target="owner/repo#ISSUE-WT1")

    launch.assert_not_called()
    assert marker.read_text() == "local changes"


def test_terminal_reuses_matching_todo_worktree_when_main_clone_is_unrelated(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    main = workdir / "owner_repo"
    worktree = workdir / "owner_repo_todo_110"
    source = tmp_path / "source"
    target = "https://codeberg.org/owner/repo.git"
    for path, origin in ((main, "https://github.com/owner/repo.git"), (source, target)):
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    (source / "work.txt").write_text("todo work")
    subprocess.run(["git", "add", "work.txt"], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        cwd=source,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-110", str(worktree)],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (main / "work.txt").write_text("other repository work")
    (worktree / "work.txt").write_text("uncommitted todo work")
    pulls = []
    monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: pulls.append(path))

    session = _ready_session(workdir)
    session.work_items.append(
        replace(_github_issue("testuser/todos", 1, todo_target=target), number=110)
    )
    launch = MagicMock(
        return_value=SimpleNamespace(
            cwd=str(worktree), session="workdash-main", pane_title="Todo", pane_id="terminal_23"
        )
    )
    monkeypatch.setattr(control_module, "launch_terminal_context", launch)

    assert session.terminal(target="owner/repo#ISSUE-WT110")["cwd"] == str(worktree)

    launch.assert_called_once_with(str(worktree), zellij_session="workdash-main")
    assert pulls == [worktree]
    assert (main / "work.txt").read_text() == "other repository work"
    assert (worktree / "work.txt").read_text() == "uncommitted todo work"
    assert subprocess.check_output(
        ["git", "remote", "get-url", "origin"], cwd=main, text=True
    ).strip() == ("https://github.com/owner/repo.git")
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=main, text=True) == (
        "?? work.txt\n"
    )


@pytest.mark.parametrize(
    ("target", "github_item", "error", "status"),
    [
        ("owner/repo#banana", _work_item(number=1, title="Issue"), "invalid_target", 400),
        ("owner/repo#404", None, "unknown_item", 404),
        (
            "owner/repo#5",
            _work_item(number=5, title="PR", item_type=WorkItemType.PR),
            "unsupported_item_type",
            400,
        ),
        ("owner/repo#ISSUE-77", RuntimeError("GitHub unavailable"), "github_error", 502),
    ],
    ids=["malformed", "missing", "wrong-kind", "outage"],
)
def test_terminal_api_rejects_unusable_targets_before_preparing_a_worktree(
    monkeypatch, target, github_item, error, status
):
    backend = MagicMock()
    if isinstance(github_item, RuntimeError):
        backend.github_client.fetch_item.side_effect = github_item
    else:
        backend.github_client.fetch_item.return_value = github_item
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    prepare = MagicMock()
    monkeypatch.setattr(control_module, "ensure_worktree", prepare)

    response = _post_item_action(session, "terminal", target)

    assert response.status_int == status
    assert response.json["ok"] is False
    assert response.json["error"]["code"] == error
    prepare.assert_not_called()


def test_analyze_uses_the_cached_analysis_of_an_item_fetched_from_github(tmp_path) -> None:
    analysis_path = tmp_path / "analysis.md"
    analysis_path.write_text("# Cached\n")
    item = _github_issue("other/tool", 7)
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = item
    backend.analysis_cache.load_analysis.return_value = "# Cached\n"
    backend.analyze_item.return_value = str(analysis_path)
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(analyze="pi")}),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    result = session.analyze(target="other/tool#7")

    assert (result["item_id"], result["cache_used"]) == ("other/tool#ISSUE-7", True)
    # Only the cache is read; no analysis agent runs.
    backend.analyze_item.assert_called_once_with(item, tool="cached")


def _github_issue(repo: str, number: int, *, todo_target: str | None = None) -> WorkItem:
    item = _work_item(number=number, title="From GitHub")
    item.repo = repo
    item.todo_target = todo_target
    return item


@pytest.mark.parametrize(
    ("target", "github_item", "code", "message"),
    [
        (
            "owner/repo#PR-WT5",
            None,
            "invalid_target",
            "'owner/repo#PR-WT5' is not a Workdash item ID, an owner/repo#NUMBER issue, "
            "or a GitHub issue or pull request URL.",
        ),
        (
            "owner/repo#5",
            _work_item(number=5, title="A PR", item_type=WorkItemType.PR),
            "unsupported_item_type",
            "owner/repo#5 is a pull request; use owner/repo#PR-5.",
        ),
        (
            "owner/repo#CHECK-5",
            _work_item(number=5, title="An issue"),
            "unsupported_item_type",
            "owner/repo#5 is an issue; use owner/repo#ISSUE-5.",
        ),
        (
            # The todo issue exists, but it targets another repository.
            "owner/repo#ISSUE-WT5",
            _github_issue("testuser/todos", 5, todo_target="other/repo"),
            "unknown_item",
            "No accessible GitHub issue or pull request matches 'owner/repo#ISSUE-WT5'.",
        ),
    ],
)
def test_explicit_targets_are_checked_against_what_github_reports(
    target, github_item, code, message
) -> None:
    backend = MagicMock()
    backend.github_client.fetch_item.return_value = github_item
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    with pytest.raises(control_module.WorkdashControlError) as error:
        session.terminal(target=target)

    assert (error.value.code, str(error.value)) == (code, message)
    assert session.work_items == []


def test_api_rejects_non_localhost_remote_addr() -> None:
    def unexpected_app(_environ, _start_response):
        raise AssertionError("non-localhost request reached the API app")

    response: list[tuple[str, dict[str, str]]] = []

    def start_response(status, headers, _exc_info=None):
        response.append((status, dict(headers)))

    body = b"".join(
        control_module._source_and_header_guarded_wsgi_app(unexpected_app)(
            {"REMOTE_ADDR": "203.0.113.1"}, start_response
        )
    )

    assert response[0][0] == "403 Forbidden"
    assert json.loads(body.decode("utf-8")) == {
        "ok": False,
        "error": {
            "code": "forbidden",
            "message": "Workdash server does not accept this source IP.",
        },
    }


def test_listeners_share_sources_and_stop_all_sockets_and_threads(monkeypatch) -> None:
    import socket
    from http.client import HTTPConnection

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    monkeypatch.setattr(
        control_module.urllib.request,
        "urlopen",
        control_module.urllib.request.build_opener(
            control_module.urllib.request.ProxyHandler({})
        ).open,
    )
    create_todo = MagicMock(side_effect=AssertionError("denied request executed a todo"))

    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=SimpleNamespace(github_client=SimpleNamespace(create_todo=create_todo)),
        work_items=[_work_item(number=1, title="Shared item")],
        suggestion_markers={},
        zellij_session=None,
    )
    hosts = ("127.0.0.1", "127.0.0.2", "127.0.0.3")
    server = control_module.WorkdashControlServer(
        session,
        listeners=(
            (hosts[0], ("127.0.0.18",)),
            (hosts[1], ("127.0.0.0/29",)),
            (hosts[2], ("127.0.0.21",)),
        ),
    )
    server.start()
    httpds, threads = list(server._httpds), list(server._threads)
    try:
        for host in hosts:
            for source, status in (
                (hosts[1], 200),
                (hosts[2], 200),
                ("127.0.0.18", 200),
                ("127.0.0.21", 200),
                ("127.0.0.6", 200),
                ("127.0.0.9", 403),
            ):
                connection = HTTPConnection(host, port, timeout=2, source_address=(source, 0))
                try:
                    connection.request(
                        "POST",
                        "/api/v0/list",
                        "{}",
                        {"Content-Type": "application/json", "X-Workdash": version("workdash")},
                    )
                    response = connection.getresponse()
                    assert response.status == status
                    envelope = json.loads(response.read())
                    if status == 200:
                        assert envelope["ok"] is True
                        [item] = envelope["result"]["items"]
                        assert (item["id"], item["title"]) == ("owner/repo#ISSUE-1", "Shared item")
                    else:
                        assert envelope["error"]["code"] == "forbidden"
                finally:
                    connection.close()
            denied = HTTPConnection(host, port, timeout=2, source_address=("127.0.0.9", 0))
            try:
                denied.request(
                    "POST",
                    "/api/v0/todo",
                    '{"text":"not allowed"}',
                    {
                        "Content-Type": "application/json",
                        "X-Workdash": version("workdash"),
                        "X-Forwarded-For": "127.0.0.1",
                        "Forwarded": "for=127.0.0.1",
                    },
                )
                response = denied.getresponse()
                assert response.status == 403
                assert json.loads(response.read())["error"]["code"] == "forbidden"
            finally:
                denied.close()
        # The real urllib client reaches both hostname and alternate-IP listeners.
        for host in ("localhost", hosts[1]):
            result = control_module.WorkdashControlClient(host).request("list")
            assert result["items"][0]["id"] == "owner/repo#ISSUE-1"
        create_todo.assert_not_called()
    finally:
        server.stop()
    assert all(not thread.is_alive() for thread in threads)
    assert all(httpd.socket.fileno() == -1 for httpd in httpds)
    for host in hosts:
        with socket.socket() as replacement:
            replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            replacement.bind((host, port))


def test_starting_the_server_starts_autoclose_and_stopping_it_ends_its_worker(monkeypatch) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Shared item")],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    server = control_module.WorkdashControlServer(session)

    server.start()
    worker = session.autoclose._worker
    assert worker is not None and worker.is_alive()

    server.stop()
    worker.join(timeout=5)
    assert not worker.is_alive()


def test_shutdown_waits_for_inflight_autodispatch_start(monkeypatch) -> None:
    import socket
    import threading

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi", analyze="pi -p")}),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    entered, release = threading.Event(), threading.Event()
    original = session.start_autodispatch

    def delayed_start(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    dispatch_release = threading.Event()

    def dispatch(**kwargs):
        assert dispatch_release.wait(5)
        return {"status": "no_ready_task"}

    monkeypatch.setattr(session, "start_autodispatch", delayed_start)
    monkeypatch.setattr(session, "dispatch_once", dispatch)
    server = control_module.WorkdashControlServer(session)
    server.start()
    results = []
    request = threading.Thread(
        target=lambda: results.append(
            control_module.WorkdashControlClient().request("autodispatch", {"scope": "owner/repo"})
        )
    )
    request.start()
    try:
        assert entered.wait(5)
        stop = threading.Thread(target=server.stop)
        stop.start()
        stop.join(1)
        assert stop.is_alive(), "shutdown returned before the request completed"
    finally:
        release.set()
        request.join(5)
        stop.join(5)
        dispatch_release.set()
    assert not request.is_alive() and not stop.is_alive()
    assert results[0]["id"] == session.autodispatch_sessions()[0]["id"]
    assert session.autodispatch_sessions()[0]["status"] == "stopped"


def test_listener_bind_failure_releases_all_earlier_sockets(monkeypatch) -> None:
    import errno
    import socket

    failing_host = "127.0.0.3"
    bound = []
    make_server = control_module.make_server

    def record_server(host, port, app, **kwargs):
        httpd = make_server(host, port, app, **kwargs)
        bound.append(httpd)
        return httpd

    monkeypatch.setattr(control_module, "make_server", record_server)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.3", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(control_module, "SERVER_PORT", port)
        server = control_module.WorkdashControlServer(
            MagicMock(),
            listeners=(
                ("127.0.0.2", ("192.0.2.1",)),
                (failing_host, ("192.0.2.2",)),
            ),
        )
        with pytest.raises(RuntimeError, match=f"{failing_host}:{port}") as error:
            server.start()
        assert "Cannot bind Workdash server" in str(error.value)
        assert "another Workdash server" in str(error.value)
        assert "WORKDASH_HOST" in str(error.value)
        assert isinstance(error.value.__cause__, OSError)
        assert error.value.__cause__.errno == errno.EADDRINUSE
        assert len(bound) == 2
        assert all(httpd.socket.fileno() == -1 for httpd in bound)
        for host in ("127.0.0.1", "127.0.0.2"):
            with socket.socket() as replacement:
                replacement.bind((host, port))
        server.stop()


def test_duplicate_extra_listener_fails_and_releases_sockets(monkeypatch) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    server = control_module.WorkdashControlServer(
        MagicMock(),
        listeners=(("127.0.0.2", ("192.0.2.1",)), ("127.0.0.2", ("192.0.2.2",))),
    )
    try:
        with pytest.raises(RuntimeError, match="127.0.0.2") as error:
            server.start()
        assert f"127.0.0.2:{port}" in str(error.value)
        assert "WORKDASH_HOST" in str(error.value)
        for host in ("127.0.0.1", "127.0.0.2"):
            with socket.socket() as replacement:
                replacement.bind((host, port))
    finally:
        server.stop()


def test_wildcard_listener_is_rejected_and_closed(monkeypatch) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    server = control_module.WorkdashControlServer(
        MagicMock(), listeners=(("0.0.0.0", ("192.0.2.1",)),)
    )
    with pytest.raises(RuntimeError, match="Cannot bind Workdash server"):
        server.start()
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(("127.0.0.1", port))


@pytest.mark.parametrize("body", [b"not json", b'{"ok":false}', b'{"ok":true,"result":[]}'])
def test_client_names_selected_host_for_invalid_responses(monkeypatch, body) -> None:
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr(control_module.urllib.request, "urlopen", urlopen)
    with pytest.raises(control_module.WorkdashControlError, match="dashboard.lan:8765") as error:
        control_module.WorkdashControlClient("dashboard.lan").request("list", {"refresh": True})
    assert error.value.code == "invalid_response"
    request = urlopen.call_args.args[0]
    assert request.get_header("X-workdash") == version("workdash")
    assert request.full_url == "http://dashboard.lan:8765/api/v0/list"
    assert json.loads(request.data) == {"refresh": True}


def test_client_preserves_api_errors_and_names_selected_host(monkeypatch) -> None:
    import io
    import urllib.error

    failure = urllib.error.HTTPError(
        "http://dashboard.lan:8765/api/v0/list",
        403,
        "Forbidden",
        {},
        io.BytesIO(b'{"ok":false,"error":{"code":"forbidden","message":"Denied"}}'),
    )
    monkeypatch.setattr(control_module.urllib.request, "urlopen", MagicMock(side_effect=failure))
    with pytest.raises(
        control_module.WorkdashControlError, match="dashboard.lan:8765.*Denied"
    ) as error:
        control_module.WorkdashControlClient("dashboard.lan").request("list")
    assert error.value.status == 403
    assert error.value.code == "forbidden"
    assert error.value.__cause__ is failure


def test_client_names_selected_host_when_unreachable(monkeypatch) -> None:
    failure = ConnectionRefusedError("connection refused")
    monkeypatch.setattr(control_module.urllib.request, "urlopen", MagicMock(side_effect=failure))
    with pytest.raises(control_module.WorkdashControlError, match="dashboard.lan:8765") as error:
        control_module.WorkdashControlClient("dashboard.lan").request("list")
    assert error.value.status == 503
    assert error.value.code == "server_unreachable"
    assert error.value.__cause__ is failure


def test_probe_answers_without_waiting_for_the_session_lock(monkeypatch) -> None:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda session: [])
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="project",
    )
    server = control_module.WorkdashControlServer(session)
    urlopen = MagicMock(wraps=control_module.urllib.request.urlopen)
    monkeypatch.setattr(control_module.urllib.request, "urlopen", urlopen)
    server.start()
    try:
        with session._lock:
            assert probe_dashboard_session(f"127.0.0.1:{port}") == "project"
        request = urlopen.call_args.args[0]
        assert request.get_method() == "POST"
        assert request.full_url == f"http://127.0.0.1:{port}/api/v0/info"
        assert json.loads(request.data) == {"panes": "none"}
    finally:
        server.stop()


def test_probe_refuses_a_server_that_is_not_a_dashboard(monkeypatch) -> None:
    import socket
    import threading
    from wsgiref.simple_server import make_server

    monkeypatch.setattr(control_module.time, "sleep", lambda seconds: None)

    def not_workdash(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"ok": true, "result": {"panes": []}}']

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    httpd = make_server("127.0.0.1", port, not_workdash)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(WorkdashControlError):
            probe_dashboard_session(f"127.0.0.1:{port}")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_show_config_api_reports_session_agents_and_default_server(monkeypatch):
    monkeypatch.delenv("WORKDASH_HOST", raising=False)
    session = WorkdashSession(
        config=WorkdashConfig(
            agents={
                "codex": AgentConfig(analyze="codex exec", launch="codex"),
                "claude": AgentConfig(analyze="claude -p", launch="claude"),
                "pi": AgentConfig(launch="pi"),
            },
        ),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        "/api/v0/show-config",
        method="POST",
        headers={"X-Workdash": version("workdash")},
        environ={"REMOTE_ADDR": "127.0.0.1"},
        content_type="application/json",
        body=b"{}",
    )

    response = request.get_response(app)

    assert response.status_int == 200
    payload = response.json
    assert payload["ok"] is True
    assert payload["result"] == {
        "agents": {"analyze": ["codex", "claude"], "code": ["codex", "claude", "pi"]},
        "server": {"host": "127.0.0.1", "port": 8765},
    }


def test_show_config_payload_reports_environment_server_address(monkeypatch):
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:18765")
    assert control_module.show_config_payload(WorkdashConfig())["server"] == {
        "host": "127.0.0.1",
        "port": 18765,
    }


def test_control_server_checks_loopback_alias_sources(monkeypatch):
    import errno
    import socket
    from http.client import HTTPConnection

    sources = (("127.0.0.2", 200), ("127.0.0.3", 403))
    for source, _ in sources:
        with socket.socket() as probe:
            try:
                probe.bind((source, 0))
            except OSError as error:
                if error.errno != errno.EADDRNOTAVAIL:
                    raise
                pytest.skip(f"loopback alias {source} is not available")

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    host = "127.0.0.1"
    monkeypatch.setenv("WORKDASH_HOST", f"{host}:{port}")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Isolated")],
        suggestion_markers={},
        zellij_session=None,
    )
    listeners = ((host, ("127.0.0.2",)),)
    server = control_module.WorkdashControlServer(session, listeners=listeners)
    server.start()
    try:
        for source, status in sources:
            connection = HTTPConnection(host, port, timeout=2, source_address=(source, 0))
            try:
                connection.request(
                    "POST",
                    "/api/v0/list",
                    "{}",
                    {"Content-Type": "application/json", "X-Workdash": version("workdash")},
                )
                response = connection.getresponse()
                assert response.status == status
                if status == 200:
                    assert json.loads(response.read())["result"]["items"][0]["id"] == (
                        "owner/repo#ISSUE-1"
                    )
            finally:
                connection.close()
    finally:
        server.stop()


def test_two_control_servers_route_cli_by_address(monkeypatch, capsys):
    import socket

    import workdash.workdash as cli

    ports = []
    servers = []
    try:
        for number in (1, 2):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            assert port not in ports
            ports.append(port)
            monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
            session = WorkdashSession(
                config=WorkdashConfig(),
                backend=MagicMock(),
                work_items=[_work_item(number=number, title=f"Server {number}")],
                suggestion_markers={},
                zellij_session=None,
            )
            server = control_module.WorkdashControlServer(session)
            server.start()
            servers.append(server)
        assert cli.main(["list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["title"] == "Server 2"
        assert cli.main(["--host", f"127.0.0.1:{ports[0]}", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["title"] == "Server 1"
    finally:
        for server in servers:
            server.stop()


def test_explicit_host_only_client_uses_default_port(monkeypatch, capsys):
    import socket
    import urllib.request

    import workdash.workdash as cli

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Default port")],
        suggestion_markers={},
        zellij_session=None,
    )
    server = control_module.WorkdashControlServer(session)
    server.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    requested = []

    def redirect(request, timeout=None):
        requested.append(request.full_url)
        return opener.open(
            urllib.request.Request(
                request.full_url.replace("127.0.0.1:8765", f"127.0.0.1:{port}"),
                data=request.data,
                headers=dict(request.header_items()),
                method=request.method,
            )
        )

    monkeypatch.setattr(control_module.urllib.request, "urlopen", redirect)
    try:
        assert cli.main(["--host", "127.0.0.1", "list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["title"] == "Default port"
        assert requested == ["http://127.0.0.1:8765/api/v0/list"]
    finally:
        server.stop()


@pytest.mark.parametrize("host_only", [True, False])
def test_local_non_loopback_primary_accepts_default_client(monkeypatch, host_only):
    import socket
    from ipaddress import ip_address

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        try:
            route.connect(("192.0.2.1", 80))
            host = route.getsockname()[0]
        except OSError:
            pytest.skip("no local IPv4 network route")
    if ip_address(host).is_loopback:
        pytest.skip("no non-loopback IPv4 address")
    with socket.socket() as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    monkeypatch.setenv("WORKDASH_HOST", host if host_only else f"{host}:{port}")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Local network")],
        suggestion_markers={},
        zellij_session=None,
    )
    server = control_module.WorkdashControlServer(session)
    server.start()
    try:
        result = control_module.WorkdashControlClient().request("list")
        assert result["items"][0]["title"] == "Local network"
    finally:
        server.stop()


def test_selected_primary_accepts_itself_but_not_an_unlisted_peer(monkeypatch):
    import errno
    import socket
    from http.client import HTTPConnection

    host = "127.0.0.2"
    unlisted = "127.0.0.3"
    for peer in (unlisted, host):
        with socket.socket() as probe:
            try:
                probe.bind((peer, 0))
            except OSError as error:
                if error.errno != errno.EADDRNOTAVAIL:
                    raise
                pytest.skip(f"loopback alias {peer} is unavailable")
            port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"{host}:{port}")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Work")],
        suggestion_markers={},
        zellij_session=None,
    )
    server = control_module.WorkdashControlServer(session)
    server.start()
    try:
        for peer, status in ((host, 200), (unlisted, 403)):
            connection = HTTPConnection(host, port, timeout=2, source_address=(peer, 0))
            try:
                connection.request(
                    "POST",
                    "/api/v0/list",
                    "{}",
                    {"Content-Type": "application/json", "X-Workdash": version("workdash")},
                )
                response = connection.getresponse()
                assert response.status == status
                envelope = json.loads(response.read())
                if status == 200:
                    assert envelope["result"]["items"][0]["id"] == "owner/repo#ISSUE-1"
                else:
                    assert envelope["error"]["code"] == "forbidden"
            finally:
                connection.close()
    finally:
        server.stop()


def test_wildcard_primary_is_rejected_without_opening_listeners(monkeypatch):
    import socket

    with socket.socket() as probe:
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"0.0.0.0:{port}")
    server = control_module.WorkdashControlServer(
        MagicMock(), listeners=(("127.0.0.2", ("192.0.2.1",)),)
    )
    with pytest.raises(RuntimeError, match="Wildcard listener binds"):
        server.start()
    assert not server._httpds


def test_grouped_code_preserves_existing_claim_and_reports_actual_tab(monkeypatch, tmp_path):
    panes = [
        {"id": 4, "title": "code_owner_repo_1", "pane_cwd": "/elsewhere", "tab_name": "actual"}
    ]
    session, _, _, launch = _coding_session(monkeypatch, tmp_path, panes)
    result = session.code(target="owner/repo#ISSUE-1", group="requested")
    assert result["status"] == "already_running"
    assert result["tab_name"] == "actual"
    launch.assert_not_called()


def test_code_api_forwards_group_and_rejects_invalid_group():
    session = MagicMock()
    session.code.return_value = {"status": "launched"}
    app = control_module._make_turbogears_app(session)
    for group, status in [("epic", 200), ("", 400), ([], 400)]:
        request = Request.blank(
            "/api/v0/code",
            method="POST",
            content_type="application/json",
            body=json.dumps({"target": "owner/repo#1", "group": group}).encode(),
        )
        assert request.get_response(app).status_int == status
    session.code.assert_called_once_with(
        target="owner/repo#1", agent=None, autostart=False, group="epic", autoclose=False
    )


def test_review_api_forwards_group_and_rejects_invalid_group():
    session = MagicMock()
    session.review.return_value = {"status": "started"}
    app = control_module._make_turbogears_app(session)
    for group, status in [("epic", 200), ("", 400), ([], 400)]:
        request = Request.blank(
            "/api/v0/review",
            method="POST",
            content_type="application/json",
            body=json.dumps({"target": "owner/repo#1", "group": group}).encode(),
        )
        assert request.get_response(app).status_int == status
    session.review.assert_called_once_with(
        target="owner/repo#1", agent=None, background=True, group="epic"
    )


def test_grouped_code_forwards_group_and_reports_live_tab(monkeypatch, tmp_path):
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    panes = []
    session, _, _, launch = _coding_session(monkeypatch, tmp_path, panes)
    launch.side_effect = lambda repo, prompt, **kwargs: (
        panes.append({"id": 30, "title": "code_owner_repo_1", "pane_cwd": repo, "tab_name": "epic"})
        or SimpleNamespace(
            session=None, pane_id="terminal_30", pane_title="code_owner_repo_1", cwd=repo
        )
    )
    result = session.code(target="owner/repo#ISSUE-1", group="epic")
    assert launch.call_args.kwargs["group"] == "epic"
    assert result["status"] == "launched"
    assert result["tab_name"] == "epic"


def test_code_rejects_blank_group_before_looking_up_item():
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    with pytest.raises(control_module.WorkdashControlError) as error:
        session.code(target="owner/repo#1", group="  ")
    assert error.value.code == "bad_request"
    session.backend.github_client.fetch_item.assert_not_called()


def _pin_test_worktree(stack, path):
    if path is None:
        return None
    fd = control_module.os.open(path, control_module.os.O_RDONLY | control_module.os.O_DIRECTORY)
    stack.callback(control_module.os.close, fd)
    return path, fd


@pytest.mark.parametrize(
    ("filename", "content_type", "encoding", "compression"),
    [
        ("image.svgz", "image/svg+xml", "gzip", "gzip"),
        ("image.svg.bz2", "application/x-bzip2", None, "bzip2"),
        ("image.svg.xz", "application/x-xz", None, "xz"),
        ("archive.tar.gz", "application/gzip", None, "gzip"),
        ("data.unknownworkdash.gz", "application/gzip", None, "gzip"),
        ("archive.tar.bz2", "application/x-bzip2", None, "bzip2"),
    ],
)
def test_compressed_item_file_response(
    tmp_path, monkeypatch, filename, content_type, encoding, compression
):
    import bz2
    import gzip
    import lzma

    worktree = tmp_path / "checkout"
    artifacts = worktree / ".workdash"
    artifacts.mkdir(parents=True)
    original = b"artifact contents"
    compressor = {"bzip2": bz2, "gzip": gzip, "xz": lzma}[compression]
    compressed = compressor.compress(original)
    (artifacts / filename).write_bytes(compressed)
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree),
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    path = f"/api/items/owner/repo%23ISSUE-1/files/.workdash/{filename}"
    request = Request.blank(path)
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    request.environ["REMOTE_ADDR"] = "192.0.2.1"
    assert request.get_response(app).status_int == 403
    for method in ("POST", "OPTIONS"):
        request = Request.blank(path, method=method)
        request.environ["REMOTE_ADDR"] = "127.0.0.1"
        assert request.get_response(app).status_int == 403
    assert response.status_int == 200
    assert response.content_type == content_type
    policy = {
        words[0]: words[1:]
        for directive in response.headers["Content-Security-Policy"].split(";")
        if (words := directive.split())
    }
    assert policy == {
        "sandbox": [],
        "default-src": ["'none'"],
        "img-src": ["http:", "https:", "data:"],
        "style-src": ["'unsafe-inline'"],
    }
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers.get("Content-Encoding") == encoding
    assert response.body == compressed
    assert compressor.decompress(response.body) == original


@pytest.mark.parametrize("symlinked_worktree", [False, True])
def test_item_files_http_confinement(tmp_path, monkeypatch, symlinked_worktree):
    worktree = tmp_path / "checkout"
    artifacts = worktree / ".workdash"
    artifacts.mkdir(parents=True)
    (artifacts / "review.md").write_text("real review")
    (artifacts / "nested").mkdir()
    (artifacts / "nested" / "review.md").write_text("nested review")
    (worktree / "secret").write_text("secret")
    (artifacts / "escape").symlink_to(worktree / "secret")
    (artifacts / "escape-dir").symlink_to(worktree)
    (artifacts / "root-alias").symlink_to(".")
    (artifacts / "alias").symlink_to(artifacts / "review.md")
    (artifacts / "alias.md").symlink_to("review.md")
    (artifacts / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    (artifacts / "data.unknownworkdash").write_bytes(b"\x00\xff")
    (artifacts / "nested-alias").symlink_to("nested")
    (artifacts / "nested" / "relative-alias").symlink_to("../review.md")
    (artifacts / "chain").symlink_to("nested/relative-alias")
    (artifacts / "nested" / "escape").symlink_to("../../secret")
    (artifacts / "chain-escape").symlink_to("nested/escape")
    (artifacts / "loop").symlink_to("loop")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    worktree_path = worktree
    if symlinked_worktree:
        worktree_path = tmp_path / "checkout-link"
        worktree_path.symlink_to(worktree, target_is_directory=True)
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree_path),
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )

    def get(path, method="GET", peer="127.0.0.1", header=version("workdash")):
        headers = {"X-Workdash": header} if header is not None else {}
        request = Request.blank(path, method=method, headers=headers)
        request.environ["REMOTE_ADDR"] = peer
        return request.get_response(app)

    base = "/api/items/owner/repo%23ISSUE-1/files/"
    response = get(base + ".workdash/review.md")
    assert response.status_int == 200 and response.text == "real review", response.text
    assert response.content_type == "text/markdown"
    assert get(base + ".workdash/nested/review.md").text == "nested review"
    for name, body, media_type in (
        ("alias.md", b"real review", "text/markdown"),
        ("image.png", b"\x89PNG\r\n\x1a\n\x00\xff", "image/png"),
        ("data.unknownworkdash", b"\x00\xff", "application/octet-stream"),
    ):
        response = get(base + ".workdash/" + name)
        assert response.status_int == 200
        assert response.body == body
        assert response.content_type == media_type
    for header in (None, "wrong"):
        response = get(base + ".workdash/review.md", header=header)
        assert response.status_int == 200
        assert response.body == b"real review"
    assert get(base + ".workdash/alias").text == "real review"
    assert get(base + ".workdash/nested-alias/relative-alias").text == "real review"
    assert get(base).json["result"]["entries"] == [
        "alias",
        "alias.md",
        "chain",
        "data.unknownworkdash",
        "image.png",
        "nested",
        "nested-alias",
        "review.md",
        "root-alias",
    ]
    for path in (
        ".workdash/../secret",
        ".workdash/%2e%2e/secret",
        "secret",
        ".workdash/escape",
        ".workdash/escape-dir/secret",
        ".workdash/root-alias",
        "/etc/passwd",
    ):
        response = get(base + path)
        assert response.status_int in (403, 404), path
        assert response.json["ok"] is False
    assert get(base, method="POST").status_int == 405
    assert get(base, peer="192.0.2.7").status_int == 403
    session.backend.github_client.fetch_item.return_value = None
    assert get("/api/items/owner/repo%23ISSUE-2/files/").status_int == 404
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, pinned, git: None)
    assert get(base).status_int == 404
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree),
    )
    (artifacts / "review.md").unlink()
    (artifacts / "escape").unlink()
    (artifacts / "escape-dir").unlink()
    (artifacts / "root-alias").unlink()
    (artifacts / "alias").unlink()
    (artifacts / "alias.md").unlink()
    (artifacts / "image.png").unlink()
    (artifacts / "data.unknownworkdash").unlink()
    (artifacts / "nested-alias").unlink()
    (artifacts / "chain").unlink()
    (artifacts / "chain-escape").unlink()
    (artifacts / "loop").unlink()
    (artifacts / "nested" / "escape").unlink()
    (artifacts / "nested" / "relative-alias").unlink()
    (artifacts / "nested" / "review.md").unlink()
    (artifacts / "nested").rmdir()
    artifacts.rmdir()
    assert get(base).status_int == 404
    artifacts.symlink_to(tmp_path)
    assert get(base).status_int in (403, 404)


@pytest.mark.parametrize("component", ["file", "alias-target", "directory", "root", "listing"])
def test_item_files_reject_symlink_substitution(tmp_path, monkeypatch, component):
    worktree = tmp_path / "checkout"
    artifacts = worktree / ".workdash"
    directory = artifacts / "nested"
    directory.mkdir(parents=True)
    (directory / "review.md").write_text("safe")
    (artifacts / "alias").symlink_to("nested/review.md")
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "nested" / "review.md").write_text("secret")
    (outside / "review.md").write_text("secret")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree),
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )

    original_open = control_module.os.open
    original_read = Path.read_bytes
    original_iterdir = Path.iterdir
    replaced = False

    def replace():
        nonlocal replaced
        if replaced:
            return
        replaced = True
        target = {
            "file": directory / "review.md",
            "alias-target": directory / "review.md",
            "directory": directory,
            "root": artifacts,
            "listing": artifacts,
        }[component]
        destination = {
            "file": outside / "review.md",
            "alias-target": outside / "review.md",
            "directory": outside,
            "root": outside,
            "listing": outside,
        }[component]
        target.rename(target.with_name(target.name + ".old"))
        target.symlink_to(destination)

    def open_with_replacement(path, flags, *args, **kwargs):
        name = {
            "file": "review.md",
            "alias-target": "review.md",
            "directory": "nested",
            "root": ".workdash",
            "listing": ".workdash",
        }[component]
        if path == name and kwargs.get("dir_fd") is not None:
            replace()
        return original_open(path, flags, *args, **kwargs)

    def read_with_replacement(path):
        replace()
        return original_read(path)

    def list_with_replacement(path):
        replace()
        return original_iterdir(path)

    monkeypatch.setattr(control_module.os, "open", open_with_replacement)
    monkeypatch.setattr(Path, "read_bytes", read_with_replacement)
    monkeypatch.setattr(Path, "iterdir", list_with_replacement)
    suffix = {
        "listing": "",
        "alias-target": ".workdash/alias",
    }.get(component, ".workdash/nested/review.md")
    request = Request.blank(
        f"/api/items/owner/repo%23ISSUE-1/files/{suffix}",
        headers={"X-Workdash": version("workdash")},
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert replaced
    assert response.status_int == 404
    assert "secret" not in response.text


@pytest.mark.parametrize("suffix", ["", ".workdash/review.md"])
def test_item_files_reject_worktree_symlink_substitution(tmp_path, monkeypatch, suffix):
    worktree = tmp_path / "checkout"
    (worktree / ".workdash").mkdir(parents=True)
    (worktree / ".workdash" / "review.md").write_text("safe")
    outside = tmp_path / "outside"
    (outside / ".workdash").mkdir(parents=True)
    (outside / ".workdash" / "review.md").write_text("secret")
    (outside / ".workdash" / "outside-only.txt").write_text("secret")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )

    original_open = control_module.os.open

    def open_with_replacement(path, flags, *args, **kwargs):
        fd = original_open(path, flags, *args, **kwargs)
        if path == worktree:
            worktree.rename(tmp_path / "original")
            worktree.symlink_to(outside)
        return fd

    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree),
    )
    monkeypatch.setattr(control_module.os, "open", open_with_replacement)
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        f"/api/items/owner/repo%23ISSUE-1/files/{suffix}",
        headers={"X-Workdash": version("workdash")},
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int in (200, 404)
    assert "secret" not in response.text
    if not suffix and response.status_int == 200:
        assert "outside-only.txt" not in response.json["result"]["entries"]
        assert response.json["result"]["entries"] == ["review.md"]


def test_item_files_listing_validates_pinned_entries_after_replacement(tmp_path, monkeypatch):
    worktree = tmp_path / "checkout"
    artifacts = worktree / ".workdash"
    artifacts.mkdir(parents=True)
    (worktree / "secret").write_text("secret")
    (artifacts / "escape").symlink_to(worktree / "secret")
    (artifacts / "safe").write_text("safe")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree),
    )
    original_listdir = control_module.os.listdir

    def list_with_replacement(fd):
        artifacts.rename(worktree / ".workdash-old")
        artifacts.mkdir()
        (artifacts / "escape").write_text("harmless")
        (artifacts / "safe").write_text("safe")
        return original_listdir(fd)

    monkeypatch.setattr(control_module.os, "listdir", list_with_replacement)
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        "/api/items/owner/repo%23ISSUE-1/files/", headers={"X-Workdash": version("workdash")}
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int == 404 or response.json["result"]["entries"] == ["safe"]


def test_item_files_listing_rejects_directory_substitution(tmp_path, monkeypatch):
    worktree = tmp_path / "checkout"
    artifacts = worktree / ".workdash"
    nested = artifacts / "nested"
    nested.mkdir(parents=True)
    (nested / "review.md").write_text("safe")
    (artifacts / "alias").symlink_to("nested/review.md")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "review.md").write_text("secret")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(
        control_module,
        "existing_worktree_path",
        lambda *args, pinned, git: _pin_test_worktree(pinned, worktree),
    )
    original_open = control_module.os.open
    replaced = False

    def open_with_replacement(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == "nested" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            nested.rename(artifacts / "old")
            nested.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(control_module.os, "open", open_with_replacement)
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        "/api/items/owner/repo%23ISSUE-1/files/", headers={"X-Workdash": version("workdash")}
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert replaced
    assert "alias" not in response.json["result"]["entries"]


def test_item_files_list_and_read_from_issue_worktree(tmp_path):
    checkout = tmp_path / "owner_repo_1"
    subprocess.run(["git", "init", "-q", "-b", "issue-1", str(checkout)], check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=checkout,
        check=True,
    )
    artifacts = checkout / ".workdash"
    artifacts.mkdir()
    (artifacts / "review.md").write_text("Review contents")
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Issue")],
        suggestion_markers={},
        zellij_session=None,
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    base = "/api/items/owner/repo%23ISSUE-1/files/"
    listing = Request.blank(base, environ={"REMOTE_ADDR": "127.0.0.1"}).get_response(app)
    assert listing.status_int == 200
    assert listing.json["result"]["entries"] == ["review.md"]
    artifact = Request.blank(
        base + ".workdash/review.md", environ={"REMOTE_ADDR": "127.0.0.1"}
    ).get_response(app)
    assert artifact.status_int == 200
    assert artifact.text == "Review contents"


@pytest.mark.parametrize("symlinked_worktree", [False, True])
def test_item_files_resolve_shared_pr_and_targeted_todo_worktrees(
    tmp_path, monkeypatch, symlinked_worktree
):
    resolve = Path.resolve

    def resolve_without_fd_path(self, *args, **kwargs):
        if self.parent == Path("/dev/fd"):
            return self
        return resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_without_fd_path)
    pr = replace(
        _work_item(number=2, title="PR", item_type=WorkItemType.PR),
        kind=WorkItemKind.AUTHORED_PR,
        closing_issue_numbers=(1,),
    )
    todo = replace(_work_item(number=3, title="Todo"), repo="owner/todos", todo_target="owner/repo")
    for name, content in (("owner_repo_1", "shared"), ("owner_repo_todo_3", "todo")):
        path = tmp_path / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "remote",
                "add",
                "origin",
                "https://github.com/owner/repo.git",
            ],
            check=True,
        )
        (path / ".workdash").mkdir()
        (path / ".workdash" / "artifact.txt").write_text(content)
        if symlinked_worktree:
            target = tmp_path / (name + "-target")
            path.rename(target)
            path.symlink_to(target, target_is_directory=True)
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path), todo_repository="owner/todos"),
        backend=MagicMock(),
        work_items=[pr, todo],
        suggestion_markers={},
        zellij_session=None,
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    for item_id, expected in (("owner/repo%23PR-2", "shared"), ("owner/repo%23ISSUE-WT3", "todo")):
        request = Request.blank(
            f"/api/items/{item_id}/files/.workdash/artifact.txt",
            headers={"X-Workdash": version("workdash")},
        )
        request.environ["REMOTE_ADDR"] = "127.0.0.1"
        response = request.get_response(app)
        assert response.status_int == 200
        assert response.text == expected


@pytest.mark.parametrize("suffix", ["", ".workdash/artifact.txt"])
def test_item_files_reject_worktree_replacement_after_resolution(tmp_path, monkeypatch, suffix):
    checkout = tmp_path / "owner_repo_1"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/repo.git",
        ],
        check=True,
    )
    (checkout / ".workdash").mkdir()
    (checkout / ".workdash" / "artifact.txt").write_text("safe")
    outside = tmp_path / "outside"
    (outside / ".workdash").mkdir(parents=True)
    (outside / ".workdash" / "artifact.txt").write_text("secret")
    (outside / ".workdash" / "outside-only.txt").write_text("secret")
    subprocess.run(["git", "init", "-q", str(outside)], check=True)
    subprocess.run(
        ["git", "-C", str(outside), "remote", "add", "origin", "https://github.com/owner/repo.git"],
        check=True,
    )
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    original = control_module.existing_worktree_path

    def replace_after_resolution(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result is not None and result[0] == checkout
        checkout.rename(tmp_path / "old")
        checkout.symlink_to(outside)
        return result

    monkeypatch.setattr(control_module, "existing_worktree_path", replace_after_resolution)
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        f"/api/items/owner/repo%23ISSUE-1/files/{suffix}",
        headers={"X-Workdash": version("workdash")},
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int in (200, 404)
    assert "secret" not in response.text
    if not suffix and response.status_int == 200:
        assert "outside-only.txt" not in response.json["result"]["entries"]
        assert response.json["result"]["entries"] == ["artifact.txt"]


def test_item_files_pins_worktree_before_git_validation(tmp_path, monkeypatch):
    from workdash.git import GitHelper

    checkout = tmp_path / "owner_repo_1"
    checkout.mkdir()
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "add",
            "origin",
            "https://github.com/owner/repo.git",
        ],
        check=True,
    )
    (checkout / ".workdash").mkdir()
    (checkout / ".workdash" / "artifact.txt").write_text("safe")
    outside = tmp_path / "outside"
    (outside / ".workdash").mkdir(parents=True)
    (outside / ".workdash" / "artifact.txt").write_text("secret")
    original = GitHelper.worktree_proves_item
    descriptors = []

    def replace_during_validation(self, candidate, item, *, directory):
        descriptors.append(int(directory.name))
        checkout.rename(tmp_path / "old")
        checkout.symlink_to(outside)
        return original(self, candidate, item, directory=directory)

    monkeypatch.setattr(GitHelper, "worktree_proves_item", replace_during_validation)
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="one")],
        suggestion_markers={},
        zellij_session=None,
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        "/api/items/owner/repo%23ISSUE-1/files/.workdash/artifact.txt",
        headers={"X-Workdash": version("workdash")},
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int in (200, 404)
    assert "secret" not in response.text
    assert descriptors
    for fd in descriptors:
        with pytest.raises(OSError):
            control_module.os.fstat(fd)


@pytest.mark.parametrize("autostart", [False, True])
def test_coding_authored_pr_uses_linked_issue_analysis_and_claim(monkeypatch, tmp_path, autostart):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, worktree, ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.analysis = "issue analysis"
    pr = _work_item(number=2, title="PR", item_type=WorkItemType.PR)
    pr.number = 51
    pr.kind = WorkItemKind.AUTHORED_PR
    pr.analysis = "pr analysis"
    session.work_items.append(pr)

    result = session.code(target="owner/repo#PR-51", agent="pi", autostart=autostart)
    assert result["item_id"] == "owner/repo#ISSUE-1"
    assert prepare.call_args.kwargs["analysis_path"] == session.analysis_url(issue)
    checkout = ensure.call_args.args[1]
    assert prepare.call_args.args[0] is checkout
    assert (checkout.repo, checkout.item_type, checkout.number) == (
        "owner/repo",
        WorkItemType.PR if autostart else WorkItemType.ISSUE,
        51 if autostart else 1,
    )
    if autostart:
        session.git.branch_tracks_repository.assert_called_with(worktree, "pr-branch", "owner/repo")
    assert ("has failing checks: tests" in _launched_prompt(launch)) == autostart
    assert github.operations.count("ReadyPullRequests") == int(autostart)

    repeated = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=autostart)

    assert (repeated["status"], repeated["pane_id"]) == ("already_running", result["pane_id"])
    launch.assert_called_once()


def test_coding_shared_worktree_does_not_report_the_requested_agent(monkeypatch, tmp_path):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    session, worktree, _ensure, launch, _prepare = _authored_session(monkeypatch, tmp_path)
    worktree.rmdir()
    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    session.git.find_worktree_for_branch.return_value = None
    session.git.local_branch_exists.return_value = False
    session.git.worktree_proves_item.return_value = True
    session.git.create_worktree.side_effect = lambda _repo, path, *_: path.mkdir()
    session.config = replace(
        session.config, agents={**session.config.agents, "claude": AgentConfig(launch="claude")}
    )
    session.work_items.append(
        replace(
            _work_item(number=1, title="PR", item_type=WorkItemType.PR),
            number=51,
            kind=WorkItemKind.AUTHORED_PR,
            closing_issue_numbers=(1,),
        )
    )

    first = session.code(target="owner/repo#PR-51", agent="pi")
    second = session.code(target="owner/repo#ISSUE-1", agent="claude")

    assert (first["status"], first["agent"], first["item_id"], first["cwd"]) == (
        "launched",
        "pi",
        "owner/repo#ISSUE-1",
        str(worktree),
    )
    assert (second["status"], second["pane_id"], second["agent"]) == (
        "already_running",
        first["pane_id"],
        None,
    )
    launch.assert_called_once()


@pytest.mark.parametrize("autostart", [False, True])
def test_coding_pr_ignores_removed_issue_link_without_changing_dashboard(
    monkeypatch, tmp_path, autostart
):
    github = install_fake_github(monkeypatch)
    github.add_pull_request("owner/repo#51")
    session, _, _, _, _ = _authored_session(monkeypatch, tmp_path)
    pr = replace(
        _work_item(number=2, title="PR", item_type=WorkItemType.PR),
        number=51,
        kind=WorkItemKind.AUTHORED_PR,
        closing_issue_numbers=(1,),
        linked_issues=(("owner/repo", 1),),
    )
    session.work_items.append(pr)
    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    session.git.find_worktree_for_branch.return_value = None
    session.git.local_branch_exists.return_value = False

    result = session.code(target="owner/repo#PR-51", agent="pi", autostart=autostart)

    assert (result["item_id"], result["cwd"]) == (
        "owner/repo#PR-51",
        str(tmp_path / "owner_repo_51"),
    )
    assert session.work_items[-1] is pr
    assert pr.closing_issue_numbers == (1,)
    assert pr.linked_issues == (("owner/repo", 1),)


def test_authored_pr_without_issue_never_uses_pr_analysis(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_pull_request("owner/repo#51")
    session, worktree, ensure, _launch, prepare = _authored_session(monkeypatch, tmp_path)
    pr = _work_item(number=2, title="PR", item_type=WorkItemType.PR)
    pr.number = 51
    pr.kind = WorkItemKind.AUTHORED_PR
    pr.analysis = "pr analysis"
    session.work_items.append(pr)
    result = session.code(target="owner/repo#PR-51", agent="pi")
    assert result["item_id"] == "owner/repo#PR-51"
    assert prepare.call_args.kwargs["analysis_path"] is None

    assert ensure.call_args.args[1] is pr
    assert result["cwd"] == str(worktree)
    session.git.branch_tracks_repository.assert_called_with(worktree, "pr-branch", "owner/repo")


def test_coding_somebody_elses_pr_uses_pr_analysis_not_the_linked_issue_analysis(
    monkeypatch, tmp_path
):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#2", closes=("owner/repo#1",), author="someone-else")
    session, _worktree, _ensure, launch, _prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.analysis = "issue analysis"
    pr = replace(
        _work_item(number=2, title="Review", item_type=WorkItemType.PR),
        analysis="pr analysis",
        closing_issue_numbers=(1,),
    )
    session.work_items.append(pr)
    monkeypatch.setattr(
        control_module, "prepare_launch_agent_prompt", launcher_module.prepare_launch_agent_prompt
    )
    monkeypatch.setattr(session.backend.github_client, "fetch_launch_context", lambda _item: {})

    session.code(target="owner/repo#CHECK-2", agent="pi")

    prompt = _launched_prompt(launch)
    assert "Discuss review findings, risks, and open questions for the author." in prompt
    assert session.analysis_url(pr) in prompt
    assert session.analysis_url(issue) not in prompt


def test_issue_redirect_preserves_issue_analysis(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    session, _worktree, _ensure, _launch, prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.analysis = "issue analysis"
    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)
    assert prepare.call_args.kwargs["analysis_path"] == session.analysis_url(issue)


def test_plan_first_issue_uses_linked_pr_as_context_not_checkout(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1", body="Issue implementation goal")
    github.add_pull_request(
        "other/implementation#51",
        closes=("owner/repo#1",),
        body="PR implementation details",
        threads=[
            thread(comment("reviewer", created=AFTER_PUSH) | {"body": "Add a regression case"})
            | {"path": "src/app.py", "line": 7}
        ],
    )
    session, worktree, ensure, launch, _ = _authored_session(monkeypatch, tmp_path)
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *_args, **kwargs: None)
    monkeypatch.setattr(
        control_module, "prepare_launch_agent_prompt", launcher_module.prepare_launch_agent_prompt
    )
    entered, release = Event(), Event()
    client = session.backend.github_client
    fetch = client.linked_authored_open_pull_request_context

    def slow_fetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return fetch(*args, **kwargs)

    monkeypatch.setattr(client, "linked_authored_open_pull_request_context", slow_fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        coding = pool.submit(session.code, target="owner/repo#ISSUE-1", agent="pi")
        try:
            assert entered.wait(5)
            listing = pool.submit(session.list_items).result(timeout=1)
            assert [item["id"] for item in listing["items"]] == ["owner/repo#ISSUE-1"]
        finally:
            release.set()
        result = coding.result(timeout=5)

    assert (result["item_id"], result["cwd"]) == ("owner/repo#ISSUE-1", str(worktree))
    issue = session.work_items[0]
    assert ensure.call_args.args[1] is issue
    prompt = _launched_prompt(launch)
    assert prompt.startswith("TASK: IMPLEMENT\n")
    assert "https://github.com/other/implementation/pull/51" in prompt
    assert "Issue implementation goal" in prompt
    assert "WORK ALREADY IN PROGRESS:" in prompt
    assert "PR implementation details" in prompt
    assert '<thread path="src/app.py" line="7">' in prompt
    assert "Add a regression case" in prompt
    assert "do not assume this checkout contains its commits" in prompt
    assert "AUTOSTART" not in prompt


def test_plan_first_authored_pr_and_issue_use_same_context_and_worktree(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    results = []
    for target in ("owner/repo#ISSUE-1", "owner/repo#PR-51"):
        session, worktree, ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)
        issue = session.work_items[0]
        issue.analysis = "issue analysis"
        pr = _work_item(number=2, title="PR", item_type=WorkItemType.PR)
        pr.number = 51
        pr.kind = WorkItemKind.AUTHORED_PR
        session.work_items.append(pr)
        monkeypatch.setattr(
            control_module,
            "existing_worktree_path",
            lambda _wd, item, **kwargs: worktree if item.item_type == WorkItemType.ISSUE else None,
        )
        result = session.code(target=target, agent="pi")
        results.append(
            (
                result["item_id"],
                ensure.call_args.args[1],
                prepare.call_args.args,
                {k: v for k, v in prepare.call_args.kwargs.items() if k != "github_client"},
                launch.call_args.args,
            )
        )
    assert results[0] == results[1]
    assert results[0][1] is results[0][2][0]
    assert results[0][1].item_type == WorkItemType.ISSUE


def test_dispatch_revalidates_and_retries_claimed_candidate(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2")
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _session: [])
    session = _ready_session(tmp_path)
    calls = []

    def code(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"status": "already_running"}
        return {
            "status": "launched",
            "pane_id": "terminal_7",
            "agent": "pi",
            "autoclose_enabled": True,
        }

    monkeypatch.setattr(session, "code", code)
    request = Request.blank(
        "/api/v0/dispatch",
        method="POST",
        content_type="application/json",
        body=json.dumps({"scope": "owner/repo", "agent": "pi"}).encode(),
    )
    response = request.get_response(control_module._make_turbogears_app(session))
    result = json.loads(response.text)["result"]
    assert response.status_int == 200
    assert result["issue_id"] == "owner/repo#ISSUE-2"
    assert result["pane_id"] == "terminal_7"
    assert [call["target"] for call in calls] == ["owner/repo#ISSUE-1", "owner/repo#ISSUE-2"]
    assert all(
        call["autostart"] and call["autoclose"] and call["group"] == "repo" for call in calls
    )


def test_dispatch_skips_claim_before_checking_launch_limit(monkeypatch, tmp_path):
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, _worktree, _ensure, launch = _coding_session(monkeypatch, tmp_path, panes)
    candidate = SimpleNamespace(item=session.work_items[0], pull_request=None)
    from workdash.work_state import ScopedReadyWork

    scope_issue = SimpleNamespace(item=session.work_items[0], is_open=True, blocked=True)
    monkeypatch.setattr(
        session, "_ready", lambda *args, **kwargs: ScopedReadyWork([candidate], scope_issue)
    )
    limit = MagicMock(side_effect=RuntimeError("limit reached"))
    on_ready = MagicMock()

    result = session.dispatch_once(
        scope="owner/repo", agent="pi", before_launch=limit, on_ready=on_ready
    )

    assert result["status"] == "no_ready_task"
    limit.assert_not_called()
    on_ready.assert_not_called()
    launch.assert_not_called()


def test_dispatch_notifies_after_launch_limit_check(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _worktree, _ensure, launch = _coding_session(monkeypatch, tmp_path, [])
    from workdash.work_state import ReadyWork, ScopedReadyWork

    candidate = ReadyWork(session.work_items[0])
    scope_issue = SimpleNamespace(item=session.work_items[0], is_open=True, blocked=True)
    monkeypatch.setattr(
        session, "_ready", lambda *args, **kwargs: ScopedReadyWork([candidate], scope_issue)
    )
    calls = []

    def limit(item_id):
        calls.append(item_id)
        raise RuntimeError("limit reached")

    with pytest.raises(RuntimeError, match="limit reached"):
        session.dispatch_once(
            scope="owner/repo",
            agent="pi",
            before_launch=limit,
            on_ready=lambda: calls.append("ready"),
        )
    assert calls == ["owner/repo#ISSUE-1"]
    launch.assert_not_called()

    calls.clear()
    result = session.dispatch_once(
        scope="owner/repo",
        agent="pi",
        before_launch=lambda item_id: calls.append(item_id),
        on_ready=lambda: calls.append("ready"),
    )
    assert result["status"] == "launched"
    assert calls == ["owner/repo#ISSUE-1", "ready"]


def test_remote_dispatch_cli_through_http(monkeypatch, tmp_path, capsys):
    import socket

    import workdash.workdash as cli

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _, _, launch = _coding_session(monkeypatch, tmp_path, [])
    session.config = replace(session.config, github_username="testuser")
    server = control_module.WorkdashControlServer(session)
    server.start()
    host = f"127.0.0.1:{port}"
    try:
        assert (
            cli.main(["dispatch", "owner/repo", "--host", host, "--agent", "missing", "--json"])
            == 1
        )
        assert "Agent 'missing' is not configured" in capsys.readouterr().err
        with pytest.raises(WorkdashControlError) as error:
            control_module.WorkdashControlClient(host).request(
                "dispatch", {"scope": "owner/repo", "agent": "missing"}
            )
        assert error.value.code == "unknown_agent"
        assert (
            cli.main(
                [
                    "dispatch",
                    "owner/repo",
                    "--host",
                    host,
                    "--agent",
                    "pi",
                    "--group",
                    "epic",
                    "--json",
                ]
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
        assert (result["status"], result["issue_id"], result["agent"], result["group"]) == (
            "launched",
            "owner/repo#ISSUE-1",
            "pi",
            "epic",
        )
        assert result["pane_id"] == "terminal_30"
        assert result["autoclose_enabled"] is True
        assert launch.call_count == 1
    finally:
        server.stop()
    assert cli.main(["dispatch", "owner/repo", "--host", host, "--json"]) == 1
    assert "No Workdash server is reachable" in capsys.readouterr().err
    with pytest.raises(WorkdashControlError) as error:
        control_module.WorkdashControlClient(host).request("dispatch", {"scope": "owner/repo"})
    assert error.value.code == "server_unreachable"


def test_dispatch_launches_and_supervises_selected_issue(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _, _, launch = _coding_session(monkeypatch, tmp_path, [])
    session.config = replace(
        session.config, github_username="testuser", todo_repository="testuser/todos"
    )

    result = session.dispatch_once(scope="owner/repo", agent="pi")

    assert (result["status"], result["issue_id"], result["action"]) == (
        "launched",
        "owner/repo#ISSUE-1",
        "implement",
    )
    assert result["pane_id"] == "terminal_30"
    assert result["autoclose_enabled"] is True
    assert launch.call_count == 1
    assert launch.call_args.kwargs["group"] == "repo"
    assert ("workdash-main", "terminal_30") in session.autoclose._panes
    assert "AUTOSTART: IMPLEMENT" in _launched_prompt(launch)


@pytest.mark.parametrize("target", ["owner/other", "https://github.com/owner/other.git"])
def test_dispatch_groups_unstarted_targeted_todo_by_implementation_repo(
    monkeypatch, tmp_path, target
):
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#7", body=todo_body(target))
    session, _, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    session.config = replace(session.config, todo_repository="testuser/todos")
    todo = _work_item(number=7, title="Todo")
    todo.repo = "testuser/todos"
    todo.todo_target = target
    session.work_items.append(todo)

    result = session.dispatch_once(scope="owner/other#ISSUE-WT7", agent="pi")

    assert (result["status"], result["issue_id"], result["group"]) == (
        "launched",
        "owner/other#ISSUE-WT7",
        "other",
    )
    assert launch.call_args.kwargs["group"] == "other"


def test_dispatch_resumes_targeted_todo_pull_request(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#7", body=todo_body("owner/other"))
    github.add_pull_request(
        "owner/other#51",
        closes=("testuser/todos#7",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _, ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)
    session.config = replace(session.config, todo_repository="testuser/todos")
    todo = _work_item(number=7, title="Todo")
    todo.repo = "testuser/todos"
    todo.todo_target = "owner/other"
    session.work_items.append(todo)

    result = session.dispatch_once(scope="owner/other#ISSUE-WT7", agent="pi")

    assert (result["issue_id"], result["action"]) == ("owner/other#ISSUE-WT7", "fix_ci")
    pr = ensure.call_args.args[1]
    assert (pr.item_type, pr.repo, pr.number) == (WorkItemType.PR, "owner/other", 51)
    assert prepare.call_args.args[0] is pr
    assert "has failing checks: tests" in _launched_prompt(launch)
    assert "This work originates from a todo" in _launched_prompt(launch)
    assert "never disclose or reference the todo in the pull request" in _launched_prompt(launch)


@pytest.mark.parametrize("change", ["closed", "blocked", "descendant", "external_pr"])
def test_dispatch_retries_when_issue_becomes_ineligible_before_final_check(
    monkeypatch, tmp_path, change
):
    github = install_fake_github(monkeypatch)
    issue = github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2")
    session, _, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    next_issue = _work_item(number=2, title="Next issue")
    next_issue.url = "https://github.com/owner/repo/issues/2"
    session.work_items.append(next_issue)
    find = session.backend.github_client.find_ready_work
    calls = 0

    def change_before_final_check(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "closed":
                issue["state"] = "CLOSED"
            elif change == "blocked":
                github.add_issue("owner/other#3")
                issue["blocked_by"] = ("owner/other#3",)
            elif change == "descendant":
                github.add_issue("owner/other#3", parent="owner/repo#1")
            else:
                github.add_pull_request(
                    "owner/repo#51", closes=("owner/repo#1",), author="someoneelse"
                )
        return find(**kwargs)

    monkeypatch.setattr(session.backend.github_client, "find_ready_work", change_before_final_check)
    result = session.dispatch_once(scope="owner/repo", agent="pi")

    assert result["issue_id"] == "owner/repo#ISSUE-2"
    assert launch.call_count == 1
    assert "github.com/owner/repo/issues/2" in _launched_prompt(launch)


def test_dispatch_retries_another_issue_for_same_pr(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#10", state="CLOSED")
    issue = github.add_issue("owner/repo#1", parent="owner/repo#10")
    github.add_issue("owner/repo#2", parent="owner/repo#10")
    github.add_issue("owner/repo#3")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1", "owner/repo#2", "owner/repo#3"),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _, ensure, launch, _ = _authored_session(monkeypatch, tmp_path)
    session.work_items.extend(
        [_work_item(number=10, title="Parent"), _work_item(number=2, title="Second")]
    )
    assert [item["id"] for item in session.ready(scope="owner/repo#10")["items"]] == [
        "owner/repo#ISSUE-1"
    ]
    find = session.backend.github_client.find_ready_work
    calls = 0

    def block_before_final_check(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            github.add_issue("owner/other#3")
            issue["blocked_by"] = ("owner/other#3",)
        return find(**kwargs)

    monkeypatch.setattr(session.backend.github_client, "find_ready_work", block_before_final_check)
    result = session.dispatch_once(scope="owner/repo#10", agent="pi")

    assert (result["status"], result["issue_id"], result["action"], result["group"]) == (
        "launched",
        "owner/repo#ISSUE-2",
        "fix_ci",
        "repo",
    )
    assert result["reasons"] == ["fix_ci"]
    assert ensure.call_args.args[1].number == 51
    assert launch.call_count == 1
    assert "has failing checks: tests" in _launched_prompt(launch)


@pytest.mark.parametrize("change", ["merged", "ci_fixed"])
def test_dispatch_retries_when_pr_stops_needing_work_before_final_check(
    monkeypatch, tmp_path, change
):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2")
    pr = github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    issue = _work_item(number=2, title="Next issue")
    issue.url = "https://github.com/owner/repo/issues/2"
    session.work_items.append(issue)
    find = session.backend.github_client.find_ready_work
    calls = 0

    def change_before_final_check(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "merged":
                pr["state"] = "MERGED"
            else:
                pr["checks"] = {HEAD_SHA: [check_run("tests")]}
        return find(**kwargs)

    monkeypatch.setattr(session.backend.github_client, "find_ready_work", change_before_final_check)
    result = session.dispatch_once(scope="owner/repo", agent="pi")

    assert result["issue_id"] == "owner/repo#ISSUE-2"
    assert result["pane_id"] == "terminal_30"
    assert launch.call_count == 1
    assert "github.com/owner/repo/issues/2" in _launched_prompt(launch)


def test_dispatch_uses_current_reasons_when_pr_remains_actionable(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    pr = github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        mergeable="CONFLICTING",
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _, ensure, launch, _ = _authored_session(monkeypatch, tmp_path)
    find = session.backend.github_client.find_ready_work
    calls = 0

    def resolve_conflict(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            pr["mergeable"] = "MERGEABLE"
        return find(**kwargs)

    monkeypatch.setattr(session.backend.github_client, "find_ready_work", resolve_conflict)
    result = session.dispatch_once(scope="owner/repo", agent="pi")

    assert (result["status"], result["issue_id"], result["pr_id"]) == (
        "launched",
        "owner/repo#ISSUE-1",
        "owner/repo#PR-51",
    )
    assert (result["action"], result["reasons"], result["mergeability"]) == (
        "fix_ci",
        ["fix_ci"],
        "mergeable",
    )
    assert ensure.call_args.args[1].number == 51
    assert launch.call_count == 1
    assert "has failing checks: tests" in _launched_prompt(launch)
    assert "merge conflict" not in _launched_prompt(launch).lower()


@pytest.mark.parametrize("replacement_pr", [False, True])
def test_dispatch_briefs_current_github_work_after_revalidation(
    monkeypatch, tmp_path, replacement_pr
):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    pr = github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("old-check", "FAILURE")]},
    )
    session, _, ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.analysis = "issue analysis"
    ready = session._ready
    calls = 0
    current_number = 52 if replacement_pr else 51
    current_head = "new-head"

    def change_after_revalidation(*args, **kwargs):
        nonlocal calls
        result = ready(*args, **kwargs)
        calls += 1
        if calls == 2:
            current = pr
            if replacement_pr:
                pr["state"] = "CLOSED"
                current = github.add_pull_request("owner/repo#52", closes=("owner/repo#1",))
            current["head"] = current_head
            current["checks"] = {current_head: [check_run("current-check", "FAILURE")]}
        return result

    monkeypatch.setattr(session, "_ready", change_after_revalidation)
    result = session.dispatch_once(scope="owner/repo", agent="pi")

    assert result["status"] == "launched"
    assert result["pr_id"] == "owner/repo#PR-51"
    assert result["failing_checks"] == ["old-check"]
    assert ensure.call_args.args[1].number == current_number
    assert prepare.call_args.args[0].number == current_number
    assert prepare.call_args.kwargs["analysis_path"] == session.analysis_url(issue)
    prompt = _launched_prompt(launch)
    assert f"https://github.com/owner/repo/pull/{current_number} at head {current_head}" in prompt
    assert "has failing checks: current-check" in prompt
    assert "old-check" not in prompt
    assert launch.call_count == 1
    supervised = session.autoclose._panes[("workdash-main", result["pane_id"])]
    assert supervised.item.number == current_number


def test_dispatch_uses_newly_actionable_pr_before_launch(monkeypatch, tmp_path):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    first = github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("old-check", "FAILURE")]},
    )
    second = github.add_pull_request("owner/other#52", closes=("owner/repo#1",))
    panes = []
    session, _, _, launch, _ = _authored_session(monkeypatch, tmp_path, panes)
    find = session.backend.github_client.find_ready_work
    calls = 0

    def switch_checks(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            first["checks"] = {HEAD_SHA: [check_run("old-check")]}
            second["checks"] = {HEAD_SHA: [check_run("new-check", "FAILURE")]}
        return find(**kwargs)

    def start(repo, *_args, **_kwargs):
        title = f"code_{Path(repo).name}"
        panes.append({"id": 30, "title": title, "pane_cwd": repo})
        return SimpleNamespace(session=None, pane_id="terminal_30", pane_title=title, cwd=repo)

    monkeypatch.setattr(session.backend.github_client, "find_ready_work", switch_checks)
    launch.side_effect = start
    git = MagicMock()
    git.find_worktree_for_branch.return_value = None
    monkeypatch.setattr(repo_worktree, "GitHelper", lambda: git)
    session.git = git
    monkeypatch.setattr(repo_worktree, "existing_worktree_path", lambda *_, **kwargs: None)
    github_helper = MagicMock()
    github_helper.fetch_worktree_head.return_value = ("pr-52-branch", "owner/other")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", github_helper.fetch_worktree_head
    )
    monkeypatch.setattr(
        session.backend.github_client, "clone_repository", github_helper.clone_repository
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)

    result = session.dispatch_once(scope="owner/repo#1", agent="pi")

    assert (result["status"], result["issue_id"], result["pr_id"], result["action"]) == (
        "launched",
        "owner/repo#ISSUE-1",
        "owner/other#PR-52",
        "fix_ci",
    )
    assert result["reasons"] == ["fix_ci"]
    assert result["group"] == launch.call_args.kwargs["group"] == "other"
    assert result["pane_id"] == "terminal_30"
    assert github_helper.fetch_worktree_head.call_args.args[0].number == 52
    assert git.create_worktree.call_args.args[2:] == ("pr-52-branch", "origin/pr-52-branch")
    assert "has failing checks: new-check" in _launched_prompt(launch)
    assert launch.call_count == 1


@pytest.mark.parametrize("branch", ["local-pr-branch", "selected-pr-branch"])
@pytest.mark.parametrize("mode", ["dispatch", "autostart", "explicit-pr"])
@pytest.mark.parametrize(
    ("head_repo", "fork_remote", "tracking_remote", "tracking_branch", "matches"),
    [
        ("owner/repo", None, "origin", "selected-pr-branch", True),
        ("testuser/repo", "testuser/repo", "fork", "selected-pr-branch", True),
        ("testuser/repo", "testuser/repo", "origin", "selected-pr-branch", False),
        ("testuser/repo", "testuser/repo", "fork", "other-branch", False),
        ("testuser/repo", "testuser/repo", "fork", "ambiguous", False),
        ("testuser/repo", "testuser/repo", None, None, False),
        ("owner/repo", None, None, None, False),
        ("testuser/repo", "other/repo", None, None, False),
        ("testuser/repo", "testuser/repo", None, "partial", False),
        ("testuser/repo", "testuser/repo", None, "missing-ref", False),
        ("testuser/repo", "other/repo", "fork", "selected-pr-branch", False),
    ],
)
def test_issue_coding_verifies_selected_pr_in_real_worktree(
    monkeypatch,
    tmp_path,
    branch,
    mode,
    head_repo,
    fork_remote,
    tracking_remote,
    tracking_branch,
    matches,
):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#50", closes=("owner/repo#1",))
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, worktree, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    session.work_items.append(
        replace(
            _work_item(number=1, title="PR", item_type=WorkItemType.PR),
            number=51,
            kind=WorkItemKind.AUTHORED_PR,
        )
    )
    worktree.rmdir()
    main = tmp_path / "owner_repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "--allow-empty", "-qm", "initial"], check=True
    )
    subprocess.run(
        ["git", "-C", str(main), "remote", "add", "origin", "https://github.com/owner/repo.git"],
        check=True,
    )
    if fork_remote:
        subprocess.run(
            [
                "git",
                "-C",
                str(main),
                "remote",
                "add",
                "fork",
                f"https://github.com/{fork_remote}.git",
            ],
            check=True,
        )
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "-qb", branch, str(worktree)], check=True
    )
    if tracking_remote is None and tracking_branch != "missing-ref":
        remote = "fork" if fork_remote else "origin"
        subprocess.run(
            [
                "git",
                "-C",
                str(main),
                "update-ref",
                f"refs/remotes/{remote}/selected-pr-branch",
                "HEAD",
            ],
            check=True,
        )
    if tracking_branch == "partial":
        subprocess.run(
            ["git", "-C", str(worktree), "config", f"branch.{branch}.remote", "fork"],
            check=True,
        )
    ambiguous = tracking_branch == "ambiguous"
    if ambiguous:
        tracking_branch = "selected-pr-branch"
    if tracking_remote:
        subprocess.run(
            [
                "git",
                "-C",
                str(main),
                "update-ref",
                f"refs/remotes/{tracking_remote}/{tracking_branch}",
                "HEAD",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "branch",
                "--set-upstream-to",
                f"{tracking_remote}/{tracking_branch}",
            ],
            check=True,
            capture_output=True,
        )
    if tracking_remote:
        assert (
            subprocess.check_output(
                ["git", "-C", str(worktree), "rev-parse", "--abbrev-ref", "@{upstream}"],
                text=True,
            ).strip()
            == f"{tracking_remote}/{tracking_branch}"
        )
    if ambiguous:
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--add",
                f"branch.{branch}.merge",
                "refs/heads/other",
            ],
            check=True,
        )
    (worktree / "uncommitted.txt").write_text("keep this")
    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    helper = MagicMock()
    helper.fetch_worktree_head.return_value = ("selected-pr-branch", head_repo)
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )
    session.git = repo_worktree.GitHelper()
    pull = MagicMock()
    monkeypatch.setattr(repo_worktree.GitHelper, "pull_ff_only", pull)

    def run():
        if mode == "dispatch":
            return session.dispatch_once(scope="owner/repo#1", agent="pi")
        target = "owner/repo#PR-51" if mode == "explicit-pr" else "owner/repo#ISSUE-1"
        return session.code(target=target, agent="pi", autostart=True)

    if not matches:
        with pytest.raises(RuntimeError, match="selected-pr-branch"):
            run()
        launch.assert_not_called()
    else:
        result = run()
        assert result["status"] == "launched"
        if mode == "dispatch":
            assert (result["pr_id"], result["action"]) == ("owner/repo#PR-51", "fix_ci")
        launch.assert_called_once()
        assert Path(launch.call_args.args[0]) == worktree
    assert (worktree / "uncommitted.txt").read_text() == "keep this"
    assert (
        subprocess.check_output(
            ["git", "-C", str(worktree), "branch", "--show-current"], text=True
        ).strip()
        == branch
    )


def test_issue_coding_does_not_launch_when_pr_head_is_unavailable(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    session, _worktree, ensure, launch, _ = _authored_session(monkeypatch, tmp_path)
    helper = MagicMock()
    helper.fetch_worktree_head.side_effect = RuntimeError("head unavailable")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )

    with pytest.raises(RuntimeError, match="head unavailable"):
        session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)
    ensure.assert_not_called()
    launch.assert_not_called()


def test_dispatch_checks_out_pull_request_head_branch(monkeypatch, tmp_path):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    panes = []
    session, _, _, launch, _ = _authored_session(monkeypatch, tmp_path, panes)

    def start(repo, *_args, **_kwargs):
        title = f"code_{Path(repo).name}"
        panes.append({"id": 30, "title": title, "pane_cwd": repo})
        return SimpleNamespace(session=None, pane_id="terminal_30", pane_title=title, cwd=repo)

    launch.side_effect = start
    git = MagicMock()
    git.find_worktree_for_branch.return_value = None
    monkeypatch.setattr(repo_worktree, "GitHelper", lambda: git)
    session.git = git
    monkeypatch.setattr(repo_worktree, "existing_worktree_path", lambda *_, **kwargs: None)
    github_helper = MagicMock()
    github_helper.fetch_worktree_head.return_value = ("existing-pr-branch", "owner/repo")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", github_helper.fetch_worktree_head
    )
    monkeypatch.setattr(
        session.backend.github_client, "clone_repository", github_helper.clone_repository
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)

    result = session.dispatch_once(scope="owner/repo", agent="pi")

    assert result["status"] == "launched"
    assert launch.call_count == 1
    assert git.create_worktree.call_args.args[2:] == (
        "existing-pr-branch",
        "origin/existing-pr-branch",
    )


def test_dispatch_rechecks_conflict_before_launch(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    pr = github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), mergeable="CONFLICTING")
    session = _ready_session(tmp_path)
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: [])
    original = session.backend.github_client.find_ready_work

    def find_then_resolve(**kwargs):
        result = original(**kwargs)
        pr["mergeable"] = "MERGEABLE"
        return result

    monkeypatch.setattr(session.backend.github_client, "find_ready_work", find_then_resolve)
    launch = MagicMock()
    monkeypatch.setattr(session, "code", launch)

    assert session.dispatch_once(scope="owner/repo") == {
        "status": "no_ready_task",
        "scope": "owner/repo",
    }
    launch.assert_not_called()


def test_dispatch_uses_cross_repo_pr_for_default_group(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/parent#1")
    github.add_pull_request(
        "owner/implementation#51", closes=("owner/parent#1",), mergeable="CONFLICTING"
    )
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: [])
    session = _ready_session(tmp_path)
    launch = MagicMock(return_value={"status": "launched", "pane_id": "terminal_1"})
    monkeypatch.setattr(session, "code", launch)
    result = session.dispatch_once(scope="owner/parent#1")
    assert result["pr_id"] == "owner/implementation#PR-51"
    assert launch.call_args.kwargs["target"] == "owner/parent#ISSUE-1"
    assert launch.call_args.kwargs["group"] == "implementation"


def test_dispatch_does_not_skip_an_unconfirmed_launch(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, path, _, launch = _coding_session(monkeypatch, tmp_path, [])
    session.config = replace(
        session.config, github_username="testuser", todo_repository="testuser/todos"
    )
    session._unconfirmed_launches[str(path)] = float("inf")
    with pytest.raises(WorkdashControlError) as error:
        session.dispatch_once(scope="owner/repo")
    assert error.value.code == "launch_unconfirmed"
    launch.assert_not_called()


def test_dispatch_resumes_the_actionable_pr_among_multiple_authored_prs(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#50", closes=("owner/repo#1",))
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _, ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)

    result = session.dispatch_once(scope="owner/repo#1", agent="pi")

    assert (result["pr_id"], result["action"]) == ("owner/repo#PR-51", "fix_ci")
    assert ensure.call_args.args[1].number == 51
    assert prepare.call_args.args[0].number == 51
    assert "has failing checks: tests" in _launched_prompt(launch)
    assert launch.call_count == 1
    assert result["autoclose_enabled"] is True


@pytest.mark.parametrize(
    "case", ["default", "no-upstream", "wrong-remote", "wrong-branch", "ambiguous"]
)
def test_dispatch_rejects_invalid_issue_branch_upstream(monkeypatch, tmp_path, case):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, worktree, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    worktree.rmdir()
    remote = tmp_path / "remote.git"
    main = tmp_path / "owner_repo"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(main)], check=True)

    def git(*args, cwd=main):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", f"url.{remote}.insteadOf", "https://github.com/owner/repo.git")
    git("remote", "set-url", "origin", "https://github.com/owner/repo.git")
    git("commit", "--allow-empty", "-qm", "initial")
    git("push", "-qu", "origin", "main")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git("worktree", "add", "-b", "issue-1", str(worktree), "origin/HEAD")
    git("commit", "--allow-empty", "-qm", "issue work", cwd=worktree)
    git("push", "origin", "HEAD", cwd=worktree)
    issue_head = git("rev-parse", "HEAD", cwd=worktree)
    assert git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=worktree) == "origin/main"
    git("commit", "--allow-empty", "-qm", "default advanced")
    git("push", "origin", "main")
    if case == "no-upstream":
        git("branch", "--unset-upstream", cwd=worktree)
    elif case == "wrong-remote":
        git("remote", "add", "other", "https://github.com/other/repo.git")
        git("config", "branch.issue-1.remote", "other")
    elif case == "wrong-branch":
        git("config", "branch.issue-1.merge", "refs/heads/other")
    elif case == "ambiguous":
        git("config", "--add", "branch.issue-1.merge", "refs/heads/other")
    (worktree / "uncommitted.txt").write_text("keep this")

    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    helper = MagicMock()
    helper.fetch_worktree_head.return_value = ("issue-1", "owner/repo")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )
    session.git = repo_worktree.GitHelper()

    config_before = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "config",
            "--get-regexp",
            r"^branch\.issue-1\.(remote|merge)$",
        ],
        capture_output=True,
        text=True,
    ).stdout
    with pytest.raises(RuntimeError, match="configure its upstream"):
        session.dispatch_once(scope="owner/repo#1", agent="pi")
    launch.assert_not_called()
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "config",
                "--get-regexp",
                r"^branch\.issue-1\.(remote|merge)$",
            ],
            capture_output=True,
            text=True,
        ).stdout
        == config_before
    )
    assert git("rev-parse", "HEAD", cwd=worktree) == issue_head
    assert (worktree / "uncommitted.txt").read_text() == "keep this"


@pytest.mark.parametrize(
    "case", ["tracked", "default", "wrong-remote", "wrong-branch", "ambiguous", "wrong-checkout"]
)
def test_dispatch_resumes_pushed_targeted_todo_branch(monkeypatch, tmp_path, case):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#7", body=todo_body("owner/other"))
    github.add_pull_request(
        "owner/other#51",
        closes=("testuser/todos#7",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, placeholder, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    placeholder.rmdir()
    session.config = replace(session.config, todo_repository="testuser/todos")
    todo = _work_item(number=7, title="Todo")
    todo.repo = "testuser/todos"
    todo.todo_target = "owner/other"
    session.work_items.append(todo)
    remote = tmp_path / "remote.git"
    main = tmp_path / "owner_other"
    worktree = tmp_path / "owner_other_todo_7"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(remote)], check=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(main)], check=True)

    def git(*args, cwd=main):
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("config", f"url.{remote}.insteadOf", "https://github.com/owner/other.git")
    git("remote", "set-url", "origin", "https://github.com/owner/other.git")
    git("commit", "--allow-empty", "-qm", "initial")
    git("push", "-qu", "origin", "main")
    git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    git("worktree", "add", "-b", "wt-7", str(worktree), "origin/HEAD")
    git("commit", "--allow-empty", "-qm", "todo work", cwd=worktree)
    git("push", "origin", "HEAD", cwd=worktree)
    head = git("rev-parse", "HEAD", cwd=worktree)
    assert git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=worktree) == "origin/main"
    git("merge", "--ff-only", "wt-7")
    git("commit", "--allow-empty", "-qm", "default advanced")
    git("push", "origin", "main")
    if case == "tracked":
        git("branch", "--set-upstream-to", "origin/wt-7", cwd=worktree)
    elif case == "wrong-remote":
        git("remote", "add", "other", "https://github.com/other/repo.git")
        git("config", "branch.wt-7.remote", "other")
    elif case == "wrong-branch":
        git("config", "branch.wt-7.merge", "refs/heads/other")
    elif case == "ambiguous":
        git("config", "--add", "branch.wt-7.merge", "refs/heads/other")
    elif case == "wrong-checkout":
        git("checkout", "-qb", "other-branch", cwd=worktree)
    (worktree / "uncommitted.txt").write_text("keep this")

    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    helper = MagicMock()
    helper.fetch_worktree_head.return_value = ("wt-7", "owner/other")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )
    session.git = repo_worktree.GitHelper()
    panes = []
    monkeypatch.setattr(
        control_module.launcher.zellij, "load_zellij_panes", lambda _session: list(panes)
    )
    launch.side_effect = lambda repo, *_args, **_kwargs: (
        panes.append({"id": 30, "title": "code_owner_other_todo_7", "pane_cwd": repo})
        or SimpleNamespace(
            session=None, pane_id="terminal_30", pane_title="code_owner_other_todo_7", cwd=repo
        )
    )

    if case == "tracked":
        result = session.dispatch_once(scope="owner/other#ISSUE-WT7", agent="pi")
        assert result["status"] == "launched"
        launch.assert_called_once()
        assert Path(launch.call_args.args[0]) == worktree
        assert git("rev-parse", "--abbrev-ref", "@{upstream}", cwd=worktree) == "origin/wt-7"
    else:
        with pytest.raises(RuntimeError, match="wt-7"):
            session.dispatch_once(scope="owner/other#ISSUE-WT7", agent="pi")
        launch.assert_not_called()
    assert git("branch", "--show-current", cwd=worktree) == (
        "other-branch" if case == "wrong-checkout" else "wt-7"
    )
    assert git("rev-parse", "HEAD", cwd=worktree) == head
    assert (worktree / "uncommitted.txt").read_text() == "keep this"


@pytest.mark.parametrize("created_from", ["origin", "fork"])
def test_untracked_pr_branch_requires_upstream(monkeypatch, tmp_path, created_from):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, worktree, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    worktree.rmdir()
    main = tmp_path / "owner_repo"

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(main), *args], check=True, capture_output=True, text=True
        )

    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-qm", "initial")
    for remote, repo in (("origin", "owner/repo"), ("fork", "testuser/repo")):
        git("remote", "add", remote, f"https://github.com/{repo}.git")
        git("update-ref", f"refs/remotes/{remote}/selected-pr-branch", "HEAD")
    git(
        "worktree",
        "add",
        "--no-track",
        "-qb",
        "selected-pr-branch",
        str(worktree),
        f"{created_from}/selected-pr-branch",
    )
    (worktree / "uncommitted.txt").write_text("keep this")
    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    helper = MagicMock()
    helper.fetch_worktree_head.return_value = ("selected-pr-branch", "testuser/repo")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )
    session.git = repo_worktree.GitHelper()
    with pytest.raises(RuntimeError, match="configure its upstream"):
        session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)
    launch.assert_not_called()
    assert (worktree / "uncommitted.txt").read_text() == "keep this"
    assert git("branch", "--show-current").stdout.strip() == "main"
    assert (
        subprocess.check_output(
            ["git", "-C", str(worktree), "branch", "--show-current"], text=True
        ).strip()
        == "selected-pr-branch"
    )


def test_dispatch_rejects_unverified_branch_worktree_before_pull(monkeypatch, tmp_path):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, placeholder, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    placeholder.rmdir()
    main = tmp_path / "owner_repo"
    worktree = tmp_path / "custom-worktree"

    def git(*args):
        return subprocess.check_output(["git", "-C", str(main), *args], text=True).strip()

    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-qm", "initial")
    git("remote", "add", "origin", "https://github.com/owner/repo.git")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    git("worktree", "add", "-qb", "selected-pr-branch", str(worktree), "origin/main")
    helper = MagicMock()
    helper.fetch_worktree_head.return_value = ("selected-pr-branch", "owner/repo")
    monkeypatch.setattr(
        session.backend.github_client, "fetch_worktree_head", helper.fetch_worktree_head
    )
    session.git = repo_worktree.GitHelper()
    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    pull = MagicMock()
    monkeypatch.setattr(repo_worktree.GitHelper, "pull_ff_only", pull)

    with pytest.raises(RuntimeError, match="configure its upstream"):
        session.dispatch_once(scope="owner/repo#1", agent="pi")
    pull.assert_not_called()
    launch.assert_not_called()


def test_ready_and_code_share_replaceable_github_state(monkeypatch, tmp_path):
    from workdash.backend import WorkdashBackend
    from workdash.work_state import PullRequestWorkState, ReadyWork, ScopedReadyWork

    panes = []
    session, _, _, launch, _ = _authored_session(monkeypatch, tmp_path, panes)
    item = session.work_items[0]
    pr = _work_item(number=2, title="Linked PR", item_type=WorkItemType.PR)
    pr.kind = WorkItemKind.AUTHORED_PR
    state = PullRequestWorkState(
        item=pr,
        state="OPEN",
        author="testuser",
        linked_issues=(item,),
        head_sha="head1",
        head_writable=True,
        base_ref="main",
        base_head_sha="base1",
        mergeability="mergeable",
        review_decision=None,
        ci_status="failing",
        failing_checks=("tests",),
        feedback=(),
    )

    class FakeGitHub:
        def __init__(self):
            self.state = state
            self.lookups = []

        def find_ready_work(self, **kwargs):
            self.lookups.append("ready")
            return ScopedReadyWork(
                [ReadyWork(item=item, pull_request=self.state)] if self.state.ready else []
            )

        def work_pull_request(self, item, **kwargs):
            self.lookups.append("linked PR")
            return self.state

        def fetch_worktree_head(self, item):
            return ("pr-branch", item.repo)

        def fetch_launch_context(self, item):
            self.lookups.append("context")
            return {"title": "Context from fake GitHub"}

        def fetch_analysis_context(self, item):
            self.lookups.append("analysis context")
            return {"body": "Analysis from fake GitHub"}

    github = FakeGitHub()
    git = session.git
    config = replace(session.config, agents={"pi": AgentConfig(launch="pi", analyze="pi")})
    backend = WorkdashBackend(github_client=github, config=config, cache_root=tmp_path / "cache")
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    session.git = git
    prompts = []
    monkeypatch.setattr(
        control_module.agent_execution.executor,
        "run_in_pane",
        lambda **kwargs: prompts.append(kwargs["prompt"]) or "Analysis result",
    )
    backend.analyze_item(
        item, "pi", cwd=str(tmp_path), zellij_session="workdash-main", group="repo"
    )
    assert "Analysis from fake GitHub" in prompts[0]
    monkeypatch.setattr(
        control_module, "prepare_launch_agent_prompt", launcher_module.prepare_launch_agent_prompt
    )

    assert session.ready(scope="owner/repo")["items"][0]["action"] == "fix_ci"
    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)
    assert result["status"] == "launched"
    assert "Context from fake GitHub" in _launched_prompt(launch)
    assert "has failing checks: tests" in _launched_prompt(launch)
    panes.clear()
    github.state = replace(
        state, ci_status="passing", failing_checks=(), mergeability="conflicting"
    )
    assert session.ready(scope="owner/repo")["items"][0]["action"] == "fix_merge_conflicts"
    assert backend.github_client.work_pull_request(item).action == "fix_merge_conflicts"
    assert (
        session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)["status"]
        == "launched"
    )
    assert "GitHub reports merge conflicts" in _launched_prompt(launch)
    assert "has failing checks: tests" not in _launched_prompt(launch)
    assert github.lookups == [
        "analysis context",
        "ready",
        "linked PR",
        "context",
        "ready",
        "linked PR",
        "linked PR",
        "context",
    ]


def test_zellij_replacement_tracks_terminal_and_coding_lifecycle(monkeypatch, tmp_path):
    class FakeZellij:
        def __init__(self):
            self.panes = {}
            self.content = {}
            self.launches = []

        def launch_pane(
            self, repo_path, command, *, context, work_action, zellij_session, group=None
        ):
            self.launches.append((repo_path, command, context, work_action, zellij_session, group))
            pane_number = len(self.launches)
            pane_id = f"terminal_{pane_number}"
            title = f"{work_action}_{Path(repo_path).name}"
            self.panes[pane_id] = {"id": pane_number, "title": title, "pane_cwd": repo_path}
            self.content[pane_id] = ""
            return launcher_module.ZellijPaneLaunch(zellij_session, pane_id, title, repo_path)

        def list_workdash_panes(self, session):
            assert session == "workdash-test"
            return list(self.panes.values())

        def send_zellij_pane_input(self, session, pane_id, data, *, raw=False):
            self.content[pane_id] += data + ("" if raw else "\n")

        def dump_zellij_pane(self, session, pane_id, *, full=False):
            return self.content[pane_id]

        def close_zellij_pane(self, session, pane_id):
            del self.panes[pane_id]

    fake = FakeZellij()
    monkeypatch.setattr(launcher_module, "zellij", fake)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda *args, **kwargs: str(tmp_path))
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, **kwargs: tmp_path)
    monkeypatch.setattr(control_module, "get_merge_base", lambda path, **kwargs: None)
    monkeypatch.setattr(
        control_module, "prepare_launch_agent_prompt", lambda *a, **k: "It's a task"
    )
    monkeypatch.setenv("SHELL", "/bin/sh")
    item = _work_item(number=1, title="Task")
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi --model 'a b'")}),
        backend=MagicMock(),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-test",
    )
    launch = session.terminal(target="owner/repo#ISSUE-1")
    pane_id = launch["pane_id"]
    assert [pane["pane_id"] for pane in session.info()["panes"]] == [pane_id]
    session.pane_send(pane_id=pane_id, data="hello")
    assert session.pane_content(pane_id=pane_id)["content"] == "hello\n"
    session.pane_close(pane_id=pane_id)
    assert session.info()["panes"] == []

    coding = session.code(target="owner/repo#ISSUE-1", agent="pi", group="my group")
    assert coding["status"] == "launched"
    assert coding["session"] == "workdash-test"
    assert coding["pane_id"] == "terminal_2"
    assert coding["cwd"] == str(tmp_path)
    repo_path, command, context, action, selected_session, group = fake.launches[-1]
    assert (repo_path, context, action, selected_session, group) == (
        str(tmp_path),
        "Failed to launch coding agent in zellij",
        "code",
        "workdash-test",
        "my group",
    )
    assert command[:2] == ["/bin/sh", "-ic"]
    assert shlex.split(command[2]) == ["pi", "--model", "a b", "It's a task"]
    assert session.code(target="owner/repo#ISSUE-1", agent="pi")["status"] == "already_running"
    assert [pane["pane_id"] for pane in session.info()["panes"]] == ["terminal_2"]
    session.pane_send(pane_id="terminal_2", data="continue")
    assert session.pane_content(pane_id="terminal_2")["content"] == "continue\n"
    session.pane_close(pane_id="terminal_2")
    assert session.info()["panes"] == []


def test_review_compatibility_delegates_raw_output_to_current_executor(monkeypatch):
    from workdash import agent_execution
    from workdash.analyzer import Analyzer

    item = _work_item(number=1, title="Task")
    fake = MagicMock()
    fake.run_in_pane.return_value = "  raw output\n"
    monkeypatch.setattr(agent_execution, "executor", fake)
    result = Analyzer()._run_analysis_command(
        item=item,
        prompt="review prompt",
        command_tokens=["agent", "-p"],
        cwd="/tmp",
        work_action="review",
        zellij_session="workdash-main",
        group="repo",
    )
    assert result == "  raw output\n"
    fake.run_in_pane.assert_called_once_with(
        item=item,
        prompt="review prompt",
        command_tokens=["agent", "-p"],
        cwd="/tmp",
        work_action="review",
        zellij_session="workdash-main",
        group="repo",
    )


def test_session_uses_its_git_to_identify_worktrees_for_ready_and_panes(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    worktree = tmp_path / "owner_repo_1"
    worktree.mkdir()
    item = _work_item(number=1, title="Issue")
    git = MagicMock()
    git.worktree_proves_item.side_effect = lambda path, item: path == worktree
    monkeypatch.setattr(control_module, "GitHelper", lambda: git)
    panes = []
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: panes)
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=GitHubClient()),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    assert len(session.ready(scope="owner/repo", fail_unconfirmed=True)["items"]) == 1
    session._unconfirmed_launches[str(worktree)] = float("inf")
    with pytest.raises(WorkdashControlError, match="still unconfirmed"):
        session.ready(scope="owner/repo", fail_unconfirmed=True)
    session._unconfirmed_launches.clear()
    panes.append({"id": 1, "title": "code_owner_repo_1", "pane_cwd": str(worktree)})
    assert session.ready(scope="owner/repo")["items"] == []
    assert session.info()["panes"][0]["item"] == "owner/repo#ISSUE-1"


def test_terminal_api_prepares_worktree_with_session_git(monkeypatch, tmp_path) -> None:
    item = _work_item(number=1, title="Issue")
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    git = MagicMock()
    git.find_worktree_for_branch.return_value = None
    session.git = git
    worktree = tmp_path / "owner_repo_1"

    def open_terminal(path, *, zellij_session):
        assert git.create_worktree.called, "prepare the worktree before opening the terminal"
        return SimpleNamespace(
            session=zellij_session,
            cwd=path,
            pane_title="terminal_owner_repo_1",
            pane_id="terminal_23",
        )

    launch = MagicMock(side_effect=open_terminal)
    monkeypatch.setattr(control_module, "launch_terminal_context", launch)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess"))

    response = _post_item_action(session, "terminal", "owner/repo#ISSUE-1")
    assert response.status_int == 200
    assert response.json == {
        "ok": True,
        "result": {
            "item_id": "owner/repo#ISSUE-1",
            "session": "workdash-main",
            "cwd": str(worktree),
            "pane_title": "terminal_owner_repo_1",
            "pane_id": "terminal_23",
        },
    }
    launch.assert_called_once_with(str(worktree), zellij_session="workdash-main")
    git.create_worktree.assert_called_once_with(
        tmp_path / "owner_repo", worktree, "issue-1", "origin/HEAD"
    )


def test_terminal_uses_current_zellij_session_without_an_override(monkeypatch, tmp_path):
    item = _work_item(number=1, title="Issue")
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=GitHubClient()),
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    worktree = str(tmp_path / "owner_repo_1")
    prepare = MagicMock(return_value=worktree)
    monkeypatch.setattr(control_module, "ensure_worktree", prepare)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(
        launcher_module.shutil,
        "which",
        lambda name: "/usr/bin/zellij" if name == "zellij" else None,
    )

    def run_zellij(argv, **kwargs):
        mark_pane_started(argv, "23")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    run = MagicMock(side_effect=run_zellij)
    monkeypatch.setattr(subprocess, "run", run)

    session.terminal(target="owner/repo#ISSUE-1")

    assert prepare.call_args.args == (str(tmp_path), item)
    run.assert_called_once()
    zellij_args, _ = pane_launch(run.call_args.args[0])
    assert zellij_args[:3] == ["/usr/bin/zellij", "action", "new-pane"]
    assert zellij_args[zellij_args.index("--cwd") + 1] == worktree


def test_session_analyzes_and_launches_coding_agent_with_real_executor(
    monkeypatch, tmp_path
) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    worktree = tmp_path / "owner_repo_1"
    worktree.mkdir()
    item = _work_item(number=1, title="Implement the issue")
    agent = tmp_path / "pi"
    agent.write_text(f"#!{sys.executable}\nprint('  Useful analysis  ')\n")
    agent.chmod(0o755)
    config = WorkdashConfig(
        agents={"pi": AgentConfig(analyze=str(agent), launch="pi")}, workdir=str(tmp_path)
    )
    backend = WorkdashBackend(cache_root=tmp_path / "cache", config=config)
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )
    panes = []
    monkeypatch.setattr(control_module, "ensure_worktree", lambda *_, **kwargs: str(worktree))
    monkeypatch.setattr(
        control_module, "analysis_worktree", lambda *_, **kwargs: nullcontext(str(worktree))
    )
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *_, **kwargs: worktree)
    monkeypatch.setattr(control_module, "get_merge_base", lambda *_, **kwargs: None)
    monkeypatch.setattr("workdash.github_client.GitHubClient.fetch_analysis_context", lambda *_: {})
    monkeypatch.setattr(
        "workdash.github_client.GitHubClient.fetch_launch_context",
        lambda *_: {"body": "Keep existing shortcuts", "url": item.url},
    )

    analysis_launches = []
    coding_launches = []

    def launch_pane_and_wait(repo_path, command, *, context, work_action, zellij_session, group):
        analysis_launches.append(
            {
                "repo_path": repo_path,
                "command": command,
                "work_action": work_action,
                "zellij_session": zellij_session,
                "group": group,
            }
        )
        subprocess.run(command, cwd=repo_path, capture_output=True, text=True)

    def launch_pane(repo, command, **kwargs):
        coding_launches.append((repo, command, kwargs))
        panes.append({"id": 30, "title": "code_owner_repo_1", "pane_cwd": str(worktree)})
        return launcher_module.ZellijPaneLaunch(
            "workdash-main", "terminal_30", "code_owner_repo_1", repo
        )

    fake_zellij = MagicMock()
    fake_zellij.list_workdash_panes.side_effect = lambda _: list(panes)
    fake_zellij.launch_pane_and_wait.side_effect = launch_pane_and_wait
    fake_zellij.launch_pane.side_effect = launch_pane
    monkeypatch.setattr(launcher_module, "zellij", fake_zellij)

    analysis = session.analyze(target="owner/repo#ISSUE-1", agent="pi")
    assert analysis["status"] == "generated"
    assert base64.b64decode(analysis["file_content"]).decode() == "Useful analysis"
    assert item.analysis == "Useful analysis"
    assert backend.analysis_cache.load_analysis(item) == "Useful analysis"
    (launch,) = analysis_launches
    assert launch["repo_path"] == str(worktree)
    assert (launch["work_action"], launch["group"]) == ("analyse", "repo")
    assert launch["zellij_session"] == "workdash-main"
    shell_command = launch["command"][2]
    assert launch["command"][:2] == ["/bin/sh", "-c"]
    assert f"-ic '{agent} " in shell_command
    assert "Implement the issue" in shell_command

    coding = session.code(target="owner/repo#ISSUE-1", agent="pi", group="team")
    assert coding["status"] == "launched"
    assert coding["pane_id"] == "terminal_30"
    assert coding["cwd"] == str(worktree)
    assert len(coding_launches) == 1
    repo, command, kwargs = coding_launches[0]
    assert repo == str(worktree)
    assert kwargs == {
        "context": "Failed to launch coding agent in zellij",
        "work_action": "code",
        "zellij_session": "workdash-main",
        "group": "team",
    }
    assert command[1] == "-ic"
    agent, prompt = shlex.split(command[2])
    assert agent == "pi"
    assert "Implement the issue" in prompt
    assert "PREVIOUS ANALYSIS:" in prompt
    assert AnalysisCache.build_analysis_url_path(item) in prompt
    assert analysis["url"] in prompt
    assert str(backend.analysis_cache.build_analysis_path(item)) not in prompt
    assert item.url in prompt
    assert "Keep existing shortcuts" in prompt
    assert "Propose a concrete implementation plan and clarifying questions." in prompt
    assert "Do not start implementing code yet." in prompt
    assert "Discuss review findings" not in prompt
    assert "AUTOSTART" not in prompt
    assert (
        "development practices, skills, and coordination workflows you have been configured"
        not in prompt
    )


def test_shared_agent_execution_can_simulate_analysis_and_coding(monkeypatch, tmp_path) -> None:
    install_fake_github(monkeypatch).add_issue("owner/repo#1")
    from workdash.analyzer import Analyzer

    panes = []
    session, worktree, _, _ = _coding_session(monkeypatch, tmp_path, panes)
    item = session.work_items[0]

    class FakeAgentExecution:
        def __init__(self):
            self.calls = []

        def run_in_pane(
            self, *, item, prompt, command_tokens, cwd, work_action, zellij_session, group
        ):
            self.calls.append(("analysis", item, prompt, command_tokens, cwd))
            return "  Analysis from fake  \n"

        def launch_vscode(self, repo_path, prompt):
            self.calls.append(("vscode", repo_path, prompt))

        def launch(self, repo_path, prompt, agent_command_tokens, *, zellij_session):
            self.calls.append(("code", repo_path, prompt, agent_command_tokens, zellij_session))
            panes.append({"id": 30, "title": "code_owner_repo_1", "pane_cwd": repo_path})
            return SimpleNamespace(
                session=zellij_session,
                pane_id="terminal_30",
                pane_title="code_owner_repo_1",
                cwd=repo_path,
            )

    fake = FakeAgentExecution()
    monkeypatch.setattr(control_module.agent_execution, "executor", fake)
    monkeypatch.setattr(Analyzer, "_collect_github_context", lambda self, item: {})
    monkeypatch.setattr(
        Analyzer, "_build_agent_prompt", lambda self, item, context, **kw: "analysis prompt"
    )

    assert (
        Analyzer().analyze(item, ["pi", "-p"], cwd=str(worktree), zellij_session="workdash-main")
        == "Analysis from fake"
    )
    assert session.code(target="owner/repo#ISSUE-1", agent="pi")["status"] == "launched"
    panes.clear()
    assert (
        session.code(target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True)["status"]
        == "launched"
    )
    assert fake.calls == [
        ("analysis", item, "analysis prompt", ["pi", "-p"], str(worktree)),
        ("code", str(worktree), "PROMPT", ["pi"], "workdash-main"),
        ("vscode", str(worktree), "PROMPT"),
    ]


def _terminal_session(workdir: Path, item: WorkItem) -> WorkdashSession:
    return WorkdashSession(
        config=WorkdashConfig(workdir=str(workdir)),
        backend=MagicMock(),
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash",
    )


def test_terminal_panes_match_worktree_and_exclude_agents_and_exited_panes(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL", "/usr/local/bin/nu")
    worktree = tmp_path / "owner_repo_1"
    worktree.mkdir()
    item = _work_item(number=1, title="Work")
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, **kwargs: worktree)
    monkeypatch.setattr(
        control_module.launcher.zellij,
        "load_zellij_panes",
        lambda _: [
            {
                "id": 1,
                "title": "code_owner_repo_1",
                "pane_command": "/bin/bash",
                "pane_cwd": str(worktree),
                "exited": False,
            },
            {
                "id": 2,
                "title": "terminal_owner_repo_1",
                "pane_command": "/bin/bash -i",
                "pane_cwd": str(worktree / "subdir"),
                "exited": False,
            },
            {
                "id": 3,
                "title": "terminal_owner_repo_1",
                "pane_command": "/bin/bash",
                "pane_cwd": str(worktree),
                "exited": True,
            },
            {"id": 4, "title": "terminal_owner_repo_2", "pane_cwd": str(worktree), "exited": False},
            {
                "id": 6,
                "title": "diff_owner_repo_1",
                "pane_command": "/bin/bash",
                "pane_cwd": str(worktree),
                "exited": False,
            },
            {
                "id": 7,
                "title": "amol@host: cwd",
                "pane_command": "/bin/bash",
                "pane_cwd": str(worktree),
            },
            {
                "id": 11,
                "title": "terminal_owner_repo_1",
                "pane_command": "nvim",
                "pane_cwd": str(worktree),
            },
            {
                "id": 12,
                "title": "terminal_owner_repo_1",
                "pane_command": "pytest",
                "pane_cwd": str(worktree),
            },
            {
                "id": 15,
                "title": "terminal_owner_repo_1",
                "pane_command": "sleep 300",
                "pane_cwd": str(worktree),
            },
            {
                "id": 14,
                "title": "my shell",
                "pane_command": "/usr/local/bin/nu -i",
                "pane_cwd": str(worktree),
            },
            {"id": 13, "title": "terminal_owner_repo_1", "pane_cwd": str(worktree)},
            {"id": 8, "title": "nvim", "pane_command": "nvim", "pane_cwd": str(worktree)},
            {"id": 9, "title": "pytest", "pane_command": "pytest", "pane_cwd": str(worktree)},
            {"id": 10, "title": "claude", "pane_cwd": str(worktree)},
            {
                "id": 5,
                "title": "terminal_owner_repo_1",
                "pane_command": "/bin/bash",
                "pane_cwd": str(tmp_path / "other" / worktree.name),
                "exited": False,
            },
        ],
    )
    session = _terminal_session(tmp_path, item)
    assert [pane["pane_id"] for pane in session.get_terminal_panes_for_item(item)] == [
        "terminal_2",
        "terminal_7",
        "terminal_11",
        "terminal_12",
        "terminal_15",
        "terminal_14",
        "terminal_13",
    ]


def test_terminal_lookup_ignores_a_viewer_in_the_worktree(tmp_path, monkeypatch):
    worktree = tmp_path / "owner_repo_1"
    worktree.mkdir()
    item = _work_item(number=1, title="Work")
    session = _terminal_session(tmp_path, item)
    session.git = MagicMock()
    session.git.worktree_proves_item.return_value = True
    viewer = {
        "id": 3,
        "title": "view",
        "pane_command": '/bin/sh -c claim="$ZELLIJ_PANE_ID"; ... '
        "workdash-pane /tmp/workdash-pane-test/launch/pane workdash view analysis.md",
        "pane_cwd": str(worktree),
    }
    list_panes = MagicMock(return_value=[viewer])
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", list_panes)

    assert session.get_terminal_panes_for_item(item) == []
    list_panes.assert_called_once_with("workdash")


@pytest.mark.parametrize("item_type", [WorkItemType.ISSUE, WorkItemType.PR])
def test_terminal_lookup_finds_worktree_reused_by_branch(tmp_path, monkeypatch, item_type):
    from workdash.launcher import _zellij_pane_name_for_work_action

    item = _work_item(number=7, title="work", item_type=item_type)
    session = _terminal_session(tmp_path, item)
    worktree = tmp_path / "custom-checkout"
    worktree.mkdir()
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, **kwargs: None)
    session.git = MagicMock()
    session.git.find_worktree_for_branch.return_value = worktree
    session.git.repository_matches.return_value = True
    session.backend.github_client.fetch_worktree_head.return_value = ("pr-branch", "fork/repo")
    title = _zellij_pane_name_for_work_action("terminal", str(worktree))
    monkeypatch.setattr(
        control_module.launcher.zellij,
        "load_zellij_panes",
        lambda _: [
            {"id": 5, "title": title, "pane_command": "/bin/bash -i", "pane_cwd": str(worktree)}
        ],
    )
    assert session.get_terminal_panes_for_item(item)[0]["pane_id"] == "terminal_5"
    repo, branch = (
        ("fork_repo", "pr-branch") if item_type == WorkItemType.PR else ("owner_repo", "issue-7")
    )
    session.git.find_worktree_for_branch.assert_called_once_with(tmp_path / repo, branch)


def test_terminal_pane_is_reused_unless_it_moved_into_another_worktree(tmp_path, monkeypatch):
    workdir = tmp_path / "workdir"
    worktree = workdir / "owner_repo_1"
    worktree.mkdir(parents=True)
    other_worktree = workdir / "other" / worktree.name
    other_worktree.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    item = _work_item(number=1, title="Work")
    panes = [
        {
            "id": 5,
            "title": "terminal_owner_repo_1",
            "pane_command": "/bin/bash -i",
            "pane_cwd": str(other_worktree),
        }
    ]
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, **kwargs: worktree)
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: list(panes))
    session = _terminal_session(workdir, item)
    assert session.get_terminal_panes_for_item(item) == []
    # Our own terminal shares the title but changed directory outside the workdir.
    panes.append(
        {
            "id": 6,
            "title": "terminal_owner_repo_1",
            "pane_command": "/bin/bash -i",
            "pane_cwd": str(elsewhere),
        }
    )
    assert [pane["pane_id"] for pane in session.get_terminal_panes_for_item(item)] == ["terminal_6"]


def test_terminal_pane_lookup_returns_nothing_without_a_zellij_session(monkeypatch, tmp_path):
    session, _worktree, _ensure, _launch = _coding_session(
        monkeypatch, tmp_path, [], zellij_session=None
    )
    assert session.get_terminal_panes_for_item(session.work_items[0]) == []


def test_control_boundary_requires_header_except_analysis_documents():
    session = MagicMock()
    session.show_config.return_value = {"ok": "yes"}
    session.list_items.return_value = {"items": []}
    session.backend.analysis_cache.load_analysis.return_value = (
        "![diagram](https://example.com/image.png)"
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )

    def call(path, method="GET", headers=None, peer="127.0.0.1"):
        req = Request.blank(path, method=method, headers=headers or {})
        req.environ["REMOTE_ADDR"] = peer
        if path == "/api/v0/list" and method == "POST":
            req.content_type = "application/json"
            req.body = b"{}"
        return req.get_response(app)

    control = "/api/v0/show-config"
    list_api = "/api/v0/list"
    artifact = "/api/items/owner/repo%23ISSUE-1/files/"
    analysis = "/api/items/owner/repo%23ISSUE-1/analysis"
    short_item = "/api/items"
    for path, method in (
        (control, "POST"),
        (list_api, "POST"),
        (artifact, "POST"),
        (analysis, "POST"),
        (short_item, "GET"),
    ):
        assert call(path, method).status_int == 403
        assert call(path, method, {"X-Workdash": "wrong"}).status_int == 403
    form = call(
        control, "POST", {"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"}
    )
    assert form.status_int == 403
    assert "Access-Control-Allow-Origin" not in form.headers
    session.show_config.assert_not_called()
    session.list_items.assert_not_called()
    assert call(control, "POST", {"X-Workdash": version("workdash")}).status_int == 200
    response = call(list_api, "POST", {"X-Workdash": version("workdash")})
    assert response.status_int == 200
    assert response.json["ok"] is True
    list_preflight = call(
        list_api,
        "OPTIONS",
        {"Origin": "https://example.com", "Access-Control-Request-Method": "POST"},
    )
    assert list_preflight.status_int == 403
    for path in (analysis, analysis + "/html"):
        assert call(path).status_int == 200
        assert call(path, peer="192.0.2.1").status_int == 403
    preflight = call(
        control,
        "OPTIONS",
        {
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-workdash",
        },
    )
    assert preflight.status_int == 403
    assert "Access-Control-Allow-Origin" not in preflight.headers
    assert "Access-Control-Allow-Headers" not in preflight.headers


@pytest.mark.parametrize("cached", [True, False], ids=["cached", "missing"])
def test_analysis_documents_serve_cached_content_without_header(tmp_path, cached):
    item = _work_item(number=1, title="Work")
    cache = AnalysisCache(tmp_path / "analysis-cache")
    markdown = "# Cached analysis\n\nRecommended next step.\n"
    if cached:
        cache.save(item, markdown)
        item.analysis = cache.load_analysis(item)
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=WorkdashBackend(analysis_cache=cache),
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    path = "/api/items/owner/repo%23ISSUE-1/analysis"
    peer = {"REMOTE_ADDR": "127.0.0.1"}
    if not cached:
        response = Request.blank(path, environ=peer).get_response(app)
        assert response.status_int == 404
        assert response.json["ok"] is False
        assert response.json["error"]["code"] == "not_found"
        return
    result = Request.blank(
        "/api/v0/analyze",
        method="POST",
        host="dashboard.test:9876",
        headers={"X-Workdash": version("workdash")},
        content_type="application/json",
        body=b'{"target":"owner/repo#ISSUE-1"}',
        environ=peer,
    ).get_response(app)
    assert result.status_int == 200
    urls = result.json["result"]
    assert urls["markdown_url"] == f"http://dashboard.test:9876{path}"
    assert urls["html_url"] == urls["markdown_url"] + "/html"
    response = Request.blank(urls["markdown_url"], environ=peer).get_response(app)
    assert response.status_int == 200
    assert response.content_type == "text/markdown"
    assert response.body == markdown.encode()
    html = Request.blank(urls["html_url"], environ=peer).get_response(app)
    assert html.status_int == 200
    assert html.content_type == "text/html"
    assert "<h1>Cached analysis</h1>" in html.text
    assert "<p>Recommended next step.</p>" in html.text


def test_control_boundary_allows_trailing_slash_analysis_documents():
    session = MagicMock()
    session.backend.analysis_cache.load_analysis.return_value = "# Analysis"
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )

    def call(path, method="GET"):
        req = Request.blank(path, method=method)
        req.environ["REMOTE_ADDR"] = "127.0.0.1"
        return req.get_response(app)

    item = "/api/items/owner/repo%23ISSUE-1"
    for path in (f"{item}/analysis/", f"{item}/analysis/html/"):
        assert call(path).status_int == 200, path
    assert call(f"{item}/analysis/").body == b"# Analysis"
    for path, method in (
        (f"{item}/files/", "POST"),
        (f"{item}/analysis/extra/", "GET"),
        ("/api/v0/show-config/", "POST"),
    ):
        assert call(path, method).status_int == 403, path


def test_fresh_analysis_needs_a_session_before_preparing_worktree(tmp_path, monkeypatch):
    item = _work_item(number=1, title="Issue")
    workdir = tmp_path / "work"
    config = WorkdashConfig(workdir=str(workdir), agents={"pi": AgentConfig(analyze="pi")})
    github = MagicMock(spec=GitHubClient)
    backend = WorkdashBackend(config=config, github_client=github, cache_root=tmp_path / "cache")
    launches = install_pane_launch(monkeypatch)
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )

    with pytest.raises(
        WorkdashControlError, match="Analysis requires a known Workdash Zellij session"
    ):
        session.analyze(target="owner/repo#ISSUE-1", prefer_cache=False)

    assert not workdir.exists()
    github.clone_repository.assert_not_called()
    github.fetch_analysis_context.assert_not_called()
    assert not launches


@pytest.mark.parametrize("outcome", ["success", "error", "empty"])
def test_issue_analysis_uses_disposable_default_branch_worktree(tmp_path, monkeypatch, outcome):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args, cwd=source):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (source / "code.txt").write_text("default code")
    git("add", ".")
    git("commit", "-m", "initial")
    item = _work_item(number=1, title="Issue")
    github = MagicMock(spec=GitHubClient)
    github.fetch_analysis_context.return_value = {"body": "Issue discussion"}
    github.clone_repository.side_effect = lambda repo, target: git(
        "clone", str(source), str(target)
    )
    monkeypatch.setenv("SHELL", "/bin/sh")
    launches = install_pane_launch(monkeypatch)
    script = (
        "import pathlib, subprocess, sys\n"
        "assert pathlib.Path('code.txt').read_text() == 'default code'\n"
        "assert subprocess.check_output(['git', 'rev-parse', '--abbrev-ref', 'HEAD']) == b'HEAD\\n'\n"
        "assert 'Issue discussion' in sys.argv[-1]\n"
        "pathlib.Path('agent-created').touch()\n"
        + {
            "success": "print('# Fresh analysis')",
            "error": "print('# Partial analysis')\nsys.exit('agent failed')",
            "empty": "pass",
        }[outcome]
    )
    workdir = tmp_path / "work"
    config = WorkdashConfig(
        workdir=str(workdir),
        agents={"pi": AgentConfig(analyze=shlex.join([sys.executable, "-c", script]))},
    )
    backend = WorkdashBackend(config=config, github_client=github, cache_root=tmp_path / "cache")
    cache = backend.analysis_cache
    if outcome != "success":
        cache.save(item, "# Prior analysis")
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="test",
    )
    if outcome == "success":
        result = session.analyze(target="owner/repo#ISSUE-1", prefer_cache=False)
        assert result["status"] == "generated"
        assert Path(result["path"]).read_text() == cache.load_analysis(item) == "# Fresh analysis"
    else:
        message = "agent failed" if outcome == "error" else "Analysis failed for owner/repo#ISSUE-1"
        with pytest.raises(RuntimeError, match=message):
            session.analyze(target="owner/repo#ISSUE-1", prefer_cache=False)
        assert cache.load_analysis(item) == "# Prior analysis"
    [launch] = launches
    assert not Path(launch["repo_path"]).exists()
    assert launch["pane_name"] == "analyse_owner_repo_1"
    assert launch["group"] == "repo"
    main = workdir / "owner_repo"
    assert not (workdir / "owner_repo_1").exists()
    assert len(git("worktree", "list", "--porcelain", cwd=main).split("worktree ")) == 2
    assert git("branch", "-a", "--list", "*issue-1", cwd=main) == ""
    github.create_issue_branch.assert_not_called()


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("target_url", [None, "https://codeberg.org/owner/target.git"])
def test_targeted_todo_analysis_uses_target_default_branch(tmp_path, monkeypatch, fail, target_url):
    repos = {"owner/todos": tmp_path / "todos", "owner/target": tmp_path / "target"}

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    for name, source in repos.items():
        source.mkdir()
        git(source, "init", "-b", "main")
        git(source, "config", "user.email", "test@example.com")
        git(source, "config", "user.name", "Test")
        (source / "code.txt").write_text(f"{name} default")
        git(source, "add", ".")
        git(source, "commit", "-m", "default")
    target = repos["owner/target"]
    git(target, "switch", "-c", "other")
    (target / "code.txt").write_text("target other branch")
    git(target, "commit", "-am", "other branch")
    git(target, "switch", "main")

    if target_url:
        # Exercise Git URL cloning without contacting the forge.
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{target}.insteadOf")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", target_url)

    item = replace(
        _work_item(number=1, title="Todo"),
        repo="owner/todos",
        todo_target=target_url or "owner/target",
    )
    backend = MagicMock()
    paths = []
    output = tmp_path / "analysis.md"
    output.write_text("analysis")

    def analyze(requested_item, *, tool, **kwargs):
        if tool == "cached":
            return None
        path = Path(kwargs["cwd"])
        paths.append(path)
        assert requested_item == item
        assert (path / "code.txt").read_text() == "owner/target default"
        (path / "agent-created").write_text("untracked")
        if fail:
            raise RuntimeError("agent failed")
        return str(output)

    backend.analyze_item.side_effect = analyze
    backend.github_client.clone_repository.side_effect = lambda repo, dest: git(
        tmp_path, "clone", str(repos[repo]), str(dest)
    )
    session = WorkdashSession(
        config=WorkdashConfig(
            workdir=str(tmp_path / "work"),
            todo_repository="owner/todos",
            agents={"pi": AgentConfig(analyze="pi")},
        ),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="test",
    )
    if fail:
        with pytest.raises(RuntimeError, match="agent failed"):
            session.analyze(target="owner/target#ISSUE-WT1")
    else:
        assert session.analyze(target="owner/target#ISSUE-WT1")["status"] == "generated"
    [temporary_checkout] = paths
    assert temporary_checkout.name == "owner_target_todo_1"
    assert not temporary_checkout.exists()
    main = tmp_path / "work" / "owner_target"
    assert len(git(main, "worktree", "list", "--porcelain").split("worktree ")) == 2
    assert "wt-1" not in git(main, "branch", "--list")
    if target_url:
        backend.github_client.clone_repository.assert_not_called()
    else:
        backend.github_client.clone_repository.assert_called_once_with("owner/target", main)
    backend.github_client.create_issue_branch.assert_not_called()


def test_issue_analysis_reuses_existing_worktree_with_local_changes(tmp_path):
    source = tmp_path / "source"
    source.mkdir()

    def git(*args, cwd=source):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (source / "code.txt").write_text("base")
    git("add", ".")
    git("commit", "-m", "initial")
    workdir = tmp_path / "work"
    workdir.mkdir()
    main = workdir / "owner_repo"
    git("clone", str(source), str(main))
    git("remote", "set-url", "origin", "https://github.com/owner/repo.git", cwd=main)
    worktree = workdir / "owner_repo_1"
    git("worktree", "add", "-b", "issue-1", str(worktree), cwd=main)
    (worktree / "code.txt").write_text("local work")
    item = _work_item(number=1, title="Issue")
    output = tmp_path / "analysis.md"
    output.write_text("analysis")
    backend = MagicMock()

    def analyze(requested_item, *, tool, **kwargs):
        if tool == "cached":
            return None
        assert Path(kwargs["cwd"]) == worktree
        assert (worktree / "code.txt").read_text() == "local work"
        return str(output)

    backend.analyze_item.side_effect = analyze
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(workdir), agents={"pi": AgentConfig(analyze="pi")}),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="test",
    )
    assert session.analyze(target="owner/repo#ISSUE-1")["status"] == "generated"
    assert (worktree / "code.txt").read_text() == "local work"
    backend.github_client.create_issue_branch.assert_not_called()


@pytest.mark.parametrize("upstream", ["none", "different"])
def test_code_creates_worktree_for_existing_pr_branch(monkeypatch, tmp_path, upstream):
    import workdash.repo_worktree as repo_worktree

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, placeholder, _, launch, _ = _authored_session(monkeypatch, tmp_path)
    placeholder.rmdir()
    origin = tmp_path / "origin.git"
    main = tmp_path / "owner_repo"

    def git(cwd, *args):
        return subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    git(tmp_path, "init", "-b", "main", str(main))
    git(main, "config", "user.email", "test@example.com")
    git(main, "config", "user.name", "Test")
    git(main, "commit", "--allow-empty", "-m", "base")
    git(main, "remote", "add", "origin", "https://github.com/owner/repo.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    git(main, "push", "origin", "main")
    git(main, "switch", "-c", "selected-pr-branch")
    git(main, "push", "origin", "selected-pr-branch")
    git(main, "commit", "--allow-empty", "-m", "local work")
    local = git(main, "rev-parse", "HEAD")
    git(main, "switch", "main")
    if upstream == "different":
        git(main, "config", "branch.selected-pr-branch.remote", "origin")
        git(main, "config", "branch.selected-pr-branch.merge", "refs/heads/main")
    monkeypatch.setattr(
        control_module, "existing_worktree_path", repo_worktree.existing_worktree_path
    )
    monkeypatch.setattr(control_module, "ensure_worktree", repo_worktree.ensure_worktree)
    head = MagicMock(return_value=("selected-pr-branch", "owner/repo"))
    monkeypatch.setattr(session.backend.github_client, "fetch_worktree_head", head)
    session.git = repo_worktree.GitHelper()
    verify = MagicMock(wraps=session.git.branch_tracks_repository)
    monkeypatch.setattr(session.git, "branch_tracks_repository", verify)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    worktree = Path(result["cwd"])
    assert result["status"] == "launched"
    launch.assert_called_once()
    assert git(worktree, "rev-parse", "HEAD") == local
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/selected-pr-branch"
    head.assert_called()
    verify.assert_called_with(worktree, "selected-pr-branch", "owner/repo")


@pytest.mark.parametrize(
    "reference",
    [
        "10",
        "#10",
        "testuser/todos#10",
        "testuser/todos#ISSUE-10",
        "https://github.com/testuser/todos/issues/10",
        "owner/repo#ISSUE-WT10",
    ],
)
def test_close_todo_api_closes_correct_issue_and_notifies(reference, monkeypatch):
    item = _work_item(number=10, title="todo")
    item.repo = "testuser/todos"
    item.todo_target = "owner/repo"
    other = replace(
        _work_item(number=10, title="unrelated"),
        created_at=datetime(2026, 2, 11, tzinfo=UTC),
    )
    commands = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: commands.append(command) or SimpleNamespace(stdout=""),
    )
    notifications = []
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[item, other],
        suggestion_markers={(WorkItemType.ISSUE, "testuser/todos", 10): "*"},
        zellij_session=None,
        items_changed_callback=lambda: notifications.append(list(session.work_items)),
    )
    assert [entry["suggested"] for entry in session.list_items()["items"]] == [True, False]
    request = Request.blank(
        "/api/v0/todo_close",
        method="POST",
        content_type="application/json",
        body=json.dumps({"item": reference}).encode(),
    )
    response = request.get_response(control_module._make_turbogears_app(session))
    assert response.status_int == 200
    result = json.loads(response.text)["result"]
    assert result == {
        "item_id": "owner/repo#ISSUE-WT10",
        "todo_repository": "testuser/todos",
        "number": 10,
        "url": "https://github.com/testuser/todos/issues/10",
        "status": "closed",
    }
    assert commands == [["gh", "issue", "close", "10", "--repo", "testuser/todos"]]
    assert session.work_items == [other]
    assert session.list_items()["items"][0]["suggested"] is True
    assert notifications == [[other]]


@pytest.mark.parametrize("targeted", [True, False])
def test_close_todo_absent_from_dashboard(targeted):
    item = _work_item(number=10, title="todo")
    item.repo = "testuser/todos"
    item.todo_target = "owner/repo"
    github = MagicMock()
    github.fetch_item.return_value = item
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=github),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    reference = "owner/repo#ISSUE-WT10" if targeted else "10"
    result = session.close_todo(item=reference)
    assert result["item_id"] == ("owner/repo#ISSUE-WT10" if targeted else "testuser/todos#ISSUE-10")
    github.close_todo.assert_called_once_with(todo_repository="testuser/todos", number=10)
    assert session.work_items == []


def test_close_todo_api_rejects_wrong_repository_and_preserves_state_on_failure(monkeypatch):
    item = _work_item(number=10, title="todo")
    item.repo = "testuser/todos"
    notifications = MagicMock()
    run = MagicMock(side_effect=subprocess.CalledProcessError(1, "gh", stderr="permission denied"))
    monkeypatch.setattr(subprocess, "run", run)
    markers = {(WorkItemType.ISSUE, "testuser/todos", 10): "*"}
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=GitHubClient()),
        work_items=[item],
        suggestion_markers=markers.copy(),
        zellij_session=None,
        items_changed_callback=notifications,
    )
    app = control_module._make_turbogears_app(session)
    for reference, status in [("other/repo#ISSUE-10", 400), ("10", 500), (12, 400)]:
        response = Request.blank(
            "/api/v0/todo_close",
            method="POST",
            content_type="application/json",
            body=json.dumps({"item": reference}).encode(),
        ).get_response(app)
        assert response.status_int == status
        if reference == "10":
            assert (
                "Failed to close todo issue 10 in testuser/todos: permission denied"
                in (json.loads(response.text)["error"]["message"])
            )
    run.assert_called_once_with(
        ["gh", "issue", "close", "10", "--repo", "testuser/todos"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert session.work_items == [item]
    assert session.suggestion_markers == markers
    notifications.assert_not_called()


def test_close_todo_rejects_mismatched_target_without_closing():
    item = _work_item(number=10, title="todo")
    item.repo = "testuser/todos"
    item.todo_target = "owner/repo"
    github = MagicMock()
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=github),
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    response = Request.blank(
        "/api/v0/todo_close",
        method="POST",
        content_type="application/json",
        body=json.dumps({"item": "wrong/repo#ISSUE-WT10"}).encode(),
    ).get_response(control_module._make_turbogears_app(session))
    assert response.status_int == 404
    github.close_todo.assert_not_called()
    assert session.work_items == [item]


@pytest.mark.parametrize("operation", ["create", "close", "resolve"])
def test_todo_github_calls_do_not_block_session_reads(operation):
    item = _work_item(number=10, title="todo")
    item.repo = "testuser/todos"
    item.todo_target = "owner/repo"
    entered = Event()
    release = Event()

    def blocked(**kwargs):
        entered.set()
        assert release.wait(5)
        return item

    github = MagicMock()
    if operation == "create":
        github.create_todo.side_effect = blocked
    elif operation == "close":
        github.close_todo.side_effect = blocked
    else:
        github.fetch_item.side_effect = lambda *args, **kwargs: blocked(**kwargs)
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=github),
        work_items=[] if operation == "resolve" else [item],
        suggestion_markers={},
        zellij_session=None,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        if operation == "create":
            mutation = executor.submit(session.todo, text="todo")
        else:
            reference = "owner/repo#ISSUE-WT10" if operation == "resolve" else "10"
            mutation = executor.submit(session.close_todo, item=reference)
        try:
            assert entered.wait(5)
            listing = executor.submit(session.list_items)
            assert len(listing.result(timeout=1)["items"]) == (operation != "resolve")
        finally:
            release.set()
        mutation.result(timeout=5)


def test_refresh_during_todo_creation_does_not_duplicate_item():
    item = replace(
        _work_item(number=10, title="todo"), repo="testuser/todos", todo_target="owner/repo"
    )

    def create(**kwargs):
        assert len(session.list_items(refresh=True)["items"]) == 1
        return item

    backend = MagicMock()
    backend.github_client.create_todo.side_effect = create
    backend.load_items.return_value = ([replace(item, title="refreshed todo")], {})
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    result = session.todo(text="todo", target="owner/repo")

    items = session.list_items()["items"]
    assert result["item_id"] == "owner/repo#ISSUE-WT10"
    assert [(row["id"], row["title"]) for row in items] == [
        ("owner/repo#ISSUE-WT10", "refreshed todo")
    ]


def test_included_todo_during_capture_keeps_target_and_can_be_closed(monkeypatch):
    import workdash.github_client as github_module
    from workdash.github_client import parse_github_item_url

    url = "https://github.com/testuser/todos/issues/110"
    node = {
        "state": "OPEN",
        "title": "todo",
        "url": url,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "subIssuesSummary": {"total": 0},
    }
    monkeypatch.setattr(subprocess, "run", fake_gh_todo_run([]))
    monkeypatch.setattr(
        github_module,
        "_run_gh_command_with_retry",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"data": {"repository": {"issue": node}}})
        ),
    )
    github = GitHubClient()
    create = github.create_todo
    github.close_todo = MagicMock()
    backend = MagicMock(github_client=github)
    backend.include_item_by_url.side_effect = lambda url, identities: IncludeResult(
        fetched_item=github.fetch_item_by_url(parse_github_item_url(url), "testuser")
    )
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )

    def create_with_include(**kwargs):
        item = create(**kwargs)
        session.include_item_by_url(url)
        return item

    github.create_todo = create_with_include
    result = session.todo(text="todo", target="owner/repo")
    assert result["item_id"] == "owner/repo#ISSUE-WT110"
    assert [row["id"] for row in session.list_items()["items"]] == [result["item_id"]]
    assert session.work_items[0].included
    assert session.close_todo(item=result["item_id"])["status"] == "closed"
    github.close_todo.assert_called_once_with(todo_repository="testuser/todos", number=110)
    assert session.work_items == []


@pytest.mark.parametrize(
    "reference, closes", [("owner/repo#ISSUE-WT110", True), ("wrong/repo#ISSUE-WT110", False)]
)
def test_close_targeted_todo_included_independently_by_url(reference, closes, monkeypatch):
    import workdash.github_client as github_module
    from workdash.github_client import parse_github_item_url

    url = "https://github.com/testuser/todos/issues/110"
    node = {
        "state": "OPEN",
        "title": "todo",
        "url": url,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-02T00:00:00Z",
        "subIssuesSummary": {"total": 0},
    }
    monkeypatch.setattr(
        github_module,
        "_run_gh_command_with_retry",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps({"data": {"repository": {"issue": node}}})
        ),
    )
    github = GitHubClient()
    actual = _work_item(number=10, title="todo")
    actual.number = 110
    actual.repo = "testuser/todos"
    actual.todo_target = "owner/repo"
    github.fetch_item = MagicMock(return_value=actual)
    github.close_todo = MagicMock()
    backend = MagicMock(github_client=github)
    backend.include_item_by_url.side_effect = lambda url, identities: IncludeResult(
        fetched_item=github.fetch_item_by_url(parse_github_item_url(url), "testuser")
    )
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    session.include_item_by_url(url)
    response = Request.blank(
        "/api/v0/todo_close",
        method="POST",
        content_type="application/json",
        body=json.dumps({"item": reference}).encode(),
    ).get_response(control_module._make_turbogears_app(session))
    assert response.status_int == (200 if closes else 404)
    if closes:
        assert json.loads(response.text)["result"]["item_id"] == reference
        github.close_todo.assert_called_once_with(todo_repository="testuser/todos", number=110)
        assert session.work_items == []
    else:
        github.close_todo.assert_not_called()
        assert len(session.work_items) == 1


def test_close_waits_for_inflight_todo_creation():
    item = _work_item(number=10, title="todo")
    item.repo = "testuser/todos"
    created = Event()
    release = Event()
    closed = Event()

    def create(**kwargs):
        created.set()
        assert release.wait(5)
        return item

    github = MagicMock()
    github.create_todo.side_effect = create
    github.close_todo.side_effect = lambda **kwargs: closed.set()
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=github),
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        creation = executor.submit(session.todo, text="todo")
        assert created.wait(5)
        lock = session._todo_lock
        attempting_close = Event()

        @contextmanager
        def observed_lock():
            attempting_close.set()
            with lock:
                yield

        session._todo_lock = observed_lock()
        closing = executor.submit(session.close_todo, item="10")
        try:
            assert attempting_close.wait(5)
            assert not closed.wait(0.3)
        finally:
            release.set()
        creation.result(timeout=5)
        closing.result(timeout=5)
    assert closed.is_set()
    assert session.work_items == []


def test_close_todo_api_updates_tui_table():
    import asyncio

    from textual.widgets import DataTable

    from workdash.tui import WorkdashApp

    todo = _work_item(number=10, title="Close this todo")
    todo.repo = "testuser/todos"
    other = _work_item(number=11, title="Keep this item")
    github = MagicMock()
    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=MagicMock(github_client=github),
        work_items=[todo, other],
        suggestion_markers={},
        zellij_session=None,
    )
    app = WorkdashApp(session=session)
    session.items_changed_callback = lambda: app.call_from_thread(app.refresh_from_session)
    api = control_module._make_turbogears_app(session)

    async def exercise():
        async with app.run_test() as pilot:
            table = app.query_one("#work-items", DataTable)
            assert {str(table.get_row_at(i)[2]).lstrip("* ") for i in range(table.row_count)} == {
                "Close this todo",
                "Keep this item",
            }
            request = Request.blank(
                "/api/v0/todo_close",
                method="POST",
                content_type="application/json",
                body=json.dumps({"item": "10"}).encode(),
            )

            def close_todo():
                response = request.get_response(api)
                assert response.status_int == 200
                return response.json["result"]

            result = await asyncio.to_thread(close_todo)
            assert result["item_id"] == "testuser/todos#ISSUE-10"
            assert result["status"] == "closed"
            await pilot.pause()
            assert table.row_count == 1
            assert str(table.get_row_at(0)[2]).lstrip("* ") == "Keep this item"
            github.close_todo.assert_called_once_with(todo_repository="testuser/todos", number=10)

    asyncio.run(exercise())


def test_dispatch_api_does_not_expose_blocked_scope_metadata(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#2")
    github.add_issue("owner/repo#1", blocked_by=("owner/repo#2",))
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: [])
    session = _ready_session(tmp_path)
    assert session.dispatch_once(scope="owner/repo#1")["blocked_issue"] == ("owner/repo", 1)
    request = Request.blank(
        "/api/v0/dispatch",
        method="POST",
        content_type="application/json",
        body=json.dumps({"scope": "owner/repo#1"}).encode(),
    )
    response = request.get_response(control_module._make_turbogears_app(session))
    assert response.status_int == 200
    assert json.loads(response.text)["result"] == {
        "status": "no_ready_task",
        "scope": "owner/repo#1",
    }


def test_info_api_filters_unrelated_panes_unless_all_requested(monkeypatch, tmp_path):
    worktree = tmp_path / "owner_repo_1"
    panes = [
        {
            "id": 1,
            "title": "code_owner_repo_1",
            "pane_cwd": str(worktree),
            "tab_id": 7,
            "tab_name": "work",
            "pane_command": "codex",
            "state": "Running",
        },
        {"id": 2, "title": "shell", "pane_cwd": str(worktree), "pane_command": "bash"},
    ]
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda _: panes)
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *args, **kwargs: worktree)
    item = _work_item(number=1, title="Work")
    item.url = "https://github.com/owner/repo/issues/1"
    session = WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path)),
        backend=MagicMock(),
        work_items=[item],
        suggestion_markers={},
        zellij_session="project",
    )
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    for payload in ({}, {"panes": "work"}, {"panes": "all"}):
        request = Request.blank(
            "/api/v0/info",
            method="POST",
            content_type="application/json",
            headers={"X-Workdash": version("workdash")},
            body=json.dumps(payload).encode(),
        )
        request.environ["REMOTE_ADDR"] = "127.0.0.1"
        response = request.get_response(app)
        assert response.status_int == 200
        assert response.json["result"]["session"] == "project"
        result = response.json["result"]["panes"]
        assert [pane["pane_id"] for pane in result] == (
            ["terminal_1", "terminal_2"] if payload.get("panes") == "all" else ["terminal_1"]
        )
        assert result[0]["session"] == "project"
        assert result[0]["tab_id"] == 7
        assert result[0]["tab_name"] == "work"
        assert result[0]["title"] == "code_owner_repo_1"
        assert result[0]["cwd"] == str(worktree)
        assert result[0]["command"] == "codex"
        assert result[0]["kind"] == "agent"
        assert result[0]["state"] == "Running"
        assert result[0]["item"] == "owner/repo#ISSUE-1"
        assert result[0]["item_url"] == "https://github.com/owner/repo/issues/1"
        if payload.get("panes") == "all":
            assert result[1]["kind"] == "unknown"
            assert result[1]["item"] == "unknown"
            assert result[1]["command"] == "bash"


@pytest.mark.parametrize(
    ("endpoint", "payload", "zellij_action", "args", "options", "output", "result"),
    [
        pytest.param(
            "content",
            {},
            "dump_zellij_pane",
            (),
            {"full": False},
            "visible output",
            {"content": "visible output", "full": False},
            id="viewport",
        ),
        pytest.param(
            "content",
            {"full": True},
            "dump_zellij_pane",
            (),
            {"full": True},
            "full output",
            {"content": "full output", "full": True},
            id="scrollback",
        ),
        pytest.param(
            "send",
            {"data": "pwd"},
            "send_zellij_pane_input",
            ("pwd",),
            {"raw": False},
            None,
            {"accepted": True, "raw": False},
            id="send-with-enter",
        ),
        pytest.param(
            "send",
            {"data": "pwd", "raw": True},
            "send_zellij_pane_input",
            ("pwd",),
            {"raw": True},
            None,
            {"accepted": True, "raw": True},
            id="send-raw",
        ),
        pytest.param(
            "close",
            {},
            "close_zellij_pane",
            (),
            {},
            None,
            {"accepted": True},
            id="close",
        ),
    ],
)
def test_pane_api_calls_zellij_and_reports_result(
    monkeypatch, endpoint, payload, zellij_action, args, options, output, result
):
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="project",
    )
    action = MagicMock(return_value=output)
    monkeypatch.setattr(launcher_module.zellij, zellij_action, action)
    response = Request.blank(
        f"/api/v0/pane/{endpoint}",
        method="POST",
        content_type="application/json",
        body=json.dumps({"pane_id": "terminal_23", **payload}).encode(),
    ).get_response(control_module._make_turbogears_app(session))
    assert response.status_int == 200
    assert response.json == {"ok": True, "result": {"pane_id": "terminal_23", **result}}
    action.assert_called_once_with("project", "terminal_23", *args, **options)


@pytest.mark.parametrize(
    ("endpoint", "zellij_action"), [("content", "dump_zellij_pane"), ("close", "close_zellij_pane")]
)
def test_pane_api_reports_zellij_failure(monkeypatch, endpoint, zellij_action):
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="project",
    )
    monkeypatch.setattr(
        launcher_module.zellij,
        zellij_action,
        MagicMock(side_effect=RuntimeError("Zellij rejected terminal_404")),
    )
    response = Request.blank(
        f"/api/v0/pane/{endpoint}",
        method="POST",
        content_type="application/json",
        body=b'{"pane_id":"terminal_404"}',
    ).get_response(control_module._make_turbogears_app(session))
    assert response.status_int == 500
    assert response.json == {
        "ok": False,
        "error": {"code": "server_error", "message": "Zellij rejected terminal_404"},
    }


def test_info_without_panes_identifies_dashboard_without_listing(monkeypatch):
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.2:9876")
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="project",
    )
    control_module.WorkdashControlServer(session, listeners=(("127.0.0.3", ("127.0.0.1",)),))
    monkeypatch.setattr(control_module, "_pane_info_payload", MagicMock(side_effect=AssertionError))
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    request = Request.blank(
        "/api/v0/info",
        method="POST",
        content_type="application/json",
        headers={"X-Workdash": version("workdash")},
        body=b'{"panes":"none"}',
    )
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int == 200
    assert response.json["result"] == {
        "session": "project",
        "version": version("workdash"),
        "addresses": ["127.0.0.2:9876", "127.0.0.3:9876"],
    }


def test_running_server_refuses_cli_after_installed_version_changes(monkeypatch, capsys):
    import socket

    import workdash.workdash as workdash_module

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    installed = ["1.0.0"]
    monkeypatch.setattr(control_module, "version", lambda _name: installed[0])
    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=backend,
        work_items=[],
        suggestion_markers={},
        zellij_session="project",
    )
    server = control_module.WorkdashControlServer(session)
    server.start()
    try:
        assert (
            control_module.WorkdashControlClient().request("info", {"panes": "none"})["version"]
            == "1.0.0"
        )
        assert workdash_module.main(["list", "--refresh"]) == 0
        backend.load_items.assert_called_once()
        backend.load_items.reset_mock()
        capsys.readouterr()

        installed[0] = "2.0.0"
        assert workdash_module.main(["list", "--refresh"]) == 1
        output = capsys.readouterr()
        assert "1.0.0" in output.err
        assert "2.0.0" in output.err
        assert "restart the dashboard" in output.err
        backend.load_items.assert_not_called()
    finally:
        server.stop()


@pytest.mark.parametrize("header", [None, "different"])
def test_version_mismatch_reports_both_versions_and_restart(header):
    session = MagicMock()
    app = control_module._source_and_header_guarded_wsgi_app(
        control_module._make_turbogears_app(session)
    )
    headers = {"X-Workdash": header} if header is not None else {}
    request = Request.blank("/api/v0/info", method="POST", headers=headers, body=b"{}")
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int == 403
    error = response.json["error"]
    assert error["code"] == "version_mismatch"
    assert repr(header) in error["message"]
    assert version("workdash") in error["message"]
    assert "restart the dashboard" in error["message"]
    session.info.assert_not_called()


def test_info_rejects_invalid_panes():
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[],
        suggestion_markers={},
        zellij_session="project",
    )
    app = control_module._make_turbogears_app(session)
    request = Request.blank(
        "/api/v0/info", method="POST", content_type="application/json", body=b'{"panes":"invalid"}'
    )
    response = request.get_response(app)
    assert response.status_int == 400
    assert response.json["error"]["code"] == "bad_request"
