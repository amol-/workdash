"""Tests for workdash.repo_worktree — worktree management for work items."""

import json
import os
import subprocess
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from workdash.git import GitHelper
from workdash.github_client import GitHubClient, todo_target_from_body
from workdash.models import WorkItem, WorkItemKind, WorkItemType, display_repo
from workdash.repo_worktree import (
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


def make_linked_pr(
    number: int = 42149,
    issue_number: int = 41830,
    *,
    kind: WorkItemKind = WorkItemKind.AUTHORED_PR,
    repo: str = "owner/repo",
) -> WorkItem:
    item = make_pr(number, repo)
    item.kind = kind
    item.closing_issue_numbers = (issue_number,)
    return item


def make_targeted_todo(number: int = 110, target: str = "owner/repo") -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="testuser/todos",
        number=number,
        title="Test todo",
        created_at=datetime(2026, 2, 1, tzinfo=UTC),
        updated_at=datetime(2026, 2, 1, tzinfo=UTC),
        url=f"https://github.com/testuser/todos/issues/{number}",
        todo_target=target,
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
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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
    assert calls[2] == ["git", "fetch", "--prune", "origin"]
    wt_add = [c for c in calls if c[0] == "git" and c[1] == "worktree" and c[2] == "add"][0]
    assert (
        calls.index(["git", "fetch", "--prune", "origin"])
        < calls.index(["git", "merge", "--ff-only", "origin/HEAD"])
        < calls.index(wt_add)
    )
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


def test_ensure_worktree_fork_pr_clones_from_fork_and_fetches_upstream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fork PR: clone from fork repo and fetch the base repo as upstream."""
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
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "remote", "get-url", "upstream"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "remote", "add", "upstream", "https://github.com/owner/repo.git"]:
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
    assert ["git", "remote", "add", "upstream", "https://github.com/owner/repo.git"] in calls
    assert ["git", "fetch", "--prune", "upstream"] in calls


def test_ensure_worktree_clones_and_creates_for_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main repo missing, issue item: clone, fetch, worktree add with issue-N branch."""
    calls: list[list[str]] = []
    linked = False

    def fake_run(*args, **kwargs):
        nonlocal linked
        cmd = args[0]
        if cmd[:3] == ["gh", "issue", "develop"]:
            linked = True
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(cmd, 0 if linked else 2, stdout="", stderr="")
        calls.append(cmd)
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            (tmp_path / "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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
    assert "origin/issue-42" in wt_add
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
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git", stderr=""
            )
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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
    assert fetch_cmds == [["git", "fetch", "--prune", "origin"]]


def test_ensure_worktree_fetches_existing_main_for_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main repo exists, worktree missing, issue: skip clone, fetch then worktree add."""
    monkeypatch.setattr(GitHelper, "fetch_origin_branch", lambda *args, **kwargs: True)
    (tmp_path / "owner_repo").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd[:3] == ["gh", "issue", "develop"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git", stderr=""
            )
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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
    assert "origin/issue-42" in wt_add


def test_existing_worktree_path_finds_the_targeted_todo_worktree_of_the_target_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The target repository's own issue-110 worktree must not be mistaken for
    # the todo's worktree even though both end with the same number.
    todo_worktree = tmp_path / "owner_repo_todo_110"
    todo_worktree.mkdir()
    (tmp_path / "owner_repo_110").mkdir()

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

    assert existing_worktree_path(str(tmp_path), make_targeted_todo()) == todo_worktree
    assert existing_worktree_path(str(tmp_path), make_issue(110)) == tmp_path / "owner_repo_110"


def test_ensure_worktree_creates_a_target_clone_branch_for_a_targeted_todo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "repo", "clone"]:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd[:3] == ["git", "worktree", "add"]:
            Path(cmd[cmd.index("-b") + 2]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "show-ref"] or cmd[:3] == ["git", "worktree", "list"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_targeted_todo())

    assert result == str(tmp_path / "owner_repo_todo_110")
    assert ["gh", "repo", "clone", "owner/repo", str(tmp_path / "owner_repo")] in calls
    assert not any(cmd[:3] == ["gh", "issue", "view"] for cmd in calls)
    wt_add = next(cmd for cmd in calls if cmd[:3] == ["git", "worktree", "add"])
    assert "wt-110" in wt_add
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


def test_existing_worktree_path_finds_the_linked_issue_worktree_for_an_authored_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pane and terminal actions of an authored pull request must land in the
    # checkout the user already opened from the issue it closes.
    issue_worktree = tmp_path / "owner_repo_41830"
    issue_worktree.mkdir()

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

    assert existing_worktree_path(str(tmp_path), make_linked_pr()) == issue_worktree
    # A pull request the user only reviews keeps its own directory, so the
    # issue's checkout is not offered for somebody else's branch.
    assert (
        existing_worktree_path(
            str(tmp_path),
            make_linked_pr(kind=WorkItemKind.REVIEW_REQUESTED_PR),
        )
        is None
    )
    # An issue closed in another repository never becomes a closing issue
    # number, so the pull request keeps its own PR-numbered worktree.
    cross_repo_pr = make_pr()
    assert existing_worktree_path(str(tmp_path), cross_repo_pr) is None


def test_existing_worktree_path_finds_the_worktree_of_a_non_lowest_closing_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A pull request closing several issues in its own repository shares one
    # checkout with all of them, so the worktree opened from a non-lowest
    # closing issue must be found too, not just the lowest-numbered one.
    item = make_pr()
    item.closing_issue_numbers = (41830, 41999)
    non_lowest_worktree = tmp_path / "owner_repo_41999"
    non_lowest_worktree.mkdir()

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

    assert existing_worktree_path(str(tmp_path), item) == non_lowest_worktree


def test_existing_worktree_path_still_finds_a_pr_numbered_worktree_for_a_linked_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Checkouts opened before the pull request linked to its issue keep their own
    # number, and their panes and terminals must stay mapped to the pull request.
    pr_worktree = tmp_path / "owner_repo_42149"
    pr_worktree.mkdir()

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

    assert existing_worktree_path(str(tmp_path), make_linked_pr()) == pr_worktree


def test_existing_worktree_path_prefers_the_pr_numbered_worktree_over_the_issue_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Both checkouts prove the pull request; the one named after the pull request
    # itself is the one its panes and terminals belong to.
    (tmp_path / "owner_repo_41830").mkdir()
    pr_worktree = tmp_path / "owner_repo_42149"
    pr_worktree.mkdir()

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

    assert existing_worktree_path(str(tmp_path), make_linked_pr()) == pr_worktree


def test_ensure_worktree_reuses_a_linked_issue_worktree_whatever_branch_it_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The checkout was opened for this work, so it is trusted to be configured
    # for it: the pull request's branch name is never looked up, and no second,
    # PR-numbered checkout of the same work is opened. The fake raises on any
    # branch inspection or gh call, so a regression to either fails here.
    (tmp_path / "owner_repo").mkdir()
    issue_worktree = tmp_path / "owner_repo_41830"
    issue_worktree.mkdir()
    (issue_worktree / "work.txt").write_text("work in progress", encoding="utf-8")
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
        if cmd[:2] == ["git", "pull"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_linked_pr())

    assert result == str(issue_worktree)
    assert not (tmp_path / "owner_repo_42149").exists()
    assert ["git", "pull", "--ff-only"] in calls
    assert (issue_worktree / "work.txt").read_text() == "work in progress"


def test_ensure_worktree_adopts_a_linked_prs_own_pr_numbered_worktree_as_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A checkout named after the pull request itself is this pull request's own
    # work whatever it is checked out on, so it is used directly: no branch
    # lookup on GitHub and no upstream fetch in a checkout that has no fork.
    (tmp_path / "owner_repo").mkdir()
    (tmp_path / "owner_repo_42149").mkdir()
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
        if cmd == ["git", "pull", "--ff-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_linked_pr())

    assert result == str(tmp_path / "owner_repo_42149")
    assert ["git", "pull", "--ff-only"] in calls


def test_ensure_worktree_names_a_fork_pr_worktree_after_the_fork_and_the_linked_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=_FORK_HEAD_INFO, stderr="")
        if cmd[:3] == ["gh", "repo", "clone"]:
            (tmp_path / "contributor_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd[:2] == ["git", "show-ref"] or cmd[:3] == ["git", "worktree", "list"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[0] == "git":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ensure_worktree(str(tmp_path), make_linked_pr())

    # The directory names the checkout's own repository (the fork) and the issue
    # the work implements, which lives in the upstream repository.
    assert result == str(tmp_path / "contributor_repo_41830")


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
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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

    with pytest.raises(RuntimeError, match="belongs to a different repository"):
        ensure_worktree(str(tmp_path), make_pr())

    assert ["git", "pull", "--ff-only"] not in calls
    assert not any(c[0] == "git" and c[1] == "worktree" and c[2] == "add" for c in calls)


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
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "config", "--local", "--get", "remote.origin.url"],
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
        if cmd == ["git", "fetch", "--prune", "upstream"]:
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
        ["git", "rev-parse", "--show-toplevel"],
        ["git", "config", "--local", "--get", "remote.origin.url"],
        ["git", "fetch", "--prune", "upstream"],
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
    """A PR's existing local branch is checked out without resetting it."""
    (tmp_path / "owner_repo").mkdir()
    calls: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git", stderr=""
            )
        if cmd[0] == "gh" and cmd[1] == "pr" and cmd[2] == "view":
            return subprocess.CompletedProcess(cmd, 0, stdout=_SAME_REPO_HEAD_INFO, stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd in (
            ["git", "merge", "--ff-only", "origin/HEAD"],
            ["git", "merge", "--ff-only", "origin/feature-branch"],
        ):
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
    assert not branch_cmds
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


def test_fetch_worktree_head_missing_gh_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """When gh CLI is not installed, GitHubClient raises RuntimeError."""

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="gh CLI"):
        GitHubClient().fetch_worktree_head(make_pr())


