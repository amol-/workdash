import json
import subprocess
from datetime import UTC, datetime

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


def test_main_does_not_print_loading_message_in_print_mode(
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
            raise AssertionError("TUI app should not be constructed in --print mode")

        def run(self) -> None:  # pragma: no cover - should not be reached
            raise AssertionError("TUI app should not run in --print mode")

    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(workdash_module, "WorkdashApp", FakeApp)

    exit_code = workdash_module.main(["--print"])

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


def test_select_workdash_session_treats_no_zellij_sessions_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(workdash_module, "list_workdash_sessions", lambda: [])

    with pytest.raises(RuntimeError, match="active Workdash-owned Zellij session is required"):
        workdash_module._select_workdash_session(None)


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


def test_main_info_json_excludes_exited_workdash_panes(
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

    exit_code = workdash_module.main(["--configure"])

    assert exit_code == 0
    assert configure_called


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


def test_main_print_mode_bypasses_zellij_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_called = False

    def fake_execvp(_file: str, _args: list[str]) -> None:
        nonlocal exec_called
        exec_called = True

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            pass

        def load_items(self, progress_callback=None):
            return [], {}

    monkeypatch.delenv("ZELLIJ", raising=False)
    monkeypatch.setattr(workdash_module.shutil, "which", lambda cmd: "/usr/bin/gh")
    _auth_status_succeeds(monkeypatch)
    monkeypatch.setattr(workdash_module.os, "execvp", fake_execvp)
    monkeypatch.setattr(workdash_module, "load_config", lambda: _VALID_CONFIG)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)

    assert workdash_module.main(["--print"]) == 0
    assert exec_called is False


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
    agent_calls: list[tuple[str, str, list[str] | None]] = []
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
    monkeypatch.setattr(
        workdash_module,
        "launch_agent_context",
        lambda repo, prompt, agent_command_tokens=None: agent_calls.append(
            (repo, prompt, agent_command_tokens)
        ),
    )
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

    assert agent_calls == [("/tmp/wt", "PROMPT", expected_tokens)]
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
