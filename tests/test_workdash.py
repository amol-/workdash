import asyncio
import base64
import errno
import io
import json
import os
import re
import shlex
import socket
import subprocess
import threading
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest
from textual.widgets import DataTable, Static

import workdash.backend as backend_module
import workdash.config as config_module
import workdash.control as control_module
import workdash.github_client as github_client_module
import workdash.workdash as workdash_module
from view_fake import ISSUE_URL, fake_gh_view_run
from workdash.backend import compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig, load_config, save_config
from workdash.control import WorkdashSession, probe_dashboard_session
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.workdash import _print_work_items, _print_work_items_result
from zellij_pane_launch import mark_pane_started, pane_launch


@pytest.fixture(autouse=True)
def _fake_dashboard_server(monkeypatch):
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", MagicMock())


@pytest.fixture(autouse=True)
def _existing_config_path(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.touch()
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)


_VALID_CONFIG = WorkdashConfig(
    github_username="testuser",
    open_command="xdg-open",
    analysis_viewer={"command": "workdash view", "format": "markdown", "needs_pane": True},
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
    monkeypatch.setattr(github_client_module.subprocess, "run", lambda *args, **kwargs: None)


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

    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)


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


def test_main_loads_initial_table_without_a_zellij_binary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    item = _issue()
    titles = []

    def load_items(_backend, progress_callback=None):
        assert progress_callback is not None
        return [item], {}

    def run_dashboard(app, *, mouse: bool) -> None:
        assert mouse is False
        assert capsys.readouterr().out.startswith("Loading work items from GitHub...\n")

        async def read_initial_table():
            async with app.run_test() as pilot:
                table = app.query_one("#work-items", DataTable)
                titles.extend(str(table.get_row_at(i)[2]) for i in range(table.row_count))
                await pilot.press("q")

        asyncio.run(read_initial_table())

    monkeypatch.setattr(
        github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh" if cmd == "gh" else None
    )
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module.WorkdashBackend, "load_items", load_items)
    monkeypatch.setattr(workdash_module.WorkdashApp, "run", run_dashboard)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    assert workdash_module.main([]) == 0
    assert titles == [item.title]


