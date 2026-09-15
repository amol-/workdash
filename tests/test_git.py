import subprocess
from pathlib import Path

import pytest

from workdash.git import GitHelper, get_current_branch, get_repo_root


def test_worktree_name_preserves_owner_repo_number_shape() -> None:
    assert GitHelper().worktree_name("owner/repo", 42) == "owner_repo_42"


def test_get_repo_root_resolves_from_a_subdirectory(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    nested_dir = tmp_path / "pkg" / "module"
    nested_dir.mkdir(parents=True)

    assert get_repo_root(nested_dir) == tmp_path.resolve()


def test_get_repo_root_rejects_a_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Not a git repository."):
        get_repo_root(tmp_path)


def test_get_current_branch_returns_the_checked_out_branch(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"], cwd=tmp_path, check=True
    )

    assert get_current_branch(tmp_path) == "feature"


def test_get_current_branch_raises_when_git_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Failed to determine the current branch."):
        get_current_branch(tmp_path)


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
            ["git", "fetch", "--prune", "upstream"],
            {"cwd": tmp_path, "check": True, "capture_output": True, "text": True},
        )
    ]


def test_fetch_remote_reports_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr="no such remote")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to fetch upstream: no such remote"):
        GitHelper().fetch_remote(tmp_path, "upstream")


def test_fast_forward_default_branch_switches_to_default_branch_before_merging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        stdout = "origin/main\n" if args[0][1] == "rev-parse" else ""
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHelper().fast_forward_default_branch(tmp_path)

    expected_kwargs = {"cwd": tmp_path, "check": True, "capture_output": True, "text": True}
    assert calls == [
        (["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], expected_kwargs),
        (["git", "switch", "main"], expected_kwargs),
        (["git", "merge", "--ff-only", "origin/HEAD"], expected_kwargs),
    ]


def test_fast_forward_default_branch_reports_git_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr="not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError, match="Failed to fast-forward the default branch: not a git repository"
    ):
        GitHelper().fast_forward_default_branch(tmp_path)
