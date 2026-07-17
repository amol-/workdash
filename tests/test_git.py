import subprocess
from pathlib import Path

import pytest

from workdash.git import GitHelper


def test_worktree_name_preserves_owner_repo_number_shape() -> None:
    assert GitHelper().worktree_name("owner/repo", 42) == "owner_repo_42"


def test_repo_from_remote_url_accepts_https_and_ssh_forms() -> None:
    assert GitHelper.repo_from_remote_url("https://github.com/owner/repo.git") == "owner/repo"
    assert GitHelper.repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"


def test_fetch_remote_runs_git_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHelper().fetch_remote(tmp_path, "upstream")

    assert calls == [
        (
            ["git", "fetch", "upstream"],
            {"cwd": tmp_path, "check": True, "capture_output": True, "text": True},
        )
    ]


def test_fetch_remote_reports_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr="no such remote")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to fetch upstream: no such remote"):
        GitHelper().fetch_remote(tmp_path, "upstream")


def test_fast_forward_default_branch_merges_fetched_origin_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHelper().fast_forward_default_branch(tmp_path)

    assert calls == [
        (
            ["git", "merge", "--ff-only", "origin/HEAD"],
            {"cwd": tmp_path, "check": True, "capture_output": True, "text": True},
        )
    ]