@pytest.mark.parametrize("argv", [[], ["--session", "another-session"]])
def test_main_bare_interactive_uses_own_zellij_session_from_environment(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    # The dashboard must find active agent panes for itself
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

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-abc123")

    assert workdash_module.main(argv) == 0

    assert captured["session"].zellij_session == "workdash-abc123"


@pytest.mark.parametrize(
    ("argv", "listeners"),
    [
        ([], ()),
        ([], (("dashboard.lan", ("192.0.2.1",)), ("192.0.2.5", ("192.0.2.2", "192.0.2.3")))),
        (["--persistent-pane", "--session", "workdash-main"], (("127.0.0.2", ("127.0.0.4",)),)),
    ],
    ids=["no-extra-listeners", "multiple-listeners", "reopened-dashboard"],
)
def test_main_server_refresh_before_tui_run_updates_session_and_repaints_when_running(
    monkeypatch: pytest.MonkeyPatch,
    argv,
    listeners,
    tmp_path,
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

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    config_path = tmp_path / "config.json"
    save_config(
        replace(_VALID_CONFIG, listens=tuple(f"{host}@{','.join(ips)}" for host, ips in listeners)),
        config_path,
    )
    monkeypatch.setattr(workdash_module, "load_config", lambda: load_config(config_path))
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", FakeControlServer)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(
        workdash_module.launcher.zellij, "list_workdash_sessions", lambda: ["workdash-main"]
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")

    if argv:
        shell = MagicMock(side_effect=SystemExit(0))
        monkeypatch.setenv("SHELL", "/bin/sh")
        monkeypatch.setattr(workdash_module.os, "execvp", shell)
        with pytest.raises(SystemExit) as stopped:
            workdash_module.main(argv)
        assert stopped.value.code == 0
        shell.assert_called_once_with("/bin/sh", ["/bin/sh", "-i"])
    else:
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

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashControlServer", FakeControlServer)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(
        workdash_module.launcher.zellij, "list_workdash_sessions", lambda: ["workdash-main"]
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-main")

    with pytest.raises(RuntimeError, match="refresh exploded"):
        workdash_module.main([])

    assert captured["server_started"] is True
    assert captured["server_stopped"] is True


@pytest.mark.parametrize("needs_pane", [False, True])
def test_main_passes_configured_agent_choices_and_separate_viewers_to_tui(
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
        analysis_viewer={"command": "analysis-viewer", "format": "html", "needs_pane": needs_pane},
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

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)

    def fake_run(command, **kwargs):
        commands.append(command)
        mark_pane_started(command, "17")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    def fake_popen(command, **kwargs):
        commands.append(command)
        return SimpleNamespace(wait=lambda **kw: 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
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
        assert commands[2] == [*prefix, "focus-pane-id", "terminal_17"]
        assert [pane_launch(command) for command in commands[:2]] == [
            (
                [
                    *prefix,
                    "new-pane",
                    "-f",
                    "-x",
                    "0",
                    "-y",
                    "0",
                    "--width",
                    "100%",
                    "--height",
                    "100%",
                    "--name",
                    "term-copy",
                    "--close-on-exit",
                ],
                [*viewer_argv, item.url],
            ),
            (
                [
                    *prefix,
                    "new-pane",
                    "-f",
                    "-x",
                    "0",
                    "-y",
                    "0",
                    "--width",
                    "100%",
                    "--height",
                    "100%",
                    "--name",
                    "analysis-viewer",
                    "--close-on-exit",
                ],
                ["analysis-viewer", html_path.as_uri()],
            ),
        ]
    else:
        assert commands == [[*viewer_argv, item.url], ["analysis-viewer", html_path.as_uri()]]

    # An item document always needs a pane of its own, whatever the open command does.
    commands.clear()
    captured_kwargs["view_callback"](item)
    assert [pane_launch(command) for command in commands] == [
        (
            [
                "/usr/bin/zellij",
                "--session",
                "workdash-selected",
                "action",
                "new-pane",
                "-f",
                "-x",
                "0",
                "-y",
                "0",
                "--width",
                "100%",
                "--height",
                "100%",
                "--name",
                "sh",
                "--close-on-exit",
            ],
            [
                "/bin/sh",
                "-c",
                '"$@" || { printf "\\nPress Enter to close this pane.\\n"; read -r _; }',
                "workdash-view",
                "/usr/bin/workdash",
                "view",
                item.url,
            ],
        )
    ]


@pytest.mark.parametrize(
    ("key", "browser_needs_pane", "failure", "status"),
    [
        pytest.param("v", False, None, "Opened viewer for issue owner/repo#12.", id="document"),
        pytest.param("o", False, None, "Opened issue owner/repo#12.", id="direct-browser"),
        pytest.param("o", True, None, "Opened issue owner/repo#12.", id="terminal-browser"),
        pytest.param("v", False, "zellij exploded", "View failed:", id="document-error"),
        pytest.param("o", False, "cannot open display", "Open failed:", id="browser-error"),
        pytest.param("o", False, "timeout", "Open failed:", id="browser-timeout"),
    ],
)
def test_main_view_menu_reaches_the_launcher(
    key, browser_needs_pane, failure, status, monkeypatch, tmp_path
):
    browser = 'term-copy --label "two words"'
    url = f"{ISSUE_URL}?a=one&b=two"
    needs_pane = key == "v" or browser_needs_pane
    tools = {"gh": "/usr/bin/gh", "workdash": "/usr/bin/workdash"}
    if needs_pane:
        tools["zellij"] = "/usr/bin/zellij"
    monkeypatch.setattr(github_client_module.shutil, "which", tools.get)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-selected")
    monkeypatch.setattr(backend_module, "_DEFAULT_INCLUDED_STORE_PATH", tmp_path / "included.json")
    config = replace(_VALID_CONFIG, open_command=browser, open_needs_pane=browser_needs_pane)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)

    selected_issue = {
        "__typename": "Issue",
        "id": "ISSUE-12",
        "number": 12,
        "title": "Crash on startup",
        "url": url,
        "createdAt": "2026-02-20T00:00:00Z",
        "updatedAt": "2026-02-25T10:00:00Z",
        "state": "OPEN",
        "repository": {"nameWithOwner": "owner/repo"},
        "subIssuesSummary": {"total": 0},
    }
    first_issue = {
        **selected_issue,
        "id": "ISSUE-11",
        "number": 11,
        "title": "Update the docs",
        "url": "https://github.com/owner/repo/issues/11",
        "updatedAt": "2026-02-26T10:00:00Z",
    }
    gh_run = fake_gh_view_run(
        {
            "auth status": {},
            "search prs": [],
            "api graphql -f query={ assigned:": {"data": {"assigned": {"nodes": []}}},
            "api graphql -f query={ tracked:": {
                "data": {"tracked": {"nodes": [first_issue, selected_issue]}}
            },
            "api graphql -f query={ repository(": {
                "data": {"repository": {"issues": {"nodes": []}}}
            },
        }
    )
    launches = []

    def run_subprocess(command, **kwargs):
        if Path(command[0]).name == "gh":
            return gh_run(command, **kwargs)
        launches.append(command)
        if not needs_pane:
            assert kwargs["timeout"] == 4
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if failure:
            raise subprocess.CalledProcessError(1, command, output="", stderr=failure)
        mark_pane_started(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    expected_status = status
    if failure:
        details = failure
        if failure == "timeout":
            details = (
                "command did not finish within 4 seconds. "
                "Opening a browser may not be supported from this session."
            )
        command_name = "workdash view" if key == "v" else browser
        expected_status = f"{status} Failed to open URL via {command_name}: {details}"

    def run_dashboard(app, *, mouse):
        async def drive_menu():
            async with app.run_test() as pilot:
                dashboard = app.screen
                footer = dashboard.query_one("#status-footer", Static)
                await pilot.press("down", "v")
                labels = [widget.render().plain for widget in app.screen.query(Static)]
                assert "(v) view" in labels and "(o) open" in labels
                await pilot.press("escape")
                assert list(app.screen_stack) == [dashboard]
                assert not launches
                await pilot.press("v", key)
                for _ in range(60):
                    if footer.render().plain == expected_status:
                        break
                    await asyncio.sleep(0.05)
                assert footer.render().plain == expected_status
                assert list(app.screen_stack) == [dashboard]

        asyncio.run(drive_menu())

    monkeypatch.setattr(subprocess, "run", run_subprocess)
    monkeypatch.setattr(workdash_module.WorkdashApp, "run", run_dashboard)
    assert workdash_module.main([]) == 0

    (launch,) = launches
    browser_argv = ["term-copy", "--label", "two words", url]
    if needs_pane:
        pane_args, viewer_argv = pane_launch(launch)
        assert pane_args == [
            tools["zellij"],
            "--session",
            "workdash-selected",
            "action",
            "new-pane",
            *"-f -x 0 -y 0 --width 100% --height 100%".split(),
            "--name",
            "sh" if key == "v" else "term-copy",
            "--close-on-exit",
        ]
        if key == "v":
            assert viewer_argv[-3:] == [tools["workdash"], "view", url]
        else:
            assert viewer_argv == browser_argv
    else:
        assert launch == browser_argv


def test_main_tui_analyze_callback_uses_worktree_and_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    item = _issue()
    analysis_path = tmp_path / "analysis.md"
    ensure_calls: list[tuple[str | None, WorkItem]] = []
    analyze_calls: list[tuple[WorkItem, str, str | None, str | None, str | None]] = []
    captured_callback: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.github_client = object()

        def load_items(self, progress_callback=None):
            assert progress_callback is not None
            return [item], {}

        def analyze_item(self, item, tool="codex", *, cwd=None, zellij_session=None, group=None):
            analyze_calls.append((item, tool, cwd, zellij_session, group))
            analysis_path.write_text("analysis body\n", encoding="utf-8")
            return str(analysis_path)

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_callback["analyze"] = kwargs["analyze_callback"]

        def run(self, *, mouse: bool) -> None:
            return None

    def fake_ensure_worktree(workdir, item, **_kwargs):
        ensure_calls.append((workdir, item))
        return "/tmp/worktree"

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", fake_ensure_worktree)
    monkeypatch.setattr(
        control_module,
        "analysis_worktree",
        lambda *args, **kwargs: nullcontext(fake_ensure_worktree(*args, **kwargs)),
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-selected")

    assert workdash_module.main([]) == 0

    result = captured_callback["analyze"](item, tool="codex")

    assert ensure_calls == [(_VALID_CONFIG.workdir, item)]
    assert analyze_calls == [(item, "codex", "/tmp/worktree", "workdash-selected", "repo")]
    assert result == str(analysis_path)


def test_main_tui_review_callback_waits_for_the_review_to_finish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    item = WorkItem(
        kind=WorkItemKind.TRACKED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=1,
        title="PR title",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url="https://example.com/pull/1",
    )
    captured_callback: dict[str, object] = {}
    review_running = threading.Event()
    release_review = threading.Event()
    review_finished = threading.Event()

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    head = subprocess.check_output(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], text=True
    ).strip()

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.github_client = SimpleNamespace(
                fetch_launch_context=lambda _item: {"state": "OPEN"},
                work_pull_request=lambda *args, **kwargs: SimpleNamespace(
                    state="OPEN", head_sha=head
                ),
            )

        def load_items(self, progress_callback=None):
            return [item], {}

        def resolve_analyze_command_tokens(self, _tool):
            return ["agent", "-p"]

        def include_item_by_url(self, _url, _existing_identities):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            captured_callback["review"] = kwargs["review_callback"]

        def run(self, *, mouse: bool) -> None:
            return None

    def blocking_review(_item, **_kwargs):
        review_running.set()
        release_review.wait(5)
        review_finished.set()
        return {"status": "reviewed", "document_path": str(tmp_path / "review.md")}

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda *args, **kwargs: str(tmp_path))
    monkeypatch.setattr(control_module, "review_pull_request", blocking_review)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: SimpleNamespace(stdout=head if cmd[1] == "rev-parse" else ""),
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-selected")

    assert workdash_module.main([]) == 0

    results: list[dict[str, object]] = []
    thread = threading.Thread(
        target=lambda: results.append(captured_callback["review"](item, "codex"))
    )
    thread.start()
    try:
        assert review_running.wait(5)
        assert not review_finished.is_set()
        thread.join(0.2)
        assert thread.is_alive()
        assert results == []
    finally:
        release_review.set()
    thread.join(10)
    assert review_finished.is_set()
    assert results == [
        {"status": "reviewed", "document_path": str(tmp_path / "review.md"), "agent": "codex"}
    ]


@pytest.mark.parametrize(
    ("items", "expected_stdout"),
    [
        pytest.param([], "No work items found.\n", id="empty"),
        pytest.param(
            [
                {
                    "id": "owner/repo#ISSUE-2",
                    "display_type": "ISSUE",
                    "updated_at": "2026-02-02T00:00:00+00:00",
                    "title": "Fix parser",
                    "suggested": True,
                },
                {
                    "id": "owner/repo#PR-1",
                    "display_type": "PR",
                    "updated_at": "2026-02-01T00:00:00+00:00",
                    "title": "Add logs",
                },
                {
                    "id": "owner/repo#REVIEW-3",
                    "display_type": "REVIEW",
                    "updated_at": "2026-01-31T00:00:00+00:00",
                    "title": "Review docs",
                },
                {
                    "id": "owner/repo#CHECK-4",
                    "display_type": "CHECK",
                    "updated_at": "2026-01-30T00:00:00+00:00",
                    "title": "Update README",
                },
            ],
            "ISSUE   owner/repo#ISSUE-2       2026-02-02 * Fix parser\n"
            "PR      owner/repo#PR-1          2026-02-01 Add logs\n"
            "REVIEW  owner/repo#REVIEW-3      2026-01-31 Review docs\n"
            "CHECK   owner/repo#CHECK-4       2026-01-30 Update README\n",
            id="multiple-rows",
        ),
    ],
)
def test_main_list_command_prints_only_the_listing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    items: list[dict[str, str | bool]],
    expected_stdout: str,
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "list"
            assert payload == {"refresh": False}
            return {"items": items}

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected config load")),
    )

    exit_code = workdash_module.main(["list"])

    assert exit_code == 0
    assert capsys.readouterr().out == expected_stdout


def test_main_list_json_hides_excluded_repositories_after_refresh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hidden = _issue("team/old")
    visible = _issue("team/current", number=2)
    markers = {(hidden.item_type, hidden.repo, hidden.number): "*"}
    session = WorkdashSession(
        config=WorkdashConfig(excluded_repositories=["Team/OLD"]),
        backend=SimpleNamespace(load_items=lambda: ([hidden, visible], markers)),  # type: ignore[arg-type]
        work_items=[],
        suggestion_markers={},
        zellij_session=None,
    )

    def request(endpoint: str, payload: dict[str, bool]) -> dict[str, object]:
        assert endpoint == "list"
        return session.list_items(refresh=payload["refresh"])

    monkeypatch.setattr(
        workdash_module, "WorkdashControlClient", lambda host: SimpleNamespace(request=request)
    )

    assert workdash_module.main(["list", "--json", "--refresh"]) == 0
    rows = json.loads(capsys.readouterr().out)["items"]
    assert [(row["id"], row["suggested"]) for row in rows] == [("team/current#ISSUE-2", False)]


@pytest.mark.parametrize(
    ("kind", "item_type", "number", "included", "label", "item_id"),
    [
        (WorkItemKind.TRACKED_PR, WorkItemType.PR, 111, True, "CHECK+", "CHECK-111"),
        (WorkItemKind.TRACKED_ISSUE, WorkItemType.ISSUE, 222, True, "ISSUE+", "ISSUE-222"),
        (WorkItemKind.REVIEW_REQUESTED_PR, WorkItemType.PR, 333, True, "REVIEW+", "REVIEW-333"),
        (WorkItemKind.AUTHORED_PR, WorkItemType.PR, 555, True, "PR+", "PR-555"),
        (WorkItemKind.TRACKED_PR, WorkItemType.PR, 444, False, "CHECK", "CHECK-444"),
    ],
)
def test_list_prints_included_suffix_by_item_id(
    kind, item_type, number, included, label, item_id, capsys
):
    updated = datetime(2026, 4, 28, tzinfo=UTC)
    item = WorkItem(
        kind=kind,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title="Dashboard item",
        url=f"https://github.com/owner/repo/{'pull' if item_type is WorkItemType.PR else 'issues'}/{number}",
        created_at=updated,
        updated_at=updated,
        included=included,
    )

    _print_work_items_result(control_module._work_items_payload([item], {}))

    assert capsys.readouterr().out.startswith(f"{label:7} owner/repo#{item_id} ")


def test_main_exits_with_error_when_gh_is_not_installed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "gh CLI is not installed" in captured.err


@pytest.mark.parametrize(
    "failure",
    [EOFError("EOF when reading a line"), PermissionError("denied")],
)
def test_main_reports_a_failed_wizard_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    failure: Exception,
) -> None:
    config_path = tmp_path / "missing.json"
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda **kwargs: (_ for _ in ()).throw(failure),
    )

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith(f"Error: {failure}")
    assert "Run 'workdash' again" not in captured.out


