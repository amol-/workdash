import base64
import json
import shlex
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from webob import Request

import workdash.control as control_module
import workdash.launcher as launcher_module
from gh_todo_fake import fake_gh_todo_run
from gh_work_state_fake import (
    AFTER_PUSH,
    HEAD_SHA,
    check_run,
    comment,
    failure,
    install_fake_github,
    thread,
    todo_body,
)
from workdash.backend import IncludeResult, WorkdashBackend
from workdash.config import AgentConfig, WorkdashConfig
from workdash.control import WorkdashControlError, WorkdashSession, format_work_item_id
from workdash.github_client import GitHubClient
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.work_instruction import render_work_instruction


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
    ("body_fields", "expected_status"),
    [
        ({}, 200),
        ({"body": None}, 200),
        ({"body": "  Details\n\n  "}, 200),
        ({"body": ""}, 400),
        ({"body": 123}, 400),
        ({"body": ["details"]}, 400),
    ],
)
def test_todo_api_validates_optional_body_without_reformatting(
    body_fields, expected_status, monkeypatch
) -> None:
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
        body=json.dumps({"text": "Short title", **body_fields}).encode(),
    )

    response = request.get_response(control_module._make_turbogears_app(session))

    assert response.status_int == expected_status
    payload = json.loads(response.text)
    if expected_status == 400:
        assert payload["error"]["code"] == "bad_request"
        assert "Optional field 'body' must be a non-empty string." in payload["error"]["message"]
        assert recorded == []
        assert session.work_items == []
    else:
        command = recorded[-1]
        metadata = '```json\n{\n  "workdash_todo_version": 1\n}\n```\n'
        body = body_fields.get("body")
        assert command[command.index("--body") + 1] == (
            metadata if body is None else metadata + "\n" + body
        )
        assert payload == {
            "ok": True,
            "result": {
                "item_id": "testuser/todos#ISSUE-110",
                "todo_repository": "testuser/todos",
                "target": None,
                "number": 110,
                "url": "https://github.com/testuser/todos/issues/110",
            },
        }


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
    launch_agent = MagicMock(
        side_effect=lambda repo, *_args, **_kwargs: (
            panes.append({"id": 30, "title": "code_owner_repo_1", "pane_cwd": repo})
            or SimpleNamespace(session=None, pane_id=None, pane_title="code_owner_repo_1", cwd=repo)
        )
    )
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

    with pytest.raises(control_module.WorkdashControlError, match="pane listing failed"):
        session.code(target="owner/repo#ISSUE-1", agent="pi")

    ensure_worktree.assert_not_called()
    launch_agent.assert_not_called()


def test_vscode_is_not_opened_while_an_agent_pane_is_live(monkeypatch, tmp_path) -> None:
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, _worktree, _ensure, _launch = _coding_session(monkeypatch, tmp_path, panes)
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module.agent_execution.executor, "launch_vscode", launch_vscode)

    result = session.code(target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True)

    assert (result["status"], result["pane_id"]) == ("already_running", "terminal_5")
    launch_vscode.assert_not_called()


def test_vscode_opens_without_a_zellij_session(monkeypatch, tmp_path) -> None:
    session, worktree, _ensure, _launch = _coding_session(
        monkeypatch, tmp_path, [], zellij_session=None
    )
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module.agent_execution.executor, "launch_vscode", launch_vscode)

    result = session.code(target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True)

    assert result["status"] == "launched"
    launch_vscode.assert_called_once_with(str(worktree), "PROMPT")


def test_an_unconfirmed_launch_whose_pane_shows_up_later_is_reported_as_running(
    monkeypatch, tmp_path
) -> None:
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


def test_code_waits_for_a_launched_agent_pane_that_zellij_lists_late(monkeypatch, tmp_path) -> None:
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    launch_agent.side_effect = None
    late_pane = {"id": 8, "title": "code_owner_repo_1", "pane_cwd": str(worktree)}
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


def test_code_without_autostart_launches_the_unchanged_briefing(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])

    session.code(target="owner/repo#ISSUE-1", agent="pi")

    assert _launched_prompt(launch_agent) == "PROMPT"
    assert github.operations == []


