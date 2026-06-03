import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import workdash.control as control_module
import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.workdash import _print_work_items, _print_work_items_result

_VALID_CONFIG = WorkdashConfig(
    github_username="testuser",
    claude=AgentConfig(analyze="claude -p", launch="claude"),
    codex=AgentConfig(analyze="codex exec", launch="codex"),
    pi=AgentConfig(launch="pi --no-tips"),
    repositories=("owner/*",),
    workdir="~/wrk",
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
    calls: dict[str, bool] = {"load": False, "run": False}

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

        def run(self) -> None:
            calls["run"] = True

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setenv("ZELLIJ", "0")

    exit_code = workdash_module.main([])

    assert exit_code == 0
    assert calls == {"load": True, "run": True}
    captured = capsys.readouterr()
    assert captured.out.startswith("Loading work items from GitHub...\n")


def test_main_server_refresh_before_tui_run_updates_session_and_repaints_when_running(
    monkeypatch: pytest.MonkeyPatch,
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
        def __init__(self, session) -> None:
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

        def run(self) -> None:
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

    assert workdash_module.main(["--server"]) == 0

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
        def __init__(self, session) -> None:
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

        def run(self) -> None:
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

    with pytest.raises(RuntimeError, match="refresh exploded"):
        workdash_module.main(["--server"])

    assert captured["server_started"] is True
    assert captured["server_stopped"] is True


def test_main_passes_configured_agent_choices_to_tui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_kwargs: dict[str, object] = {}
    config = WorkdashConfig(
        github_username="testuser",
        codex=AgentConfig(analyze="codex exec"),
        repositories=("owner/*",),
        workdir="~/wrk",
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

        def run(self) -> None:
            return None

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

    assert captured_kwargs["analyze_choices"] == config.tui_analyze_choices()
    assert captured_kwargs["code_choices"] == config.tui_code_choices()


def test_main_tui_analyze_callback_uses_worktree_and_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _issue()
    ensure_calls: list[tuple[str | None, WorkItem]] = []
    analyze_calls: list[tuple[WorkItem, str]] = []
    captured_callback: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is not None
            return [item], {}

        def analyze_item(self, item, tool="codex"):
            analyze_calls.append((item, tool))
            return "/tmp/analysis.md"

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_callback["analyze"] = kwargs["analyze_callback"]

        def run(self) -> None:
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
    assert analyze_calls == [(item, "codex")]
    assert result == "/tmp/analysis.md"


def test_main_list_command_does_not_print_loading_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeControlClient:
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


@pytest.mark.parametrize("argv", [[], ["--server"]])
def test_main_exits_with_config_guidance_when_startup_config_command_is_malformed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi 'broken"),
        repositories=("owner/repo",),
        workdir="~/wrk",
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
        (["info", "--all"], "info", {"include_all_panes": True}),
        (
            ["analyze", "owner/repo#ISSUE-1", "--agent", "codex"],
            "analyze",
            {"target": "owner/repo#ISSUE-1", "agent": "codex"},
        ),
        (
            ["code", "owner/repo#ISSUE-1", "--agent", "pi"],
            "code",
            {"target": "owner/repo#ISSUE-1", "agent": "pi"},
        ),
    ],
)
def test_main_server_backed_commands_are_pure_http_clients(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    expected_endpoint: str,
    expected_payload: dict[str, object],
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            requests.append((endpoint, dict(payload or {})))
            if endpoint == "list":
                return {"items": []}
            if endpoint == "info":
                return {"session": "workdash", "panes": []}
            if endpoint == "analyze":
                return {
                    "item_id": "owner/repo#ISSUE-1",
                    "path": "/tmp/analysis.md",
                    "agent": "codex",
                    "cache_used": False,
                    "status": "generated",
                }
            if endpoint == "code":
                return {
                    "item_id": "owner/repo#ISSUE-1",
                    "session": "workdash",
                    "agent": "pi",
                    "cwd": "/tmp/wt",
                    "pane_title": "code_owner_repo_1",
                    "pane_id": "terminal_1",
                }
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

    assert workdash_module.main(argv) == 0

    assert requests == [(expected_endpoint, expected_payload)]


def test_main_server_backed_command_reports_unreachable_server(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            raise workdash_module.WorkdashControlError(
                "server_unreachable",
                "No Workdash server is reachable at 127.0.0.1:8765. "
                "Start one with `workdash --server`.",
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["info"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "workdash --server" in captured.err


def test_main_analyze_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            assert payload == {"target": "owner/repo#ISSUE-1", "agent": None}
            return {
                "item_id": "owner/repo#ISSUE-1",
                "path": "/tmp/analysis.md",
                "agent": "codex",
                "cache_used": False,
                "status": "generated",
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1"]) == 0

    output = capsys.readouterr().out
    assert "Item: owner/repo#ISSUE-1" in output
    assert "Agent: codex" in output
    assert "Status: generated" in output
    assert "Path: /tmp/analysis.md" in output


def test_main_analyze_json_outputs_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = {
        "item_id": "owner/repo#ISSUE-1",
        "path": "/tmp/cached.md",
        "agent": "codex",
        "cache_used": True,
        "status": "cached",
    }

    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            assert payload == {"target": "https://github.com/owner/repo/issues/1", "agent": None}
            return response

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert (
        workdash_module.main(["--json", "analyze", "https://github.com/owner/repo/issues/1"]) == 0
    )

    assert json.loads(capsys.readouterr().out) == response


def test_main_analyze_reports_server_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            raise workdash_module.WorkdashControlError(
                "unknown_agent", "Analysis agent 'vscode' is not configured."
            )

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--agent", "vscode"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Analysis agent 'vscode' is not configured" in captured.err


def test_main_code_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "code"
            assert payload == {"target": "owner/repo#ISSUE-1", "agent": "pi"}
            return {
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
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "code"
            assert payload == {"target": "https://github.com/owner/repo/issues/1", "agent": "codex"}
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
        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            raise workdash_module.WorkdashControlError(
                "unknown_agent",
                "Coding agent 'vscode' is not a configured terminal-backed agent.",
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
    assert "not a configured terminal-backed agent" in captured.err
    assert "vscode" in captured.err


def test_main_info_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeControlClient:
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

        def run(self) -> None:
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

    def fake_launch_agent_context(repo, prompt, agent_command_tokens=None, *, zellij_session=None):
        agent_calls.append((repo, prompt, agent_command_tokens, zellij_session))
        return SimpleNamespace(
            session=zellij_session,
            pane_id=None,
            pane_title="code_wt",
            cwd=repo,
        )

    monkeypatch.setattr(control_module, "launch_agent_context", fake_launch_agent_context)
    monkeypatch.setattr(
        control_module,
        "launch_vscode_context",
        lambda repo, prompt: vscode_calls.append((repo, prompt)),
    )
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

    captured_callback["launch"](item, tool)

    assert agent_calls == [("/tmp/wt", "PROMPT", expected_tokens, None)]
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

        def run(self) -> None:
            return None

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda _wd, _item: "/tmp/wt")
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

    with pytest.raises(control_module.WorkdashControlError, match="Coding agent 'bogus'"):
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