def test_ensure_worktree_creates_workdir_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The workdir base directory is created automatically if it doesn't exist."""
    monkeypatch.setattr(GitHelper, "fetch_origin_branch", lambda *args, **kwargs: True)
    nested_workdir = str(tmp_path / "deep" / "wrk")

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd[:3] == ["gh", "issue", "develop"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "gh" and cmd[1] == "repo" and cmd[2] == "clone":
            Path(nested_workdir, "owner_repo").mkdir(exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[0] == "git" and cmd[1] == "fetch":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd == ["git", "switch", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
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
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(cmd, kwargs["cwd"])
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git", stderr=""
            )
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

    result = GitHelper().find_worktree_for_branch(tmp_path / "repo", "feature-branch")
    assert result is None


def test_find_worktree_for_branch_returns_none_when_main_missing() -> None:
    """If the main repo directory doesn't exist, returns None without running git."""
    result = GitHelper().find_worktree_for_branch(Path("/nonexistent"), "feature-branch")
    assert result is None


def test_ensure_new_issue_worktree_fast_forwards_parent_and_child_to_origin_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh issue worktree and its reserved clone use the fetched default commit."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    workdir = tmp_path / "workdir"
    main = workdir / "owner_repo"

    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(seed)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=seed, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=seed, check=True)
    (seed / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=seed, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(
        ["git", "push", "-u", "origin", "main"], cwd=seed, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", "refs/heads/main"],
        cwd=origin,
        check=True,
    )
    workdir.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    subprocess.run(
        ["git", "clone", "https://github.com/owner/repo.git", str(main)],
        check=True,
        capture_output=True,
    )

    (seed / "README.md").write_text("updated\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "updated"], cwd=seed, check=True, capture_output=True)
    subprocess.run(["git", "push"], cwd=seed, check=True, capture_output=True)
    latest = subprocess.run(
        ["git", "rev-parse", "main"], cwd=origin, check=True, capture_output=True, text=True
    ).stdout.strip()

    child = Path(ensure_worktree(str(workdir), make_issue()))

    parent_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=main, check=True, capture_output=True, text=True
    ).stdout.strip()
    child_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=child, check=True, capture_output=True, text=True
    ).stdout.strip()
    assert parent_ref == latest
    assert child_ref == latest


@pytest.mark.parametrize(
    ("target", "origin", "matches"),
    [
        ("https://codeberg.org/owner/repo.git", "git@codeberg.org:owner/repo", True),
        ("ssh://git@gitlab.com/group/owner/repo", "git://gitlab.com/group/owner/repo.git", True),
        ("https://codeberg.org/owner/repo", "https://github.com/owner/repo", False),
        ("owner/repo", "https://codeberg.org/owner/repo", False),
        ("owner/repo", "git@github.com:owner/repo.git", True),
        ("owner/repo", "git@github.com:Owner/Repo.git", True),
        ("https://github.com/owner/repo", "git@github.com:Owner/Repo.git", True),
        ("https://codeberg.org/owner/repo", "git@codeberg.org:Owner/Repo.git", False),
        ("https://gitlab.com/group/owner/repo", "https://gitlab.com/other/owner/repo", False),
    ],
)
def test_targeted_todo_discovery_requires_host_and_full_path(
    tmp_path, monkeypatch, target, origin, matches
):
    candidate = tmp_path / "owner_repo_todo_110"
    candidate.mkdir()
    item = make_targeted_todo()
    item.todo_target = target

    def fake_run(command, **kwargs):
        if command == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(command, kwargs["cwd"])
        assert command == ["git", "config", "--local", "--get", "remote.origin.url"]
        return subprocess.CompletedProcess(command, 0, stdout=origin, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert existing_worktree_path(str(tmp_path), item) == (candidate if matches else None)


@pytest.mark.parametrize("existing", ["main", "worktree"])
def test_github_issue_reuses_repository_with_differently_cased_origin(
    tmp_path, monkeypatch, existing
):
    path = tmp_path / ("Owner_Repo" if existing == "main" else "Owner_Repo_42")
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:owner/repo.git"], cwd=path, check=True
    )
    item = make_issue(repo="Owner/Repo")
    if existing == "worktree":
        monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: None)
        assert ensure_worktree(str(tmp_path), item) == str(path)
    else:
        monkeypatch.setattr(GitHelper, "find_worktree_for_branch", lambda self, path, branch: None)
        monkeypatch.setattr(GitHelper, "fetch_remote", lambda self, path, remote: None)
        monkeypatch.setattr(GitHelper, "fast_forward_default_branch", lambda self, path: None)
        monkeypatch.setattr(GitHelper, "fetch_origin_branch", lambda *args, **kwargs: True)
        monkeypatch.setattr(GitHelper, "create_worktree", lambda self, *args: None)
        assert ensure_worktree(str(tmp_path), item, github_client=MagicMock()) == str(
            tmp_path / "Owner_Repo_42"
        )


