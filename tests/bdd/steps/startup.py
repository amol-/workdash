"""Step definitions for dashboard startup scenarios."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time

import pytest
from pytest_bdd import given, parsers, then, when

from test_zellij_integration import (
    _free_dashboard_address,
    _private_socket_dir,
    _temporary_home,
    dashboard_address_recovery,
    dashboard_restart,
    live_zellij_session,
    running_dashboard_return,
)
from workdash.config import AgentConfig, WorkdashConfig, save_config


@given("the GitHub CLI is installed but not authenticated")
def _gh_unauthenticated(scenario_state):
    scenario_state["gh_unauthenticated"] = True


@given(
    parsers.parse('the user has a tracked issue titled "{title}"'), target_fixture="startup_pane"
)
def _tracked_issue_at_startup(title, scenario_state, monkeypatch, tmp_path):
    home = _temporary_home(tmp_path)
    save_config(
        WorkdashConfig(
            github_username="testuser",
            open_command="/bin/true",
            analysis_viewer={"command": "workdash view", "format": "markdown", "needs_pane": True},
            agents={"agent": AgentConfig(launch="sh")},
            repositories=("owner/repo",),
            workdir=str(tmp_path),
            todo_repository="testuser/todos",
        ),
        tmp_path / "home" / ".config" / "workdash" / "config.json",
    )
    signed_in = tmp_path / "signed-in"
    if not scenario_state.get("gh_unauthenticated"):
        signed_in.touch()
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f'  "auth status") test -f {shlex.quote(str(signed_in))} ;;\n'
        f'  "auth login") touch {shlex.quote(str(signed_in))} ;;\n'
        "  *) exit 1 ;;\nesac\n"
    )
    gh.chmod(0o755)
    script = tmp_path / "workdash"
    script.write_text(
        f"#!{sys.executable}\n"
        "from datetime import UTC, datetime\n"
        "import workdash.workdash as cli\n"
        "from workdash.models import WorkItem, WorkItemKind, WorkItemType\n"
        "item = WorkItem(kind=WorkItemKind.TRACKED_ISSUE, item_type=WorkItemType.ISSUE, "
        f"repo='owner/repo', number=1, title={title!r}, created_at=datetime.now(UTC), "
        "updated_at=datetime.now(UTC), url='https://example.com/1')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([item], {})\n"
        "raise SystemExit(cli.main())\n"
    )
    script.chmod(0o755)

    def action(*args):
        return subprocess.run(
            [shutil.which("zellij"), "--session", session, "action", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout

    monkeypatch.setenv("HOME", home)
    monkeypatch.setenv("PATH", str(tmp_path), prepend=":")
    monkeypatch.setenv("WORKDASH_HOST", _free_dashboard_address())
    with live_zellij_session(monkeypatch) as session:
        panes = json.loads(action("list-panes", "--json", "--all"))
        pane = next(p for p in panes if not p["is_plugin"])
        pane_id = f"terminal_{pane['id']}"

        def type_command(command):
            action("write-chars", "--pane-id", pane_id, command)
            action("send-keys", "--pane-id", pane_id, "Enter")

        def wait_for_text(text):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                screen = action("dump-screen", "--pane-id", pane_id)
                if text in screen:
                    return screen
                time.sleep(0.1)
            pytest.fail(f"{pane_id} did not show {text!r}:\n{screen}")

        yield type_command, wait_for_text


@when("the user runs `workdash`")
@when("the user runs `workdash` again")
def _user_starts_interactive_dashboard(startup_pane):
    type_command, _ = startup_pane
    type_command("workdash")


@then("Workdash tells the user to sign in with `gh auth login`")
def _reports_gh_auth_required(startup_pane, tmp_path):
    _, wait_for_text = startup_pane
    screen = wait_for_text(f"{tmp_path / 'gh'} auth login")
    assert "gh CLI is not authenticated" in screen


@when("the user signs in with `gh auth login`")
def _sign_in_to_github(startup_pane):
    type_command, _ = startup_pane
    type_command("gh auth login")


@then(parsers.parse('the dashboard opens in the current pane and shows "{title}"'))
def _dashboard_opens_showing_the_issue(title, startup_pane):
    _, wait_for_text = startup_pane
    wait_for_text(title)


@given(
    "the dashboard is running in a named session and the active tab is a work tab",
    target_fixture="dashboard_return",
)
def _dashboard_in_other_tab(tmp_path):
    if not shutil.which("zellij"):
        pytest.skip("Zellij is not installed")
    with running_dashboard_return(tmp_path, new_last_input=True) as dashboard:
        yield dashboard


@given("the user has moved the configuration file aside", target_fixture="moved_config_path")
def _move_config_aside(dashboard_return, tmp_path):
    config = tmp_path / "home" / ".config" / "workdash" / "config.json"
    config.rename(config.with_suffix(".backup"))
    return config


@when(
    "the user runs `workdash --session NAME --refresh` from another terminal and sends input there",
    target_fixture="returning_cli_output",
)
def _reattach_with_refresh(dashboard_return):
    output = dashboard_return["attach"]()
    dashboard_return["send_input_before_focus"]()
    return output


@then("Workdash reports that the new options are ignored")
def _return_reports_ignored_options(returning_cli_output):
    assert b"new options are ignored" in returning_cli_output


@when("the user presses `/` in the returning terminal")
def _press_filter_in_returning_terminal(dashboard_return):
    dashboard_return["press_filter"]()


@then("the filter opens in the original dashboard without starting a second dashboard")
def _original_dashboard_opens_filter(dashboard_return):
    dashboard_return["verify_original_dashboard"]()


@then("Workdash does not prompt for setup or recreate the configuration file")
def _return_does_not_run_setup(returning_cli_output, moved_config_path):
    assert b"Attaching..." in returning_cli_output
    assert b"Open command" not in returning_cli_output
    assert not moved_config_path.exists()


@given(
    "the user is outside Zellij and has not yet opened a Workdash session",
    target_fixture="dashboard_lifecycle",
)
def _before_first_launch(tmp_path):
    if not shutil.which("zellij"):
        pytest.skip("Zellij is not installed")
    with _private_socket_dir() as sockets:
        yield from dashboard_restart(tmp_path, sockets)


@when("the user launches `workdash` from the terminal", target_fixture="original_panes")
def _launch_dashboard(dashboard_lifecycle):
    return dashboard_lifecycle["launch_dashboard"]()


@then("the dashboard responds to `/` and its sibling shell accepts work")
def _dashboard_and_shell_are_usable(dashboard_lifecycle, original_panes):
    dashboard_lifecycle["verify_dashboard_and_shell"](original_panes)


@when("the user runs `workdash --refresh` in the sibling shell")
def _refresh_from_sibling_shell(dashboard_lifecycle, original_panes):
    dashboard_lifecycle["refresh_from_shell"](original_panes)


@then("Workdash reports that the new options are ignored and leaves the original dashboard running")
def _original_dashboard_keeps_running(dashboard_lifecycle, original_panes):
    dashboard_lifecycle["verify_original_dashboard"](original_panes)


@when(
    "the user presses `q` in the dashboard and runs `workdash --debug` from another terminal",
    target_fixture="reopened_panes",
)
def _quit_and_reopen_dashboard(dashboard_lifecycle, original_panes):
    return dashboard_lifecycle["reopen_dashboard"](original_panes)


@then("the reopened dashboard is usable and the sibling shell still holds the user's work")
def _reopened_dashboard_preserves_shell_work(dashboard_lifecycle, reopened_panes):
    dashboard_lifecycle["verify_reopened_dashboard_and_shell"](reopened_panes)


@given(
    "the dashboard in session `other` is running at the selected address",
    target_fixture="address_recovery",
)
def _address_in_use(tmp_path):
    if not shutil.which("zellij"):
        pytest.skip("Zellij is not installed")
    with _private_socket_dir() as sockets:
        yield from dashboard_address_recovery(tmp_path, sockets)


@when("the user runs `workdash --session project` from another terminal", target_fixture="blocked")
def _try_project_at_used_address(address_recovery):
    _, try_project, _, _ = address_recovery
    return try_project()


@then("Workdash identifies the session and address already in use")
def _reports_address_in_use(address_recovery, blocked):
    original_address, _, _, _ = address_recovery
    assert (
        f"session 'other' already has a Workdash dashboard at {original_address}" in blocked.stderr
    )
    assert "set WORKDASH_HOST to a different address" in blocked.stderr


@when(
    "the user sets `WORKDASH_HOST` to a free address and reruns `workdash --session project`",
    target_fixture="project_dashboard",
)
def _start_project_on_free_address(address_recovery):
    _, _, restart_project, _ = address_recovery
    return restart_project()


@then("`workdash info` finds `project` at the new address and `other` at the original address")
def _both_dashboards_are_listed(address_recovery, project_dashboard):
    _, _, _, verify_both_dashboards = address_recovery
    verify_both_dashboards(project_dashboard)