def test_main_runs_configuration_when_the_config_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config_path = tmp_path / "missing.json"
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda **kwargs: save_config(_VALID_CONFIG, config_path),
    )
    monkeypatch.setattr(
        workdash_module,
        "WorkdashBackend",
        lambda *args, **kwargs: pytest.fail("first run should not load work items"),
    )
    # A machine without Zellij is the main first-time case: setup must run before
    # anything needs the Zellij binary it installs.
    monkeypatch.setattr(workdash_module.launcher.shutil, "which", lambda command: None)
    monkeypatch.delenv("ZELLIJ", raising=False)

    exit_code = workdash_module.main([])

    assert exit_code == 0
    assert load_config(config_path) == _VALID_CONFIG
    captured = capsys.readouterr()
    assert "Run 'workdash' again to start the dashboard." in captured.out
    assert "zellij is not installed" not in captured.err


def test_persistent_pane_reports_missing_config_without_running_setup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config_path = tmp_path / "session-home" / ".config" / "workdash" / "config.json"
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda **kwargs: save_config(_VALID_CONFIG, config_path),
    )

    exit_code = workdash_module.main(["--persistent-pane"])

    assert not config_path.exists(), "the dashboard pane must not create configuration"
    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == f"Error: Configuration file not found at {config_path}.\n"
    assert not captured.out


@pytest.mark.parametrize("path_is_set", [True, False], ids=["set-path", "unset-path"])
def test_first_run_replaces_private_gh_without_a_system_install(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    path_is_set: bool,
) -> None:
    config_path = tmp_path / "missing.json"
    private_bin = tmp_path / "bin"
    private_bin.mkdir()
    system_bin = tmp_path / "system-bin"
    system_bin.mkdir()
    gh = private_bin / "gh"
    for binary in (private_bin / "zellij", gh, system_bin / "claude"):
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
    gh.write_text("not an executable format\n", encoding="utf-8")

    original_path = str(system_bin) if path_is_set else None
    if path_is_set:
        monkeypatch.setenv("PATH", str(system_bin))
    else:
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.setattr(os, "confstr", lambda name: str(system_bin))
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(workdash_module.launcher, "_WORKDASH_LOCAL_BIN", private_bin)

    from unittest.mock import MagicMock

    list_sessions = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    monkeypatch.setattr(workdash_module.launcher.subprocess, "run", list_sessions)

    def install_gh():
        gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        return str(gh)

    answers = iter(["/bin/sh", "", "", "", "", "octocat", "", ""])
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda **kwargs: config_module.configure(
            config_path,
            **kwargs,
            input_fn=lambda prompt: next(answers),
            install_zellij_fn=lambda: str(private_bin / "zellij"),
            install_gh_fn=install_gh,
        ),
    )

    assert workdash_module.main([]) == 0
    output = capsys.readouterr().out
    assert f"Installed GitHub CLI to: {gh}" in output
    assert "Run 'workdash' again to start the dashboard." in output
    assert gh.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert os.environ.get("PATH") == original_path
    assert [call.args[0] for call in list_sessions.call_args_list] == [
        [str(private_bin / "zellij"), "list-sessions", "--no-formatting"]
    ]


def test_main_does_not_configure_again_when_the_config_file_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config_path = tmp_path / "config.json"
    save_config(WorkdashConfig(github_username="testuser"), config_path)
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(workdash_module, "load_config", lambda: load_config(config_path))
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda **kwargs: pytest.fail("an existing config must not be reconfigured"),
    )
    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "missing configuration fields" in captured.err
    assert "workdash --configure" in captured.err


def test_main_reports_a_dangling_config_symlink_without_running_the_wizard(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    config_path = tmp_path / "config-link.json"
    config_path.symlink_to(tmp_path / "missing.json")
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(workdash_module, "load_config", lambda: load_config(config_path))
    monkeypatch.setattr(
        workdash_module,
        "configure",
        lambda **kwargs: pytest.fail("a dangling config symlink must not start the wizard"),
    )
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setenv("ZELLIJ", "1")

    assert workdash_module.main([]) == 1
    assert "workdash --configure" in capsys.readouterr().err
    assert config_path.is_symlink()
    assert not config_path.exists()


@pytest.mark.parametrize(
    ("config", "missing"),
    [
        (WorkdashConfig(), ("github_username", "repositories", "workdir")),
        (replace(_VALID_CONFIG, agents={}), ("agents",)),
    ],
    ids=["incomplete", "no-agents"],
)
def test_main_exits_with_error_when_config_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: WorkdashConfig,
    missing: tuple[str, ...],
) -> None:
    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    assert workdash_module.main([]) == 1

    error = capsys.readouterr().err
    prefix = "Error: missing configuration fields: "
    fields = next(
        line.removeprefix(prefix) for line in error.splitlines() if line.startswith(prefix)
    )
    assert set(missing) <= set(fields.split(", "))
    if missing == ("agents",):
        assert fields == "agents"
    assert "--configure" in error