def test_same_repo_pr_reused_with_differently_cased_directory(tmp_path, monkeypatch):
    path = tmp_path / "owner_repo_42"
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:owner/repo.git"], cwd=path, check=True
    )
    subprocess.run(
        ["git", "remote", "add", "upstream", "git@github.com:other/repo.git"], cwd=path, check=True
    )
    monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: None)
    fetches = []
    monkeypatch.setattr(
        GitHelper, "fetch_remote", lambda self, path, remote: fetches.append((path, remote))
    )
    monkeypatch.setattr(
        GitHubClient, "fetch_worktree_head", lambda *args: pytest.fail("unexpected lookup")
    )

    assert ensure_worktree(str(tmp_path), make_pr(repo="Owner/Repo")) == str(path)
    assert fetches == []


def test_fork_worktree_reused_with_differently_cased_upstream(tmp_path, monkeypatch):
    path = tmp_path / "contributor_repo_42"
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:contributor/repo.git"],
        cwd=path,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "upstream", "git@github.com:Owner/Repo.git"], cwd=path, check=True
    )
    monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: None)
    fetches = []
    monkeypatch.setattr(
        GitHelper, "fetch_remote", lambda self, path, remote: fetches.append((path, remote))
    )
    monkeypatch.setattr(
        GitHubClient, "fetch_worktree_head", lambda *args: pytest.fail("unexpected lookup")
    )

    assert ensure_worktree(str(tmp_path), make_pr(repo="owner/repo")) == str(path)
    assert fetches == [(path, "upstream")]


def test_github_issue_reuses_worktree_with_mixed_casing(tmp_path, monkeypatch):
    path = tmp_path / "Owner_Repo_42"
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:owner/repo.git"], cwd=path, check=True
    )
    monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: None)
    monkeypatch.setattr(
        GitHelper,
        "clone_github_repository",
        lambda *args, **kwargs: pytest.fail("unexpected clone"),
    )
    assert ensure_worktree(str(tmp_path), make_issue(repo="OWNER/REPO")) == str(path)


