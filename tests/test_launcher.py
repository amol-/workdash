import shutil
import subprocess
from datetime import UTC, datetime

import pytest

import workdash.launcher as launcher_module
from workdash.launcher import (
    build_launch_agent_prompt,
    exec_zellij_wrapped_workdash,
    launch_agent_context,
    launch_terminal_context,
    launch_vscode_context,
    open_in_browser,
    open_markdown,
    prepare_launch_agent_prompt,
)
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def make_work_item(
    item_type: WorkItemType,
    *,
    kind: WorkItemKind | None = None,
) -> WorkItem:
    return WorkItem(
        kind=(
            kind
            if kind is not None
            else (
                WorkItemKind.AUTHORED_PR
                if item_type == WorkItemType.PR
                else WorkItemKind.TRACKED_ISSUE
            )
        ),
        item_type=item_type,
        repo="owner/repo",
        number=42,
        title="Implement launch prompt",
        created_at=datetime(2026, 2, 20, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 21, 10, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/42",
    )


def test_open_in_browser_runs_xdg_open_for_valid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        captured["check"] = kwargs.get("check")
        captured["capture_output"] = kwargs.get("capture_output")
        captured["text"] = kwargs.get("text")
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None
    )

    open_in_browser("https://example.com/issues/11")

    assert captured["command"] == ["xdg-open", "https://example.com/issues/11"]
    assert captured["check"] is True
    assert captured["timeout"] == 4


def test_open_in_browser_uses_open_when_xdg_open_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/open" if name == "open" else None,
    )

    open_in_browser("https://example.com/issues/11")

    assert captured["command"] == ["open", "https://example.com/issues/11"]


def test_open_in_browser_rejects_empty_url() -> None:
    with pytest.raises(ValueError, match="URL must be a non-empty string"):
        open_in_browser("   ")


def test_open_in_browser_raises_clear_error_when_no_browser_open_command_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="Neither xdg-open nor open is installed or on PATH"):
        open_in_browser("https://example.com/issues/11")


def test_open_in_browser_raises_clear_error_when_xdg_open_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="cannot open display",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None
    )

    with pytest.raises(RuntimeError, match="Failed to open URL via xdg-open: cannot open display"):
        open_in_browser("https://example.com/issues/11")


def test_open_in_browser_times_out_when_browser_command_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        shutil, "which", lambda name: f"/usr/bin/{name}" if name == "xdg-open" else None
    )

    with pytest.raises(RuntimeError, match="Opening a browser may not be supported"):
        open_in_browser("https://example.com/issues/11")


def test_open_markdown_uses_open_when_xdg_open_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}
    markdown_path = tmp_path / "analysis.md"
    markdown_path.write_text("# Analysis\n", encoding="utf-8")

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name: "/usr/bin/open" if name == "open" else None,
    )

    open_markdown(str(markdown_path))

    assert captured["command"] == ["open", str(markdown_path.with_suffix(".html"))]


def test_workdash_local_bin_is_appended_to_path_without_displacing_global_bins() -> None:
    existing_path = f"/usr/local/bin{launcher_module.os.pathsep}/usr/bin"

    updated_path = launcher_module._path_with_workdash_local_bin(existing_path)

    assert updated_path.startswith(existing_path)
    assert updated_path.endswith(str(launcher_module._WORKDASH_LOCAL_BIN))
    assert launcher_module._path_with_workdash_local_bin(updated_path) == updated_path


def test_launch_agent_context_uses_new_zellij_pane_when_in_zellij(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        captured["check"] = kwargs.get("check")
        captured["capture_output"] = kwargs.get("capture_output")
        captured["text"] = kwargs.get("text")
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zellij" if name == "zellij" else None
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_agent_context("/tmp/amol-_repoze.who_52", "review this change")

    assert captured["command"] == [
        "/usr/bin/zellij",
        "action",
        "new-pane",
        "--name",
        "code_amol-_repoze.who_52",
        "--cwd",
        "/tmp/amol-_repoze.who_52",
        "--",
        "/bin/bash",
        "-ic",
        "codex 'review this change'",
    ]
    assert captured["check"] is True
    assert captured["capture_output"] is True
    assert captured["text"] is True


def test_launch_agent_context_raises_clear_error_when_zellij_launch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="pane launch failed",
        )

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zellij" if name == "zellij" else None
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError, match="Failed to launch coding agent in zellij: pane launch failed"
    ):
        launch_agent_context("/tmp/repo", "review this change")