@pytest.mark.parametrize(
    ("config", "diagnostic"),
    [
        (replace(_VALID_CONFIG, open_command=""), "missing configuration fields: open"),
        (replace(_VALID_CONFIG, open_command="   "), "missing configuration fields: open"),
        (
            replace(_VALID_CONFIG, open_command="open 'broken"),
            "invalid configuration fields: open: No closing quotation",
        ),
        (
            replace(
                _VALID_CONFIG,
                agents={**_VALID_CONFIG.agents, "pi": AgentConfig(launch="pi 'broken")},
            ),
            "invalid configuration fields: agents.pi.launch: No closing quotation",
        ),
    ],
    ids=["no-opener", "blank-opener", "malformed-opener", "malformed-agent"],
)
def test_main_rejects_invalid_commands_before_zellij_wrapping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    config: WorkdashConfig,
    diagnostic: str,
) -> None:
    def run(command, **kwargs):
        assert command in [
            ["/usr/bin/gh", "auth", "status"],
            ["/usr/bin/zellij", "list-sessions", "--no-formatting"],
        ]
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    execvp = MagicMock()
    backend = MagicMock()
    monkeypatch.setattr(os, "execvp", execvp)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", backend)

    assert workdash_module.main([]) == 1

    execvp.assert_not_called()
    backend.assert_not_called()
    error = capsys.readouterr().err
    assert diagnostic in error
    assert "workdash --configure" in error
    assert "Traceback" not in error


