import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import workdash.control as control_module
from workdash.backend import IncludeResult
from workdash.config import WorkdashConfig
from workdash.control import WorkdashSession, format_work_item_id
from workdash.models import WorkItem, WorkItemKind, WorkItemType


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


def test_session_refresh_schedules_tui_repaint_through_callback() -> None:
    initial_item = _work_item(number=1, title="Initial")
    refreshed_item = _work_item(number=2, title="Refreshed", item_type=WorkItemType.PR)
    marker = {(WorkItemType.PR, "owner/repo", 2): "*"}
    repainted_item_ids: list[list[str]] = []

    class FakeBackend:
        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [refreshed_item], marker

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

    result = session.list_items(refresh=True)

    assert [item["id"] for item in result["items"]] == ["owner/repo#CHECK-2"]
    assert [item["display_type"] for item in result["items"]] == ["CHECK"]
    assert [format_work_item_id(item) for item in session.work_items] == ["owner/repo#CHECK-2"]
    assert session.suggestion_markers == marker
    assert repainted_item_ids == [["owner/repo#CHECK-2"]]


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
        linked_issue=("owner/repo", 41830),
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
        control_module,
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

    assert [pane["pane_id"] for pane in session.get_agent_panes_for_item(item)] == ["terminal_1"]
    assert [pane["item"] for pane in session.info()["panes"]] == ["owner/repo#PR-42149"]


def test_todo_notifies_the_live_tui_after_capturing(monkeypatch) -> None:
    """A captured todo must reach a running TUI without waiting for the next refresh."""

    initial_item = _work_item(number=1, title="Initial")
    todo_item = _work_item(number=10, title="Fix the flaky test")
    todo_item.repo = "testuser/todos"
    todo_item.todo_target = "owner/repo"
    create_todo = MagicMock(return_value=todo_item)
    monkeypatch.setattr(control_module, "create_todo", create_todo)
    repainted_item_ids: list[list[str]] = []

    session = WorkdashSession(
        config=WorkdashConfig(todo_repository="testuser/todos"),
        backend=object(),  # type: ignore[arg-type]
        work_items=[initial_item],
        suggestion_markers={},
        zellij_session=None,
        items_changed_callback=lambda: repainted_item_ids.append(
            [format_work_item_id(item) for item in session.work_items]
        ),
    )

    result = session.todo(text="  Fix the flaky test  ", target="owner/repo")

    create_todo.assert_called_once_with(
        todo_repository="testuser/todos", text="Fix the flaky test", target="owner/repo"
    )
    assert result["item_id"] == "owner/repo#ISSUE-WT10"
    assert repainted_item_ids == [["owner/repo#ISSUE-1", "owner/repo#ISSUE-WT10"]]


def test_session_analyze_returns_cached_analysis_without_configured_agents(tmp_path) -> None:
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

    result = session.analyze(target="owner/repo#ISSUE-1")

    assert result == {
        "item_id": "owner/repo#ISSUE-1",
        "path": str(cached_path),
        "agent": "cached",
        "cache_used": True,
        "status": "cached",
        "content_type": "text/markdown",
        "file_name": "cached-analysis.md",
        "file_content": base64.b64encode(b"# cached analysis\n").decode("ascii"),
    }
    assert analyze_calls == [(item, "cached")]


def test_api_rejects_non_localhost_remote_addr() -> None:
    def unexpected_app(_environ, _start_response):
        raise AssertionError("non-localhost request reached the API app")

    response: list[tuple[str, dict[str, str]]] = []

    def start_response(status, headers, _exc_info=None):
        response.append((status, dict(headers)))

    body = b"".join(
        control_module._localhost_only_wsgi_app(unexpected_app)(
            {"REMOTE_ADDR": "203.0.113.1"}, start_response
        )
    )

    assert response[0][0] == "403 Forbidden"
    assert json.loads(body.decode("utf-8")) == {
        "ok": False,
        "error": {
            "code": "forbidden",
            "message": "Workdash V0 only accepts localhost clients.",
        },
    }