def test_code_with_autostart_adds_the_implementation_appendix_once(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    prompt = _launched_prompt(launch_agent)
    assert result["status"] == "launched"
    assert prompt.startswith("PROMPT\n\nAUTOSTART: IMPLEMENT, PUBLISH AND VERIFY")
    assert prompt.count("AUTOSTART:") == 1
    assert prompt.endswith(render_work_instruction("implement", session.work_items[0]))
    assert prompt.count("coordination workflows") == 1


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


def test_code_on_a_live_agent_sends_nothing_and_reads_no_github_state_even_when_it_is_down(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyIssueTree"] = failure("HTTP 502: Bad Gateway")
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, panes)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

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


@pytest.mark.parametrize("autostart", [False, True])
def test_code_on_a_live_authored_pr_reports_its_pane_during_github_outage(
    monkeypatch, tmp_path, autostart
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

    result = session.code(target="owner/repo#PR-1", agent="pi", autostart=autostart)

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
    "pull_request",
    [
        {"checks": {HEAD_SHA: [check_run("tests", "FAILURE")]}},
        {"mergeable": "CONFLICTING"},
        {"threads": [thread(comment("reviewer", created=AFTER_PUSH))]},
    ],
    ids=["failing_ci", "merge_conflicts", "review_feedback"],
)
def test_autostarting_an_issue_with_an_authored_pull_request_repairs_that_pull_request(
    pull_request, monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), **pull_request)
    session, _worktree, _ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)

    result = session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    [state] = GitHubClient().evaluate_pull_requests(
        [("owner/repo", 51)], github_username="testuser", todo_repository="testuser/todos"
    )
    prompt = _launched_prompt(launch_agent)
    worked_on = prepare.call_args.args[0]
    assert (worked_on.item_type, worked_on.number) == (WorkItemType.PR, 51)
    # The result still names the target that was asked for.
    assert result["item_id"] == "owner/repo#ISSUE-1"
    assert prompt.startswith("PROMPT\n\nAUTOSTART: IMPLEMENT")
    assert prompt.endswith(render_work_instruction(state.action, state))
    # The configured-practices rule belongs to the appendix, so repairs get it too.
    assert prompt.count("coordination workflows") == 1


def test_autostarting_a_pull_request_waiting_for_others_orders_no_work(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", None, status="IN_PROGRESS")]},
    )
    session, _worktree, _ensure, launch_agent, _prepare = _authored_session(monkeypatch, tmp_path)

    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    prompt = _launched_prompt(launch_agent)
    assert "AUTOSTART" not in prompt
    assert "still without a final CI result" in prompt
    assert "--add-assignee" not in prompt and "force-push" not in prompt


def test_autostarting_an_issue_closed_by_somebody_elses_pull_request_still_implements_it(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",), author="someone-else")
    session, _worktree, _ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)

    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert prepare.call_args.args[0] is session.work_items[0]
    assert "AUTOSTART: IMPLEMENT" in _launched_prompt(launch_agent)
    assert "Continue the assigned issue" in _launched_prompt(launch_agent)


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


def test_autostart_implements_a_targeted_todo_without_a_pull_request(monkeypatch, tmp_path) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("testuser/todos#7", body=todo_body("owner/other"))
    session, _worktree, _ensure, launch_agent, prepare = _authored_session(monkeypatch, tmp_path)
    session.config = replace(session.config, todo_repository="testuser/todos")
    todo = _work_item(number=7, title="Todo")
    todo.repo = session.config.todo_repository
    todo.todo_target = "owner/other"
    session.work_items.append(todo)

    session.code(target=format_work_item_id(todo), agent="pi", autostart=True)

    assert prepare.call_args.args[0] is todo
    assert "Continue the assigned issue" in _launched_prompt(launch_agent)


def test_autostarting_a_review_reads_no_github_state_and_stays_review_only(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])
    review = _work_item(number=2, title="Review", item_type=WorkItemType.PR)
    session.work_items.append(review)

    session.code(target="owner/repo#CHECK-2", agent="pi", autostart=True)

    prompt = _launched_prompt(launch_agent)
    assert prompt.startswith("PROMPT\n\nAUTOSTART: REVIEW")
    assert "coordination workflows" not in prompt
    assert "--add-assignee" not in prompt and "force-push" not in prompt
    assert github.operations == []