@pytest.mark.parametrize(
    ("argv", "expected_endpoint", "expected_payload"),
    [
        (["list"], "list", {"refresh": False}),
        (["list", "--json"], "list", {"refresh": False}),
        (["list", "--refresh"], "list", {"refresh": True}),
        (["info"], "info", {"panes": "work"}),
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
        (["info", "--all"], "info", {"panes": "all"}),
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

    list_result = {
        "items": [
            {
                "id": "owner/repo#ISSUE-42",
                "type": "issue",
                "display_type": "ISSUE",
                "kind": "tracked_issue",
                "repo": "owner/repo",
                "number": 42,
                "title": "Fix parser",
                "url": "https://github.com/owner/repo/issues/42",
                "created_at": "2026-02-01T00:00:00+00:00",
                "updated_at": "2026-02-02T00:00:00+00:00",
                "suggested": True,
                "analysis_available": False,
                "analysis_url": None,
            }
        ]
    }

    class FakeControlClient:
        def __init__(self, selected_host="127.0.0.1") -> None:
            assert selected_host == host

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            requests.append((endpoint, dict(payload or {})))
            if endpoint == "list":
                return list_result if "--json" in argv else {"items": []}
            if endpoint == "info":
                return {"session": "workdash", "panes": []}
            if endpoint == "ready":
                return {"scope": payload["scope"], "items": [ready_item]}
            if endpoint == "analyze":
                return {
                    "item_id": "owner/repo#ISSUE-1",
                    "path": str(analysis_path),
                    "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
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
        workdash_module.GitHubClient,
        "check_authentication",
        lambda self: (_ for _ in ()).throw(AssertionError("unexpected GitHub preflight")),
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
        workdash_module.launcher.zellij,
        "exec_zellij_wrapped_workdash",
        lambda _argv: (_ for _ in ()).throw(AssertionError("unexpected Zellij wrapper")),
    )

    def unexpected_app(*args, **kwargs):
        pytest.fail("Server-backed commands must not start the TUI")

    monkeypatch.setattr(workdash_module, "WorkdashApp", unexpected_app)

    if environment_host is None:
        monkeypatch.delenv("WORKDASH_HOST", raising=False)
    else:
        monkeypatch.setenv("WORKDASH_HOST", environment_host)
    host_args = ["--host", host] if host_position else []
    command = host_args + argv if host_position == "before" else argv + host_args
    assert workdash_module.main(command) == 0

    output, stderr = capsys.readouterr()
    if expected_endpoint == "info":
        assert "gh CLI" not in output + stderr
    if expected_endpoint == "analyze":
        analysis_url = next(
            line.removeprefix("Analysis URL: ")
            for line in output.splitlines()
            if line.startswith("Analysis URL: ")
        )
        assert analysis_url == "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis"
    assert requests == [(expected_endpoint, expected_payload)]
    if expected_endpoint == "list" and "--json" in argv:
        assert json.loads(output) == list_result
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
    def refuse_connection(_request, *, timeout):
        raise ConnectionRefusedError

    monkeypatch.setattr("urllib.request.urlopen", refuse_connection)

    assert workdash_module.main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Start one with `workdash`." in captured.err


def test_unreachable_server_reports_startup_guidance():
    import socket

    with socket.socket() as unavailable:
        unavailable.bind(("127.0.0.1", 0))
        port = unavailable.getsockname()[1]
        client = workdash_module.WorkdashControlClient(f"127.0.0.1:{port}")
        with pytest.raises(workdash_module.WorkdashControlError) as error:
            client.request("info")
    assert "Start one with `workdash`." in str(error.value)


def test_main_analyze_human_output_formats_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    markdown_url = "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis"
    html_url = f"{markdown_url}/html"

    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "analyze"
            assert payload == {"target": "owner/repo#ISSUE-1", "agent": None}
            return {
                "item_id": "owner/repo#ISSUE-1",
                "path": "/server/cache/analysis.md",
                "url": markdown_url,
                "markdown_url": markdown_url,
                "html_url": html_url,
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
    analysis_url = next(
        line.removeprefix("Analysis URL: ")
        for line in output.splitlines()
        if line.startswith("Analysis URL: ")
    )
    assert analysis_url == markdown_url
    assert html_url not in output
    assert "Item: owner/repo#ISSUE-1" in output
    assert "Agent: codex" in output
    assert "Status: generated" in output


def test_main_analyze_json_outputs_analysis_url_without_server_content_fields(
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
                "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
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
    assert payload == {
        "item_id": "owner/repo#ISSUE-1",
        "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
        "agent": "codex",
        "cache_used": True,
        "status": "cached",
    }


def test_main_analyze_json_reports_the_same_server_analysis_url_twice(
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
                "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
                "agent": "codex",
                "cache_used": False,
                "status": "generated",
                "content_type": "text/markdown",
                "file_name": "owner_repo_ISSUE1.md",
                "file_content": base64.b64encode(b"analysis body\n").decode("ascii"),
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--json"]) == 0
    first_payload = json.loads(capsys.readouterr().out)
    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--json"]) == 0
    second_payload = json.loads(capsys.readouterr().out)

    assert "path" not in first_payload
    assert "file_content" not in first_payload
    assert "file_name" not in first_payload
    assert "content_type" not in first_payload
    assert first_payload == second_payload
    assert first_payload["url"] == "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis"


@pytest.mark.parametrize(
    ("response_fields", "expected_error"),
    [
        ({"url": None}, "missing the analysis url"),
        ({"url": 123}, "missing the analysis url"),
        ({"url": ""}, "missing the analysis url"),
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
                "url": "http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
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


@pytest.mark.parametrize(
    ("arguments", "expected_payload", "result", "expected_lines"),
    [
        (
            ["code", "owner/repo#ISSUE-1", "--agent", "pi", *flags],
            {"target": "owner/repo#ISSUE-1", "agent": "pi", **mode},
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "session": "workdash-main",
                "agent": "pi",
                "cwd": "/tmp/wt",
                "pane_title": "code_wt",
                "pane_id": "terminal_7",
            },
            [
                "Status: launched",
                "Item: owner/repo#ISSUE-1",
                "Agent: pi",
                "Session: workdash-main",
                "Tab: -",
                "Cwd: /tmp/wt",
                "Pane title: code_wt",
                "Pane id: terminal_7",
            ],
        )
        for flags, mode in [([], {}), (["--autostart"], {"autostart": True})]
    ]
    + [
        (
            ["code", "owner/repo#ISSUE-1", "--agent", "pi", "--autoclose"],
            {
                "target": "owner/repo#ISSUE-1",
                "agent": "pi",
                "autostart": True,
                "autoclose": True,
            },
            {
                "status": "launched",
                "item_id": "owner/repo#ISSUE-1",
                "session": "workdash-main",
                "agent": "pi",
                "cwd": "/tmp/wt",
                "pane_title": "code_wt",
                "pane_id": "terminal_7",
                "autoclose_enabled": True,
            },
            [
                "Status: launched",
                "Item: owner/repo#ISSUE-1",
                "Agent: pi",
                "Session: workdash-main",
                "Tab: -",
                "Cwd: /tmp/wt",
                "Pane title: code_wt",
                "Pane id: terminal_7",
                "Autoclose: enabled",
            ],
        ),
        (
            ["code", "owner/repo#ISSUE-1", "--agent", "pi", "--autoclose"],
            {
                "target": "owner/repo#ISSUE-1",
                "agent": "pi",
                "autostart": True,
                "autoclose": True,
            },
            {
                "status": "already_running",
                "item_id": "owner/repo#ISSUE-1",
                "session": "workdash-main",
                "agent": None,
                "cwd": "/tmp/wt",
                "pane_title": "code_wt",
                "pane_id": "terminal_7",
                "autoclose_enabled": False,
            },
            [
                "Status: already_running",
                "Item: owner/repo#ISSUE-1",
                "Agent: -",
                "Session: workdash-main",
                "Tab: -",
                "Cwd: /tmp/wt",
                "Pane title: code_wt",
                "Pane id: terminal_7",
                "Autoclose: not enabled",
            ],
        ),
    ],
)
def test_main_code_human_output_formats_the_server_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    expected_payload: dict[str, object],
    result: dict[str, object],
    expected_lines: list[str],
) -> None:
    class FakeControlClient:
        def __init__(self, host="127.0.0.1") -> None:
            assert host == "127.0.0.1"

        def request(self, endpoint: str, payload: dict[str, object] | None = None):
            assert endpoint == "code"
            assert payload == expected_payload
            return result

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(arguments) == 0

    assert capsys.readouterr().out.splitlines() == expected_lines


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
            assert payload == {"panes": "work"}
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
            assert payload == {"panes": "work"}
            return response

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)

    assert workdash_module.main(["--json", "info"]) == 0

    assert json.loads(capsys.readouterr().out) == response


def test_main_configure_reports_invalid_listeners(monkeypatch, tmp_path, capsys):
    from workdash.config import configure

    path = tmp_path / "config.json"
    original = '{"listens": "127.0.0.2@127.0.0.4"}'
    path.write_text(original)
    monkeypatch.setattr(workdash_module, "configure", lambda **kwargs: configure(path))

    assert workdash_module.main(["--configure"]) == 1
    error = capsys.readouterr().err
    assert "listens" in error and "list" in error and str(path) in error
    assert path.read_text() == original


def test_main_configure_reports_an_unparsable_config_file_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    from workdash.config import configure

    config_path = tmp_path / "config.json"
    config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(workdash_module, "configure", lambda **kwargs: configure(config_path))

    exit_code = workdash_module.main(["--configure"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("Error: Configuration at ")
    assert "is not valid JSON" in captured.err


def test_main_configure_runs_setup_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_called = False

    def fake_configure(*, github_client, create_only):
        nonlocal configure_called
        configure_called = True
        assert not create_only
        return _VALID_CONFIG

    monkeypatch.setattr(workdash_module, "configure", fake_configure)
    monkeypatch.setattr(
        workdash_module.GitHubClient,
        "check_authentication",
        lambda self: (_ for _ in ()).throw(AssertionError("unexpected gh preflight")),
    )

    exit_code = workdash_module.main(["--configure"])

    assert exit_code == 0
    assert configure_called


@pytest.mark.parametrize("argv", [["--help"], ["--version"]])
def test_main_help_and_version_exit_without_gh_preflight(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(
        workdash_module.GitHubClient,
        "check_authentication",
        lambda self: (_ for _ in ()).throw(AssertionError("unexpected gh preflight")),
    )

    with pytest.raises(SystemExit) as error:
        workdash_module.main(argv)

    assert error.value.code == 0


def test_main_outside_zellij_replaces_process_with_zellij(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapper_calls: list[object] = []

    def fake_wrapper(argv, preflight):
        assert preflight()
        wrapper_calls.append(argv)
        raise SystemExit(0)

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(
        workdash_module.launcher.zellij, "exec_zellij_wrapped_workdash", fake_wrapper
    )

    with pytest.raises(SystemExit):
        workdash_module.main(["--refresh"])
    assert wrapper_calls == [["--refresh"]]


def test_main_reports_gh_authentication_failure_before_invalid_config_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fake_run(*args, **kwargs):
        raise github_client_module.subprocess.CalledProcessError(1, ["gh", "auth", "status"])

    config_path = tmp_path / "config.json"
    config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(workdash_module, "load_config", lambda: load_config(config_path))
    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "gh CLI is not authenticated" in captured.err
    assert "is not valid JSON" not in captured.err
    assert (
        "Run this command to authenticate the GitHub CLI used by workdash:\n"
        "  /usr/bin/gh auth login\n"
    ) in captured.err


@pytest.mark.parametrize("gh_installed", [False, True], ids=["missing-gh", "expired-auth"])
def test_main_outside_zellij_checks_gh_before_replacing_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    gh_installed,
) -> None:
    wrapper_calls: list[object] = []

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(
        github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh" if gh_installed else None
    )
    if gh_installed:
        monkeypatch.setattr(
            github_client_module.subprocess,
            "run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, ["gh", "auth", "status"])),
        )
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "exec_zellij_wrapped_workdash",
        lambda argv, preflight: wrapper_calls.append(argv) if preflight() else None,
    )

    exit_code = workdash_module.main([])

    assert exit_code == 1
    assert wrapper_calls == []
    assert (
        "gh CLI is not authenticated" if gh_installed else "gh CLI is not installed"
    ) in capsys.readouterr().err


def test_main_outside_zellij_reports_missing_zellij(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr("workdash.launcher.shutil.which", lambda cmd: None)

    exit_code = workdash_module.main([])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "zellij is not installed or not configured" in captured.err
    assert "--configure" in captured.err


def test_main_preserves_a_zellij_wrapper_error_when_config_access_is_denied(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from unittest.mock import MagicMock

    monkeypatch.delenv("ZELLIJ", raising=False)
    config_path = MagicMock(spec=Path)
    config_path.lstat.side_effect = PermissionError("Configuration is inaccessible")
    monkeypatch.setattr(workdash_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "exec_zellij_wrapped_workdash",
        MagicMock(side_effect=RuntimeError("Failed to list Zellij sessions")),
    )

    assert workdash_module.main([]) == 1
    assert capsys.readouterr().err == "Error: Failed to list Zellij sessions\n"


@pytest.fixture
def startup_zellij(monkeypatch):
    launcher = workdash_module.launcher
    monkeypatch.setattr(
        launcher.shutil, "which", lambda command: "/usr/bin/zellij" if command == "zellij" else None
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:9999")
    monkeypatch.setattr(
        workdash_module.WorkdashApp, "run", lambda *args, **kwargs: pytest.fail("dashboard started")
    )
    execvp = MagicMock(side_effect=SystemExit(0))
    monkeypatch.setattr(launcher.os, "execvp", execvp)
    sessions = MagicMock(return_value="project [Created now]\n")

    def run(command, **kwargs):
        assert command == ["/usr/bin/zellij", "list-sessions", "--no-formatting"]
        return subprocess.CompletedProcess(command, 0, sessions(), "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    return sessions, execvp


@pytest.fixture
def deny_startup_config_access(monkeypatch, tmp_path):
    config_path = workdash_module.CONFIG_PATH
    lstat, path_open = Path.lstat, Path.open

    def denied_lstat(path):
        if path == config_path:
            raise PermissionError("Permission denied")
        return lstat(path)

    def denied_open(path, *args, **kwargs):
        if path == config_path or path.is_relative_to(tmp_path / "sessions"):
            raise PermissionError("Permission denied")
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    monkeypatch.setattr(Path, "open", denied_open)


@pytest.mark.parametrize("config_state", ["invalid-json", "access-denied", "missing"])
def test_main_attaches_to_running_dashboard_without_usable_config(
    monkeypatch, capsys, request, startup_zellij, config_state
):
    _, execvp = startup_zellij
    config_path = workdash_module.CONFIG_PATH
    if config_state == "invalid-json":
        config_path.write_text("{invalid json", encoding="utf-8")
    elif config_state == "access-denied":
        request.getfixturevalue("deny_startup_config_access")
    else:
        config_path.unlink()

    probe = MagicMock(return_value="project")
    monkeypatch.setattr(control_module, "probe_dashboard_session", probe)
    wizard = MagicMock()
    auth = MagicMock()
    monkeypatch.setattr(workdash_module, "configure", wizard)
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", auth)

    with pytest.raises(SystemExit) as exit_info:
        workdash_module.main(["--session", "project"])

    assert exit_info.value.code == 0
    probe.assert_called_once_with("127.0.0.1:9999")
    execvp.assert_called_once_with("/usr/bin/zellij", ["/usr/bin/zellij", "attach", "project"])
    wizard.assert_not_called()
    auth.assert_not_called()
    captured = capsys.readouterr()
    assert "options are ignored. Attaching..." in captured.out
    assert not captured.err
    if config_state == "missing":
        assert not config_path.exists()


def test_main_outside_zellij_reports_invalid_config_json(monkeypatch, capsys, startup_zellij):
    _, execvp = startup_zellij
    config_path = workdash_module.CONFIG_PATH
    config_path.write_text("{invalid json", encoding="utf-8")
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: load_config(config_path))

    assert workdash_module.main([]) == 1

    captured = capsys.readouterr()
    (line,) = (captured.out + captured.err).splitlines()
    assert line.startswith(f"Error: Configuration at {config_path} is not valid JSON:")
    execvp.assert_not_called()


@pytest.mark.parametrize("zellij_available", [True, False], ids=["no-dashboard", "no-zellij"])
def test_main_outside_zellij_reports_denied_config_access(
    monkeypatch, capsys, startup_zellij, deny_startup_config_access, zellij_available
):
    sessions, execvp = startup_zellij
    wizard = MagicMock()
    monkeypatch.setattr(workdash_module, "configure", wizard)
    if not zellij_available:
        monkeypatch.setattr(workdash_module.launcher.shutil, "which", lambda command: None)

    assert workdash_module.main(["--session", "project"] if zellij_available else []) == 1

    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err == (
        f"Error: Cannot inspect configuration at {workdash_module.CONFIG_PATH}: Permission denied\n"
    )
    assert sessions.call_count == int(zellij_available)
    execvp.assert_not_called()
    wizard.assert_not_called()


@pytest.mark.parametrize(
    "address_state", ["different-session", "indeterminate", "version-mismatch"]
)
def test_main_refuses_to_start_at_an_unusable_dashboard_address(
    monkeypatch, capsys, startup_zellij, address_state
):
    _, execvp = startup_zellij
    address = "127.0.0.1:9999"
    if address_state == "different-session":
        monkeypatch.setattr(control_module, "probe_dashboard_session", lambda address: "other")
    else:
        monkeypatch.setattr(control_module, "probe_dashboard_session", probe_dashboard_session)
        if address_state == "indeterminate":
            status, code, message = 404, "not_found", "No such endpoint."
        else:
            status, code, message = (
                403,
                "version_mismatch",
                "Workdash client version differs from dashboard version; restart the dashboard.",
            )

        def urlopen(request, *, timeout):
            assert request.full_url == f"http://{address}/api/v0/info"
            body = json.dumps({"ok": False, "error": {"code": code, "message": message}})
            raise HTTPError(request.full_url, status, message, {}, io.BytesIO(body.encode()))

        monkeypatch.setattr(control_module.urllib.request, "urlopen", urlopen)

    assert workdash_module.main(["--session", "project"]) == 1

    captured = capsys.readouterr()
    assert not captured.out
    assert address in captured.err
    if address_state == "different-session":
        assert "'other'" in captured.err
    elif address_state == "indeterminate":
        assert "Cannot tell whether a Workdash dashboard is running" in captured.err
    else:
        assert "version" in captured.err and "restart the dashboard" in captured.err
        assert "Cannot tell" not in captured.err
    execvp.assert_not_called()


@pytest.mark.parametrize(
    "location, local_session, served_session",
    [
        pytest.param("outside", "running", "other", id="other-dashboard-outside-zellij"),
        pytest.param("inside", "running", "other", id="other-dashboard-inside-zellij"),
        pytest.param("outside", "missing", "project", id="no-local-session"),
        pytest.param("outside", "exited", "project", id="exited-local-session"),
    ],
)
def test_main_runs_first_time_setup_when_it_cannot_attach(
    monkeypatch, capsys, startup_zellij, location, local_session, served_session
):
    sessions, execvp = startup_zellij
    config_path = workdash_module.CONFIG_PATH
    config_path.unlink()
    if location == "inside":
        monkeypatch.setenv("ZELLIJ", "0")
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", "current")
        monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    if local_session == "missing":
        sessions.return_value = ""
    elif local_session == "exited":
        sessions.return_value = "project [Created now] (EXITED - attach to resurrect)\n"
    probe = MagicMock(return_value=served_session)
    monkeypatch.setattr(control_module, "probe_dashboard_session", probe)

    def configure(*, github_client, create_only):
        assert create_only
        save_config(_VALID_CONFIG, config_path)

    monkeypatch.setattr(workdash_module, "configure", configure)

    assert workdash_module.main(["--session", "project"]) == 0

    assert load_config(config_path) == _VALID_CONFIG
    captured = capsys.readouterr()
    assert "Run 'workdash' again to start the dashboard." in captured.out
    assert not captured.err
    if local_session == "running":
        probe.assert_called_once_with("127.0.0.1:9999")
    else:
        probe.assert_not_called()
    assert sessions.call_count == int(location == "outside")
    execvp.assert_not_called()


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
            from workdash.github_client import GitHubClient

            self.github_client = GitHubClient()
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

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    from gh_work_state_fake import install_fake_github

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda _wd, _item, **kwargs: "/tmp/wt")
    monkeypatch.setattr(control_module, "get_merge_base", lambda _path, **kwargs: None)
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
            pane_id="terminal_4",
            pane_title="code_wt",
            cwd=repo,
        )

    monkeypatch.setattr(
        control_module.agent_execution.executor, "launch", fake_launch_agent_context
    )
    monkeypatch.setattr(
        control_module.launcher.zellij, "load_zellij_panes", lambda _session: list(panes)
    )
    monkeypatch.setattr(control_module, "existing_worktree_path", lambda _wd, _item, **kwargs: None)
    monkeypatch.setattr(
        control_module.agent_execution.executor,
        "launch_vscode",
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

    # The TUI stays plan-first and unsupervised: its launch asks for neither option.
    assert [(call["autostart"], call["autoclose"]) for call in code_calls] == [(False, False)]
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
            from workdash.github_client import GitHubClient

            self.github_client = GitHubClient()
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

    monkeypatch.setattr(github_client_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(control_module, "ensure_worktree", lambda _wd, _item, **_kwargs: "/tmp/wt")
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")

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
    assert "owner/repo#REVIEW-9" in output_line


@pytest.mark.parametrize(
    "listener",
    [
        "",
        "127.0.0.2",
        "127.0.0.2@",
        "127.0.0.2@192.0.2.1,",
        "127.0.0.2@client.lan",
        "0.0.0.0@192.0.2.1",
        "*@192.0.2.1",
        "dashboard.lan:9000@192.0.2.1",
    ],
)
def test_invalid_configured_listeners_abort_startup(monkeypatch, capsys, listener):
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(
        workdash_module, "load_config", lambda: replace(_VALID_CONFIG, listens=(listener,))
    )
    assert workdash_module.main([]) == 1
    assert "listens" in capsys.readouterr().err


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


def test_main_show_config_json_reports_agents_and_default_server(monkeypatch, capsys):
    monkeypatch.delenv("WORKDASH_HOST", raising=False)
    config = replace(
        _VALID_CONFIG,
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "pi": AgentConfig(launch="pi"),
        },
    )
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)

    assert workdash_module.main(["show-config", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "agents": {"analyze": ["codex"], "code": ["codex", "pi"]},
        "server": {"host": "127.0.0.1", "port": 8765},
    }


def test_address_validation_and_host_only_default(monkeypatch, capsys):
    monkeypatch.delenv("WORKDASH_HOST", raising=False)
    assert control_module.server_address() == ("127.0.0.1", 8765)
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
        ["show-config"],
        ["--configure"],
        ["branchdiff"],
        ["branchinfo"],
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


def test_startup_serves_each_dashboard_with_its_own_state_and_session(monkeypatch):
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        addresses = [f"127.0.0.1:{sock.getsockname()[1]}" for sock in (first, second)]
    names = ["workdash-one", "demo"]
    items = [[replace(_issue(number=i), title=f"Issue {i}")] for i in (1, 2)]
    markers = [compute_suggestion_markers(rows) for rows in items]
    dashboards = []

    class Dashboard:
        def __init__(self, **kwargs):
            dashboards.append(kwargs)

        def run(self, *, mouse):
            if len(dashboards) == 1:
                with monkeypatch.context() as other:
                    other.setenv("ZELLIJ_SESSION_NAME", names[1])
                    other.setenv("WORKDASH_HOST", addresses[1])
                    assert workdash_module.main([]) == 0
                return

            for index, (address, name, dashboard) in enumerate(zip(addresses, names, dashboards)):
                session = dashboard["session"]
                assert dashboard["work_items"] is session.work_items
                assert dashboard["suggestion_markers"] is session.suggestion_markers
                assert session.suggestion_markers == markers[index]
                client = control_module.WorkdashControlClient(address)
                assert client.request("info", {"panes": "none"})["session"] == name
                assert [row["title"] for row in client.request("list")["items"]] == [
                    items[index][0].title
                ]

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", names[0])
    monkeypatch.setenv("WORKDASH_HOST", addresses[0])
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(
        workdash_module.launcher.zellij, "list_workdash_sessions", lambda: [names[0]]
    )
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "exec_zellij_wrapped_workdash",
        lambda *args, **kwargs: pytest.fail("inside Zellij, do not wrap or attach"),
    )
    loads = iter(zip(items, markers))
    backend = SimpleNamespace(load_items=lambda **kwargs: next(loads))
    monkeypatch.setattr(workdash_module, "WorkdashBackend", lambda **kwargs: backend)
    monkeypatch.setattr(
        workdash_module, "WorkdashControlServer", control_module.WorkdashControlServer
    )
    monkeypatch.setattr(workdash_module, "WorkdashApp", Dashboard)

    assert workdash_module.main([]) == 0
    assert len(dashboards) == 2


def test_startup_reports_an_occupied_server_address_before_running_tui(monkeypatch, capsys):
    def bind_occupied(host, port, app):
        assert (host, port) == ("127.0.0.1", 18765)
        raise OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:18765")
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    backend = SimpleNamespace(load_items=lambda **kwargs: ([], {}))
    monkeypatch.setattr(workdash_module, "WorkdashBackend", lambda **kwargs: backend)
    app = MagicMock()
    monkeypatch.setattr(workdash_module, "WorkdashApp", app)
    monkeypatch.setattr(
        workdash_module, "WorkdashControlServer", control_module.WorkdashControlServer
    )
    monkeypatch.setattr(control_module, "make_server", bind_occupied)

    assert workdash_module.main([]) != 0
    error = capsys.readouterr().err
    assert "Cannot bind Workdash server at 127.0.0.1:18765" in error
    assert os.strerror(errno.EADDRINUSE) in error
    assert "WORKDASH_HOST" in error
    app.return_value.run.assert_not_called()


def test_server_inside_zellij_requires_current_session_name(monkeypatch, capsys):
    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", lambda **kwargs: backend)
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "list_workdash_sessions",
        lambda: pytest.fail("must not select another session"),
    )
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    assert workdash_module.main([]) == 1
    assert "Cannot determine the current Zellij session" in capsys.readouterr().err


def test_version_writes_to_stdout_without_preflight(monkeypatch, capsys):
    monkeypatch.setenv("WORKDASH_HOST", "invalid:port")
    monkeypatch.setattr(
        workdash_module.GitHubClient,
        "check_authentication",
        lambda self: pytest.fail("unexpected preflight"),
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
        "dispatch",
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
    # Agents must learn both CLI launch modes and that nothing is merged.
    assert any(argv[0] == "code" and "--autostart" in argv for argv in examples)
    for text in (
        "`workdash code ITEM` uses the plan-first briefing by default",
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
    # Ready previews issue-only dispatch, which supervises one selected task.
    for text in (
        "Pull requests are never ready items",
        "issues with actionable authored pull",
        "No ready work found.",
        "status=no_ready_task",
        "actual `pane_id`",
        "never start one yourself",
    ):
        assert text in skill
    assert "autocode" not in skill


def test_code_group_is_forwarded_to_remote_server_and_reports_actual_tab(monkeypatch, capsys):
    class Client:
        def __init__(self, host):
            assert host == "dashboard.lan"

        def request(self, endpoint, payload):
            assert endpoint == "code"
            assert payload == {
                "target": "owner/repo#1",
                "agent": None,
                "group": "epic",
            }
            return {
                "status": "already_running",
                "item_id": "owner/repo#ISSUE-1",
                "agent": None,
                "session": "server",
                "cwd": "/repo",
                "pane_title": "code_repo",
                "pane_id": "terminal_1",
                "tab_name": "actual",
            }

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", Client)
    assert (
        workdash_module.main(["code", "owner/repo#1", "--group", "epic", "--host", "dashboard.lan"])
        == 0
    )
    assert "Tab: actual" in capsys.readouterr().out


def test_interactive_diff_prepares_worktree_with_session_git(monkeypatch, tmp_path) -> None:
    from unittest.mock import MagicMock

    item = _issue()
    git = MagicMock()
    git.find_worktree_for_branch.return_value = None
    backend = MagicMock()
    backend.load_items.return_value = ([item], {})
    config = replace(_VALID_CONFIG, workdir=str(tmp_path))
    results = []

    class FakeApp:
        def __init__(self, **kwargs):
            self.callback = kwargs["worktree_callback"]
            kwargs["session"].git = git

        def run(self, *, mouse):
            results.append(self.callback(item))

    monkeypatch.setattr(
        workdash_module.WorkdashCommands, "_load_config_and_backend", lambda self: (config, backend)
    )
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("real subprocess"))

    assert workdash_module.WorkdashCommands().interactive() == 0
    worktree = tmp_path / "owner_repo_1"
    assert results == [str(worktree)]
    git.create_worktree.assert_called_once_with(
        tmp_path / "owner_repo", worktree, "issue-1", "origin/HEAD"
    )


@pytest.mark.parametrize("failing", [False, True])
def test_interactive_records_the_serving_dashboard_for_its_session(monkeypatch, failing) -> None:
    import socket
    from unittest.mock import MagicMock

    from workdash.dashboard_record import DashboardRecord, DashboardRecords

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    monkeypatch.setattr(
        workdash_module, "WorkdashControlServer", control_module.WorkdashControlServer
    )
    monkeypatch.setattr(control_module.launcher.zellij, "load_zellij_panes", lambda session: [])
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "project")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    monkeypatch.setattr(
        workdash_module.WorkdashCommands,
        "_load_config_and_backend",
        lambda self: (_VALID_CONFIG, backend),
    )
    recorded = []

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, mouse) -> None:
            recorded.append(DashboardRecords().load("project"))
            assert control_module.WorkdashControlClient().request("info")["session"] == "project"
            if failing:
                raise RuntimeError("dashboard crashed")

    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    if failing:
        with pytest.raises(RuntimeError, match="dashboard crashed"):
            workdash_module.WorkdashCommands().interactive()
    else:
        assert workdash_module.WorkdashCommands().interactive() == 0

    assert recorded == [
        DashboardRecord(
            session="project", pid=os.getpid(), address=f"127.0.0.1:{port}", pane_id="3"
        )
    ]
    assert DashboardRecords().load("project") is None


def test_a_failed_record_write_stops_the_dashboard_server(monkeypatch, tmp_path) -> None:
    import socket
    from unittest.mock import MagicMock

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    monkeypatch.setattr(
        workdash_module, "WorkdashControlServer", control_module.WorkdashControlServer
    )
    unwritable = tmp_path / "record-file"
    unwritable.write_text("")
    monkeypatch.setattr("workdash.dashboard_record.SESSIONS_DIR", unwritable / "sessions")
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "project")
    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    monkeypatch.setattr(
        workdash_module.WorkdashCommands,
        "_load_config_and_backend",
        lambda self: (_VALID_CONFIG, backend),
    )
    monkeypatch.setattr(workdash_module, "WorkdashApp", MagicMock())

    with pytest.raises(OSError):
        workdash_module.WorkdashCommands().interactive()

    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(("127.0.0.1", port))


def test_interactive_leaves_another_process_dashboard_record_alone(monkeypatch) -> None:
    from unittest.mock import MagicMock

    from workdash.dashboard_record import DashboardRecord, DashboardRecords

    other = DashboardRecord(session="project", pid=os.getppid(), address="127.0.0.1:9999")
    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    monkeypatch.setattr(
        workdash_module.WorkdashCommands,
        "_load_config_and_backend",
        lambda self: (_VALID_CONFIG, backend),
    )
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "project")

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, mouse) -> None:
            DashboardRecords().save(other)

    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)

    assert workdash_module.WorkdashCommands().interactive() == 0

    assert DashboardRecords().load("project") == other


def test_the_dashboard_record_makes_the_next_start_attach(monkeypatch, capsys) -> None:
    import socket
    from unittest.mock import MagicMock

    from workdash.dashboard_record import DashboardRecords

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    with socket.socket() as idle:
        idle.bind(("127.0.0.1", 0))
        other_port = idle.getsockname()[1]
    monkeypatch.setattr(control_module, "SERVER_PORT", port)
    # The suite stubs the probe; this test asks the dashboard it started.
    monkeypatch.setattr(control_module, "probe_dashboard_session", probe_dashboard_session)
    monkeypatch.setattr(
        workdash_module, "WorkdashControlServer", control_module.WorkdashControlServer
    )
    launched = workdash_module.launcher
    monkeypatch.setattr(launched.zellij, "load_zellij_panes", lambda session: [])
    monkeypatch.setattr(launched, "_resolve_zellij_binary", lambda: "/usr/bin/zellij")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "project [Created now]\n", "")

    monkeypatch.setattr(launched.subprocess, "run", run)
    execs = []
    monkeypatch.setattr(
        launched.os,
        "execvp",
        lambda file, command: execs.append((file, command)) or (_ for _ in ()).throw(SystemExit(0)),
    )
    pauses = []
    monkeypatch.setattr(launched.time, "sleep", lambda seconds: pauses.append(seconds))
    monkeypatch.setenv("ZELLIJ", "1")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "project")
    backend = MagicMock()
    backend.load_items.return_value = ([], {})
    monkeypatch.setattr(
        workdash_module.WorkdashCommands,
        "_load_config_and_backend",
        lambda self: (_VALID_CONFIG, backend),
    )

    class FakeApp:
        def __init__(self, **kwargs) -> None:
            pass

        def run(self, *, mouse) -> None:
            # The next start prefers another address than the recorded dashboard serves.
            monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{other_port}")
            launched.zellij.exec_zellij_wrapped_workdash(["--session", "project"])

    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)
    with pytest.raises(SystemExit):
        workdash_module.WorkdashCommands().interactive()

    assert "options are ignored" in capsys.readouterr().out
    assert pauses == [1]
    assert execs == [("/usr/bin/zellij", ["/usr/bin/zellij", "attach", "project"])]
    assert not any("new-pane" in command for command in calls)
    assert DashboardRecords().load("project") is None


