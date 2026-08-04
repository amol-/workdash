import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from workdash.analyzer import Analyzer
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
        title="Implement analysis engine",
        created_at=datetime(2026, 2, 20, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 2, 21, 10, 0, 0, tzinfo=UTC),
        url="https://github.com/owner/repo/pull/42",
    )


def test_analyze_for_pr_writes_diff_to_tempfile_and_injects_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    captured: dict[str, str] = {}

    diff_text = "diff --git a/foo.py b/foo.py\n+print('hello')\n"

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append((command, kwargs))
        if command[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=(
                    '{"number":42,"title":"Implement analysis engine",'
                    '"body":"Body text","state":"OPEN"}'
                ),
                stderr="",
            )
        if command[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout=diff_text,
                stderr="",
            )
        # Analysis runs through $SHELL -ic "codex exec '<prompt>'"
        assert command[1] == "-ic"
        shell_cmd = command[2]
        assert shell_cmd.startswith("codex exec ")
        assert "reviewing a GitHub pull request" in shell_cmd
        assert "well-formed CommonMark Markdown" in shell_cmd
        assert "Markdown link that points to the exact definition line on GitHub" in shell_cmd
        assert "headRefName" in shell_cmd
        # The diff content must NOT be embedded in the argv — that's what blew
        # past the kernel's per-argv-string limit. Only the temp file path is.
        assert "+print('hello')" not in shell_cmd
        # The prompt must mention the temp file, and that file must exist with
        # the full diff in it while the agent is running.
        assert "workdash-owner-repo-42-" in shell_cmd
        match = re.search(r"/tmp/workdash-owner-repo-42-[^\s\"']+\.diff", shell_cmd)
        assert match is not None, shell_cmd
        diff_path = match.group(0)
        captured["diff_path"] = diff_path
        assert Path(diff_path).read_text(encoding="utf-8") == diff_text
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="## Summary\nLooks good.\n",
            stderr="",
        )

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    output = Analyzer().analyze(make_work_item(WorkItemType.PR))

    assert output == "## Summary\nLooks good."
    assert len(calls) == 3
    assert calls[0][0][:3] == ["gh", "pr", "view"]
    assert calls[1][0][:3] == ["gh", "pr", "diff"]
    assert calls[2][0][:2] == ["/bin/sh", "-ic"]
    # Temp file is cleaned up after the agent returns.
    assert not Path(captured["diff_path"]).exists()


def test_analyze_for_issue_uses_issue_template_and_no_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        if command[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":42,"title":"Issue title","body":"Issue body"}',
                stderr="",
            )
        assert command[1] == "-ic"
        shell_cmd = command[2]
        assert shell_cmd.startswith("codex exec ")
        assert "analyzing a GitHub issue" in shell_cmd
        assert "well-formed CommonMark Markdown" in shell_cmd
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="## Summary\nNeeds investigation.\n",
            stderr="",
        )

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    output = Analyzer().analyze(make_work_item(WorkItemType.ISSUE))

    assert output == "## Summary\nNeeds investigation."
    assert len(calls) == 2
    assert calls[0][:2] == ["gh", "issue"]
    assert calls[1][:2] == ["/bin/sh", "-ic"]


def test_analyze_for_a_targeted_todo_links_sources_in_the_target_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Source links point at the code analyzed, while the item stays the todo issue."""

    item = make_work_item(WorkItemType.ISSUE, kind=WorkItemKind.ASSIGNED_ISSUE)
    item.repo = "testuser/todos"
    item.number = 110
    item.url = "https://github.com/testuser/todos/issues/110"
    item.todo_target = "owner/repo"
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        if command[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":110,"title":"Fix the flaky test","body":""}',
                stderr="",
            )
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="# done\n", stderr="")

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert Analyzer().analyze(item) == "# done"
    # Context still comes from the todo issue, which lives in the todo repository.
    assert "testuser/todos" in calls[0]
    prompt = calls[1][2]
    assert "https://github.com/owner/repo/blob/main/" in prompt
    assert "https://github.com/testuser/todos/blob" not in prompt
    # The analysis header must name the issue it was written from.
    assert "[testuser/todos#110](https://github.com/testuser/todos/issues/110)" in prompt
    assert "- repo: testuser/todos" in prompt


def test_analyze_for_review_requested_pr_also_fetches_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":42,"title":"Review me","body":"Body text","state":"OPEN"}',
                stderr="",
            )
        if command[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="diff --git a/foo.py b/foo.py\n+print('hello')\n",
                stderr="",
            )
        assert command[1] == "-ic"
        shell_cmd = command[2]
        assert "reviewing a GitHub pull request" in shell_cmd
        # Diff is passed via a temp file path, not embedded in the prompt.
        assert "+print('hello')" not in shell_cmd
        assert "workdash-owner-repo-42-" in shell_cmd
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="## Summary\nReview completed.\n",
            stderr="",
        )

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    output = Analyzer().analyze(
        make_work_item(
            WorkItemType.PR,
            kind=WorkItemKind.REVIEW_REQUESTED_PR,
        )
    )

    assert output == "## Summary\nReview completed."
    assert calls[0][:3] == ["gh", "pr", "view"]
    assert calls[1][:3] == ["gh", "pr", "diff"]
    assert calls[2][:2] == ["/bin/sh", "-ic"]


def test_analyze_uses_caller_supplied_agent_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        command = args[0]
        calls.append(command)
        if command[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":42,"title":"Issue title","body":"Issue body"}',
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="## Summary\nok\n",
            stderr="",
        )

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    Analyzer().analyze(
        make_work_item(WorkItemType.ISSUE),
        command_tokens=["codex", "exec", "--model", "gpt-5"],
    )

    shell_cmd = calls[1][2]
    assert shell_cmd.startswith("codex exec --model gpt-5 ")


def test_analyze_returns_none_for_empty_agent_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:2] == ["gh", "issue"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":42,"title":"Issue title","body":"Issue body"}',
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="   \n  ",
            stderr="",
        )

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert Analyzer().analyze(make_work_item(WorkItemType.ISSUE)) is None


def test_analyze_raises_runtime_error_when_gh_context_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:2] == ["gh", "pr"]:
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr="permission denied",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="{}",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to gather gh context for pr owner/repo#42"):
        Analyzer().analyze(make_work_item(WorkItemType.PR))


def test_analyze_raises_runtime_error_when_gh_context_command_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:2] == ["gh", "pr"]:
            raise FileNotFoundError("gh")
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="{}",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        Analyzer().analyze(make_work_item(WorkItemType.PR))


def test_analyze_raises_runtime_error_when_agent_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":42,"title":"Implement analysis engine","body":"Body"}',
                stderr="",
            )
        if command[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="diff content",
                stderr="",
            )
        raise subprocess.CalledProcessError(
            returncode=2,
            cmd=command,
            stderr="codex backend unavailable",
        )

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to generate analysis for pr owner/repo#42"):
        Analyzer().analyze(make_work_item(WorkItemType.PR))


def test_analyze_raises_runtime_error_when_shell_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        command = args[0]
        if command[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout='{"number":42,"title":"Implement analysis engine","body":"Body"}',
                stderr="",
            )
        if command[:3] == ["gh", "pr", "diff"]:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="diff content",
                stderr="",
            )
        raise FileNotFoundError(command[0])

    monkeypatch.setenv("SHELL", "/nonexistent/shell")
    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="shell '/nonexistent/shell' not found"):
        Analyzer().analyze(make_work_item(WorkItemType.PR))