def test_launch_terminal_context_uses_new_zellij_pane_when_in_zellij(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*args, **kwargs):
        captured["command"] = args[0]
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zellij" if name == "zellij" else None
    )
    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_terminal_context("/tmp/amol-_repoze.who_52")

    assert captured["command"] == [
        "/usr/bin/zellij",
        "action",
        "new-pane",
        "--name",
        "terminal_amol-_repoze.who_52",
        "--cwd",
        "/tmp/amol-_repoze.who_52",
        "--",
        "/bin/bash",
        "-i",
    ]


def test_launch_terminal_context_raises_clear_error_when_zellij_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZELLIJ", "0")
    monkeypatch.setattr(shutil, "which", lambda name: None)

    with pytest.raises(RuntimeError, match="zellij is not installed or not configured"):
        launch_terminal_context("/tmp/repo")


def test_launch_terminal_context_requires_active_zellij_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZELLIJ", raising=False)

    with pytest.raises(RuntimeError, match="require an active Zellij session"):
        launch_terminal_context("/tmp/repo")


def test_launch_agent_context_requires_active_zellij_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ZELLIJ", raising=False)

    with pytest.raises(RuntimeError, match="require an active Zellij session"):
        launch_agent_context("/tmp/repo", "review this change")


def test_exec_zellij_wrapped_workdash_replaces_process_with_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_calls: list[tuple[str, list[str]]] = []

    def fake_which(command: str) -> str | None:
        if command == "zellij":
            return "/usr/bin/zellij"
        return None

    def fake_execvp(file: str, args: list[str]) -> None:
        exec_calls.append((file, args))
        raise SystemExit(0)

    monkeypatch.setattr(shutil, "which", fake_which)
    monkeypatch.setattr(launcher_module.os, "execvp", fake_execvp)
    monkeypatch.setattr(launcher_module.secrets, "token_hex", lambda length: "abc123ef")
    monkeypatch.setattr(launcher_module.sys, "argv", ["/usr/local/bin/workdash"])
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/nu")

    with pytest.raises(SystemExit):
        exec_zellij_wrapped_workdash(["--refresh"])

    assert exec_calls == [
        (
            "/usr/bin/zellij",
            [
                "/usr/bin/zellij",
                "--layout",
                exec_calls[0][1][2],
            ],
        )
    ]
    with open(exec_calls[0][1][2], encoding="utf-8") as layout_file:
        layout = layout_file.read()
    assert 'session_name "workdash-abc123ef"' in layout
    assert 'on_force_close "quit"' in layout
    assert "session_serialization false" in layout
    assert "disable_session_metadata true" in layout
    assert "show_startup_tips false" in layout
    assert "attach_to_session false" in layout
    assert 'tab name="workdash"' in layout
    assert 'pane command="/usr/local/bin/workdash" close_on_exit=true' in layout
    assert 'args "--direct" "--refresh"' in layout
    assert "kill-session" not in layout
    assert "trap " not in layout
    assert "compact-bar" not in layout


def test_exec_zellij_wrapped_workdash_preserves_module_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_calls: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: "/usr/bin/zellij" if command == "zellij" else None,
    )
    monkeypatch.setattr(
        launcher_module.os,
        "execvp",
        lambda file, args: exec_calls.append((file, args)) or (_ for _ in ()).throw(SystemExit(0)),
    )
    monkeypatch.setattr(launcher_module.secrets, "token_hex", lambda length: "abc123ef")
    monkeypatch.setenv("SHELL", "/bin/bash")
    monkeypatch.setattr(
        launcher_module.sys,
        "argv",
        ["/repo/src/workdash/__main__.py"],
    )
    monkeypatch.setattr(launcher_module.sys, "executable", "/usr/bin/python3.12")

    with pytest.raises(SystemExit):
        exec_zellij_wrapped_workdash(["--refresh"])

    with open(exec_calls[0][1][2], encoding="utf-8") as layout_file:
        layout = layout_file.read()
    assert 'tab name="workdash"' in layout
    assert 'pane command="/usr/bin/python3.12" close_on_exit=true' in layout
    assert 'args "-m" "workdash" "--direct" "--refresh"' in layout
    assert "kill-session" not in layout


