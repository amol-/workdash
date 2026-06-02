import json
import subprocess
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import workdash.workdash as workdash_module
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.repo_worktree import worktree_path
from workdash.workdash import _print_work_items

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
    monkeypatch.setattr(workdash_module, "ensure_worktree", fake_ensure_worktree)
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

    result = captured_callback["analyze"](item, tool="codex")

    assert ensure_calls == [(_VALID_CONFIG.workdir, item)]
    assert analyze_calls == [(item, "codex")]
    assert result == "/tmp/analysis.md"


def test_main_list_command_does_not_print_loading_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [], {}

        def analyze_item(self, _item, tool="codex"):
            return None

    class FakeApp:
        def __init__(self, **kwargs) -> None:  # pragma: no cover - should not be reached
            raise AssertionError("TUI app should not be constructed for list command")

        def run(self) -> None:  # pragma: no cover - should not be reached
            raise AssertionError("TUI app should not run for list command")

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)

    exit_code = workdash_module.main(["list"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "No work items found.\n"


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


def test_main_exits_with_config_guidance_when_interactive_config_command_is_malformed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
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
    monkeypatch.setenv("ZELLIJ", "0")

    exit_code = workdash_module.main([])

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
    "argv",
    [
        ["info"],
        ["analyze", "owner/repo#ISSUE-1"],
        ["code", "owner/repo#ISSUE-1"],
    ],
)
def test_main_orchestration_commands_report_gh_error_before_selecting_zellij_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: "gh CLI is not installed")
    monkeypatch.setattr(
        workdash_module,
        "_select_workdash_session",
        lambda _session: (_ for _ in ()).throw(AssertionError("unexpected session selection")),
    )

    assert workdash_module.main(argv) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "gh CLI is not installed" in captured.err


def test_main_list_reports_gh_error_before_loading_items(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: "gh CLI is not installed")
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("unexpected config loading")),
    )

    assert workdash_module.main(["list"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "gh CLI is not installed" in captured.err


@pytest.mark.parametrize(
    ("argv", "expected_dispatch"),
    [
        (["list"], "list"),
        (["info"], "info"),
        (["analyze", "owner/repo#ISSUE-1"], "analyze"),
        (["code", "owner/repo#ISSUE-1"], "code"),
    ],
)
def test_main_dispatch_commands_run_gh_preflight_once(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], expected_dispatch: str
) -> None:
    events: list[str] = []

    def fake_preflight():
        events.append("preflight")
        return None

    class FakeCommands:
        def list_items(self, *, json_output: bool) -> int:
            events.append("list")
            return 0

        def info(self, *, session: str | None, json_output: bool, include_all_panes: bool) -> int:
            events.append("info")
            return 0

        def analyze_cli(
            self,
            *,
            target: str,
            agent: str | None,
            session: str | None,
            json_output: bool,
        ) -> int:
            events.append("analyze")
            return 0

        def code_cli(
            self,
            *,
            target: str,
            agent: str | None,
            session: str | None,
            json_output: bool,
        ) -> int:
            events.append("code")
            return 0

    monkeypatch.setattr(workdash_module, "_check_gh_preflight", fake_preflight)
    monkeypatch.setattr(workdash_module, "WorkdashCommands", FakeCommands)

    assert workdash_module.main(argv) == 0

    assert events == ["preflight", expected_dispatch]


def test_main_analyze_human_output_defaults_to_codex_and_runs_shared_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _issue()
    analyze_calls: list[tuple[WorkItem, str]] = []
    ensure_calls: list[tuple[str, WorkItem]] = []

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [item], {}

        def analyze_item(self, item, tool="codex"):
            analyze_calls.append((item, tool))
            return None if tool == "cached" else "/tmp/analysis.md"

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda workdir, item: ensure_calls.append((workdir, item)) or "/tmp/worktree",
    )

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1"]) == 0

    output = capsys.readouterr().out
    assert "Item: owner/repo#ISSUE-1" in output
    assert "Agent: codex" in output
    assert "Status: generated" in output
    assert "Path: /tmp/analysis.md" in output
    assert analyze_calls == [(item, "cached"), (item, "codex")]
    assert ensure_calls == [(_VALID_CONFIG.workdir, item)]