@pytest.mark.parametrize(
    ("target", "origin", "reused"),
    [
        ("https://github.com/OWNER/REPO.git", "git@github.com:owner/repo.git", True),
        ("https://codeberg.org/owner/repo.git", "git@codeberg.org:owner/repo.git", False),
        ("https://codeberg.org/Owner/Repo.git", "git@codeberg.org:owner/repo.git", False),
        ("https://github.com/Owner/Repo.git", "git@codeberg.org:owner/repo.git", False),
    ],
)
def test_targeted_todo_worktree_casing_depends_on_host(
    tmp_path, monkeypatch, target, origin, reused
):
    path = tmp_path / "Owner_Repo_todo_110"
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    (path / "work.txt").write_text("todo work")
    item = make_targeted_todo(target=target)
    assert existing_worktree_path(str(tmp_path), item) == (path if reused else None)
    with ExitStack() as pinned:
        result = existing_worktree_path(str(tmp_path), item, pinned=pinned)
        assert (result[0] if result else None) == (path if reused else None)
        if reused:
            _, fd = result
            assert os.path.samestat(os.fstat(fd), path.stat())
            file_fd = os.open("work.txt", os.O_RDONLY, dir_fd=fd)
            try:
                assert os.read(file_fd, 100) == b"todo work"
            finally:
                os.close(file_fd)
    if reused:
        pulls = []
        monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: pulls.append(path))
        assert ensure_worktree(str(tmp_path), item) == str(path)
        assert pulls == [path]


def test_targeted_todo_reuses_matching_worktree_when_main_clone_is_another_repository(
    tmp_path, monkeypatch
):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    main = workdir / "owner_repo"
    worktree = workdir / "owner_repo_todo_110"
    source = tmp_path / "source"
    target = "https://codeberg.org/owner/repo.git"
    for path, origin in ((main, "https://github.com/owner/repo.git"), (source, target)):
        subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=path, check=True)
    (source / "work.txt").write_text("todo work")
    subprocess.run(["git", "add", "work.txt"], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "initial",
        ],
        cwd=source,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-110", str(worktree)],
        cwd=source,
        check=True,
        capture_output=True,
    )
    (main / "work.txt").write_text("other repository work")
    (worktree / "work.txt").write_text("uncommitted todo work")
    pulls = []
    monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: pulls.append(path))

    assert ensure_worktree(str(workdir), make_targeted_todo(target=target)) == str(worktree)
    assert pulls == [worktree]
    assert (main / "work.txt").read_text() == "other repository work"
    assert (worktree / "work.txt").read_text() == "uncommitted todo work"


@pytest.mark.parametrize("occupied", ["main", "worktree", "branch"])
def test_targeted_todo_conflicts_never_mutate_another_repository(tmp_path, monkeypatch, occupied):
    main = tmp_path / "owner_repo"
    main.mkdir()
    candidate = tmp_path / ("owner_repo_todo_110" if occupied == "worktree" else "custom")
    candidate.mkdir()
    marker = candidate / "work.txt"
    marker.write_text("uncommitted work")
    item = make_targeted_todo()
    item.todo_target = "https://codeberg.org/owner/repo.git"
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if command == ["git", "config", "--local", "--get", "remote.origin.url"]:
            wrong = kwargs["cwd"] == (main if occupied == "main" else candidate)
            origin = "https://github.com/owner/repo" if wrong else item.todo_target
            return subprocess.CompletedProcess(command, 0, stdout=origin, stderr="")
        if command == ["git", "rev-parse", "--show-toplevel"]:
            return _git_show_toplevel(command, kwargs["cwd"])
        if command == ["git", "worktree", "list", "--porcelain"]:
            return subprocess.CompletedProcess(
                command, 0, stdout=f"worktree {candidate}\nbranch refs/heads/wt-110\n", stderr=""
            )
        raise AssertionError(f"Unexpected mutation: {command}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="belongs to a different repository"):
        ensure_worktree(str(tmp_path), item)
    assert marker.read_text() == "uncommitted work"
    assert all(command[0] == "git" for command in calls)


def test_url_target_clone_worktree_reuse_and_missing_default_head(tmp_path, monkeypatch):
    # Redirect Git transport to a disposable local origin; no forge is contacted.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin"
    subprocess.run(["git", "init", "-b", "main", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=origin, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=origin, check=True)
    (origin / "source.py").write_text("value = 42\n")
    subprocess.run(["git", "add", "."], cwd=origin, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=origin, check=True, capture_output=True)
    target = "https://gitlab.com/group/owner/repo.git"
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", target)
    real_run = subprocess.run
    issue_bodies = []

    def run(command, **kwargs):
        if command[:3] == ["gh", "label", "create"]:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["gh", "issue", "create"]:
            issue_bodies.append(command[command.index("--body") + 1])
            return subprocess.CompletedProcess(
                command, 0, stdout="https://github.com/testuser/todos/issues/110\n", stderr=""
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    item = GitHubClient().create_todo(
        todo_repository="testuser/todos", text="Test todo", target=target
    )
    item.todo_target = todo_target_from_body(issue_bodies[0])
    assert item.todo_target == target
    assert display_repo(item) == "owner/repo"
    workdir = tmp_path / "workdir"

    worktree = Path(ensure_worktree(str(workdir), item))

    assert worktree.name == "owner_repo_todo_110"
    assert (worktree / "source.py").read_text() == "value = 42\n"
    assert existing_worktree_path(str(workdir), item) == worktree
    assert ensure_worktree(str(workdir), item) == str(worktree)
    item.todo_target = "https://github.com/group/owner/repo.git"
    assert existing_worktree_path(str(workdir), item) is None
    with pytest.raises(RuntimeError, match="belongs to a different repository"):
        ensure_worktree(str(workdir), item)
    assert (worktree / "source.py").read_text() == "value = 42\n"
    item.todo_target = target
    main = workdir / "owner_repo"
    subprocess.run(
        ["git", "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"], cwd=main, check=True
    )
    item.number = 111
    next_worktree = Path(ensure_worktree(str(workdir), item))
    assert (next_worktree / "source.py").read_text() == "value = 42\n"
    branch = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=next_worktree,
        check=True,
        capture_output=True,
        text=True,
    )
    assert branch.stdout.strip() == "wt-111"


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("git"),
        subprocess.CalledProcessError(128, ["git", "clone"], stderr="repository not found"),
    ],
)
def test_url_clone_failures_have_context_and_preserve_cause(tmp_path, monkeypatch, failure):
    item = make_targeted_todo()
    item.todo_target = "https://forge.example/owner/repo.git"

    def fake_run(command, **kwargs):
        assert command == ["git", "clone", item.todo_target, str(tmp_path / "owner_repo")]
        raise failure

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(
        RuntimeError, match="Failed to clone https://forge.example/owner/repo.git"
    ) as error:
        ensure_worktree(str(tmp_path), item)
    assert error.value.__cause__ is failure