@pytest.mark.parametrize("config_missing", [False, True], ids=["configured", "config-moved-aside"])
def test_main_inside_zellij_leaves_a_running_dashboard_alone(
    monkeypatch, capsys, config_missing
) -> None:
    config_path = workdash_module.CONFIG_PATH
    if config_missing:
        config_path.unlink()
    auth = MagicMock(return_value=None)
    wizard = MagicMock()
    monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", auth)
    monkeypatch.setattr(workdash_module, "configure", wizard)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "project")
    monkeypatch.setenv("ZELLIJ_PANE_ID", "3")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:9999")
    probe = MagicMock(return_value="project")
    monkeypatch.setattr(control_module, "probe_dashboard_session", probe)
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "name_dashboard_pane",
        lambda session, pane: pytest.fail("a running dashboard keeps its pane name"),
    )
    monkeypatch.setattr(
        workdash_module, "WorkdashApp", lambda **kwargs: pytest.fail("the dashboard must not start")
    )

    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "exec_zellij_wrapped_workdash",
        lambda *args, **kwargs: pytest.fail("inside Zellij, do not wrap or attach"),
    )

    assert workdash_module.main(["--refresh"]) == 0
    assert "options are ignored" in capsys.readouterr().out
    probe.assert_called_once_with("127.0.0.1:9999")
    wizard.assert_not_called()
    if config_missing:
        auth.assert_not_called()
        assert not config_path.exists()