def test_main_analyze_reuses_cache_for_github_url_without_worktree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _issue()
    item.url = "https://github.com/owner/repo/issues/1"
    item.analysis = "cached analysis"
    analyze_calls: list[tuple[WorkItem, str]] = []

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

        def analyze_item(self, item, tool="codex"):
            analyze_calls.append((item, tool))
            return "/tmp/cached.md" if tool == "cached" else None

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda _workdir, _item: (_ for _ in ()).throw(AssertionError("unexpected worktree")),
    )

    assert workdash_module.main(["--json", "analyze", item.url]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["item_id"] == "owner/repo#ISSUE-1"
    assert payload["path"] == "/tmp/cached.md"
    assert payload["agent"] == "codex"
    assert payload["cache_used"] is True
    assert payload["status"] == "cached"
    assert analyze_calls == [(item, "cached")]


def test_main_analyze_rejects_explicit_unsupported_agent_before_cached_result(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _issue()
    item.analysis = "cached analysis"

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

        def analyze_item(self, item, tool="codex"):  # pragma: no cover - should not run
            raise AssertionError("unsupported explicit agent should fail before cache lookup")

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda _workdir, _item: (_ for _ in ()).throw(AssertionError("unexpected worktree")),
    )

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--agent", "vscode"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Analysis agent 'vscode' is not configured" in captured.err


def test_main_analyze_accepts_selected_agent_when_unrelated_agent_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        codex=AgentConfig(analyze="codex exec"),
        repositories=("owner/repo",),
        workdir="~/wrk",
    )
    analyze_calls: list[tuple[WorkItem, str]] = []

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [item], {}

        def analyze_item(self, item, tool="codex"):
            analyze_calls.append((item, tool))
            return None if tool == "cached" else "/tmp/analysis.md"

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "ensure_worktree", lambda _workdir, _item: "/tmp/wt")

    assert (
        workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--agent", "codex", "--json"]) == 0
    )

    assert json.loads(capsys.readouterr().out)["agent"] == "codex"
    assert analyze_calls == [(item, "cached"), (item, "codex")]


def test_main_analyze_requires_explicit_session_when_multiple_are_active(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(
        workdash_module, "list_workdash_sessions", lambda: ["workdash-a", "workdash-b"]
    )
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("configuration should not load")),
    )

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1"]) == 1

    captured = capsys.readouterr()
    assert "Multiple Workdash-owned Zellij sessions" in captured.err
    assert "--session" in captured.err


