"""Tests for workdash.repo_worktree — worktree management for work items."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.repo_worktree import (
    _find_worktree_for_branch,
    _get_pr_head_info,
    ensure_worktree,
    existing_worktree_path,
    main_repo_path,
    worktree_path,
)

_SAME_REPO_HEAD_INFO = json.dumps(
    {
        "headRefName": "feature-branch",
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "owner"},
    }
)

_FORK_HEAD_INFO = json.dumps(
    {
        "headRefName": "feature-branch",
        "headRepository": {"name": "repo"},
        "headRepositoryOwner": {"login": "contributor"},
    }
)


def make_pr(number: int = 42, repo: str = "owner/repo") -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.AUTHORED_PR,
        item_type=WorkItemType.PR,
        repo=repo,
        number=number,
        title="Test PR",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url=f"https://github.com/{repo}/pull/{number}",
    )


def make_issue(number: int = 42, repo: str = "owner/repo") -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo=repo,
        number=number,
        title="Test Issue",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url=f"https://github.com/{repo}/issues/{number}",
    )


def _git_show_toplevel(cmd: list[str], cwd: Path | str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout=f"{Path(cwd).resolve()}\n", stderr="")


# --- Pure path tests (no mocking) ---


def test_main_repo_path_uses_owner_underscore_repo() -> None:
    result = main_repo_path("~/wrk", "owner/repo")
    assert result == Path("~/wrk").expanduser() / "owner_repo"


def test_main_repo_path_rejects_invalid_repo() -> None:
    with pytest.raises(ValueError, match="Invalid repo format"):
        main_repo_path("~/wrk", "badformat")


def test_worktree_path_appends_number() -> None:
    result = worktree_path("~/wrk", "owner/repo", 42)
    assert result == Path("~/wrk").expanduser() / "owner_repo_42"


# --- ensure_worktree tests (mock subprocess.run) ---


def test_ensure_worktree_clones_fetches_and_creates_for_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main repo missing, worktree missing, same-repo PR: gh pr view, clone, fetch, worktree add."""
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            (tmp_path / "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "owner_repo_42")
    assert calls[0][:3] == ["gh", "pr", "view"]
    assert calls[1][:3] == ["gh", "repo", "clone"]
    assert calls[2] == ["git", "fetch", "origin"]
    wt_add = [c for c in calls if c[0] == "git" and c[1] == "worktree" and c[2] == "add"][0]
    assert "-b" in wt_add
    assert "feature-branch" in wt_add
    assert "origin/feature-branch" in wt_add
    config_cmds = [c for c in calls if c[0] == "git" and c[1] == "config"]
    assert ["git", "config", "branch.feature-branch.remote", "origin"] in config_cmds
    assert [
        "git",
        "config",
        "branch.feature-branch.merge",
        "refs/heads/feature-branch",
    ] in config_cmds


def test_ensure_worktree_fork_pr_clones_from_fork(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork PR: clone from fork repo (contributor/repo), not base repo (owner/repo)."""
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_FORK_HEAD_INFO, stderr="")
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            # Clones contributor/repo, not owner/repo
            (tmp_path / "contributor_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "contributor_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "contributor_repo_42")
    clone_cmd = [c for c in calls if c[0] == "gh" and c[1] == "repo" and c[2] == "clone"][0]
    assert "contributor/repo" in clone_cmd


def test_ensure_worktree_clones_and_creates_for_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main repo missing, issue item: clone, fetch, worktree add with issue-N branch."""
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            (tmp_path / "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_issue())

    assert result == str(tmp_path / "owner_repo_42")
    assert not any(c[0] == "gh" and c[1] == "pr" for c in calls)
    wt_add = [c for c in calls if c[0] == "git" and c[1] == "worktree" and c[2] == "add"][0]
    assert "-b" in wt_add
    assert "issue-42" in wt_add
    assert "origin/HEAD" in wt_add
    config_cmds = [c for c in calls if c[0] == "git" and c[1] == "config"]
    assert ["git", "config", "branch.issue-42.remote", "origin"] in config_cmds
    assert ["git", "config", "branch.issue-42.merge", "refs/heads/issue-42"] in config_cmds


def test_ensure_worktree_fetches_existing_main_for_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main repo exists, worktree missing, PR: skip clone, fetch then worktree add."""
    (tmp_path / "owner_repo").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "owner_repo_42")
    assert not any(c[0] == "gh" and c[1] == "repo" and c[2] == "clone" for c in calls)
    fetch_cmds = [c for c in calls if c[0] == "git" and c[1] == "fetch"]
    assert fetch_cmds == [["git", "fetch", "origin"]]


def test_ensure_worktree_fetches_existing_main_for_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main repo exists, worktree missing, issue: skip clone, fetch then worktree add."""
    (tmp_path / "owner_repo").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_issue())

    assert result == str(tmp_path / "owner_repo_42")
    assert not any(c[0] == "gh" and c[1] == "repo" for c in calls)
    wt_add = [c for c in calls if c[0] == "git" and c[1] == "worktree" and c[2] == "add"][0]
    assert "issue-42" in wt_add
    assert "origin/HEAD" in wt_add


def test_existing_worktree_path_finds_direct_worktree_when_origin_matches_item_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "owner_repo_42"
    candidate.mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) == candidate


def test_existing_worktree_path_returns_none_when_workdir_is_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = tmp_path / "workdir"
    workdir.write_text("not a directory", encoding="utf-8")

    def fake_run(*args, **kwargs):
        raise AssertionError(f"Unexpected command: {args[0]}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(workdir), make_pr()) is None


def test_existing_worktree_path_ignores_global_remote_in_plain_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "owner_repo_42"
    candidate.mkdir()
    subprocess.run(["git", "init"], cwd=candidate, check=True, capture_output=True, text=True)
    global_config = tmp_path / "global-gitconfig"
    global_config.write_text(
        '[remote "origin"]\n\turl = https://github.com/owner/repo.git\n',
        encoding="utf-8",
    )
    (tmp_path / "home").mkdir()
    (tmp_path / "xdg").mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

    inherited = subprocess.run(
        ["git", "config", "--get", "remote.origin.url"],
        cwd=candidate,
        capture_output=True,
        text=True,
    )
    local = subprocess.run(
        ["git", "config", "--local", "--get", "remote.origin.url"],
        cwd=candidate,
        capture_output=True,
        text=True,
    )

    assert inherited.returncode == 0
    assert inherited.stdout.strip() == "https://github.com/owner/repo.git"
    assert local.returncode != 0
    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_existing_worktree_path_ignores_plain_subdir_in_matching_parent_repo(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init"], cwd=parent, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
        cwd=parent,
        check=True,
        capture_output=True,
        text=True,
    )
    (parent / "owner_repo_42").mkdir()

    assert existing_worktree_path(str(parent), make_pr()) is None


def test_existing_worktree_path_ignores_direct_worktree_without_matching_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "owner_repo_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/repo.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_existing_worktree_path_ignores_manually_renamed_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "custom_name_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/contributor/repo.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_existing_worktree_path_finds_workdash_shaped_fork_pr_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "contributor_repo-fork_42"
    candidate.mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/contributor/repo-fork.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) == candidate


def test_existing_worktree_path_ignores_unrelated_renamed_fork_like_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "other_repo-fork_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/repo-fork.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_existing_worktree_path_ignores_unrelated_same_name_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "other_repo_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/repo.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_existing_worktree_path_returns_none_for_ambiguous_number_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "contributor_repo-fork_42").mkdir()
    (tmp_path / "other_repo-fork_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        cwd = kwargs["cwd"]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, cwd)
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            repo = (
                "contributor/repo-fork"
                if cwd.name.startswith("contributor_")
                else "other/repo-fork"
            )
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"https://github.com/{repo}.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_existing_worktree_path_ignores_unrelated_number_suffix_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "scratch_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/scratch.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert existing_worktree_path(str(tmp_path), make_pr()) is None


def test_ensure_worktree_does_not_pull_unrelated_direct_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "owner_repo_42").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/repo.git\n", stderr=""
            )
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            (tmp_path / "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            raise subprocess.CalledProcessError(1, cmd, stderr="path already exists")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to create worktree"):
        ensure_worktree(str(tmp_path), make_pr())

    assert ["git", "pull", "--ff-only"] not in calls
    assert any(c[0] == "git" and c[1] == "worktree" and c[2] == "add" for c in calls)