def test_todo_close_json_uses_server(monkeypatch, capsys):
    result = {
        "item_id": "me/todos#ISSUE-42",
        "status": "closed",
        "url": "https://github.com/me/todos/issues/42",
    }
    requests = []

    class Client:
        def __init__(self, host):
            assert host == "dashboard.lan"

        def request(self, endpoint, payload):
            requests.append((endpoint, payload))
            return result

    monkeypatch.setattr(workdash_module, "WorkdashControlClient", Client)
    assert (
        workdash_module.main(["todo", "--close", "#42", "--host", "dashboard.lan", "--json"]) == 0
    )
    assert requests == [("todo_close", {"item": "#42"})]
    assert json.loads(capsys.readouterr().out) == result


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        ["--close", "42", "text"],
        ["--close", "42", "--target", "owner/repo"],
        ["--close", "42", "--body", "details"],
    ],
)
def test_todo_rejects_invalid_arguments(arguments, capsys):
    with pytest.raises(SystemExit) as error:
        workdash_module.main(["todo", *arguments])
    assert error.value.code == 2
    assert "todo requires TEXT or --close ITEM" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--server", "--direct", "--listen"])
def test_removed_flags_are_rejected(flag, capsys):
    with pytest.raises(SystemExit) as error:
        workdash_module.main([flag])
    assert error.value.code == 2
    assert f"unrecognized arguments: {flag}" in capsys.readouterr().err
