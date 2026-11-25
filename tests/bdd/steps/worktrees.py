"""Dashboard worktree journeys and the legacy Git fake used by todo and analysis."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when
from textual.widgets import Static

from workdash.backend import WorkdashBackend
from workdash.control import WorkdashSession, format_work_item_id
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from zellij_pane_launch import mark_pane_started

from .common import install_zellij_agent_fakes, make_valid_config, make_work_item, run_app


def _install_fake_git(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    workdir: Path,
    existing_worktrees: dict[str, str] | None = None,
) -> None:
    """Patch ``subprocess.run`` with a recorder that simulates git/gh.

    :param dict existing_worktrees: Optional mapping of repo directory name
        to the branch it is currently on — used to simulate an existing
        worktree being discovered by ``git worktree list``.
    """

    recorded: list[list[str]] = []
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
        if cmd[:3] == ["gh", "issue", "develop"]:
            raise subprocess.CalledProcessError(1, cmd)
        if cmd[:3] == ["gh", "repo", "clone"] or cmd[:2] == ["git", "clone"]:
            target = Path(cmd[-1])
            target.mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=str(kwargs["cwd"]), stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            origin = scenario_state.get("origin_url") or (
                scenario_state["todo_item"].todo_target
                if "todo_item" in scenario_state
                else "https://github.com/owner/repo.git"
            )
            if ":" not in origin:
                origin = f"https://github.com/{origin}.git"
            return subprocess.CompletedProcess(cmd, 0, stdout=origin, stderr="")
        if cmd == ["git", "ls-remote", "--symref", "origin", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="ref: refs/heads/main\tHEAD\nabc123\tHEAD\n", stderr=""
            )
        if cmd[:2] == ["git", "ls-remote"]:
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="")
        if cmd[:2] == ["git", "fetch"] or cmd[:4] == ["git", "remote", "set-head", "origin"]:
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
            elif "--detach" in cmd:
                wt_path = Path(cmd[4])
            else:
                wt_path = Path(cmd[3])
            wt_path.mkdir(parents=True, exist_ok=True)
            for name, content in scenario_state.get("target_source", {}).items():
                source = wt_path / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(content)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "worktree", "remove"]:
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


@then(parsers.parse('the worktree is checked out on a branch named "{branch_template}"'))
def _worktree_on_named_branch(branch_template: str, scenario_state: dict[str, Any]) -> None:
    item: WorkItem = scenario_state["work_item"]
    expected = branch_template.replace("<number>", str(item.number))
    recorded = scenario_state["_recorded_git_calls"]
    add_cmd = next(cmd for cmd in recorded if cmd[:3] == ["git", "worktree", "add"])
    assert expected in add_cmd, (expected, add_cmd)


_ISSUE_BODY = "Keep the existing keyboard shortcuts."
_DEFAULT_CODE = "current project code\n"
_PR_CODE = "proposed project code\n"
_LOCAL_EDIT = "my draft notes\n"
_REMOTE_EDIT = "new project code from upstream\n"


@pytest.fixture
def worktree_remote(tmp_path, monkeypatch, scenario_state):
    """Use a local Git origin; only the GitHub and Zellij boundaries are simulated."""
    remote = tmp_path / "remote"
    url = "https://github.com/owner/repo.git"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{remote}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", url)
    _git(tmp_path, "init", "-b", "main", str(remote))
    _git(remote, "config", "user.name", "Test User")
    _git(remote, "config", "user.email", "test@example.com")
    (remote / "source.py").write_text(_DEFAULT_CODE)
    (remote / "notes.txt").write_text("initial notes\n")
    _git(remote, "add", ".")
    _git(remote, "commit", "-m", "Default branch code")

    install_zellij_agent_fakes(scenario_state, monkeypatch)
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/zellij" if name == "zellij" else None
    )
    monkeypatch.setenv("SHELL", "/bin/sh")
    run = subprocess.run

    def external_command(command, **kwargs):
        match command[:3]:
            case ["gh", "repo", "clone"]:
                assert command[3] == "owner/repo"
                return run(["git", "clone", url, command[4]], **kwargs)
            case ["gh", "issue", "develop"]:
                _, _, _, _, _, repo, _, branch = command
                assert repo == "owner/repo"
                _git(remote, "branch", branch, "main")
                payload = ""
            case ["gh", "issue", "view"]:
                item = scenario_state["dashboard_item"]
                assert command[3:6] == [str(item.number), "--repo", item.repo]
                payload = json.dumps(
                    dict(number=item.number, title=item.title, url=item.url, body=_ISSUE_BODY)
                )
            case ["gh", "pr", "view"]:
                assert command[3:6] == ["73", "--repo", "owner/repo"]
                payload = json.dumps(
                    dict(
                        headRefName=scenario_state["pr_branch"],
                        headRepository={"name": "repo"},
                        headRepositoryOwner={"login": "owner"},
                    )
                )
            case _ if Path(command[0]).name == "zellij":
                assert "new-pane" in command, command
                cwd = command[command.index("--cwd") + 1]
                scenario_state["terminal_cwd"] = Path(cwd)
                scenario_state["panes"].append(
                    {"id": 22, "title": f"terminal_{Path(cwd).name}", "pane_cwd": cwd}
                )
                mark_pane_started(command, "22")
                payload = ""
            case _:
                assert command[0] == "git", command
                return run(command, **kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", external_command)
    return remote


@given("an issue is selected for a repository I have not cloned")
def _select_new_issue(scenario_state, worktree_remote):
    scenario_state["dashboard_item"] = make_work_item(number=42, title="Keyboard shortcuts")


@given("a pull request by another author is selected")
def _select_pr_for_review(scenario_state, worktree_remote):
    scenario_state["pr_branch"] = "contribution"
    _git(worktree_remote, "switch", "-c", "contribution")
    (worktree_remote / "source.py").write_text(_PR_CODE)
    _git(worktree_remote, "commit", "-am", "Proposed changes")
    _git(worktree_remote, "switch", "main")
    scenario_state["dashboard_item"] = make_work_item(
        number=73, item_type=WorkItemType.PR, kind=WorkItemKind.TRACKED_PR
    )


@given("I started work from an issue and left local changes in its checkout")
@given("I closed the terminal for an issue after making local edits")
def _leave_issue_work(scenario_state, worktree_remote, tmp_path, monkeypatch):
    _select_new_issue(scenario_state, worktree_remote)
    _open_from_code_menu("t", scenario_state, tmp_path, monkeypatch)
    checkout = scenario_state.pop("terminal_cwd")
    (checkout / "notes.txt").write_text(_LOCAL_EDIT)
    scenario_state["original_checkout"] = checkout
    scenario_state["panes"].clear()  # The user has exited the previous shell.


@given("my pull request from that branch now replaces the issue on the dashboard")
def _authored_pr_replaces_issue(scenario_state, worktree_remote):
    issue = scenario_state["dashboard_item"]
    branch = _git(scenario_state["original_checkout"], "branch", "--show-current")
    scenario_state["pr_branch"] = branch
    _git(worktree_remote, "switch", branch)
    (worktree_remote / "source.py").write_text(_PR_CODE)
    _git(worktree_remote, "commit", "-am", "First implementation")
    _git(worktree_remote, "switch", "main")
    pr = make_work_item(number=73, item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR)
    pr.closing_issue_numbers = (issue.number,)
    scenario_state["dashboard_item"] = pr


@given("a new commit is available on its branch that does not conflict with my edits")
def _publish_new_code(worktree_remote):
    _git(worktree_remote, "switch", "issue-42")
    (worktree_remote / "source.py").write_text(_REMOTE_EDIT)
    _git(worktree_remote, "commit", "-am", "Upstream changes")
    _git(worktree_remote, "switch", "main")


@when(parsers.parse('I press "c" and choose "{choice}" from the code menu'))
def _open_from_code_menu(choice, scenario_state, tmp_path, monkeypatch):
    item = scenario_state["dashboard_item"]
    config = replace(make_valid_config(), workdir=str(tmp_path / "work"))
    backend = WorkdashBackend(config=config, cache_root=tmp_path / "cache")
    monkeypatch.setattr(backend.github_client, "work_pull_request", lambda *_args, **_kwargs: None)
    session = WorkdashSession(
        config=config,
        backend=backend,
        work_items=[item],
        suggestion_markers={},
        zellij_session="workdash-main",
    )

    async def interactions(app, pilot):
        footer = app.query_one("#status-footer", Static)
        await pilot.press("c", choice)
        async with asyncio.timeout(5):
            while not (status := footer.render().plain).startswith(
                ("Launched", "Opened terminal", "Launch failed", "Terminal failed")
            ):
                await asyncio.sleep(0.01)
        scenario_state["worktree_status"] = status

    run_app(
        config=config,
        work_items=[item],
        session=session,
        interactions=interactions,
        launch_callback=lambda item, agent: session.code(
            target=format_work_item_id(item), agent=agent, allow_vscode=True
        ),
        terminal_callback=lambda item: session.terminal(target=format_work_item_id(item)),
    )
    if choice == "t":
        assert scenario_state["worktree_status"] == (
            f"Opened terminal for {item.item_type.value} {item.repo}#{item.number}."
        )


@then("the chosen coding agent opens with the repository's current default-branch code")
def _agent_starts_with_default_code(scenario_state, worktree_remote):
    [(cwd, _, command, _)] = scenario_state["agent_launches"]
    assert command == ["claude"]
    path = Path(cwd)
    assert (path / "source.py").read_text() == _DEFAULT_CODE
    assert _git(path, "rev-parse", "HEAD") == _git(worktree_remote, "rev-parse", "main")


@then("the agent is preloaded with the issue's GitHub context")
def _agent_sees_issue(scenario_state):
    item = scenario_state["dashboard_item"]
    [(_, prompt, _, _)] = scenario_state["agent_launches"]
    for context in (item.repo, str(item.number), item.url, _ISSUE_BODY):
        assert context in prompt


@then("the TUI reports that the session was launched for that issue")
def _agent_launch_reported(scenario_state):
    status = scenario_state["worktree_status"]
    assert status == "Launched claude for issue owner/repo#42.", status


@then("the terminal opens on the pull request's branch with its proposed changes")
def _terminal_has_pr_changes(scenario_state, worktree_remote):
    cwd = scenario_state["terminal_cwd"]
    assert _git(cwd, "branch", "--show-current") == "contribution"
    assert _git(cwd, "rev-parse", "HEAD") == _git(worktree_remote, "rev-parse", "contribution")
    assert (cwd / "source.py").read_text() == _PR_CODE


@then("the terminal opens in the same checkout with my local changes")
def _same_checkout_with_local_edits(scenario_state):
    checkout = scenario_state["terminal_cwd"]
    assert checkout == scenario_state["original_checkout"]
    assert (checkout / "notes.txt").read_text() == _LOCAL_EDIT


@then("the latest remote changes are visible in the checkout")
def _checkout_has_updated_code(scenario_state):
    assert (scenario_state["terminal_cwd"] / "source.py").read_text() == _REMOTE_EDIT


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()