def test_exec_zellij_wrapped_workdash_preserves_workdash_module_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exec_calls: list[tuple[str, list[str]]] = []

    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: "/usr/bin/zellij" if command == "zellij" else None,
    )
    monkeypatch.setattr(
        launcher_module.os,
        "execvp",
        lambda file, args: exec_calls.append((file, args)) or (_ for _ in ()).throw(SystemExit(0)),
    )
    monkeypatch.setattr(launcher_module.secrets, "token_hex", lambda length: "abc123ef")
    monkeypatch.setattr(
        launcher_module.sys,
        "argv",
        ["/repo/src/workdash/workdash.py"],
    )
    monkeypatch.setattr(launcher_module.sys, "executable", "/usr/bin/python3.12")
    monkeypatch.setenv("SHELL", "/bin/bash")

    with pytest.raises(SystemExit):
        exec_zellij_wrapped_workdash(["--refresh"])

    with open(exec_calls[0][1][2], encoding="utf-8") as layout_file:
        layout = layout_file.read()
    assert 'pane command="/usr/bin/python3.12" close_on_exit=true' in layout
    assert 'args "-m" "workdash" "--direct" "--refresh"' in layout


def test_exec_zellij_wrapped_workdash_reports_exec_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: "/usr/bin/zellij" if command == "zellij" else None,
    )
    monkeypatch.setattr(
        launcher_module.os,
        "execvp",
        lambda _file, _args: (_ for _ in ()).throw(OSError("permission denied")),
    )

    with pytest.raises(RuntimeError, match="failed to start zellij: permission denied"):
        exec_zellij_wrapped_workdash([])


def test_exec_zellij_wrapped_workdash_reports_missing_zellij(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)

    with pytest.raises(RuntimeError, match="zellij is not installed or not configured"):
        exec_zellij_wrapped_workdash([])


def test_build_launch_agent_prompt_includes_required_metadata_and_non_start_instruction() -> None:
    prompt = build_launch_agent_prompt(
        item=make_work_item(WorkItemType.PR),
        repo_path="/tmp/repo",
        github_context={
            "state": "OPEN",
            "author": {"login": "testuser"},
            "assignees": [{"login": "dev1"}],
            "labels": [{"name": "bug"}],
            "createdAt": "2026-02-20T10:00:00Z",
            "updatedAt": "2026-02-21T10:00:00Z",
            "isDraft": False,
            "reviewDecision": "REVIEW_REQUIRED",
            "body": "details",
        },
    )

    assert "session for this GitHub pull request" in prompt
    assert "Propose a concrete implementation plan and clarifying questions." in prompt
    assert "Do not start implementing code yet." in prompt
    assert "- type: pr" in prompt
    assert "- repo: owner/repo" in prompt
    assert "- number: 42" in prompt
    assert "- title: Implement launch prompt" in prompt
    assert "- url: https://github.com/owner/repo/pull/42" in prompt
    assert "- expected local checkout path: /tmp/repo" in prompt
    assert '"state": "OPEN"' in prompt
    assert "PREVIOUS ANALYSIS" not in prompt


def test_build_launch_agent_prompt_for_review_requested_pr_is_review_focused() -> None:
    prompt = build_launch_agent_prompt(
        item=make_work_item(
            WorkItemType.PR,
            kind=WorkItemKind.REVIEW_REQUESTED_PR,
        ),
        repo_path="/tmp/repo",
        github_context={
            "state": "OPEN",
            "author": {"login": "testuser"},
            "assignees": [{"login": "dev1"}],
            "labels": [{"name": "bug"}],
            "createdAt": "2026-02-20T10:00:00Z",
            "updatedAt": "2026-02-21T10:00:00Z",
            "isDraft": False,
            "reviewDecision": "REVIEW_REQUIRED",
            "body": "details",
        },
    )

    assert "session to review this GitHub pull request" in prompt
    assert "Discuss review findings, risks, and open questions for the author." in prompt
    assert "Do not start implementing code changes." in prompt
    assert "- kind: review_requested_pr" in prompt