def test_ensure_worktree_does_not_pull_unrelated_number_suffix_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "scratch_42").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/other/scratch.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            (tmp_path / "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "owner_repo_42")
    assert ["git", "pull", "--ff-only"] not in calls


def test_ensure_worktree_pulls_existing_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worktree exists: pull ff-only, no gh call, no clone/fetch/worktree add."""
    (tmp_path / "owner_repo").mkdir()
    (tmp_path / "owner_repo_42").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "owner_repo_42")
    assert calls == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "config", "--local", "--get", "remote.origin.url"],
        ["git", "pull", "--ff-only"],
    ]


def test_ensure_worktree_finds_fork_worktree_with_upstream_without_gh_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing fork worktree is found only when upstream proves the base repo."""
    (tmp_path / "contributor_repo_42").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/contributor/repo.git\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.upstream.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        if cmd == ["git", "pull", "--ff-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "contributor_repo_42")
    assert calls == [
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "config", "--local", "--get", "remote.origin.url"],
        ["git", "config", "--local", "--get", "remote.upstream.url"],
        ["git", "pull", "--ff-only"],
    ]


def test_ensure_worktree_pull_failure_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When worktree exists and pull fails, return the path without raising."""
    (tmp_path / "owner_repo").mkdir()
    (tmp_path / "owner_repo_42").mkdir()

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        if cmd[0] == "git" and cmd[1] == "pull":
            raise subprocess.CalledProcessError(1, cmd, stderr="merge conflict")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "owner_repo_42")


def test_ensure_worktree_pr_with_existing_local_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR with local branch already existing: branch -f then worktree add without -b."""
    (tmp_path / "owner_repo").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "branch" and cmd[2] == "-f":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            (tmp_path / "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(tmp_path / "owner_repo_42")
    branch_cmds = [c for c in calls if c[0] == "git" and c[1] == "branch" and c[2] == "-f"]
    assert len(branch_cmds) == 1
    assert "feature-branch" in branch_cmds[0]
    assert "origin/feature-branch" in branch_cmds[0]
    wt_add = [c for c in calls if c[0] == "git" and c[1] == "worktree" and c[2] == "add"][0]
    assert "-b" not in wt_add
    assert "feature-branch" in wt_add
    config_cmds = [c for c in calls if c[0] == "git" and c[1] == "config"]
    assert ["git", "config", "branch.feature-branch.remote", "origin"] in config_cmds
    assert [
        "git",
        "config",
        "branch.feature-branch.merge",
        "refs/heads/feature-branch",
    ] in config_cmds


def test_ensure_worktree_clone_failure_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clone failure should propagate as RuntimeError."""

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            raise subprocess.CalledProcessError(1, cmd, stderr="auth required")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to clone"):
        ensure_worktree(str(tmp_path), make_pr())


def test_get_pr_head_info_missing_gh_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gh CLI is not installed, _get_pr_head_info raises RuntimeError."""

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI"):
        _get_pr_head_info(make_pr())


def test_ensure_worktree_creates_workdir_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workdir base directory is created automatically if it doesn't exist."""
    nested_workdir = str(tmp_path / "deep" / "wrk")

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            Path(nested_workdir, "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "prune":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "show-ref":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "add":
            Path(nested_workdir, "owner_repo_42").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "config":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(nested_workdir, make_issue())

    assert result == str(Path(nested_workdir) / "owner_repo_42")
    assert Path(nested_workdir).exists()


def test_ensure_worktree_found_by_git_worktree_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TUI worktree preparation still reuses a git-tracked target branch."""
    (tmp_path / "owner_repo").mkdir()
    unexpected_wt = tmp_path / "custom_name"
    unexpected_wt.mkdir()
    calls: list[list[str]] = []

    porcelain_output = (
        f"worktree {tmp_path / 'owner_repo'}\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        f"worktree {unexpected_wt}\n"
        "HEAD def456\n"
        "branch refs/heads/feature-branch\n"
        "\n"
    )

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "git" and cmd[1] == "worktree" and cmd[2] == "list":
            return subprocess.CompletedProcess(cmd, 0, stdout=porcelain_output, stderr="")
        if cmd[0] == "git" and cmd[1] == "pull":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_pr())

    assert result == str(unexpected_wt)
    # Should not attempt to clone, fetch, or create a worktree
    assert not any(c[0] == "gh" and c[1] == "repo" for c in calls)
    assert not any(c[0] == "git" and c[1] == "fetch" for c in calls)
    assert not any(c[0] == "git" and c[1] == "worktree" and c[2] == "add" for c in calls)
    # Should have pulled in the found worktree
    pull_cmds = [c for c in calls if c[0] == "git" and c[1] == "pull"]
    assert len(pull_cmds) == 1


def test_find_worktree_for_branch_returns_none_when_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No worktree checked out on the target branch: returns None."""
    (tmp_path / "repo").mkdir()
    porcelain_output = f"worktree {tmp_path / 'repo'}\nHEAD abc123\nbranch refs/heads/main\n\n"

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout=porcelain_output, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = _find_worktree_for_branch(tmp_path / "repo", "feature-branch")
    assert result is None


def test_find_worktree_for_branch_returns_none_when_main_missing() -> None:
    """If the main repo directory doesn't exist, returns None without running git."""
    result = _find_worktree_for_branch(Path("/nonexistent"), "feature-branch")
    assert result is None
