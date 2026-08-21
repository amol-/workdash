"""Step definitions for worktree feature scenarios.

Covers issue-worktree, pr-worktree, worktree-layout and worktree-reuse.
Real behaviour under test lives in ``workdash.repo_worktree.ensure_worktree``;
only ``subprocess.run`` is faked to avoid invoking real git/gh binaries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.repo_worktree import ensure_worktree

from .common import make_work_item


def _install_fake_git(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    workdir: Path,
    head_repo_owner: str = "owner",
    head_repo_name: str = "repo",
    head_ref: str = "feature-branch",
    existing_worktrees: dict[str, str] | None = None,
) -> None:
    """Patch ``subprocess.run`` with a recorder that simulates git/gh.

    :param dict existing_worktrees: Optional mapping of repo directory name
        to the branch it is currently on — used to simulate an existing
        worktree being discovered by ``git worktree list``.
    """

    recorded: list[list[str]] = []
    head_info = json.dumps(
        {
            "headRefName": head_ref,
            "headRepository": {"name": head_repo_name},
            "headRepositoryOwner": {"login": head_repo_owner},
        }
    )
    existing = existing_worktrees or {}

    def _porcelain() -> str:
        parts: list[str] = []
        for directory, branch in existing.items():
            parts.append(
                f"worktree {workdir / directory}\nHEAD abc123\nbranch refs/heads/{branch}\n\n"
            )
        return "".join(parts)

    def fake_run(*args, **kwargs):
        cmd = args[0]
        recorded.append(list(cmd))
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=head_info, stderr="")
        if cmd[:3] == ["gh", "repo", "clone"]:
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="origin/main\n", stderr="")
        if cmd[:2] == ["git", "switch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "merge", "--ff-only", "origin/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "remote", "get-url", "upstream"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[:3] == ["git", "remote", "add"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "remote", "set-url"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "worktree", "prune"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "show-ref"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd[:3] == ["git", "worktree", "list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=_porcelain(), stderr="")
        if cmd[:3] == ["git", "worktree", "add"]:
            wt_path = Path(cmd[-2]) if cmd[-3] == "-b" or "-b" in cmd else Path(cmd[3])
            # Layout: either ["add", "-b", branch, path, start] or ["add", path, branch]
            if "-b" in cmd:
                path_index = cmd.index("-b") + 2
                wt_path = Path(cmd[path_index])
            else:
                wt_path = Path(cmd[3])
            wt_path.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "config"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "pull"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "branch", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command in worktree scenario: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    scenario_state["_recorded_git_calls"] = recorded


# --------------------------------------------------------------------------
# F-WORKTREES-ISSUE
# --------------------------------------------------------------------------


@given("the user needs a worktree for an issue")
def _user_needs_issue_worktree(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        repo="owner/repo",
        number=42,
        title="Issue work",
    )
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(scenario_state, monkeypatch, workdir=tmp_path)


@when("the system prepares the worktree")
def _system_prepares_worktree(scenario_state: dict[str, Any]) -> None:
    workdir = scenario_state["workdir"]
    item = scenario_state["work_item"]
    scenario_state["worktree_path"] = ensure_worktree(str(workdir), item)


@then(parsers.parse('the worktree is checked out on a branch named "{branch_template}"'))
def _worktree_on_named_branch(branch_template: str, scenario_state: dict[str, Any]) -> None:
    item: WorkItem = scenario_state["work_item"]
    expected = branch_template.replace("<number>", str(item.number))
    recorded = scenario_state["_recorded_git_calls"]
    add_cmd = next(cmd for cmd in recorded if cmd[:3] == ["git", "worktree", "add"])
    assert expected in add_cmd, (expected, add_cmd)


@then("that branch was created from the repository's current default branch")
def _branch_from_default(scenario_state: dict[str, Any]) -> None:
    recorded = scenario_state["_recorded_git_calls"]
    add_cmd = next(cmd for cmd in recorded if cmd[:3] == ["git", "worktree", "add"])
    assert "origin/HEAD" in add_cmd, add_cmd
    assert any(cmd[:4] == ["git", "fetch", "--prune", "origin"] for cmd in recorded), (
        "expected git fetch origin before branch creation"
    )


# --------------------------------------------------------------------------
# F-WORKTREES-PR
# --------------------------------------------------------------------------


@given("the user needs a worktree for a pull request")
def _user_needs_pr_worktree(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        repo="owner/repo",
        number=42,
        title="PR work",
    )
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(scenario_state, monkeypatch, workdir=tmp_path)


@then("the worktree is checked out on the pull request's head branch")
def _worktree_on_head_branch(scenario_state: dict[str, Any]) -> None:
    recorded = scenario_state["_recorded_git_calls"]
    add_cmd = next(cmd for cmd in recorded if cmd[:3] == ["git", "worktree", "add"])
    assert "feature-branch" in add_cmd, add_cmd
    assert "origin/feature-branch" in add_cmd, add_cmd


@given("the user needs a worktree for an authored pull request that closes an issue")
def _user_needs_worktree_for_pr_closing_an_issue(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        repo="owner/repo",
        number=42149,
        title="Implement the issue",
    )
    item.linked_issue = ("owner/repo", 41830)
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(scenario_state, monkeypatch, workdir=tmp_path)


@then("the worktree directory is named after the issue the pull request closes")
def _worktree_named_after_linked_issue(scenario_state: dict[str, Any]) -> None:
    item: WorkItem = scenario_state["work_item"]
    assert item.linked_issue is not None
    assert Path(scenario_state["worktree_path"]).name == f"owner_repo_{item.linked_issue[1]}"


@given("the user already has a worktree opened from an issue")
def _user_already_has_an_issue_worktree(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "owner_repo").mkdir()
    (tmp_path / "owner_repo_41830").mkdir()
    scenario_state["workdir"] = tmp_path
    scenario_state["_pre_existing_worktree"] = tmp_path / "owner_repo_41830"

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{Path(kwargs['cwd']).resolve()}\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        if cmd[:2] == ["git", "pull"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command in linked issue reuse scenario: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)


@given("the user authored a pull request that closes that issue")
def _user_authored_a_pr_closing_that_issue(scenario_state: dict[str, Any]) -> None:
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        repo="owner/repo",
        number=42149,
        title="Implement the issue",
    )
    item.linked_issue = ("owner/repo", 41830)
    scenario_state["work_item"] = item


@given("the pull request originates in a fork of the upstream repository")
def _pr_originates_in_fork(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        repo="owner/repo",
        number=42,
        title="Fork PR",
    )
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(
        scenario_state,
        monkeypatch,
        workdir=tmp_path,
        head_repo_owner="contributor",
        head_repo_name="repo",
    )


@then("the worktree is backed by a clone of the fork's repository")
def _clone_from_fork(scenario_state: dict[str, Any]) -> None:
    recorded = scenario_state["_recorded_git_calls"]
    clone_cmd = next(cmd for cmd in recorded if cmd[:3] == ["gh", "repo", "clone"])
    assert "contributor/repo" in clone_cmd, clone_cmd


@then("the fork worktree has an upstream remote for the pull request's base repository")
def _fork_worktree_has_upstream_remote(scenario_state: dict[str, Any]) -> None:
    recorded = scenario_state["_recorded_git_calls"]
    assert ["git", "remote", "add", "upstream", "https://github.com/owner/repo.git"] in recorded
    assert ["git", "fetch", "--prune", "upstream"] in recorded


# --------------------------------------------------------------------------
# F-WORKTREES-LAYOUT
# --------------------------------------------------------------------------


@given("the repository has never been used on this machine")
def _repo_never_used(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        repo="owner/repo",
        number=7,
        title="Fresh issue",
    )
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(scenario_state, monkeypatch, workdir=tmp_path)


@given("a pull request comes from a fork of the upstream repository")
def _pr_from_fork_layout(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        repo="owner/repo",
        number=55,
        title="Fork layout PR",
    )
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(
        scenario_state,
        monkeypatch,
        workdir=tmp_path,
        head_repo_owner="contributor",
        head_repo_name="forked-repo",
    )


@given("several work items already have prepared worktrees")
def _other_worktrees_exist(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-create two sibling worktrees so we can later assert they remain.
    (tmp_path / "owner_repo").mkdir()
    (tmp_path / "owner_repo_1").mkdir()
    (tmp_path / "owner_repo_1" / "marker.txt").write_text("one")
    (tmp_path / "owner_repo_2").mkdir()
    (tmp_path / "owner_repo_2" / "marker.txt").write_text("two")
    scenario_state["_preexisting_worktrees"] = [
        tmp_path / "owner_repo_1",
        tmp_path / "owner_repo_2",
    ]
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        repo="owner/repo",
        number=3,
        title="New issue",
    )
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    _install_fake_git(scenario_state, monkeypatch, workdir=tmp_path)


@when("the user triggers an action that needs the worktree")
def _user_triggers_action_needing_worktree(scenario_state: dict[str, Any]) -> None:
    workdir = scenario_state["workdir"]
    item = scenario_state["work_item"]
    scenario_state["worktree_path"] = ensure_worktree(str(workdir), item)


@when("the user triggers an action that prepares a new worktree")
def _user_triggers_action_preparing_new(scenario_state: dict[str, Any]) -> None:
    _user_triggers_action_needing_worktree(scenario_state)


@then(parsers.parse('the system clones the repository into "{template}"'))
def _clone_into_templated_path(template: str, scenario_state: dict[str, Any]) -> None:
    workdir = scenario_state["workdir"]
    expected = Path(
        template.replace("<workdir>", str(workdir))
        .replace("<owner>", "owner")
        .replace("<repo>", "repo")
    )
    assert expected.is_dir(), f"Expected clone path {expected} to exist after clone"


@then("a worktree for the work item is created alongside it")
def _worktree_alongside(scenario_state: dict[str, Any]) -> None:
    workdir = scenario_state["workdir"]
    item: WorkItem = scenario_state["work_item"]
    expected = workdir / f"owner_repo_{item.number}"
    assert expected.is_dir(), f"Expected worktree {expected} to exist"


@then("the worktree directory is named after the fork's owner and repository")
def _worktree_named_after_fork(scenario_state: dict[str, Any]) -> None:
    wt_path = Path(scenario_state["worktree_path"])
    assert wt_path.name.startswith("contributor_forked-repo_"), wt_path
    assert wt_path.is_dir()


@then("the new worktree is created")
def _new_worktree_created(scenario_state: dict[str, Any]) -> None:
    wt_path = Path(scenario_state["worktree_path"])
    assert wt_path.is_dir(), wt_path


@then("the existing worktrees remain untouched")
def _existing_worktrees_untouched(scenario_state: dict[str, Any]) -> None:
    for pre_existing in scenario_state["_preexisting_worktrees"]:
        assert pre_existing.is_dir()
        assert (pre_existing / "marker.txt").read_text() in {"one", "two"}


# --------------------------------------------------------------------------
# F-WORKTREES-REUSE
# --------------------------------------------------------------------------


@given("the work item already has a prepared worktree")
def _work_item_has_worktree(
    scenario_state: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = make_work_item(
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        repo="owner/repo",
        number=9,
        title="Reuse PR",
    )
    (tmp_path / "owner_repo").mkdir()
    wt_dir = tmp_path / "owner_repo_9"
    wt_dir.mkdir()
    (wt_dir / "local_marker.txt").write_text("preserved")
    scenario_state["work_item"] = item
    scenario_state["workdir"] = tmp_path
    scenario_state["_pre_existing_worktree"] = wt_dir
    scenario_state["_pull_failed"] = False

    pull_behavior = {"fail": False}
    recorded: list[list[str]] = []

    def fake_run(*args, **kwargs):
        cmd = args[0]
        recorded.append(list(cmd))
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=f"{Path(kwargs['cwd']).resolve()}\n", stderr=""
            )
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="https://github.com/owner/repo.git\n", stderr=""
            )
        if cmd[:2] == ["git", "pull"]:
            if pull_behavior["fail"]:
                raise subprocess.CalledProcessError(1, cmd, stderr="diverged")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"Unexpected command in reuse scenario: {cmd}")

    monkeypatch.setattr(subprocess, "run", fake_run)
    scenario_state["_pull_behavior"] = pull_behavior
    scenario_state["_recorded_git_calls"] = recorded


@given("the remote has new commits that can be fast-forwarded")
def _remote_has_ff_commits(scenario_state: dict[str, Any]) -> None:
    scenario_state["_pull_behavior"]["fail"] = False


@given("local work in the worktree diverges from the remote")
def _local_work_diverges(scenario_state: dict[str, Any]) -> None:
    scenario_state["_pull_behavior"]["fail"] = True


@then("the same worktree is returned to the user")
def _same_worktree_returned(scenario_state: dict[str, Any]) -> None:
    returned = Path(scenario_state["worktree_path"])
    assert returned == scenario_state["_pre_existing_worktree"]


@then("the worktree is updated to the latest remote state")
def _worktree_updated(scenario_state: dict[str, Any]) -> None:
    recorded = scenario_state["_recorded_git_calls"]
    assert any(cmd[:3] == ["git", "pull", "--ff-only"] for cmd in recorded)


@then("the local work is not discarded")
def _local_work_preserved(scenario_state: dict[str, Any]) -> None:
    wt_dir = scenario_state["_pre_existing_worktree"]
    assert (wt_dir / "local_marker.txt").read_text() == "preserved"