def test_autostart_reports_a_failed_work_state_lookup_without_launching(
    monkeypatch, tmp_path
) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyIssueTree"] = failure("HTTP 502: Bad Gateway")
    session, _worktree, _ensure, launch_agent = _coding_session(monkeypatch, tmp_path, [])

    with pytest.raises(WorkdashControlError) as raised:
        session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)

    assert (raised.value.code, raised.value.status) == ("github_error", HTTPStatus.BAD_GATEWAY)
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
        control_module._source_filtered_wsgi_app(unexpected_app)(
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
            (hosts[0], ("127.0.0.5",)),
            (hosts[1], (hosts[2],)),
            (hosts[2], (hosts[1],)),
        ),
    )
    server.start()
    httpds, threads = list(server._httpds), list(server._threads)
    try:
        # Both explicit sources can use every listener, including the default.
        for host in hosts:
            for source in (*hosts[1:], "127.0.0.5"):
                connection = HTTPConnection(host, port, timeout=2, source_address=(source, 0))
                try:
                    connection.request(
                        "POST", "/api/v0/list", "{}", {"Content-Type": "application/json"}
                    )
                    response = connection.getresponse()
                    assert response.status == 200
                    assert (
                        json.loads(response.read())["result"]["items"][0]["title"] == "Shared item"
                    )
                finally:
                    connection.close()
            denied = HTTPConnection(host, port, timeout=2, source_address=("127.0.0.4", 0))
            try:
                denied.request(
                    "POST",
                    "/api/v0/todo",
                    '{"text":"not allowed"}',
                    {
                        "Content-Type": "application/json",
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
        with pytest.raises(RuntimeError, match="127.0.0.2"):
            server.start()
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
    failure = OSError("connection refused")
    monkeypatch.setattr(control_module.urllib.request, "urlopen", MagicMock(side_effect=failure))
    with pytest.raises(control_module.WorkdashControlError, match="dashboard.lan:8765") as error:
        control_module.WorkdashControlClient("dashboard.lan").request("list")
    assert error.value.status == 503
    assert error.value.code == "server_unreachable"
    assert error.value.__cause__ is failure


def test_environment_selects_real_server_and_cli(monkeypatch, capsys):
    import socket

    import workdash.workdash as cli

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
    server = control_module.WorkdashControlServer(session, listeners=((host, ("127.0.0.2",)),))
    server.start()
    try:
        assert cli.main(["list", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["items"][0]["title"] == "Isolated"
        assert control_module.show_config_payload(WorkdashConfig())["server"] == {
            "host": host,
            "port": port,
        }
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

    def redirect(request):
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
    panes = []
    session, _, _, launch = _coding_session(monkeypatch, tmp_path, panes)
    launch.side_effect = lambda repo, prompt, **kwargs: (
        panes.append({"id": 30, "title": "code_owner_repo_1", "pane_cwd": repo, "tab_name": "epic"})
        or SimpleNamespace(session=None, pane_id=None, pane_title="code_owner_repo_1", cwd=repo)
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))

    def get(path, method="GET", peer="127.0.0.1"):
        request = Request.blank(path, method=method)
        request.environ["REMOTE_ADDR"] = peer
        return request.get_response(app)

    base = "/api/items/owner/repo%23ISSUE-1/files/"
    response = get(base + ".workdash/review.md")
    assert response.status_int == 200 and response.text == "real review", response.text
    assert response.content_type == "text/plain"
    assert get(base + ".workdash/nested/review.md").text == "nested review"
    assert get(base + ".workdash/alias").text == "real review"
    assert get(base + ".workdash/nested-alias/relative-alias").text == "real review"
    assert get(base).json["result"]["entries"] == [
        "alias",
        "chain",
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))

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
    request = Request.blank(f"/api/items/owner/repo%23ISSUE-1/files/{suffix}")
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))
    request = Request.blank(f"/api/items/owner/repo%23ISSUE-1/files/{suffix}")
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))
    request = Request.blank("/api/items/owner/repo%23ISSUE-1/files/")
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))
    request = Request.blank("/api/items/owner/repo%23ISSUE-1/files/")
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert replaced
    assert "alias" not in response.json["result"]["entries"]


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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))
    for item_id, expected in (("owner/repo%23PR-2", "shared"), ("owner/repo%23ISSUE-WT3", "todo")):
        request = Request.blank(f"/api/items/{item_id}/files/.workdash/artifact.txt")
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))
    request = Request.blank(f"/api/items/owner/repo%23ISSUE-1/files/{suffix}")
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
    app = control_module._source_filtered_wsgi_app(control_module._make_turbogears_app(session))
    request = Request.blank("/api/items/owner/repo%23ISSUE-1/files/.workdash/artifact.txt")
    request.environ["REMOTE_ADDR"] = "127.0.0.1"
    response = request.get_response(app)
    assert response.status_int in (200, 404)
    assert "secret" not in response.text
    assert descriptors
    for fd in descriptors:
        with pytest.raises(OSError):
            control_module.os.fstat(fd)


def test_coding_authored_pr_uses_linked_issue_analysis_and_claim(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )
    session, _worktree, _ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.analysis = "issue analysis"
    pr = _work_item(number=2, title="PR", item_type=WorkItemType.PR)
    pr.number = 51
    pr.kind = WorkItemKind.AUTHORED_PR
    pr.analysis = "pr analysis"
    session.work_items.append(pr)

    result = session.code(target="owner/repo#PR-51", agent="pi", autostart=True)
    assert result["item_id"] == "owner/repo#ISSUE-1"
    assert prepare.call_args.kwargs["analysis_path"] == session.analysis_url(issue)
    assert prepare.call_args.args[0].number == 51
    assert "has failing checks: tests" in _launched_prompt(launch)
    assert (
        session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)["status"]
        == "already_running"
    )