def test_main_analyze_reports_malformed_agent_command_as_config_error_before_items(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex 'broken", launch="codex"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir="~/wrk",
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:  # pragma: no cover - should not run
            raise AssertionError("backend should not load invalid runtime config")

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda _workdir, _item: (_ for _ in ()).throw(AssertionError("unexpected worktree")),
    )

    assert workdash_module.main(["analyze", "owner/repo#ISSUE-1", "--agent", "codex"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error: invalid configuration fields")
    assert "agents.codex.analyze" in captured.err
    assert "No closing quotation" in captured.err
    assert "workdash --configure" in captured.err
    assert "Traceback" not in captured.err


def test_main_analyze_rejects_targets_outside_current_items(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [_issue(number=2)], {}

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)

    assert workdash_module.main(["analyze", "https://github.com/owner/repo/issues/1"]) == 1

    captured = capsys.readouterr()
    assert "current Workdash item ID or GitHub issue/PR URL" in captured.err


def test_main_code_reports_malformed_agent_command_as_config_error_before_items(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex 'broken"),
        pi=AgentConfig(launch="pi"),
        repositories=("owner/repo",),
        workdir="~/wrk",
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:  # pragma: no cover - should not run
            raise AssertionError("backend should not load invalid runtime config")

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda _workdir, _item: (_ for _ in ()).throw(AssertionError("unexpected worktree")),
    )

    assert workdash_module.main(["code", "owner/repo#ISSUE-1", "--agent", "codex"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Error: invalid configuration fields")
    assert "agents.codex.launch" in captured.err
    assert "No closing quotation" in captured.err
    assert "workdash --configure" in captured.err


def test_main_code_json_launches_selected_agent_through_shared_action(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _issue()
    item.url = "https://github.com/owner/repo/issues/1"
    ensure_calls: list[tuple[str, WorkItem]] = []
    zellij_commands: list[list[str]] = []

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.analysis_cache = type(
                "_C", (), {"build_analysis_path": staticmethod(lambda _i: "/tmp/analysis.md")}
            )()

        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [item], {}

    def fake_run(*args, **kwargs):
        command = args[0]
        zellij_commands.append(command)
        if command[-5:] == ["--json", "--all", "--command", "--state", "--tab"]:
            stdout = (
                '[{"id": 1, "title": "workdash"}]'
                if len(zellij_commands) == 1
                else '[{"id": 1, "title": "workdash"}, '
                '{"id": 23, "title": "code_owner_repo_1", "pane_cwd": "/tmp/owner_repo_1"}]'
            )
            return subprocess.CompletedProcess(args=command, returncode=0, stdout=stdout, stderr="")
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        workdash_module, "list_workdash_sessions", lambda: ["workdash-main", "workdash-alt"]
    )
    monkeypatch.setattr(workdash_module.shutil, "which", lambda _cmd: "/usr/bin/zellij")
    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda workdir, item: ensure_calls.append((workdir, item)) or "/tmp/owner_repo_1",
    )
    monkeypatch.setattr(workdash_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(
        workdash_module, "prepare_launch_agent_prompt", lambda *args, **kwargs: "PROMPT"
    )

    assert (
        workdash_module.main(
            ["code", item.url, "--agent", "codex", "--session", "workdash-main", "--json"]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": "codex",
        "cwd": "/tmp/owner_repo_1",
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_23",
    }
    assert ensure_calls == [(_VALID_CONFIG.workdir, item)]
    assert zellij_commands == [
        [
            "/usr/bin/zellij",
            "--session",
            "workdash-main",
            "action",
            "list-panes",
            "--json",
            "--all",
            "--command",
            "--state",
            "--tab",
        ],
        [
            "/usr/bin/zellij",
            "--session",
            "workdash-main",
            "action",
            "new-pane",
            "--name",
            "code_owner_repo_1",
            "--cwd",
            "/tmp/owner_repo_1",
            "--",
            "/bin/bash",
            "-ic",
            "codex PROMPT",
        ],
        [
            "/usr/bin/zellij",
            "--session",
            "workdash-main",
            "action",
            "list-panes",
            "--json",
            "--all",
            "--command",
            "--state",
            "--tab",
        ],
    ]


def test_main_code_human_output_reports_launch_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _issue()

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.analysis_cache = type(
                "_C", (), {"build_analysis_path": staticmethod(lambda _i: "/tmp/analysis.md")}
            )()

        def load_items(self, progress_callback=None):
            return [item], {}

    monkeypatch.setattr(
        workdash_module, "_select_workdash_session", lambda _session: "workdash-main"
    )
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "ensure_worktree", lambda _workdir, _item: "/tmp/wt")
    monkeypatch.setattr(workdash_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(
        workdash_module, "prepare_launch_agent_prompt", lambda *args, **kwargs: "PROMPT"
    )
    monkeypatch.setattr(
        workdash_module,
        "launch_agent_context",
        lambda *args, **kwargs: SimpleNamespace(
            session="workdash-main",
            pane_id="terminal_7",
            pane_title="code_wt",
            cwd="/tmp/wt",
        ),
    )

    assert workdash_module.main(["code", "owner/repo#ISSUE-1", "--agent", "pi"]) == 0

    output = capsys.readouterr().out
    assert "Item: owner/repo#ISSUE-1" in output
    assert "Agent: pi" in output
    assert "Session: workdash-main" in output
    assert "Cwd: /tmp/wt" in output
    assert "Pane title: code_wt" in output
    assert "Pane id: terminal_7" in output


def test_main_code_requires_explicit_session_when_multiple_are_active(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(
        workdash_module, "list_workdash_sessions", lambda: ["workdash-a", "workdash-b"]
    )
    monkeypatch.setattr(
        workdash_module,
        "load_config",
        lambda: (_ for _ in ()).throw(AssertionError("configuration should not load")),
    )

    assert workdash_module.main(["code", "owner/repo#ISSUE-1"]) == 1

    captured = capsys.readouterr()
    assert "Multiple Workdash-owned Zellij sessions" in captured.err
    assert "--session" in captured.err


def test_main_code_rejects_non_terminal_agent_before_worktree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):  # pragma: no cover - should not run
            raise AssertionError("items should not load for unsupported agent")

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
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


def test_main_info_json_maps_live_pane_cwd_to_current_work_item_not_title(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )
    load_calls = 0

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            assert config is not None

        def load_items(self, progress_callback=None):
            nonlocal load_calls
            load_calls += 1
            assert progress_callback is None
            return [item], {}

    known_cwd = str(worktree_path(config.workdir, item.repo, item.number))
    worktree_path(config.workdir, item.repo, item.number).mkdir(parents=True)
    _git_origin_proves(monkeypatch, known_cwd, item.repo)
    pane_cwd = f"{known_cwd}/subdir"
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {
                "id": 1,
                "title": "code_owner_repo_999",
                "pane_cwd": pane_cwd,
                "pane_command": "pi",
                "tab_id": 7,
                "tab_name": "work",
                "is_focused": True,
                "is_floating": False,
                "exited": False,
            },
            {
                "id": 2,
                "title": "terminal_owner_repo_1",
                "pane_cwd": str(tmp_path / "other"),
                "terminal_command": "bash",
            },
        ],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    assert load_calls == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"] == "workdash"
    assert payload["panes"][0]["title"] == "code_owner_repo_999"
    assert payload["panes"][0]["item"] == "owner/repo#ISSUE-1"
    assert payload["panes"][0]["cwd"] == pane_cwd
    assert payload["panes"][0]["state"] == "running"
    assert payload["panes"][0]["tab_name"] == "work"
    assert payload["panes"][0]["focused"] is True
    assert payload["panes"][1]["title"] == "terminal_owner_repo_1"
    assert payload["panes"][1]["item"] == "unknown"


def test_main_info_json_does_not_map_unproven_planned_worktree_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    guessed_cwd = worktree_path(config.workdir, item.repo, item.number)
    guessed_cwd.mkdir(parents=True)

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [{"id": 1, "title": "code_owner_repo_1", "pane_cwd": str(guessed_cwd)}],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["panes"][0]["item"] == "unknown"


def test_pane_info_uses_longest_worktree_root_for_descendant_cwd() -> None:
    pane = {
        "id": 1,
        "title": "code_owner_repo_1",
        "pane_cwd": "/worktree/nested/subdir",
    }

    result = workdash_module._pane_info(
        selected_session="workdash",
        pane=pane,
        item_by_cwd={
            "/worktree": "owner/repo#ISSUE-1",
            "/worktree/nested": "owner/repo#ISSUE-2",
        },
    )

    assert result["item"] == "owner/repo#ISSUE-2"


def test_main_info_json_maps_symlinked_workdir_to_resolved_pane_cwd(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    physical_workdir = tmp_path / "physical"
    symlink_workdir = tmp_path / "symlink"
    worktree = worktree_path(str(physical_workdir), item.repo, item.number)
    worktree.mkdir(parents=True)
    symlink_workdir.symlink_to(physical_workdir, target_is_directory=True)
    _git_origin_proves(
        monkeypatch,
        worktree_path(str(symlink_workdir), item.repo, item.number),
        item.repo,
    )
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(symlink_workdir),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {
                "id": 1,
                "title": "code_owner_repo_1",
                "pane_cwd": str(worktree.resolve()),
            }
        ],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["panes"][0]["item"] == "owner/repo#ISSUE-1"


def test_main_info_json_rejects_unrelated_same_name_pr_origin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    created_at = datetime(2026, 2, 1, tzinfo=UTC)
    item = WorkItem(
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=42,
        title="Review title",
        created_at=created_at,
        updated_at=created_at,
        url="https://example.com/42",
    )
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    unrelated_cwd = tmp_path / "other_repo_42"
    unrelated_cwd.mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            if str(kwargs.get("cwd")) == str(unrelated_cwd):
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{unrelated_cwd}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/repo.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {
                "id": 1,
                "title": "code_other_repo_42",
                "pane_cwd": str(unrelated_cwd),
            }
        ],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["panes"][0]["item"] == "unknown"


def test_main_info_json_maps_fork_pr_worktree_cwd_to_current_work_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    created_at = datetime(2026, 2, 1, tzinfo=UTC)
    item = WorkItem(
        kind=WorkItemKind.REVIEW_REQUESTED_PR,
        item_type=WorkItemType.PR,
        repo="owner/repo",
        number=42,
        title="Review title",
        created_at=created_at,
        updated_at=created_at,
        url="https://example.com/42",
    )
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    fork_cwd = tmp_path / "contributor_repo-fork_42"
    fork_cwd.mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            if str(kwargs.get("cwd")) == str(fork_cwd):
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{fork_cwd}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/contributor/repo-fork.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(workdash_module.subprocess, "run", fake_run)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {
                "id": 1,
                "title": "code_contributor_repo-fork_42",
                "pane_cwd": str(fork_cwd),
            }
        ],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["panes"][0]["item"] == "owner/repo#REVIEW-42"


def test_main_info_human_output_shows_mapped_and_unknown_items(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    known_cwd = str(worktree_path(config.workdir, item.repo, item.number))
    worktree_path(config.workdir, item.repo, item.number).mkdir(parents=True)
    _git_origin_proves(monkeypatch, known_cwd, item.repo)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {"id": 1, "title": "code_owner_repo_1", "pane_cwd": known_cwd},
            {"id": 2, "title": "terminal_owner_repo_999", "pane_cwd": str(tmp_path / "other")},
        ],
    )

    assert workdash_module.main(["info"]) == 0

    output = capsys.readouterr().out
    assert "Session: workdash" in output
    assert "item=owner/repo#ISSUE-1" in output
    assert "item=unknown" in output


def test_main_info_json_default_excludes_ordinary_panes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    known_cwd = str(worktree_path(config.workdir, item.repo, item.number))
    worktree_path(config.workdir, item.repo, item.number).mkdir(parents=True)
    _git_origin_proves(monkeypatch, known_cwd, item.repo)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {"id": 1, "title": "code_owner_repo_1", "pane_cwd": known_cwd},
            {"id": 2, "title": "shell", "pane_cwd": known_cwd},
        ],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [pane["title"] for pane in payload["panes"]] == ["code_owner_repo_1"]
    assert payload["panes"][0]["kind"] == "agent"
    assert payload["panes"][0]["item"] == "owner/repo#ISSUE-1"


