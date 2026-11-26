"""Step definitions for CLI orchestration feature scenarios."""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_bdd import given, parsers, then, when

import workdash.github_client as github_client_module
from gh_work_state_fake import failure, install_fake_github
from workdash.backend import compute_suggestion_markers
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType

from .common import NOW_UTC, ensure_api_session, install_zellij_agent_fakes, make_work_item


def _cli_config(workdir: str) -> WorkdashConfig:
    return WorkdashConfig(
        github_username="testuser",
        agents={
            "codex": AgentConfig(analyze="codex exec", launch="codex"),
            "claude": AgentConfig(analyze="claude -p", launch="claude"),
            "pi": AgentConfig(launch="pi"),
        },
        repositories=("owner/repo",),
        workdir=workdir,
        todo_repository="testuser/todos",
    )


def _run_workdash(
    argv: list[str],
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    real_worktree: bool = False,
) -> None:
    import workdash.control as control_module
    import workdash.workdash as workdash_module

    workdir = scenario_state.get("workdir", "/tmp/workdash-bdd")
    config = _cli_config(workdir)
    items = list(scenario_state.get("work_items", []))
    markers = compute_suggestion_markers(items)
    real_backend_class = workdash_module.WorkdashBackend

    class FakeAnalyzer:
        def analyze(self, _item, command_tokens=None):  # pragma: no cover - should not run
            raise AssertionError("analyzer should not run with malformed command")

    class FakeBackend:
        def __init__(self, config=None, **kwargs) -> None:
            self.real = real_backend_class(config=config, analyzer=FakeAnalyzer())
            self.analysis_cache = self.real.analysis_cache

        def load_items(self, progress_callback=None):
            scenario_state["backend_loads"] = scenario_state.get("backend_loads", 0) + 1
            assert progress_callback is None
            return list(items), dict(markers)

        def resolve_analyze_command_tokens(self, tool="codex"):
            return self.real.resolve_analyze_command_tokens(tool)

        def analyze_item(self, item, tool="codex"):
            scenario_state.setdefault("analyze_calls", []).append((item, tool))
            if tool == "cached":
                return scenario_state.get("cached_analysis_path") if item.analysis else None
            if "analysis_path" not in scenario_state:
                raise AssertionError("scenario must set analysis_path from tmp_path")
            analysis_path = Path(scenario_state["analysis_path"])
            analysis_path.parent.mkdir(parents=True, exist_ok=True)
            analysis_path.write_text("analysis body\n", encoding="utf-8")
            return str(analysis_path)

    github_run = subprocess.run
    ensure_worktree = control_module.ensure_worktree

    def fake_run(*args, **kwargs):
        cmd = args[0]
        if real_worktree and cmd[0] == "git":
            return github_run(*args, **kwargs)
        if cmd[:2] == ["gh", "api"] and scenario_state.get("github_lookup_installed"):
            return github_run(*args, **kwargs)
        if cmd[:3] == ["gh", "issue", "view"] and scenario_state.get("real_launch_context"):
            return github_run(*args, **kwargs)
        if cmd[:3] == ["gh", "pr", "view"] and scenario_state.get("github_lookup_installed"):
            return github_run(*args, **kwargs)
        if cmd == ["git", "rev-parse", "--show-toplevel"]:
            cwd = Path(kwargs["cwd"]).resolve()
            if str(kwargs["cwd"]) in scenario_state.get("git_origins", {}):
                return subprocess.CompletedProcess(cmd, 0, stdout=f"{cwd}\n", stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == ["git", "config", "--local", "--get", "remote.origin.url"]:
            repo = scenario_state.get("git_origins", {}).get(str(kwargs.get("cwd")))
            if repo is not None:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"https://github.com/{repo}.git\n", stderr=""
                )
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    if scenario_state.get("api_session") is not None:

        class FakeControlClient:
            def __init__(self, host="127.0.0.1") -> None:
                assert host == (argv[argv.index("--host") + 1] if "--host" in argv else "127.0.0.1")
                scenario_state["client_host"] = host

            def request(
                self, endpoint: str, payload: dict[str, object] | None = None
            ) -> dict[str, object]:
                payload = payload or {}
                scenario_state.setdefault("control_requests", []).append(
                    {"endpoint": endpoint, "payload": dict(payload)}
                )
                if endpoint == "info":
                    return scenario_state["api_session"].info(panes=payload.get("panes", "work"))
                if endpoint == "analyze":
                    return scenario_state["api_session"].analyze(
                        target=payload["target"],
                        agent=payload.get("agent"),
                        group=payload.get("group"),
                    )
                if endpoint == "code":
                    return scenario_state["api_session"].code(
                        target=payload["target"],
                        agent=payload.get("agent"),
                        autostart=payload.get("autostart", False),
                        group=payload.get("group"),
                        autoclose=payload.get("autoclose", False),
                    )
                if endpoint == "dispatch":
                    return scenario_state["api_session"].dispatch_once(
                        scope=payload["scope"],
                        agent=payload.get("agent"),
                        group=payload.get("group"),
                    )
                if endpoint == "terminal":
                    return scenario_state["api_session"].terminal(
                        target=payload["target"],
                    )
                if endpoint == "ready":
                    return scenario_state["api_session"].ready(scope=payload["scope"])
                if endpoint == "pane/content":
                    return scenario_state["api_session"].pane_content(
                        pane_id=payload["pane_id"],
                        full=bool(payload.get("full", False)),
                    )
                if endpoint == "pane/close":
                    return scenario_state["api_session"].pane_close(
                        pane_id=payload["pane_id"],
                    )
                raise AssertionError(f"Unexpected control endpoint: {endpoint}")

        monkeypatch.setattr(workdash_module, "WorkdashControlClient", FakeControlClient)
        install_zellij_agent_fakes(scenario_state, monkeypatch)
        if "known_worktree_path" in scenario_state:
            monkeypatch.setattr(
                control_module,
                "existing_worktree_path",
                lambda _workdir, _item, **kwargs: scenario_state["known_worktree_path"],
            )
        monkeypatch.setattr(control_module, "get_merge_base", lambda _path, **kwargs: None)
        if not scenario_state.get("real_launch_context"):
            monkeypatch.setattr(
                control_module,
                "prepare_launch_agent_prompt",
                lambda *args, **kwargs: "PROMPT",
            )
        monkeypatch.setattr(
            control_module,
            "launch_terminal_context",
            lambda repo_path, *, zellij_session=None: (
                scenario_state.setdefault("terminal_launch_calls", []).append(
                    (repo_path, zellij_session)
                )
                or SimpleNamespace(
                    session=zellij_session,
                    pane_id="terminal_23",
                    pane_title="terminal_owner_repo_1",
                    cwd=repo_path,
                )
            ),
        )
        if "pane_content" in scenario_state:

            def dump_pane(_session, pane_id, *, full=False):
                return scenario_state["pane_content"][pane_id]

            monkeypatch.setattr(control_module.launcher.zellij, "dump_zellij_pane", dump_pane)

        def close_pane(_session, pane_id):
            panes = scenario_state["panes"]
            panes.remove(next(pane for pane in panes if f"terminal_{pane['id']}" == pane_id))

        monkeypatch.setattr(control_module.launcher.zellij, "close_zellij_pane", close_pane)

    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)
    if scenario_state.get("client_missing_tools"):
        monkeypatch.setattr(
            workdash_module.GitHubClient,
            "check_authentication",
            lambda self: (_ for _ in ()).throw(AssertionError("unexpected GitHub preflight")),
        )
    else:
        monkeypatch.setattr(workdash_module.GitHubClient, "check_authentication", lambda self: None)
    monkeypatch.setattr(workdash_module, "load_config", lambda: config)
    monkeypatch.setattr(workdash_module, "WorkdashBackend", FakeBackend)
    monkeypatch.setattr(
        workdash_module,
        "ensure_worktree",
        lambda workdir, item, **_kwargs: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(Path(workdir) / "owner_repo_1")
        ),
    )
    # Also mock repo_worktree.ensure_worktree for control.py which imports it directly
    import workdash.repo_worktree as repo_worktree_module

    monkeypatch.setattr(
        repo_worktree_module,
        "ensure_worktree",
        lambda workdir, item, **_kwargs: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or str(Path(workdir) / "owner_repo_1")
        ),
    )
    # Mock control_module.ensure_worktree since control.py imports it
    monkeypatch.setattr(
        "workdash.github_client.GitHubClient.fetch_worktree_head",
        lambda self, item: ("pr-branch", item.repo),
    )
    monkeypatch.setattr(
        control_module,
        "GitHelper",
        lambda: SimpleNamespace(
            worktree_proves_item=lambda path, item: (
                scenario_state.get("git_origins", {}).get(str(path)) == item.repo
            ),
            branch_tracks_repository=lambda path, branch, repo: (
                (str(path), branch, repo) in scenario_state["pr_worktrees"].values()
                if "pr_worktrees" in scenario_state
                else True
            ),
            find_worktree_for_branch=lambda *_: None,
        ),
    )
    if "api_session" in scenario_state and not real_worktree:
        scenario_state["api_session"].git = control_module.GitHelper()
    monkeypatch.setattr(
        control_module,
        "ensure_worktree",
        ensure_worktree
        if real_worktree
        else lambda workdir, item, **_kwargs: (
            scenario_state.setdefault("ensure_calls", []).append((workdir, item))
            or scenario_state.get("pr_worktree_paths", {}).get(
                item.number, str(Path(workdir) / "owner_repo_1")
            )
        ),
    )
    monkeypatch.setattr(
        control_module,
        "analysis_worktree",
        lambda *args, **kwargs: nullcontext(control_module.ensure_worktree(*args, **kwargs)),
    )
    monkeypatch.setattr(
        workdash_module.launcher.zellij,
        "list_workdash_sessions",
        lambda: list(scenario_state.get("sessions", [])),
    )

    scenario_state["exit_code"] = workdash_module.main(argv)
    captured = capsys.readouterr()
    scenario_state["stdout"] = captured.out
    scenario_state["stderr"] = captured.err
    scenario_state["output"] = captured.out + captured.err


