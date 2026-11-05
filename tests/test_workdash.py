import base64
import json
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import workdash.control as control_module
import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.workdash import _print_work_items, _print_work_items_result

_VALID_CONFIG = WorkdashConfig(
    github_username="testuser",
    open_command="xdg-open",
    agents={
        "codex": AgentConfig(analyze="codex exec", launch="codex"),
        "claude": AgentConfig(analyze="claude -p", launch="claude"),
        "pi": AgentConfig(launch="pi --no-tips"),
    },
    repositories=("owner/*",),
    workdir="~/wrk",
    todo_repository="testuser/todos",
)


def _auth_status_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workdash_module.subprocess, "run", lambda *args, **kwargs: None)


def _git_origin_proves(monkeypatch: pytest.MonkeyPatch, worktree: object, repo: str) -> None:
    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            if str(kwargs.get("cwd")) == str(worktree):
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{worktree}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            if str(kwargs.get("cwd")) == str(worktree):
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"https://github.com/{repo}.git\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)


def _issue(repo: str = "owner/repo", number: int = 1) -> WorkItem:
    created_at = datetime(2026, 2, 1, tzinfo=UTC)
    return WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo=repo,
        number=number,
        title="Issue title",
        created_at=created_at,
        updated_at=created_at,
        url=f"https://example.com/{number}",
    )