@pytest.mark.parametrize("occupied", ["owner_repo", "owner_repo_42"])
def test_github_issue_rejects_same_named_clone_or_worktree_on_another_host(
    tmp_path, monkeypatch, occupied
):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    candidate = tmp_path / occupied
    subprocess.run(["git", "init", str(candidate)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://codeberg.org/owner/repo.git"],
        cwd=candidate,
        check=True,
    )
    (candidate / "work.txt").write_text("local work")
    real_run = subprocess.run

    def read_only_run(command, **kwargs):
        assert command == ["git", "rev-parse", "--show-toplevel"] or command == [
            "git",
            "config",
            "--local",
            "--get",
            "remote.origin.url",
        ]
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", read_only_run)
    assert existing_worktree_path(str(tmp_path), make_issue()) is None
    with pytest.raises(RuntimeError, match="belongs to a different repository"):
        ensure_worktree(str(tmp_path), make_issue())
    assert (candidate / "work.txt").read_text() == "local work"


@pytest.mark.parametrize("existing_branch", [False, True])
def test_same_repo_pr_head_casing_does_not_configure_fork_upstream(
    tmp_path, monkeypatch, existing_branch
):
    main = tmp_path / "owner_repo"
    subprocess.run(["git", "init", str(main)], check=True, capture_output=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:owner/repo.git"], cwd=main, check=True
    )
    branch_worktree = tmp_path / "other-worktree"
    monkeypatch.setattr(
        GitHubClient, "fetch_worktree_head", lambda self, item: ("feature", "owner/repo")
    )
    monkeypatch.setattr(
        GitHelper,
        "find_worktree_for_branch",
        lambda self, path, branch: branch_worktree if existing_branch else None,
    )
    monkeypatch.setattr(GitHelper, "pull_ff_only", lambda self, path: None)
    fetches = []
    monkeypatch.setattr(
        GitHelper, "fetch_remote", lambda self, path, remote: fetches.append(remote)
    )
    monkeypatch.setattr(GitHelper, "fast_forward_default_branch", lambda self, path: None)
    monkeypatch.setattr(
        GitHelper, "ensure_upstream_remote", lambda *args: pytest.fail("unexpected fork upstream")
    )
    created = []
    monkeypatch.setattr(GitHelper, "create_worktree", lambda self, *args: created.append(args))

    result = ensure_worktree(str(tmp_path), make_pr(repo="Owner/Repo"))

    if existing_branch:
        assert result == str(branch_worktree)
        assert fetches == []
        assert created == []
    else:
        assert result == str(tmp_path / "owner_repo_42")
        assert fetches == ["origin"]
        assert created == [(main, tmp_path / "owner_repo_42", "feature", "origin/feature")]


def test_fake_git_prepares_and_reuses_review_worktree_with_merge_base(monkeypatch, tmp_path):
    from webob import Request

    from workdash import control, launcher
    from workdash.config import AgentConfig, WorkdashConfig

    class FakeGit:
        def __init__(self):
            self.worktrees = {}
            self.fetches = []
            self.pulls = []
            self.clones = []

        def __bool__(self):
            return False

        def worktree_proves_item(self, candidate, item, *, directory=None):
            return candidate in self.worktrees and item.number == 42

        def find_worktree_for_branch(self, main, branch):
            return next((path for path, ref in self.worktrees.items() if ref == branch), None)

        def clone_github_repository(self, repo, path, *, github_client):
            self.clones.append((repo, path))
            path.mkdir()

        def fetch_remote(self, main, remote):
            self.fetches.append(remote)

        def fast_forward_default_branch(self, main):
            pass

        def ensure_upstream_remote(self, main, repo):
            assert repo == "owner/repo"

        def local_branch_exists(self, main, branch):
            return False

        def create_worktree(self, main, path, branch, start):
            assert branch == "feature"
            assert start == "origin/feature"
            path.mkdir()
            self.worktrees[path] = branch

        def pull_ff_only(self, path):
            self.pulls.append(path)

        def repository_matches(self, path, repo):
            return repo == "fork/repo"

        def merge_base_with_origin_default(self, path):
            assert Path(path) in self.worktrees
            return "base123"

    git = FakeGit()
    github = MagicMock()
    github.fetch_worktree_head.return_value = ("feature", "fork/repo")
    github.fetch_launch_context.return_value = {}
    git_factory = MagicMock(return_value=git)
    monkeypatch.setattr(control, "GitHelper", git_factory)
    monkeypatch.setattr(launcher, "collect_launch_github_context", lambda item: {})
    launch = MagicMock()
    monkeypatch.setattr(control.agent_execution.executor, "launch_vscode", launch)
    item = make_pr()
    item.kind = WorkItemKind.TRACKED_PR
    analysis_file = tmp_path / "analysis.md"
    analysis_file.write_text("Analysis findings")
    backend = MagicMock()
    backend.github_client = github
    backend.analyze_item.return_value = str(analysis_file)
    session = control.WorkdashSession(
        config=WorkdashConfig(workdir=str(tmp_path), agents={"pi": AgentConfig(analyze="pi")}),
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected process")
    )
    result = session.code(target="owner/repo#CHECK-42", agent="vscode", allow_vscode=True)
    path = result["cwd"]
    assert result["status"] == "launched"
    assert git.clones == [("fork/repo", tmp_path / "fork_repo")]
    assert "git diff base123..HEAD" in launch.call_args.args[1]
    assert git.fetches == ["origin", "upstream"]
    session.zellij_session = "workdash-main"
    analysis = session.analyze(target="owner/repo#CHECK-42", agent="pi", prefer_cache=False)
    assert analysis["status"] == "generated"
    assert backend.analyze_item.call_args.kwargs == {
        "tool": "pi",
        "cwd": path,
        "zellij_session": "workdash-main",
        "group": "repo",
    }
    assert git.pulls == [Path(path)]
    assert git.fetches == ["origin", "upstream", "upstream"]
    artifacts = Path(path) / ".workdash"
    artifacts.mkdir()
    (artifacts / "PR_REVIEW.md").write_text("Review findings")
    app = control._make_turbogears_app(session)
    response = Request.blank(
        "/api/items/owner/repo%23CHECK-42/files/.workdash/PR_REVIEW.md"
    ).get_response(app)
    assert response.status_int == 200
    assert response.text == "Review findings"
    git_factory.assert_called_once_with()


def test_existing_local_branch_and_upstream_are_preserved(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-m", "base")
    git("branch", "issue-42")
    git("config", "branch.issue-42.remote", "custom")
    git("config", "branch.issue-42.merge", "refs/heads/custom-branch")
    git("commit", "--allow-empty", "-m", "new default")
    original = git("rev-parse", "issue-42")

    GitHelper().create_worktree(tmp_path, tmp_path / "checkout", "issue-42", "HEAD")

    assert git("rev-parse", "issue-42") == original
    assert git("config", "branch.issue-42.remote") == "custom"
    assert git("config", "branch.issue-42.merge") == "refs/heads/custom-branch"


@pytest.mark.parametrize(
    "case",
    [
        "local",
        "local_offline",
        "remote",
        "linked",
        "fallback",
        "fallback_no_refresh",
        "fallback_offline",
        "race",
        "single_fallback",
        "single_remote",
        "single_linked",
        "single_retry",
        "stale_remote",
    ],
)
def test_issue_branch_selection_with_real_git(tmp_path, monkeypatch, case):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    workdir = tmp_path / "work"
    main = workdir / "owner_repo"

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "commit", "--allow-empty", "-m", "base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    workdir.mkdir()
    git(
        workdir,
        "clone",
        *(["--single-branch"] if case.startswith("single_") else []),
        str(origin),
        str(main),
    )
    git(main, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    expected = git(seed, "rev-parse", "HEAD")
    if case in {"local", "local_offline"}:
        git(main, "config", "user.name", "Test")
        git(main, "config", "user.email", "test@example.com")
        git(main, "switch", "-c", "issue-42")
        git(main, "commit", "--allow-empty", "-m", "local work")
        expected = git(main, "rev-parse", "HEAD")
        git(main, "switch", "main")
        git(main, "config", "branch.issue-42.remote", "custom")
        git(main, "config", "branch.issue-42.merge", "refs/heads/custom")
        if case == "local_offline":
            origin.rename(tmp_path / "unavailable.git")
    if case in {"remote", "single_remote"}:
        git(seed, "switch", "-c", "issue-42")
        git(seed, "commit", "--allow-empty", "-m", "remote work")
        expected = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "origin", "issue-42")
    if case == "stale_remote":
        git(seed, "switch", "-c", "issue-42")
        git(seed, "commit", "--allow-empty", "-m", "deleted remote work")
        git(seed, "push", "origin", "issue-42")
        git(main, "fetch", "origin", "refs/heads/issue-42:refs/remotes/origin/issue-42")
        git(seed, "push", "origin", "--delete", "issue-42")
        git(main, "config", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
        assert git(main, "rev-parse", "origin/issue-42") != expected
    github = MagicMock()
    if case == "race":
        git(seed, "switch", "-c", "issue-42")
        git(seed, "commit", "--allow-empty", "-m", "concurrent work")
        expected = git(seed, "rev-parse", "HEAD")

        def create_then_fail(*args):
            git(seed, "push", "origin", "issue-42")
            raise RuntimeError("link failed")

        github.create_issue_branch.side_effect = create_then_fail
    elif case == "fallback_offline":

        def go_offline(*args):
            origin.rename(tmp_path / "unavailable.git")
            raise RuntimeError("link failed")

        github.create_issue_branch.side_effect = go_offline
    elif case in {"linked", "single_linked", "single_retry", "stale_remote"}:
        github.create_issue_branch.side_effect = lambda *args: git(
            seed, "push", "origin", "main:issue-42"
        )
    elif case in {"fallback", "single_fallback"}:
        git(seed, "switch", "-c", "new-default")
        git(seed, "commit", "--allow-empty", "-m", "new default")
        expected = git(seed, "rev-parse", "HEAD")
        git(seed, "push", "origin", "new-default")
        git(origin, "symbolic-ref", "HEAD", "refs/heads/new-default")
        github.create_issue_branch.side_effect = RuntimeError("link failed")
    elif case == "fallback_no_refresh":
        git(origin, "symbolic-ref", "HEAD", "refs/heads/missing")
        assert (
            subprocess.run(
                ["git", "remote", "set-head", "origin", "--auto"],
                cwd=main,
                capture_output=True,
                text=True,
            ).returncode
            != 0
        )
        github.create_issue_branch.side_effect = RuntimeError("link failed")

    if case == "single_retry":
        with monkeypatch.context() as patch:
            patch.setattr(
                GitHelper, "create_worktree", MagicMock(side_effect=RuntimeError("setup failed"))
            )
            with pytest.raises(RuntimeError, match="setup failed"):
                ensure_worktree(str(workdir), make_issue(), github_client=github)
        git(seed, "switch", "issue-42")
        git(seed, "commit", "--allow-empty", "-m", "work after setup failed")
        git(seed, "push", "origin", "issue-42")
        expected = git(seed, "rev-parse", "HEAD")

    worktree = Path(ensure_worktree(str(workdir), make_issue(), github_client=github))
    assert git(worktree, "branch", "--show-current") == "issue-42"
    assert git(worktree, "rev-parse", "HEAD") == expected
    if case in {"local", "local_offline"}:
        assert git(main, "config", "branch.issue-42.remote") == "custom"
        assert git(main, "config", "branch.issue-42.merge") == "refs/heads/custom"
    elif case in {
        "remote",
        "linked",
        "race",
        "single_remote",
        "single_linked",
        "single_retry",
        "stale_remote",
    }:
        assert git(worktree, "config", "branch.issue-42.remote") == "origin"
        assert git(worktree, "config", "branch.issue-42.merge") == "refs/heads/issue-42"
        assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/issue-42"
    if case in {"fallback", "fallback_no_refresh", "fallback_offline", "single_fallback"}:
        assert (
            subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "@{upstream}"],
                cwd=worktree,
                capture_output=True,
            ).returncode
            != 0
        )
    if case in {"local", "local_offline", "remote", "single_remote"}:
        github.create_issue_branch.assert_not_called()
    else:
        github.create_issue_branch.assert_called_once_with("owner/repo", 42, "issue-42")
    if case == "single_fallback":
        git(seed, "commit", "--allow-empty", "-m", "later default work")
        git(seed, "push", "origin", "new-default")
        next_worktree = Path(ensure_worktree(str(workdir), make_issue(43), github_client=github))
        assert git(next_worktree, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
        assert git(worktree, "rev-parse", "HEAD") == expected
        git(seed, "switch", "-c", "latest-default")
        git(seed, "commit", "--allow-empty", "-m", "latest default")
        git(seed, "push", "origin", "latest-default")
        git(origin, "symbolic-ref", "HEAD", "refs/heads/latest-default")
        git(seed, "push", "origin", "--delete", "new-default")
        git(main, "fetch", "origin")
        latest_worktree = Path(ensure_worktree(str(workdir), make_issue(44), github_client=github))
        assert git(latest_worktree, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
    if case in {"remote", "linked", "single_remote", "single_linked", "single_retry"}:
        git(seed, "push", "origin", "--delete", "issue-42")
        git(main, "fetch", "--prune", "origin")
        assert git(worktree, "rev-parse", "HEAD") == expected


def test_origin_branch_lookup_does_not_treat_remote_failure_as_missing(tmp_path, monkeypatch):
    def fail(command, **kwargs):
        return subprocess.CompletedProcess(command, 128, stdout="", stderr="access denied")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(RuntimeError, match="access denied"):
        GitHelper().fetch_origin_branch(tmp_path, "issue-42", missing_ok=True)


@pytest.mark.parametrize("divergent", [False, True])
def test_existing_pr_branch_fast_forwards_without_losing_local_work(
    tmp_path, monkeypatch, divergent
):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    workdir = tmp_path / "work"
    main = workdir / "owner_repo"

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "commit", "--allow-empty", "-m", "base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git(seed, "switch", "-c", "feature")
    git(seed, "push", "origin", "feature")
    workdir.mkdir()
    git(workdir, "clone", str(origin), str(main))
    git(main, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    git(main, "config", "user.name", "Test")
    git(main, "config", "user.email", "test@example.com")
    git(main, "switch", "-c", "feature")
    if divergent:
        git(main, "commit", "--allow-empty", "-m", "local work")
    local = git(main, "rev-parse", "HEAD")
    git(main, "switch", "main")
    git(main, "config", "branch.feature.remote", "custom")
    git(main, "config", "branch.feature.merge", "refs/heads/custom")
    git(seed, "commit", "--allow-empty", "-m", "remote work")
    git(seed, "push", "origin", "feature")
    remote = git(seed, "rev-parse", "HEAD")
    github = MagicMock()
    github.fetch_worktree_head.return_value = ("feature", "owner/repo")

    worktree = Path(ensure_worktree(str(workdir), make_pr(), github_client=github))

    assert git(worktree, "rev-parse", "HEAD") == (local if divergent else remote)
    assert git(main, "config", "branch.feature.remote") == "origin"
    assert git(main, "config", "branch.feature.merge") == "refs/heads/feature"


def test_analysis_existing_clone_without_origin_head(tmp_path, monkeypatch):
    from workdash.repo_worktree import analysis_worktree

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    workdir = tmp_path / "work"
    main = workdir / "owner_repo"

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.com")
    (seed / "source.py").write_text("latest source\n")
    git(seed, "add", "source.py")
    git(seed, "commit", "-m", "base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    workdir.mkdir()
    git(workdir, "clone", str(origin), str(main))
    git(main, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    git(main, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    (seed / "source.py").write_text("updated source\n")
    git(seed, "commit", "-am", "update")
    git(seed, "push", "origin", "main")
    github = MagicMock()

    with analysis_worktree(
        str(workdir), make_issue(), git=GitHelper(), github_client=github
    ) as path:
        assert (Path(path) / "source.py").read_text() == "updated source\n"
        assert git(path, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
        assert git(path, "branch", "--show-current") == ""
    assert not Path(path).exists()
    github.create_issue_branch.assert_not_called()


@pytest.mark.parametrize("single_branch", [False, True])
def test_analysis_refreshes_changed_remote_default(tmp_path, monkeypatch, single_branch):
    from workdash.repo_worktree import analysis_worktree

    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    origin, seed, workdir = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.com")
    (seed / "source").write_text("old default")
    git(seed, "add", "source")
    git(seed, "commit", "-m", "old")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    workdir.mkdir()
    main = workdir / "owner_repo"
    git(workdir, "clone", *(["--single-branch"] if single_branch else []), str(origin), str(main))
    git(main, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    fetch_config = git(main, "config", "--get-all", "remote.origin.fetch")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    git(seed, "switch", "-c", "new-default")
    (seed / "source").write_text("new default")
    git(seed, "commit", "-am", "new")
    git(seed, "push", "origin", "new-default")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/new-default")

    with analysis_worktree(
        str(workdir), make_issue(), git=GitHelper(), github_client=MagicMock()
    ) as path:
        assert (Path(path) / "source").read_text() == "new default"
        assert git(path, "rev-parse", "HEAD") == git(seed, "rev-parse", "HEAD")
    assert git(main, "config", "--get-all", "remote.origin.fetch") == fetch_config
    if single_branch:
        git(main, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        fetch_config = git(main, "config", "--get-all", "remote.origin.fetch")

    git(seed, "push", "origin", "--delete", "main")
    (seed / "source").write_text("later default")
    git(seed, "commit", "-am", "later")
    git(seed, "push", "origin", "new-default")
    with analysis_worktree(
        str(workdir), make_issue(), git=GitHelper(), github_client=MagicMock()
    ) as path:
        assert (Path(path) / "source").read_text() == "later default"
    assert git(main, "config", "--get-all", "remote.origin.fetch") == fetch_config
    assert git(main, "rev-parse", "main") != git(seed, "rev-parse", "HEAD")


@pytest.mark.parametrize("divergent", [False, True])
def test_local_issue_branch_updates_from_its_upstream(tmp_path, monkeypatch, divergent):
    def git(cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    origin, seed, workdir = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "work"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "init", "-b", "main", str(seed))
    git(seed, "config", "user.name", "Test")
    git(seed, "config", "user.email", "test@example.com")
    git(seed, "commit", "--allow-empty", "-m", "base")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git(seed, "switch", "-c", "shared")
    git(seed, "push", "origin", "shared")
    workdir.mkdir()
    main = workdir / "owner_repo"
    git(workdir, "clone", str(origin), str(main))
    git(main, "remote", "set-url", "origin", "https://github.com/owner/repo.git")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{origin}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")
    git(main, "config", "user.name", "Test")
    git(main, "config", "user.email", "test@example.com")
    git(main, "switch", "-c", "issue-42", "--track", "origin/shared")
    if divergent:
        git(main, "commit", "--allow-empty", "-m", "local")
    local = git(main, "rev-parse", "HEAD")
    git(main, "switch", "main")
    git(seed, "commit", "--allow-empty", "-m", "remote")
    git(seed, "push", "origin", "shared")
    remote = git(seed, "rev-parse", "HEAD")

    github = MagicMock()
    worktree = Path(ensure_worktree(str(workdir), make_issue(), github_client=github))
    assert git(worktree, "rev-parse", "HEAD") == (local if divergent else remote)
    assert git(worktree, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/shared"
    github.create_issue_branch.assert_not_called()


@pytest.mark.parametrize("target", [None, "owner/repo"])
def test_todo_worktrees_never_create_github_issue_branches(tmp_path, target):
    item = make_targeted_todo()
    item.todo_target = target
    git = MagicMock(spec=GitHelper)
    git.find_worktree_for_branch.return_value = None
    git.local_branch_exists.return_value = False
    git.fetch_origin_branch.return_value = False
    github = MagicMock(spec=GitHubClient)
    repo = target or item.repo
    main = main_repo_path(str(tmp_path), repo)
    main.mkdir()

    result = ensure_worktree(
        str(tmp_path), item, git=git, github_client=github, todo_repository="TESTUSER/TODOS"
    )

    branch = "wt-110" if target else "issue-110"
    expected = worktree_path(str(tmp_path), repo, 110, todo=target is not None)
    assert result == str(expected)
    github.create_issue_branch.assert_not_called()
    git.create_worktree.assert_called_once_with(main, expected, branch, "origin/HEAD")