def test_main_info_all_json_includes_ordinary_live_non_plugin_panes_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    known_cwd = str(worktree_path(config.workdir, item.repo, item.number))
    worktree_path(config.workdir, item.repo, item.number).mkdir(parents=True)
    _git_origin_proves(monkeypatch, known_cwd, item.repo)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {"id": 1, "title": "code_owner_repo_1", "pane_cwd": known_cwd},
            {
                "id": 2,
                "title": "shell",
                "pane_cwd": known_cwd,
                "pane_command": "bash",
                "tab_id": 7,
                "tab_name": "scratch",
                "exited": False,
                "is_plugin": False,
            },
            {"id": 3, "title": "old-shell", "pane_cwd": known_cwd, "exited": True},
            {
                "id": 4,
                "title": "held-shell",
                "pane_cwd": known_cwd,
                "is_held": True,
                "exited": False,
            },
            {"id": 5, "title": "status", "pane_cwd": known_cwd, "is_plugin": True},
        ],
    )

    assert workdash_module.main(["info", "--all", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [pane["title"] for pane in payload["panes"]] == ["code_owner_repo_1", "shell"]
    assert payload["panes"][0]["kind"] == "agent"
    assert payload["panes"][0]["item"] == "owner/repo#ISSUE-1"
    assert payload["panes"][1]["kind"] == "unknown"
    assert payload["panes"][1]["item"] == "unknown"
    assert payload["panes"][1]["cwd"] == known_cwd
    assert payload["panes"][1]["command"] == "bash"
    assert payload["panes"][1]["tab_name"] == "scratch"


def test_main_info_json_excludes_exited_or_held_workdash_panes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    item = _issue()
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [item], {}

    known_cwd = str(worktree_path(config.workdir, item.repo, item.number))
    worktree_path(config.workdir, item.repo, item.number).mkdir(parents=True)
    _git_origin_proves(monkeypatch, known_cwd, item.repo)
    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "load_zellij_panes",
        lambda _session: [
            {"id": 1, "title": "code_owner_repo_1", "pane_cwd": known_cwd},
            {"id": 2, "title": "code_owner_repo_2", "pane_cwd": known_cwd, "exited": True},
            {
                "id": 3,
                "title": "terminal_owner_repo_3",
                "pane_cwd": known_cwd,
                "exited": True,
            },
            {
                "id": 4,
                "title": "terminal_owner_repo_4",
                "pane_cwd": known_cwd,
                "is_held": True,
                "exited": False,
            },
        ],
    )

    assert workdash_module.main(["info", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert [pane["title"] for pane in payload["panes"]] == ["code_owner_repo_1"]


def test_main_info_top_level_json_flag_returns_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    config = WorkdashConfig(
        github_username="testuser",
        claude=AgentConfig(analyze="claude -p", launch="claude"),
        codex=AgentConfig(analyze="codex exec", launch="codex"),
        pi=AgentConfig(launch="pi --no-tips"),
        repositories=("owner/repo",),
        workdir=str(tmp_path),
    )

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [], {}

    monkeypatch.setattr(workdash_module, "_select_workdash_session", lambda _session: "workdash")
    monkeypatch.setattr(workdash_module, "_check_gh_preflight", lambda: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "load_zellij_panes", lambda _session: [])

    assert workdash_module.main(["--json", "info"]) == 0

    assert json.loads(capsys.readouterr().out) == {"session": "workdash", "panes": []}


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
    """_launch must hand the right launch tokens to launch_agent_context for each tool.

    The dispatch table inside main()._launch is the only place where the tool
    string is mapped to the configured launch command, so we capture the
    closure via a FakeApp and exercise it directly.
    """

    captured_callback: dict[str, object] = {}
    agent_calls: list[tuple[str, str, list[str] | None, str | None]] = []
    vscode_calls: list[tuple[str, str]] = []

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.analysis_cache = type(
                "_C", (), {"build_analysis_path": staticmethod(lambda _i: "/x")}
            )()

        def load_items(self, progress_callback=None):
            return [], {}

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
    monkeypatch.setattr(workdash_module, "ensure_worktree", lambda _wd, _item: "/tmp/wt")
    monkeypatch.setattr(workdash_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(
        workdash_module,
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

    monkeypatch.setattr(workdash_module, "launch_agent_context", fake_launch_agent_context)
    monkeypatch.setattr(
        workdash_module,
        "launch_vscode_context",
        lambda repo, prompt: vscode_calls.append((repo, prompt)),
    )
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

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
    captured_callback["launch"](item, tool)

    assert agent_calls == [("/tmp/wt", "PROMPT", expected_tokens, None)]
    assert vscode_calls == []


def test_main_launch_callback_raises_on_unknown_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_launch must surface an explicit error for unrecognized tool strings.

    A silent fallback to codex would mask wiring bugs in the TUI; the
    explicit ValueError ensures the failure is visible via the launch
    error path instead.
    """

    captured_callback: dict[str, object] = {}

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.analysis_cache = type(
                "_C", (), {"build_analysis_path": staticmethod(lambda _i: "/x")}
            )()

        def load_items(self, progress_callback=None):
            return [], {}

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
    monkeypatch.setattr(workdash_module, "ensure_worktree", lambda _wd, _item: "/tmp/wt")
    monkeypatch.setattr(workdash_module, "get_merge_base", lambda _path: None)
    monkeypatch.setattr(
        workdash_module,
        "prepare_launch_agent_prompt",
        lambda *args, **kwargs: "PROMPT",
    )
    monkeypatch.setattr(
        workdash_module,
        "launch_agent_context",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setenv("ZELLIJ", "0")

    assert workdash_module.main([]) == 0

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

    with pytest.raises(ValueError, match="Unsupported coding agent"):
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