def test_authored_pr_without_issue_never_uses_pr_analysis(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_pull_request("owner/repo#51")
    session, _worktree, _ensure, _launch, prepare = _authored_session(monkeypatch, tmp_path)
    pr = _work_item(number=2, title="PR", item_type=WorkItemType.PR)
    pr.number = 51
    pr.kind = WorkItemKind.AUTHORED_PR
    pr.analysis = "pr analysis"
    session.work_items.append(pr)
    result = session.code(target="owner/repo#PR-51", agent="pi")
    assert result["item_id"] == "owner/repo#PR-51"
    assert prepare.call_args.kwargs["analysis_path"] is None


def test_issue_redirect_preserves_issue_analysis(monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    session, _worktree, _ensure, _launch, prepare = _authored_session(monkeypatch, tmp_path)
    issue = session.work_items[0]
    issue.analysis = "issue analysis"
    session.code(target="owner/repo#ISSUE-1", agent="pi", autostart=True)
    assert prepare.call_args.kwargs["analysis_path"] == session.analysis_url(issue)


def test_plan_first_issue_resumes_authored_pr_branch_without_existing_worktree(
    monkeypatch, tmp_path
):
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#1",))
    session, _worktree, _ensure, launch, prepare = _authored_session(monkeypatch, tmp_path)
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *_args, **kwargs: None)
    result = session.code(target="owner/repo#ISSUE-1", agent="pi")
    assert result["item_id"] == "owner/repo#ISSUE-1"
    assert prepare.call_args.args[0].number == 51
    assert "AUTOSTART" not in _launched_prompt(launch)


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
    monkeypatch.setattr(session, "_ready", lambda *args, **kwargs: [candidate])
    limit = MagicMock(side_effect=RuntimeError("limit reached"))

    result = session.dispatch_once(scope="owner/repo", agent="pi", before_launch=limit)

    assert result["status"] == "no_ready_task"
    limit.assert_not_called()
    launch.assert_not_called()


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
        return SimpleNamespace(session=None, pane_id=None, pane_title=title, cwd=repo)

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
        return SimpleNamespace(session=None, pane_id=None, pane_title=title, cwd=repo)

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
            session=None, pane_id=None, pane_title="code_owner_other_todo_7", cwd=repo
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
    from workdash.work_state import PullRequestWorkState, ReadyWork

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
            return [ReadyWork(item=item, pull_request=self.state)] if self.state.ready else []

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

        def load_zellij_panes(self, session):
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


def test_terminal_prepares_worktree_with_session_git(monkeypatch, tmp_path) -> None:
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
    monkeypatch.setattr(
        control_module,
        "launch_terminal_context",
        lambda path, **kwargs: SimpleNamespace(
            session=kwargs["zellij_session"], cwd=path, pane_title="terminal", pane_id="1"
        ),
    )
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess"))

    result = session.terminal(target="owner/repo#ISSUE-1")

    worktree = tmp_path / "owner_repo_1"
    assert result["cwd"] == str(worktree)
    git.create_worktree.assert_called_once_with(
        tmp_path / "owner_repo", worktree, "issue-1", "origin/HEAD"
    )


def test_session_analyzes_and_launches_coding_agent_with_real_executor(
    monkeypatch, tmp_path
) -> None:
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
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda *_, **kwargs: worktree)
    monkeypatch.setattr(control_module, "get_merge_base", lambda *_, **kwargs: None)
    monkeypatch.setattr("workdash.github_client.GitHubClient.fetch_analysis_context", lambda *_: {})
    monkeypatch.setattr("workdash.github_client.GitHubClient.fetch_launch_context", lambda *_: {})

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
    fake_zellij.load_zellij_panes.side_effect = lambda _: list(panes)
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
    shell_command = shlex.split(command[2])
    assert shell_command[0] == "pi"
    assert "Implement the issue" in shell_command[1]
    assert analysis["url"] in shell_command[1]


def test_shared_agent_execution_can_simulate_analysis_and_coding(monkeypatch, tmp_path) -> None:
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
                "id": 14,
                "title": "my shell",
                "pane_command": "/usr/local/bin/nu -i",
                "pane_cwd": str(worktree),
            },
            {"id": 13, "title": "terminal_owner_repo_1", "pane_cwd": str(worktree)},
            {"id": 8, "title": "nvim", "pane_cwd": str(worktree)},
            {"id": 9, "title": "pytest", "pane_cwd": str(worktree)},
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
        "terminal_14",
        "terminal_13",
    ]


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
    elsewhere = tmp_path / "elsewhere" / worktree.name
    elsewhere.mkdir(parents=True)
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