@given("exactly one active Workdash-owned Zellij session exists")
def _one_workdash_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = ["workdash-main"]


@given("multiple active Workdash-owned Zellij sessions exist")
def _multiple_workdash_sessions(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = ["workdash-main", "workdash-alt"]


@given("no active Workdash-owned Zellij session exists")
def _no_workdash_sessions(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = []


@given("an active Workdash-owned Zellij session exists")
def _a_workdash_session(scenario_state: dict[str, Any]) -> None:
    scenario_state["sessions"] = ["workdash-main"]


@given("that session has an agent and a terminal for `owner/repo#ISSUE-1`")
def _session_has_work_panes(scenario_state: dict[str, Any], tmp_path: Path) -> None:
    cwd = str(tmp_path / "wrk" / "owner_repo_1")
    scenario_state["known_worktree_path"] = cwd
    scenario_state["panes"] = [
        {"id": 1, "title": "code_owner_repo_1", "pane_cwd": cwd, "pane_command": "pi"},
        {"id": 2, "title": "terminal_owner_repo_1", "pane_cwd": cwd, "pane_command": "bash"},
    ]


@given("the user has an ordinary shell in the `scratch` tab")
def _session_has_ordinary_shell(scenario_state: dict[str, Any]) -> None:
    scenario_state["panes"].append(
        {"id": 11, "title": "shell", "tab_name": "scratch", "pane_command": "bash"}
    )


@given('a work terminal `terminal_23` displays "Local checks finished."')
def _work_terminal_has_output(scenario_state: dict[str, Any]) -> None:
    scenario_state["panes"] = [{"id": 23, "title": "terminal_owner_repo_1"}]
    scenario_state["pane_content"] = {"terminal_23": "Local checks finished.\n"}


@given("work terminals `terminal_26` and `terminal_27` are open")
def _work_terminals_are_open(scenario_state: dict[str, Any]) -> None:
    scenario_state["panes"] = [
        {"id": 26, "title": "terminal_owner_repo_1"},
        {"id": 27, "title": "terminal_owner_repo_2"},
    ]


@given("the current Workdash items include an assigned issue without cached analysis")
def _current_items_include_uncached_issue(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    scenario_state.setdefault("workdir", str(tmp_path / "wrk"))
    item = make_work_item(
        item_type=WorkItemType.ISSUE,
        kind=WorkItemKind.ASSIGNED_ISSUE,
        number=1,
        title="Fix the issue",
        created_at=NOW_UTC,
        updated_at=NOW_UTC,
    )
    work_items[:] = [item]
    scenario_state["work_items"] = list(work_items)
    scenario_state["analysis_path"] = str(tmp_path / "analysis.md")


@given("the current Workdash items include an assigned issue")
def _current_items_include_assigned_issue(
    scenario_state: dict[str, Any], work_items: list[WorkItem], tmp_path: Path
) -> None:
    _current_items_include_uncached_issue(scenario_state, work_items, tmp_path)


@when("the user runs `workdash info`")
def _run_info(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash info --all`")
def _run_info_all(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["info", "--all"], scenario_state, monkeypatch, capsys)


@when("the user starts `workdash` and retries `workdash info`")
def _retry_info_after_startup(
    scenario_state: dict[str, Any],
    work_items: list[WorkItem],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Model the dashboard becoming available; startup itself has its own journeys.
    ensure_api_session(scenario_state, work_items, tmp_path)
    _run_workdash(["info"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex`")
def _run_analyze(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["analyze", "owner/repo#ISSUE-1", "--agent", "codex"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash code owner/repo#ISSUE-1 --agent codex --json`")
def _run_code_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["code", "owner/repo#ISSUE-1", "--agent", "codex", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash analyze owner/repo#ISSUE-99 --agent codex --json`")
def _run_analyze_unknown_issue_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["analyze", "owner/repo#ISSUE-99", "--agent", "codex", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash code owner/repo#ISSUE-1 --agent vscode --json`")
def _run_code_vscode_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["code", "owner/repo#ISSUE-1", "--agent", "vscode", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when(parsers.parse("the user runs `workdash {action:w} {target} --agent {agent:w}`"))
def _run_action_with_agent(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    target: str,
    agent: str,
) -> None:
    if action == "code" and scenario_state.get("github_lookup_installed"):
        scenario_state["real_launch_context"] = True
    _run_workdash(
        [action, target, "--agent", agent],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash terminal owner/repo#ISSUE-1`")
def _run_terminal(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["terminal", "owner/repo#ISSUE-1"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@when("the user runs `workdash read terminal_23`")
def _run_read(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["read", "terminal_23"], scenario_state, monkeypatch, capsys)


@when("the user runs `workdash close terminal_26`")
def _run_close(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(["close", "terminal_26"], scenario_state, monkeypatch, capsys)


@then(
    "the listing identifies `terminal_1` as the agent and `terminal_2` as the terminal "
    "for `owner/repo#ISSUE-1`"
)
def _listing_identifies_work_panes(scenario_state: dict[str, Any]) -> None:
    lines = scenario_state["stdout"].splitlines()
    assert any(
        line.split()[:2] == ["agent", "terminal_1"] and "item=owner/repo#ISSUE-1" in line
        for line in lines
    )
    assert any(
        line.split()[:2] == ["terminal", "terminal_2"] and "item=owner/repo#ISSUE-1" in line
        for line in lines
    )


@given("authored pull request `owner/repo#50` has a branch but no linked issue")
def _unlinked_authored_pr(scenario_state, monkeypatch, tmp_path):
    github = install_fake_github(monkeypatch)
    github.failures["ReadyPullRequests"] = failure("PR readiness unavailable")
    scenario_state["pull_request"] = github.add_pull_request("owner/repo#50")
    github.add_issue("owner/repo#1")
    session = scenario_state["api_session"]
    monkeypatch.setattr(
        session.backend.github_client,
        "fetch_worktree_head",
        lambda item: (f"pr-{item.number}", item.repo),
    )

    main = Path(session.config.workdir) / "owner_repo"
    remote = tmp_path / "remote.git"
    main.mkdir(parents=True)

    def git(*args):
        subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)

    git("init", "-qb", "main")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "--allow-empty",
        "-qm",
        "initial",
    )
    git("branch", "pr-50")
    git("branch", "issue-1")
    git("clone", "--bare", ".", str(remote))
    url = "https://github.com/owner/repo.git"
    git("config", f"url.{remote}.insteadOf", url)
    git("remote", "add", "origin", url)
    git("fetch", "origin")
    git("remote", "set-head", "origin", "main")


@when("the user runs `workdash code https://github.com/owner/repo/pull/50`")
def _code_named_pr(scenario_state, monkeypatch, capsys):
    _run_workdash(
        ["code", "https://github.com/owner/repo/pull/50"],
        scenario_state,
        monkeypatch,
        capsys,
        real_worktree=True,
    )


@then(parsers.parse("code opens in the checkout for `{checkout}`"))
def _code_opens_in_checkout(checkout, scenario_state):
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    assert "Status: launched" in scenario_state["stdout"]
    path = Path(scenario_state["api_session"].config.workdir) / checkout
    assert f"Cwd: {path}\n" in scenario_state["stdout"]


@when("the author links pull request `owner/repo#50` to issue `owner/repo#1`")
def _link_pull_request(scenario_state):
    path = Path(scenario_state["agent_launches"][0][0]) / "draft.txt"
    path.write_text("local work", encoding="utf-8")
    scenario_state["pull_request"]["closes"] = ("owner/repo#1",)


@then("the original pull request checkout is left untouched")
def _old_checkout_left_untouched(scenario_state):
    path = Path(scenario_state["agent_launches"][0][0])
    assert (path / "draft.txt").read_text(encoding="utf-8") == "local work"
    assert (
        subprocess.check_output(["git", "branch", "--show-current"], cwd=path, text=True).strip()
        == "pr-50"
    )


@then("the command tells the user to start it with `workdash`")
def _info_guides_startup(scenario_state: dict[str, Any]) -> None:
    assert "Start one with `workdash`." in scenario_state["stderr"]


@then("the listing identifies the shell in `scratch` as `terminal_11`")
def _listing_identifies_shell(scenario_state: dict[str, Any]) -> None:
    assert any(
        "terminal_11 shell " in line and "tab=scratch" in line
        for line in scenario_state["stdout"].splitlines()
    )


@then(parsers.parse("the listing shows `{pane_id}`"))
def _listing_shows_pane(pane_id: str, scenario_state: dict[str, Any]) -> None:
    assert pane_id in scenario_state["stdout"].split()


@then(parsers.parse("the listing does not show `{pane_id}`"))
def _listing_does_not_show_pane(pane_id: str, scenario_state: dict[str, Any]) -> None:
    assert pane_id not in scenario_state["stdout"].split()


@then('the command displays "Local checks finished."')
def _displays_local_check_output(scenario_state: dict[str, Any]) -> None:
    assert "Local checks finished." in scenario_state["stdout"]


@then("the system reports the Workdash session name")
@then("the system reports the Workdash-owned session name")
def _reports_session_name(scenario_state: dict[str, Any]) -> None:
    assert "Session: workdash-main" in scenario_state["stdout"]


@then("the system does not choose a session")
def _does_not_choose_session(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0
    assert "Session:" not in scenario_state["stdout"]


@then("the command exits with a non-zero status")
def _exits_non_zero(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] != 0


@then("the system lists the candidate Workdash-owned sessions")
def _lists_candidate_sessions(scenario_state: dict[str, Any]) -> None:
    output = scenario_state["output"]
    assert "workdash-main" in output
    assert "workdash-alt" in output


@then("the system asks the user to pass `--session`")
def _asks_for_session(scenario_state: dict[str, Any]) -> None:
    assert "--session" in scenario_state["output"]


@then("the system analyzes the current item with the selected configured agent")
def _analyzes_with_selected_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    item = scenario_state["work_items"][0]
    assert scenario_state["analyze_calls"] == [(item, "cached"), (item, "codex")]
    assert scenario_state["ensure_calls"] == [(scenario_state["workdir"], item)]


@then("the system launches code for the current item with the selected configured agent")
def _launches_code_with_selected_agent(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    item = scenario_state["work_items"][0]
    assert scenario_state["ensure_calls"] == [(scenario_state["workdir"], item)]
    assert scenario_state["agent_launches"] == [
        (
            str(Path(scenario_state["workdir"]) / "owner_repo_1"),
            "PROMPT",
            ["codex"],
            "workdash-main",
        )
    ]


@then(
    "the system returns JSON with status `launched`, the item ID, selected agent, selected "
    "session, cwd, pane title, and pane ID"
)
def _returns_code_json(scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    assert payload == {
        "status": "launched",
        "item_id": "owner/repo#ISSUE-1",
        "session": "workdash-main",
        "agent": "codex",
        "cwd": str(Path(scenario_state["workdir"]) / "owner_repo_1"),
        "pane_title": "code_owner_repo_1",
        "pane_id": "terminal_23",
        "tab_name": None,
    }


@then("the command shows the Markdown report link for `owner/repo#ISSUE-1` from `codex`")
def _reports_markdown_link(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    assert {
        "Item: owner/repo#ISSUE-1",
        "Agent: codex",
        "Analysis URL: http://127.0.0.1:8765/api/items/owner/repo%23ISSUE-1/analysis",
    } <= set(scenario_state["stdout"].splitlines())


@then("the command identifies the new shell's pane and checkout for `owner/repo#ISSUE-1`")
def _identifies_shell_and_checkout(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state["stderr"]
    assert {
        "Item: owner/repo#ISSUE-1",
        f"Cwd: {Path(scenario_state['workdir']) / 'owner_repo_1'}",
        "Pane id: terminal_23",
    } <= set(scenario_state["stdout"].splitlines())


@then(parsers.parse('the system reports "{message}"'))
def _reports_error_message(scenario_state: dict[str, Any], message: str) -> None:
    assert f"Error: {message}" in scenario_state["stderr"]


@then("the system reports that an active Workdash-owned Zellij session is required")
def _reports_session_required(scenario_state: dict[str, Any]) -> None:
    assert "active Workdash-owned Zellij session" in scenario_state["output"]
    assert "required" in scenario_state["output"]


@given(
    "the configured agents are `pi` with only a launch command "
    "and `aider` with only an analyze command"
)
def _configured_agents_pi_launch_only_aider_analyze_only(scenario_state: dict[str, Any]) -> None:
    scenario_state["api_session"].config = WorkdashConfig(
        github_username="testuser",
        agents={
            "pi": AgentConfig(launch="pi"),
            "aider": AgentConfig(analyze="aider --message"),
        },
        repositories=("owner/repo",),
        workdir=scenario_state.get("workdir", "/tmp/workdash-bdd"),
        todo_repository="testuser/todos",
    )


@given("the current Workdash items do not include `owner/repo#ISSUE-99`")
def _current_items_do_not_include_issue_99(scenario_state: dict[str, Any]) -> None:
    assert all(item.number != 99 for item in scenario_state.get("work_items", []))


@given("the client process cannot find GitHub CLI or Zellij on PATH")
def _client_process_cannot_find_github_or_zellij(scenario_state: dict[str, Any]) -> None:
    scenario_state["client_missing_tools"] = True


@then("the command requests code launch from the local Workdash server")
def _requests_code_launch(scenario_state: dict[str, Any]) -> None:
    control_requests = scenario_state.get("control_requests", [])
    assert any(req["endpoint"] == "code" for req in control_requests)


@given("pull request #51 has an existing worktree on branch `pr-branch`")
def _pr_worktree(scenario_state, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run = subprocess.run
    run(["git", "init", "-q", str(repo)], check=True)
    run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    path = str(Path(scenario_state["api_session"].config.workdir) / "owner_repo_51")
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    run(["git", "-C", str(repo), "worktree", "add", "-qb", "pr-branch", path], check=True)
    scenario_state["pr_worktrees"] = {51: (path, "pr-branch", "owner/repo")}
    scenario_state["pr_worktree_paths"] = {51: path}


@when(parsers.parse("the user runs `workdash code {target} --agent pi --autostart`"))
def _start_autonomous_code(target, scenario_state, monkeypatch, capsys):
    scenario_state["real_launch_context"] = True
    _run_workdash(
        ["code", target, "--agent", "pi", "--autostart"], scenario_state, monkeypatch, capsys
    )


@then(parsers.parse("the command reports that pi launched in a pane for `{item_id}`"))
def _reports_launched_agent(item_id: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0
    lines = scenario_state["stdout"].splitlines()
    assert {"Status: launched", f"Item: {item_id}", "Agent: pi"} <= set(lines)
    assert any(line.startswith("Pane id: terminal_") for line in lines)


@then("the command shows the existing pull request checkout as the pane's working directory")
def _reports_pull_request_checkout(scenario_state: dict[str, Any]) -> None:
    path, _branch, _repo = scenario_state["pr_worktrees"][51]
    assert f"Cwd: {path}" in scenario_state["stdout"].splitlines()


@then("the agent is asked to start implementing without routine confirmation")
def _starts_implementation(scenario_state):
    [(_, prompt, _, _)] = scenario_state["agent_launches"]
    assert "start implementing without asking for routine confirmation" in " ".join(prompt.split())


@then("the command confirms that it accepted the close")
def _confirms_pane_close_accepted(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["stdout"] == "Accepted close for terminal_26.\n"


@then(parsers.parse("the command connects to host `{host}`"))
def _command_connects_to_host(host: str, scenario_state: dict[str, Any]) -> None:
    assert scenario_state["client_host"] == host


@when("the user runs `workdash code owner/repo#99 --agent pi --host dashboard.lan --json`")
def _run_remote_code_json(
    scenario_state: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_workdash(
        ["code", "owner/repo#99", "--agent", "pi", "--host", "dashboard.lan", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@then(
    parsers.parse(
        "the system returns JSON with status `launched` for `{item_id}` and the new pane ID"
    )
)
def _returns_launched_json(item_id: str, scenario_state: dict[str, Any]) -> None:
    payload = json.loads(scenario_state["stdout"])
    [launched_pane] = scenario_state["panes"]
    assert (payload["status"], payload["item_id"], payload["pane_id"]) == (
        "launched",
        item_id,
        f"terminal_{launched_pane['id']}",
    )


@then("the system prints status `already_running` and the running pane ID")
def _prints_already_running(scenario_state: dict[str, Any]) -> None:
    assert scenario_state["exit_code"] == 0, scenario_state
    assert "Status: already_running" in scenario_state["stdout"]
    assert "Agent: -" in scenario_state["stdout"]
    assert "Pane id: terminal_7" in scenario_state["stdout"]


@when("the user runs `workdash code owner/repo#ISSUE-1 --group epic --host dashboard.lan --json`")
def _run_grouped_code(scenario_state, monkeypatch, capsys):
    _run_workdash(
        ["code", "owner/repo#ISSUE-1", "--group", "epic", "--host", "dashboard.lan", "--json"],
        scenario_state,
        monkeypatch,
        capsys,
    )


@then("the result reports the agent in tab `epic`")
def _grouped_code_result(scenario_state):
    assert json.loads(scenario_state["stdout"])["tab_name"] == "epic"