def test_build_launch_agent_prompt_includes_analysis_path_when_provided() -> None:
    prompt = build_launch_agent_prompt(
        item=make_work_item(WorkItemType.PR),
        repo_path="/tmp/repo",
        github_context={"state": "OPEN"},
        analysis_path="/tmp/cache/analyses/owner_repo_PR42.md",
    )
    assert "PREVIOUS ANALYSIS:" in prompt
    assert "/tmp/cache/analyses/owner_repo_PR42.md" in prompt
    assert "Read it before proceeding" in prompt


def test_build_launch_agent_prompt_uses_issue_template_for_issues() -> None:
    prompt = build_launch_agent_prompt(
        item=make_work_item(WorkItemType.ISSUE),
        repo_path="/tmp/repo",
        github_context={"state": "OPEN"},
    )
    assert "session for this GitHub issue" in prompt
    assert "- type: issue" in prompt


def test_prepare_launch_agent_prompt_collects_gh_context_and_builds_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                '{"number":42,"title":"Implement launch prompt","state":"OPEN",'
                '"author":{"login":"testuser"},"assignees":[],"labels":[],'
                '"createdAt":"2026-02-20T10:00:00Z","updatedAt":"2026-02-21T10:00:00Z"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    prompt = prepare_launch_agent_prompt(make_work_item(WorkItemType.PR), "/tmp/repo")

    assert calls == [
        [
            "gh",
            "pr",
            "view",
            "42",
            "--repo",
            "owner/repo",
            "--json",
            (
                "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
                "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName"
            ),
        ]
    ]
    assert "- expected local checkout path: /tmp/repo" in prompt
    assert "Do not start implementing code yet." in prompt
    assert '"state": "OPEN"' in prompt


def test_prepare_launch_agent_prompt_collects_issue_gh_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=(
                '{"number":42,"title":"Implement launch prompt","state":"OPEN",'
                '"author":{"login":"testuser"},"assignees":[],"labels":[],'
                '"createdAt":"2026-02-20T10:00:00Z","updatedAt":"2026-02-21T10:00:00Z"}'
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    prompt = prepare_launch_agent_prompt(make_work_item(WorkItemType.ISSUE), "/tmp/repo")

    assert calls == [
        [
            "gh",
            "issue",
            "view",
            "42",
            "--repo",
            "owner/repo",
            "--json",
            "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt",
        ]
    ]
    assert "- type: issue" in prompt


def test_prepare_launch_agent_prompt_raises_clear_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        prepare_launch_agent_prompt(make_work_item(WorkItemType.PR), "/tmp/repo")


def test_prepare_launch_agent_prompt_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="permission denied",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to gather launch context for pr owner/repo#42"):
        prepare_launch_agent_prompt(make_work_item(WorkItemType.PR), "/tmp/repo")


def test_launch_vscode_context_opens_folder_then_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(*args, **kwargs):
        calls.append(
            {
                "command": args[0],
                "check": kwargs.get("check"),
                "capture_output": kwargs.get("capture_output"),
                "text": kwargs.get("text"),
            }
        )
        return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    launch_vscode_context("/tmp/repo", "explain this codebase")

    assert len(calls) == 2
    assert calls[0]["command"] == ["code", "--new-window", "/tmp/repo"]
    assert calls[0]["check"] is True
    assert calls[0]["capture_output"] is True
    assert calls[0]["text"] is True
    assert calls[1]["command"] == ["code", "chat", "explain this codebase", "--reuse-window"]
    assert calls[1]["check"] is True
    assert calls[1]["capture_output"] is True
    assert calls[1]["text"] is True


def test_launch_vscode_context_raises_when_code_not_on_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("code")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to open VSCode"):
        launch_vscode_context("/tmp/repo", "explain this codebase")


def test_launch_vscode_context_raises_when_chat_command_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0

    def fake_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return subprocess.CompletedProcess(args=args[0], returncode=0, stdout="", stderr="")
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="copilot not available",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to start Copilot chat"):
        launch_vscode_context("/tmp/repo", "explain this codebase")


def test_launch_vscode_context_rejects_empty_repo_path() -> None:
    with pytest.raises(ValueError, match="Repository path must be a non-empty string"):
        launch_vscode_context("  ", "explain this codebase")


def test_launch_vscode_context_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError, match="Prompt must be a non-empty string"):
        launch_vscode_context("/tmp/repo", "  ")