def test_main_prints_loading_message_before_tui_start(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: dict[str, bool] = {"load": False, "run": False, "mouse": True}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is not None
            calls["load"] = True
            return [], {}

        def analyze_item(self, _item, tool="codex"):
            return None

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            assert kwargs["work_items"] == []
            assert kwargs["suggestion_markers"] == {}

        def run(self, *, mouse: bool) -> None:
            calls["run"] = True
            calls["mouse"] = mouse

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setenv("ZELLIJ", "0")

    exit_code = workdash_module.main([])

    assert exit_code == 0
    assert calls == {"load": True, "run": True, "mouse": False}
    captured = capsys.readouterr()
    assert captured.out.startswith("Loading work items from GitHub...\n")


def test_main_bare_interactive_uses_own_zellij_session_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Outside --server, the dashboard must find active agent panes for itself
    # (e.g. to offer focusing one instead of only starting a new session), so
    # it needs its own Zellij session name without asking Zellij to enumerate
    # sessions, which is reserved for a separate process finding it later.
    captured: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [], {}

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured["session"] = kwargs["session"]

        def run(self, *, mouse: bool) -> None:
            pass

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-abc123")

    assert workdash_module.main([]) == 0

    assert captured["session"].zellij_session == "workdash-abc123"


@pytest.mark.parametrize(
    "listeners",
    [(), (("dashboard.lan", ("192.0.2.1",)), ("192.0.2.5", ("192.0.2.2", "192.0.2.3")))],
)
def test_main_server_refresh_before_tui_run_updates_session_and_repaints_when_running(
    monkeypatch: pytest.MonkeyPatch,
    listeners,
) -> None:
    initial_item = _issue(number=1)
    early_refresh_item = _issue(number=2)
    running_refresh_item = _issue(number=3)
    captured: dict[str, object] = {"scheduled_callbacks": []}
    load_results = [initial_item, early_refresh_item, running_refresh_item]

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            item = load_results.pop(0)
            return [item], {}

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeControlServer:
        def __init__(self, session, *, listeners=()) -> None:
            captured["listeners"] = listeners
            self.session = session

        def start(self) -> None:
            assert self.session.items_changed_callback is not None
            payload = self.session.list_items(refresh=True)
            captured["early_api_ids"] = [item["id"] for item in payload["items"]]
            captured["early_session_ids"] = [item.number for item in self.session.work_items]
            captured["server_started"] = True

        def stop(self) -> None:
            captured["server_stopped"] = True

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            self.is_running = False
            captured["app"] = self
            captured["app_session"] = kwargs["session"]

        def call_from_thread(self, callback):
            if not self.is_running:
                raise RuntimeError("App is not running")
            captured["scheduled_callbacks"].append(callback.__name__)
            callback()

        def refresh_from_session(self) -> None:
            session = captured["app_session"]
            captured["app_refreshed_ids"] = [item.number for item in session.work_items]

        def run(self, *, mouse: bool) -> None:
            self.is_running = True
            session = captured["app_session"]
            session.list_items(refresh=True)
            captured["app_ran"] = True

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", FakeControlServer)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(workdash_module, "list_workdash_sessions", lambda: ["workdash-main"])
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")

    argv = ["--server"]
    for host, ips in listeners:
        argv.extend(["--listen", f"{host}@{','.join(ips)}"])
    assert workdash_module.main(argv) == 0
    assert captured["listeners"] == listeners

    assert captured["app_session"].items_changed_callback is not None
    assert captured["early_api_ids"] == ["owner/repo#ISSUE-2"]
    assert captured["early_session_ids"] == [2]
    assert captured["scheduled_callbacks"] == ["refresh_from_session"]
    assert captured["app_refreshed_ids"] == [3]
    assert captured["server_started"] is True
    assert captured["app_ran"] is True
    assert captured["server_stopped"] is True


def test_main_server_refresh_callback_reraises_unrelated_runtime_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _issue()
    captured: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeControlServer:
        def __init__(self, session, *, listeners=()) -> None:
            assert listeners == ()
            self.session = session

        def start(self) -> None:
            captured["server_started"] = True

        def stop(self) -> None:
            captured["server_stopped"] = True

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            self.is_running = True
            captured["app_session"] = kwargs["session"]

        def call_from_thread(self, callback):
            raise RuntimeError("refresh exploded")

        def refresh_from_session(self) -> None:
            raise AssertionError("refresh should be reached through call_from_thread")

        def run(self, *, mouse: bool) -> None:
            session = captured["app_session"]
            session.items_changed_callback()

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", FakeControlServer)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(workdash_module, "list_workdash_sessions", lambda: ["workdash-main"])
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")

    with pytest.raises(RuntimeError, match="refresh exploded"):
        workdash_module.main(["--server"])

    assert captured["server_started"] is True
    assert captured["server_stopped"] is True


@pytest.mark.parametrize("needs_pane", [False, True])
def test_main_passes_configured_agent_choices_and_shared_viewer_to_tui(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    needs_pane: bool,
) -> None:
    captured_kwargs: dict[str, object] = {}
    commands: list[list[str]] = []
    config = WorkdashConfig(
        github_username="testuser",
        open_command='term-copy --label "two words"',
        open_needs_pane=needs_pane,
        agents={"codex": AgentConfig(analyze="codex exec")},
        repositories=("owner/*",),
        workdir="~/wrk",
        todo_repository="testuser/todos",
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [], {}

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_kwargs.update(kwargs)

        def run(self, *, mouse: bool) -> None:
            return None

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="terminal_17\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-selected")

    assert workdash_module.main([]) == 0

    assert captured_kwargs["analyze_choices"] == config.tui_analyze_choices()
    assert captured_kwargs["code_choices"] == config.tui_code_choices()
    commands.clear()
    item = replace(_issue(), url="https://example.com/1?q=$(touch nope);&x=two words")
    analysis_path = tmp_path / "analysis ; literal.md"
    analysis_path.write_text("# Viewer analysis\n\n**Rendered content**")
    captured_kwargs["open_callback"](item)
    captured_kwargs["open_markdown_callback"](str(analysis_path))
    html_path = analysis_path.with_suffix(".html")
    assert "<h1>Viewer analysis</h1>" in html_path.read_text()
    assert "<strong>Rendered content</strong>" in html_path.read_text()
    viewer_argv = ["term-copy", "--label", "two words"]
    if needs_pane:
        prefix = ["/usr/bin/zellij", "--session", "workdash-selected", "action"]
        assert commands == [
            [*prefix, "new-pane", "--", *viewer_argv, item.url],
            [*prefix, "toggle-fullscreen", "-p", "terminal_17"],
            [*prefix, "new-pane", "--", *viewer_argv, str(html_path)],
            [*prefix, "toggle-fullscreen", "-p", "terminal_17"],
        ]
    else:
        assert commands == [[*viewer_argv, item.url], [*viewer_argv, str(html_path)]]


def test_main_tui_analyze_callback_uses_worktree_and_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    item = _issue()
    analysis_path = tmp_path / "analysis.md"
    ensure_calls: list[tuple[str | None, WorkItem]] = []
    analyze_calls: list[tuple[WorkItem, str, str | None]] = []
    captured_callback: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is not None
            return [item], {}

        def analyze_item(self, item, tool="codex", *, cwd=None):
            analyze_calls.append((item, tool, cwd))
            analysis_path.write_text("analysis body\n", encoding="utf-8")
            return str(analysis_path)

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_callback["analyze"] = kwargs["analyze_callback"]

        def run(self, *, mouse: bool) -> None:
            return None

    def fake_ensure_worktree(workdir, item):
        ensure_calls.append((workdir, item))
        return "/tmp/worktree"

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", fake_ensure_worktree)
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

    result = captured_callback["analyze"](item, tool="codex")

    assert ensure_calls == [(_VALID_CONFIG.workdir, item)]
    assert analyze_calls == [(item, "codex", "/tmp/worktree")]
    assert result == str(analysis_path)


def test_main_list_command_does_not_print_loading_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "list"
            assert payload == {"refresh": False}
            return {"items": []}

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected config load")),
    )

    exit_code = workdash_module.main(["list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "No work items found.\n"


def test_print_server_backed_list_uses_display_type_without_changing_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _print_work_items_result(
        {
            "items": [
                {
                    "id": "owner/repo#PR-2",
                    "display_type": "PR+",
                    "updated_at": "2026-02-02T00:00:00+00:00",
                    "title": "Included PR",
                    "suggested": False,
                }
            ]
        }
    )

    output_line = capsys.readouterr().out.strip()
    assert output_line.startswith("PR+     owner/repo#PR-2")


def test_main_exits_with_error_when_gh_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setenv("ZELLIJ", "0")

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "gh CLI is not installed" in captured.err


def test_main_exits_with_error_when_config_incomplete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: WorkdashConfig())
    monkeypatch.setenv("ZELLIJ", "0")

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "missing configuration fields" in captured.err
    assert "--configure" in captured.err


@pytest.mark.parametrize("command", ["", "   ", "open 'broken"])
def test_main_rejects_missing_or_malformed_viewer_before_zellij_wrapping(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    config = replace(_VALID_CONFIG, open_command=command)

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(
        workdash_module,
        "WorkdashBackend",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("backend should not load invalid runtime config")
        ),
    )
    monkeypatch.setattr(
        workdash_module,
        "exec_zellij_wrapped_workdash",
        lambda _argv: (_ for _ in ()).throw(AssertionError("unexpected Zellij wrapper")),
    )
    monkeypatch.delenv("ZELLIJ", raising=False)

    assert workdash_module.main([]) == 1
    captured = capsys.readouterr()
    assert "open" in captured.err
    if command.strip():
        assert "No closing quotation" in captured.err
    else:
        assert "missing configuration fields: open" in captured.err
    assert "workdash --configure" in captured.err


@pytest.mark.parametrize("argv", [[], ["--server"]])
def test_main_exits_with_config_guidance_when_startup_config_command_is_malformed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    config = WorkdashConfig(
        github_username="testuser",
        open_command="xdg-open",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(launch="pi 'broken"),
        },
        repositories=("owner/repo",),
        workdir="~/wrk",
        todo_repository="testuser/todos",
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:  # pragma: no cover - should not run
            raise AssertionError("backend should not load invalid runtime config")

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "exec_zellij_wrapped_workdash",
        lambda _argv: (_ for _ in ()).throw(AssertionError("unexpected Zellij wrapper")),
    )
    monkeypatch.delenv("ZELLIJ", raising=False)

    exit_code = workdash_module.main(argv)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "invalid configuration fields" in captured.err
    assert "agents.pi.launch" in captured.err
    assert "workdash --configure" in captured.err


