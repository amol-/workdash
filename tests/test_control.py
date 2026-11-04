import base64
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from webob import Request

import workdash.control as control_module
from gh_todo_fake import fake_gh_todo_run
from workdash.backend import IncludeResult
from workdash.config import AgentConfig, WorkdashConfig
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


@pytest.mark.parametrize("body", [None, "  Context\n\nMore details\n  "])
def test_todo_notifies_the_live_tui_after_capturing(body, monkeypatch) -> None:
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
        backend=MagicMock(),
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
    monkeypatch.setattr(control_module, "load_zellij_panes", lambda _session: list(panes))
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda _wd, _item: worktree)
    monkeypatch.setattr(control_module, "ensure_worktree", ensure_worktree)
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(control_module, "prepare_launch_agent_prompt", lambda *a, **k: "PROMPT")
    monkeypatch.setattr(control_module, "launch_agent_context", launch_agent)
    monkeypatch.setattr(control_module, "_LAUNCH_VISIBILITY_INTERVAL_SECONDS", 0)
    session = WorkdashSession(
        config=WorkdashConfig(agents={"pi": AgentConfig(launch="pi")}, workdir=str(tmp_path)),
        backend=MagicMock(),
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
        # The agent changed directory, but its title still names the worktree.
        {"id": 9, "title": "code_owner_repo_1", "pane_cwd": "/elsewhere"},
        {"id": 4, "title": "code_owner_repo_1", "pane_cwd": str(worktree / "src")},
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
        "cwd": str(worktree / "src"),
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_4",
    }
    ensure_worktree.assert_not_called()
    launch_agent.assert_not_called()
    # The loaded dashboard row is used as is, without a GitHub lookup.
    session.backend.github_client.fetch_item.assert_not_called()
    assert [
        pane["pane_id"] for pane in session.get_agent_panes_for_item(session.work_items[0])
    ] == [
        "terminal_4",
        "terminal_9",
    ]


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


def test_vscode_is_not_opened_while_an_agent_pane_is_live(monkeypatch, tmp_path) -> None:
    panes = [{"id": 5, "title": "code_owner_repo_1", "pane_cwd": str(tmp_path / "owner_repo_1")}]
    session, _worktree, _ensure, _launch = _coding_session(monkeypatch, tmp_path, panes)
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module, "launch_vscode_context", launch_vscode)

    result = session.code(target="owner/repo#ISSUE-1", agent="vscode", allow_vscode=True)

    assert (result["status"], result["pane_id"]) == ("already_running", "terminal_5")
    launch_vscode.assert_not_called()


def test_vscode_opens_without_a_zellij_session(monkeypatch, tmp_path) -> None:
    session, worktree, _ensure, _launch = _coding_session(
        monkeypatch, tmp_path, [], zellij_session=None
    )
    launch_vscode = MagicMock()
    monkeypatch.setattr(control_module, "launch_vscode_context", launch_vscode)

    assert session.get_agent_panes_for_item(session.work_items[0]) == []
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
        control_module, "load_zellij_panes", MagicMock(side_effect=[[], [], [late_pane]])
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
    monkeypatch.setattr(control_module, "create_todo", create_todo)
    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=MagicMock(),
        work_items=[_work_item(number=1, title="Shared item")],
        suggestion_markers={},
        zellij_session=None,
    )
    hosts = ("127.0.0.1", "127.0.0.2", "127.0.0.3")
    server = control_module.WorkdashControlServer(
        session, listeners=((hosts[1], (hosts[2],)), (hosts[2], (hosts[1],)))
    )
    server.start()
    httpds, threads = list(server._httpds), list(server._threads)
    try:
        # Both explicit sources can use every listener, including the default.
        for host in hosts:
            for source in hosts[1:]:
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


@pytest.mark.parametrize("failing_host", ["127.0.0.3", "localhost"])
def test_listener_bind_failure_releases_all_earlier_sockets(monkeypatch, failing_host) -> None:
    import errno
    import socket

    bound = []
    make_server = control_module.make_server

    def record_server(host, port, app):
        httpd = make_server(host, port, app)
        bound.append(httpd)
        return httpd

    monkeypatch.setattr(control_module, "make_server", record_server)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.3", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(control_module, "SERVER_PORT", port)
        server = control_module.WorkdashControlServer(
            MagicMock(), listeners=(("127.0.0.2", ("192.0.2.1",)), (failing_host, ("192.0.2.2",)))
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


def test_resolved_wildcard_listener_is_rejected_and_closed(monkeypatch) -> None:
    monkeypatch.setattr(control_module, "SERVER_PORT", 0)
    bound = []
    make_server = control_module.make_server

    def record_server(host, port, app):
        httpd = make_server(host, port, app)
        bound.append(httpd)
        return httpd

    monkeypatch.setattr(control_module, "make_server", record_server)
    server = control_module.WorkdashControlServer(MagicMock(), listeners=(("0", ("192.0.2.1",)),))
    with pytest.raises(RuntimeError, match="Wildcard listener binds"):
        server.start()
    assert len(bound) == 2
    assert all(httpd.socket.fileno() == -1 for httpd in bound)


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