def test_select_workdash_session_treats_no_zellij_sessions_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workdash_module, "list_workdash_sessions", lambda: [])

    with pytest.raises(RuntimeError, match="active Workdash-owned Zellij session is required"):
        workdash_module._select_workdash_session(None)


@pytest.mark.parametrize(
    ("argv", "expected_endpoint", "expected_payload"),
    [
        (["list"], "list", {"refresh": False}),
        (["list", "--refresh"], "list", {"refresh": True}),
        (["info"], "info", {"include_all_panes": False}),
        (["ready", "any"], "ready", {"scope": "any"}),
        (["ready", "owner/repo#42", "--json"], "ready", {"scope": "owner/repo#42"}),
        (["terminal", "owner/repo#ISSUE-1"], "terminal", {"target": "owner/repo#ISSUE-1"}),
        (
            ["todo", "Fix it", "--target", "owner/repo"],
            "todo",
            {"text": "Fix it", "target": "owner/repo"},
        ),
        (
            [
                "todo",
                "Fix it",
                "--target",
                "owner/repo",
                "--body",
                "  Details\n\nMore context\n",
                "--json",
            ],
            "todo",
            {"text": "Fix it", "target": "owner/repo", "body": "  Details\n\nMore context\n"},
        ),
        (["close", "terminal_23"], "pane/close", {"pane_id": "terminal_23"}),
        (["info", "--all"], "info", {"include_all_panes": True}),
        (
            ["analyze", "owner/repo#ISSUE-1", "--agent", "codex"],
            "analyze",
            {"target": "owner/repo#ISSUE-1", "agent": "codex"},
        ),
        (
            ["code", "owner/repo#ISSUE-1", "--agent", "pi"],
            "code",
            {"target": "owner/repo#ISSUE-1", "agent": "pi", "autostart": True},
        ),
        (["read", "terminal_23"], "pane/content", {"pane_id": "terminal_23", "full": False}),
        (
            ["read", "terminal_23", "--full"],
            "pane/content",
            {"pane_id": "terminal_23", "full": True},
        ),
        (
            ["write", "terminal_23", "continue"],
            "pane/send",
            {"pane_id": "terminal_23", "data": "continue", "raw": False},
        ),
        (
            ["write", "terminal_23", "continue", "--raw"],
            "pane/send",
            {"pane_id": "terminal_23", "data": "continue", "raw": True},
        ),
    ],
)
@pytest.mark.parametrize(
    ("host", "environment_host", "host_position"),
    [
        ("127.0.0.1", None, None),
        ("192.0.2.10", None, "after"),
        ("dashboard.lan", None, "after"),
        ("127.0.0.1:19876", "127.0.0.1:19876", None),
        ("dashboard.lan:1234", "127.0.0.1:19876", "before"),
        ("dashboard.lan:1234", "127.0.0.1:19876", "after"),
    ],
)
def test_main_server_backed_commands_are_pure_http_clients(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
    host: str,
    environment_host: str | None,
    host_position: str | None,
    argv: list[str],
    expected_endpoint: str,
    expected_payload: dict[str, object],
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []
    analysis_path = tmp_path / "missing-analysis.md"
    ready_item = {
        "id": "owner/repo#PR-51",
        "type": "pr",
        "title": "Fix the parser",
        "url": "https://github.com/owner/repo/pull/51",
        "action": "address_review",
        "reasons": ["address_review", "fix_ci"],
        "issue_id": "owner/repo#ISSUE-42",
    }

    class FakeControlClient:
        def __init__(self, selected_host="127.0.0.1") -> None:
            assert selected_host == host

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            requests.append((endpoint, dict(payload or {})))
            if endpoint == "list":
                return {"items": []}
            if endpoint == "info":
                return {"session": "workdash", "panes": []}
            if endpoint == "ready":
                return {"scope": payload["scope"], "items": [ready_item]}
            if endpoint == "analyze":
                return {
                    "item_id": "owner/repo#ISSUE-1",
                    "path": str(analysis_path),
                    "agent": "codex",
                    "cache_used": False,
                    "status": "generated",
                    "content_type": "text/markdown",
                    "file_name": "analysis.md",
                    "file_content": base64.b64encode(b"analysis body\n").decode("ascii"),
                }
            if endpoint == "code":
                return {
                    "status": "launched",
                    "item_id": "owner/repo#ISSUE-1",
                    "session": "workdash",
                    "agent": "pi",
                    "cwd": "/tmp/wt",
                    "pane_title": "code_owner_repo_1",
                    "pane_id": "terminal_1",
                }
            if endpoint == "terminal":
                return {
                    "item_id": "owner/repo#ISSUE-1",
                    "session": "workdash",
                    "cwd": "/tmp/wt",
                    "pane_title": "terminal_owner_repo_1",
                    "pane_id": "terminal_1",
                }
            if endpoint == "todo":
                return {
                    "item_id": "owner/repo#ISSUE-WT1",
                    "todo_repository": "me/todos",
                    "target": payload["target"],
                    "url": "https://example.com/1",
                }
            if endpoint == "pane/close":
                return {"pane_id": payload["pane_id"], "accepted": True}
            if endpoint == "pane/content":
                return {"pane_id": "terminal_23", "content": "ready\n", "full": payload["full"]}
            if endpoint == "pane/send":
                return {"pane_id": "terminal_23", "raw": payload["raw"], "accepted": True}
            raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    monkeypatch.setattr(
        workdash_module,
        "_check_gh_preflight",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected GitHub preflight")),
    )
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected config load")),
    )
    monkeypatch.setattr(
        workdash_module,
        "WorkdashBackend",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unexpected backend construction")
        ),
    )
    monkeypatch.setattr(
        workdash_module,
        "_select_workdash_session",
        lambda _session: (_ for _ in ()).throw(AssertionError("unexpected Zellij inspection")),
    )
    monkeypatch.setattr(
        workdash_module,
        "exec_zellij_wrapped_workdash",
        lambda _argv: (_ for _ in ()).throw(AssertionError("unexpected Zellij wrapper")),
    )

    if environment_host is None:
        monkeypatch.delenv("WORKDASH_HOST", raising=False)
    else:
        monkeypatch.setenv("WORKDASH_HOST", environment_host)
    host_args = ["--host", host] if host_position else []
    command = host_args + argv if host_position == "before" else argv + host_args
    assert workdash_module.main(command) == 0

    output = capsys.readouterr().out
    if expected_endpoint == "analyze":
        local_path = next(
            line.removeprefix("Analysis path: ")
            for line in output.splitlines()
            if line.startswith("Analysis path: ")
        )
        os.unlink(local_path)
    assert requests == [(expected_endpoint, expected_payload)]
    if expected_endpoint == "ready" and "--json" in argv:
        assert json.loads(output)["items"] == [ready_item]
    elif expected_endpoint == "ready":
        assert output == (
            "PR    owner/repo#PR-51             address_review       Fix the parser "
            "https://github.com/owner/repo/pull/51 [address_review,fix_ci] "
            "(issue owner/repo#ISSUE-42)\n"
        )
    if expected_endpoint == "todo" and "--json" in argv:
        assert json.loads(output) == {
            "item_id": "owner/repo#ISSUE-WT1",
            "todo_repository": "me/todos",
            "target": "owner/repo",
            "url": "https://example.com/1",
        }


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["info"],
        ["ready", "any"],
        ["analyze", "owner/repo#ISSUE-1"],
        ["code", "owner/repo#ISSUE-1"],
        ["read", "terminal_23"],
        ["write", "terminal_23", "continue"],
    ],
)
def test_main_server_backed_command_reports_unreachable_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            raise workdash_module.WorkdashControlError(
                "server_unreachable",
                "No Workdash server is reachable at 127.0.0.1:8765. "
                "Start one with `workdash --server`.",
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "workdash --server" in captured.err


def test_main_analyze_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            assert payload == {"target": "owner/repo#ISSUE-1", "agent": None}
            return {
                "item_id": "owner/repo#ISSUE-1",
                "path": "/server/cache/analysis.md",
                "agent": "codex",
                "cache_used": False,
                "status": "generated",
                "content_type": "text/markdown",
                "file_name": "analysis.md",
                "file_content": base64.b64encode(b"analysis body\n").decode("ascii"),
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1"]) == 0

    output = capsys.readouterr().out
    analysis_path = next(
        line.removeprefix("Analysis path: ")
        for line in output.splitlines()
        if line.startswith("Analysis path: ")
    )
    try:
        assert "Item: owner/repo#ISSUE-1" in output
        assert "Agent: codex" in output
        assert "Status: generated" in output
        assert open(analysis_path, encoding="utf-8").read() == "analysis body\n"
    finally:
        os.unlink(analysis_path)


def test_main_analyze_json_outputs_local_analysis_path_without_server_content_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            assert payload == {"target": "https://github.com/owner/repo/issues/1", "agent": None}
            return {
                "item_id": "owner/repo#ISSUE-1",
                "path": "/server/cache/cached.md",
                "agent": "codex",
                "cache_used": True,
                "status": "cached",
                "content_type": "text/markdown",
                "file_name": "cached.md",
                "file_content": base64.b64encode(b"cached body\n").decode("ascii"),
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert (
        workdash_module.main(["--json", "analyze", "https://github.com/owner/repo/issues/1"]) == 0
    )

    payload = json.loads(capsys.readouterr().out)
    analysis_path = payload.pop("analysis_path")
    try:
        assert payload == {
            "item_id": "owner/repo#ISSUE-1",
            "agent": "codex",
            "cache_used": True,
            "status": "cached",
        }
        assert open(analysis_path, encoding="utf-8").read() == "cached body\n"
    finally:
        os.unlink(analysis_path)


def test_main_analyze_json_writes_unique_secure_temp_file_from_server_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            return {
                "item_id": "owner/repo#ISSUE-1",
                "path": "/server/cache/analysis.md",
                "agent": "codex",
                "cache_used": False,
                "status": "generated",
                "content_type": "text/markdown",
                "file_name": "owner_repo_ISSUE1.md",
                "file_content": base64.b64encode(b"analysis body\n").decode("ascii"),
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    analysis_paths = []
    try:
        assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--json"]) == 0
        first_payload = json.loads(capsys.readouterr().out)
        analysis_paths.append(first_payload["analysis_path"])
        assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--json"]) == 0
        second_payload = json.loads(capsys.readouterr().out)
        analysis_paths.append(second_payload["analysis_path"])

        assert "copy_path" not in first_payload
        assert "file_content" not in first_payload
        assert "file_name" not in first_payload
        assert "content_type" not in first_payload
        assert analysis_paths[0] != analysis_paths[1]
        for analysis_path in analysis_paths:
            assert analysis_path.startswith(
                os.path.join(tempfile.gettempdir(), "workdash-owner-repo-ISSUE1-")
            )
            assert analysis_path.endswith(".md")
            assert os.stat(analysis_path).st_mode & 0o077 == 0
            assert open(analysis_path, encoding="utf-8").read() == "analysis body\n"
    finally:
        for analysis_path in analysis_paths:
            try:
                os.unlink(analysis_path)
            except FileNotFoundError:
                pass


@pytest.mark.parametrize(
    ("response_fields", "expected_error"),
    [
        ({}, "missing base64 file_content"),
        ({"file_content": 123}, "missing base64 file_content"),
        ({"file_content": "not base64"}, "invalid analysis content"),
        ({"file_content": "☃"}, "invalid analysis content"),
    ],
)
def test_main_analyze_reports_invalid_content_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response_fields: dict[str, object],
    expected_error: str,
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            return {
                "item_id": "owner/repo#ISSUE-1",
                "path": "/server/cache/analysis.md",
                "agent": "codex",
                "cache_used": False,
                "status": "generated",
                "content_type": "text/markdown",
                "file_name": "analysis.md",
                **response_fields,
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert expected_error in captured.err
    assert "Traceback" not in captured.err


def test_main_analyze_reports_server_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            raise workdash_module.WorkdashControlError(
                "unknown_agent", "Agent 'vscode' is not configured."
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--agent", "vscode"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Agent 'vscode' is not configured" in captured.err


def test_main_code_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "code"
            assert payload == {"target": "owner/repo#ISSUE-1", "agent": "pi", "autostart": True}
            return {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "session": "workdash-main",
                "agent": "pi",
                "cwd": "/tmp/wt",
                "pane_title": "code_wt",
                "pane_id": "terminal_7",
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["code", "owner/repo#ISSUE-1", "--agent", "pi"]) == 0

    output = capsys.readouterr().out
    assert "Status: launched" in output
    assert "Item: owner/repo#ISSUE-1" in output
    assert "Agent: pi" in output
    assert "Session: workdash-main" in output
    assert "Cwd: /tmp/wt" in output
    assert "Pane title: code_wt" in output
    assert "Pane id: terminal_7" in output


def test_main_code_json_outputs_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": "codex",
        "cwd": "/tmp/owner_repo_1",
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_23",
    }

    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "code"
            assert payload == {
                "target": "https://github.com/owner/repo/issues/1",
                "agent": "codex",
                "autostart": True,
            }
            return response

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert (
        workdash_module.main(
            ["code", "https://github.com/owner/repo/issues/1", "--agent", "codex", "--json"]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out) == response


def test_main_code_reports_server_error_before_local_worktree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            raise workdash_module.WorkdashControlError(
                "unknown_agent",
                "Agent 'vscode' is not configured.",
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda _workdir, _item: (_ for _ in ()).throw(AssertionError("unexpected worktree")),
    )

    assert workdash_module.main(["code", "owner/repo#ISSUE-1", "--agent", "vscode"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Agent 'vscode' is not configured." in captured.err
    assert "vscode" in captured.err


def test_main_read_human_output_prints_pane_content(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "pane/content"
            assert payload == {"pane_id": "terminal_23", "full": False}
            return {"pane_id": "terminal_23", "content": "agent output\nnext", "full": False}

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["read", "terminal_23"]) == 0

    assert capsys.readouterr().out == "agent output\nnext\n"


def test_main_read_json_outputs_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {"pane_id": "terminal_23", "content": "history", "full": True}

    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "pane/content"
            assert payload == {"pane_id": "terminal_23", "full": True}
            return response

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["read", "terminal_23", "--full", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == response


def test_main_write_human_output_confirms_accepted_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "pane/send"
            assert payload == {"pane_id": "terminal_23", "data": "continue", "raw": False}
            return {"pane_id": "terminal_23", "raw": False, "accepted": True}

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["write", "terminal_23", "continue"]) == 0

    assert capsys.readouterr().out == "Accepted input for terminal_23 (raw: no).\n"


def test_main_write_json_outputs_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {"pane_id": "terminal_23", "raw": True, "accepted": True}

    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "pane/send"
            assert payload == {"pane_id": "terminal_23", "data": "continue", "raw": True}
            return response

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["write", "terminal_23", "continue", "--no-enter", "--json"]) == 0

    assert json.loads(capsys.readouterr().out) == response


def test_main_info_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "info"
            assert payload == {"include_all_panes": False}
            return {
                "session": "workdash",
                "panes": [
                    {
                        "kind": "agent",
                        "pane_id": "terminal_1",
                        "title": "code_owner_repo_1",
                        "cwd": "/tmp/wt",
                        "command": "pi",
                        "tab_name": "work",
                        "state": "running",
                        "item": "owner/repo#ISSUE-1",
                    }
                ],
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["info"]) == 0

    output = capsys.readouterr().out
    assert "Session: workdash" in output
    assert "item=owner/repo#ISSUE-1" in output
    assert "code_owner_repo_1" in output


def test_main_info_json_outputs_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {"session": "workdash", "panes": []}

    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "info"
            assert payload == {"include_all_panes": False}
            return response

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["--json", "info"]) == 0

    assert json.loads(capsys.readouterr().out) == response


def test_main_configure_runs_setup_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_called = False

    def fake_configure():
        nonlocal configure_called
        configure_called = True
        return _VALID_CONFIG

    monkeypatch.setattr(workdash_module, "configure", fake_configure)
    monkeypatch.setattr(
        workdash_module,
        "_check_gh_preflight",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected gh preflight")),
    )

    exit_code = workdash_module.main(["--configure"])

    assert exit_code == 0
    assert configure_called


@pytest.mark.parametrize("argv", [["--help"], ["--version"]])
def test_main_help_and_version_exit_without_gh_preflight(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(
        workdash_module,
        "_check_gh_preflight",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected gh preflight")),
    )

    with pytest.raises(SystemExit) as error:
        workdash_module.main(argv)

    assert error.value.code == 0


def test_main_outside_zellij_replaces_process_with_zellij(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper_calls: list[object] = []

    def fake_wrapper(argv):
        wrapper_calls.append(argv)
        raise SystemExit(0)

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "exec_zellij_wrapped_workdash", fake_wrapper)

    with pytest.raises(SystemExit):
        workdash_module.main(["--refresh"])
    assert wrapper_calls == [["--refresh"]]


def test_main_direct_mode_bypasses_zellij_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(
        workdash_module.os,
        "execvp",
        lambda _file, _args: (_ for _ in ()).throw(AssertionError("unexpected exec")),
    )

    exit_code = workdash_module.main(["--direct"])

    assert exit_code == 1
    assert "gh CLI is not installed" in capsys.readouterr().err


def test_main_exits_with_error_when_gh_is_not_authenticated(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fake_run(*args, **kwargs):
        raise workdash_module.subprocess.CalledProcessError(1, ["gh", "auth", "status"])

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setenv("ZELLIJ", "0")

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "gh CLI is not authenticated" in captured.err
    assert (
        "Run this command to authenticate the GitHub CLI used by workdash:\n"
        "  /usr/bin/gh auth login\n"
    ) in captured.err


def test_main_outside_zellij_checks_gh_before_replacing_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    wrapper_calls: list[object] = []

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(workdash_module, "exec_zellij_wrapped_workdash", wrapper_calls.append)

    exit_code = workdash_module.main([])

    assert exit_code == 1
    assert wrapper_calls == []
    assert "gh CLI is not installed" in capsys.readouterr().err


def test_main_outside_zellij_reports_missing_zellij(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr("workdash.launcher.shutil.which", lambda cmd: None)

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "zellij is not installed or not configured" in captured.err
    assert "--configure" in captured.err


@pytest.mark.parametrize(
    "tool, expected_tokens",
    [
        ("claude", ["claude"]),
        ("codex", ["codex"]),
        ("pi", ["pi", "--no-tips"]),
    ],
)
def test_main_launch_callback_dispatches_agent_command_tokens_per_tool(
    monkeypatch: pytest.MonkeyPatch, tool: str, expected_tokens: list[str]
) -> None:
    captured_callback: dict[str, object] = {}
    agent_calls: list[tuple[str, str, list[str] | None, str | None]] = []
    vscode_calls: list[tuple[str, str]] = []
    item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="t",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url="https://example.com/1",
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.analysis_cache = type(
                "_C", (), {"build_analysis_path": staticmethod(lambda _i: "/x")}
            )()

        def load_items(self, progress_callback=None):
            return [item], {}

        def analyze_item(self, _item, tool="codex"):
            return None

        def include_item_by_url(self, _url, _existing):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_callback["launch"] = kwargs["launch_callback"]

        def run(self, *, mouse: bool) -> None:
            return None

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda _wd, _item: "/tmp/wt")
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(
        control_module,
        "prepare_launch_agent_prompt",
        lambda *args, **kwargs: "PROMPT",
    )

    panes: list[dict[str, object]] = []

    def fake_launch_agent_context(repo, prompt, agent_command_tokens=None, *, zellij_session=None):
        agent_calls.append((repo, prompt, agent_command_tokens, zellij_session))
        panes.append({"id": 4, "title": "code_wt", "pane_cwd": repo})
        return SimpleNamespace(
            session=zellij_session,
            pane_id=None,
            pane_title="code_wt",
            cwd=repo,
        )

    monkeypatch.setattr(control_module, "launch_agent_context", fake_launch_agent_context)
    monkeypatch.setattr(control_module, "load_zellij_panes", lambda _session: list(panes))
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda _wd, _item: None)
    monkeypatch.setattr(
        control_module,
        "launch_vscode_context",
        lambda repo, prompt: vscode_calls.append((repo, prompt)),
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")

    code_calls: list[dict[str, object]] = []
    real_code = control_module.WorkdashSession.code

    def spy_code(self, **kwargs):
        code_calls.append(kwargs)
        return real_code(self, **kwargs)

    monkeypatch.setattr(control_module.WorkdashSession, "code", spy_code)

    assert workdash_module.main([]) == 0

    result = captured_callback["launch"](item, tool)

    # The TUI stays plan-first: its launch never asks for autostart, so the briefing is unchanged.
    assert [call["autostart"] for call in code_calls] == [False]
    assert agent_calls == [("/tmp/wt", "PROMPT", expected_tokens, "workdash-main")]
    assert (result["status"], result["pane_id"]) == ("launched", "terminal_4")
    assert vscode_calls == []


def test_main_launch_callback_raises_on_unknown_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_callback: dict[str, object] = {}
    item = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="t",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url="https://example.com/1",
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.analysis_cache = type(
                "_C", (), {"build_analysis_path": staticmethod(lambda _i: "/x")}
            )()

        def load_items(self, progress_callback=None):
            return [item], {}

        def analyze_item(self, _item, tool="codex"):
            return None

        def include_item_by_url(self, _url, _existing):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_callback["launch"] = kwargs["launch_callback"]

        def run(self, *, mouse: bool) -> None:
            return None

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda _wd, _item: "/tmp/wt")
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

    with pytest.raises(
        control_module.WorkdashControlError, match="Agent 'bogus' is not configured"
    ):
        captured_callback["launch"](item, "bogus")


def test_print_work_items_prefixes_suggested_title_with_marker(
    capsys: pytest.CaptureFixture[str],
) -> None:
    created_at = datetime(2026, 2, 1, tzinfo=UTC)
    suggested = WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=7,
        title="Suggested issue",
        created_at=created_at,
        updated_at=created_at,
        url="https://example.com/issues/7",
    )
    other = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=8,
        title="Other item",
        created_at=created_at,
        updated_at=created_at,
        url="https://example.com/pull/8",
    )

    _print_work_items(
        [suggested, other],
        {(WorkItemType.ISSUE, "owner/repo", 7): "*"},
    )

    output_lines = capsys.readouterr().out.strip().splitlines()
    assert output_lines[0].endswith("2026-02-01 * Suggested issue")
    assert output_lines[1].endswith("2026-02-01 Other item")


def test_print_work_items_uses_review_label_for_review_requested_pr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    created_at = datetime(2026, 2, 1, tzinfo=UTC)
    review_item = WorkItem(
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=9,
        title="Review this",
        created_at=created_at,
        updated_at=created_at,
        url="https://example.com/pull/9",
    )

    _print_work_items([review_item], {})

    output_line = capsys.readouterr().out.strip()
    assert output_line.startswith("REVIEW")


@pytest.mark.parametrize(
    "argv",
    [
        ["--listen", "127.0.0.2@192.0.2.1"],
        ["--server", "--listen"],
        *[
            ["--server", "--listen", value]
            for value in (
                "",
                "127.0.0.2",
                "@192.0.2.1",
                "127.0.0.2@",
                "127.0.0.2@,192.0.2.1",
                "127.0.0.2@192.0.2.1,",
                "127.0.0.2@192.0.2.1,,192.0.2.2",
                "127.0.0.2@client.lan",
                "127.0.0.2@192.0.2.0/24",
                "127.0.0.2@999.0.0.1",
                "127.0.0.2@192.0.2.1@192.0.2.2",
                "0.0.0.0@192.0.2.1",
                "*@192.0.2.1",
                "::@192.0.2.1",
                "dashboard.lan:9000@192.0.2.1",
            )
        ],
    ],
)
def test_main_rejects_invalid_listeners_before_loading_local_state(
    monkeypatch, capsys, argv
) -> None:
    def unexpected_preflight():
        raise AssertionError("invalid CLI reached GitHub preflight")

    monkeypatch.setattr(workdash_module, "_check_gh_preflight", unexpected_preflight)
    with pytest.raises(SystemExit) as error:
        workdash_module.main(argv)
    assert error.value.code == 2
    assert "--listen" in capsys.readouterr().err


def test_server_listener_flags_reach_the_zellij_dashboard(monkeypatch, tmp_path) -> None:
    import workdash.launcher as launcher_module

    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(launcher_module, "_resolve_zellij_binary", lambda: "/usr/bin/zellij")
    monkeypatch.delenv("ZELLIJ", raising=False)
    captured = {}

    def capture_layout(command, *, session_name):
        captured["command"] = command
        return str(tmp_path / "layout.kdl")

    def execvp(_file, _args):
        raise SystemExit(0)

    monkeypatch.setattr(launcher_module, "_write_zellij_startup_layout", capture_layout)
    monkeypatch.setattr(launcher_module.os, "execvp", execvp)
    argv = ["--server", "--listen", "dashboard.lan@192.0.2.1", "--listen", "192.0.2.5@192.0.2.2"]
    with pytest.raises(SystemExit) as error:
        workdash_module.main(argv)
    assert error.value.code == 0
    assert captured["command"][-len(argv) :] == argv
    assert "--direct" in captured["command"]


def test_environment_address_and_explicit_host_precedence(monkeypatch, capsys):
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:19876")
    assert workdash_module._parse_args(["list"]).host == "127.0.0.1:19876"
    assert workdash_module._parse_args(["--host", "example.org", "list"]).host == "example.org"
    assert (
        workdash_module._parse_args(["list", "--host", "example.org:1234"]).host
        == "example.org:1234"
    )
    monkeypatch.setenv("WORKDASH_HOST", "bad:port")
    assert workdash_module._parse_args(["list", "--host", "example.org"]).host == "example.org"
    with pytest.raises(SystemExit):
        workdash_module._parse_args(["list"])
    assert "WORKDASH_HOST" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["info"],
        ["ready", "owner/repo"],
        ["analyze", "item"],
        ["code", "item"],
        ["terminal", "item"],
        ["todo", "text"],
        ["read", "pane"],
        ["write", "pane", "text"],
        ["close", "pane"],
    ],
)
def test_all_clients_accept_host_on_either_side(monkeypatch, args):
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:19876")
    assert workdash_module._parse_args(args).host == "127.0.0.1:19876"
    monkeypatch.setenv("WORKDASH_HOST", "bad:port")
    assert workdash_module._parse_args(["--host", "example.org", *args]).host == "example.org"
    assert (
        workdash_module._parse_args([*args, "--host", "example.org:1234"]).host
        == "example.org:1234"
    )


def test_address_validation_and_host_only_default(monkeypatch, capsys):
    monkeypatch.setenv("WORKDASH_HOST", "dashboard.lan")
    with pytest.raises(ValueError, match="WORKDASH_HOST"):
        control_module.server_address()
    assert workdash_module._parse_args(["list", "--host", "example.org"]).host == "example.org"
    monkeypatch.setenv("WORKDASH_HOST", "localhost:invalid")
    with pytest.raises(SystemExit):
        workdash_module.main(["list"])
    error = capsys.readouterr().err
    assert "WORKDASH_HOST" in error and "localhost:invalid" in error
    assert control_module.server_address("dashboard.lan") == ("dashboard.lan", 8765)


@pytest.mark.parametrize("value", ["", ":1234", "localhost:", "localhost:0", "localhost:65536"])
@pytest.mark.parametrize("explicit", [False, True])
def test_cli_rejects_invalid_control_addresses(monkeypatch, capsys, value, explicit):
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:9876" if explicit else value)
    args = ["list", "--host", value] if explicit else ["list"]
    with pytest.raises(SystemExit):
        workdash_module.main(args)
    error = capsys.readouterr().err
    assert ("--host" if explicit else "WORKDASH_HOST") in error
    assert repr(value) in error


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--server"],
        ["show-config"],
        ["--configure"],
        ["branchdiff"],
        ["branchinfo"],
        ["--server", "list"],
        ["--configure", "list"],
    ],
)
def test_host_is_rejected_for_nonclient_commands(monkeypatch, capsys, args):
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:9999")
    monkeypatch.setattr(
        workdash_module, "WorkdashCommands", lambda **kwargs: pytest.fail("command was executed")
    )
    monkeypatch.setattr(workdash_module, "configure", lambda: pytest.fail("setup was executed"))
    with pytest.raises(SystemExit) as error:
        workdash_module.main(["--host", "127.0.0.1:9001", *args])
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert "--host" in message and "client" in message


def test_server_inside_zellij_requires_current_session_name(monkeypatch, capsys):
    from unittest.mock import MagicMock

    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", lambda **kwargs: backend)
    monkeypatch.setattr(
        workdash_module,
        "list_workdash_sessions",
        lambda: pytest.fail("must not select another session"),
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    assert workdash_module.main(["--direct", "--server"]) == 1
    assert "Cannot determine the current Zellij session" in capsys.readouterr().err


def test_server_outside_zellij_selects_single_existing_workdash_session(monkeypatch):
    from unittest.mock import MagicMock

    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    sessions = []
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", lambda **kwargs: backend)
    monkeypatch.setattr(workdash_module, "list_workdash_sessions", lambda: ["workdash-existing"])
    monkeypatch.setattr(
        workdash_module,
        "WorkdashApp",
        lambda **kwargs: sessions.append(kwargs["session"].zellij_session) or MagicMock(),
    )
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", MagicMock())
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    assert workdash_module.main(["--direct", "--server"]) == 0
    assert sessions == ["workdash-existing"]


def test_version_writes_to_stdout_without_preflight(monkeypatch, capsys):
    monkeypatch.setenv("WORKDASH_HOST", "invalid:port")
    monkeypatch.setattr(
        workdash_module, "_check_gh_preflight", lambda: pytest.fail("unexpected preflight")
    )
    with pytest.raises(SystemExit) as error:
        workdash_module.main(["--version"])
    assert error.value.code == 0
    captured = capsys.readouterr()
    assert captured.out == f"workdash {workdash_module.__version__}\n"
    assert captured.err == ""


def test_workdash_control_skill_examples_are_valid_commands_and_explain_code_claims() -> None:
    skill = (Path(__file__).parents[1] / "skills/workdash-control/SKILL.md").read_text()
    examples = [
        shlex.split(line, comments=True)[1:]
        for block in re.findall(r"```bash\n(.*?)```", skill, re.DOTALL)
        for line in block.splitlines()
        if line.startswith("workdash ")
    ]

    assert {argv[0] for argv in examples} >= {
        "code",
        "list",
        "info",
        "ready",
        "read",
        "write",
        "close",
    }
    for argv in examples:
        workdash_module._parse_args(argv)
    # The live code pane is the claim, so agents must learn to reuse it.
    assert "already_running" in skill
    assert "no `workdash claim` command" in skill
    with pytest.raises(SystemExit):
        workdash_module._parse_args(["claim", "owner/repo#1"])
    # Agents must learn what CLI code does, how the TUI differs, and that nothing is merged.
    for text in (
        "There is no `--autostart` option: the CLI always asks for",
        "plan-first briefing",
        "resumes that pull request in place",
        "merge conflicts, or failing CI",
        "`no_checks`",
        "never as CI passed",
        "Nothing is injected into it",
        "never merges a pull request or closes an issue",
    ):
        assert text in " ".join(skill.split())
    assert "--autoclose" not in skill
    with pytest.raises(SystemExit):
        workdash_module._parse_args(["code", "owner/repo#1", "--autostart"])
    # Agents must learn that ready is advice, ordered pull requests first, and needs the server.
    for text in (
        "read-only advice",
        "sub-issue",
        "Pull requests needing work come first",
        "No ready work found.",
        "never start a server just to run `ready`",
    ):
        assert text in skill
