"""Drive the dashboard view menu in a real, isolated terminal session."""

import json
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from pytest_bdd import given, parsers, then, when

from test_zellij_integration import live_zellij_session
from view_fake import ISSUE_URL, issue_routes


@given(parsers.parse('the dashboard has selected the issue "{title}"'))
def _selected_item(scenario_state, title):
    scenario_state["title"] = title


@given(parsers.parse('I have pressed Down to select the second issue "{title}"'))
def _second_item(scenario_state, title):
    scenario_state.update(title=title, select_second=True)


@given("my configured browser opens links outside the terminal")
def _external_browser(scenario_state):
    scenario_state["browser_needs_pane"] = False


@given("my configured browser needs its own terminal")
def _terminal_browser(scenario_state):
    scenario_state["browser_needs_pane"] = True


@when(parsers.parse("I press v then {key} on the dashboard"))
def _press_view_keys(scenario_state, key, tmp_path, request, monkeypatch):
    home = tmp_path / "home"
    config_dir = home / ".config" / "workdash"
    config_dir.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    zellij_config = tmp_path / "zellij.kdl"
    zellij_config.write_text("show_release_notes false\nshow_startup_tips false\n")
    browser_log = tmp_path / "browser-url"
    browser = bin_dir / "test-browser"
    needs_pane = scenario_state.get("browser_needs_pane", False)
    browser.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$1" > {shlex.quote(str(browser_log))}\n'
        'printf "GitHub page: %s\\n" "$1"\n'
        "if [ -t 1 ]; then read -r _; fi\n"
    )
    browser.chmod(0o755)
    (config_dir / "config.json").write_text(
        json.dumps(
            {
                "github_username": "test",
                "open": str(browser),
                "open_needs_pane": needs_pane,
                "analysis_viewer": {
                    "command": "workdash view",
                    "format": "markdown",
                    "needs_pane": True,
                },
                "agents": {"test": {"launch": "sh"}},
                "repositories": ["owner/repo"],
                "workdir": str(tmp_path),
                "todo_repository": "test/todos",
            }
        )
    )
    workdash = bin_dir / "workdash"
    workdash.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m workdash "$@"\n')
    workdash.chmod(0o755)

    issue_node = {
        "__typename": "Issue",
        "id": "ISSUE-12",
        "number": 12,
        "title": scenario_state["title"],
        "url": ISSUE_URL,
        "createdAt": "2026-02-20T00:00:00Z",
        "updatedAt": "2026-02-25T10:00:00Z",
        "state": "OPEN",
        "repository": {"nameWithOwner": "owner/repo"},
        "subIssuesSummary": {"total": 2},
    }
    preceding_title = "Update the docs"
    issue_nodes = [issue_node]
    if scenario_state.get("select_second"):
        issue_nodes.insert(
            0,
            {
                **issue_node,
                "id": "ISSUE-11",
                "number": 11,
                "title": preceding_title,
                "url": "https://github.com/owner/repo/issues/11",
                "updatedAt": "2026-02-26T10:00:00Z",
                "subIssuesSummary": {"total": 0},
            },
        )
    routes = issue_routes() | {
        "auth status": {},
        "search prs": [],
        "api graphql -f query={ assigned:": {"data": {"assigned": {"nodes": []}}},
        "api graphql -f query={ tracked:": {"data": {"tracked": {"nodes": issue_nodes}}},
        "api graphql -f query={ repository(": {"data": {"repository": {"issues": {"nodes": []}}}},
    }
    gh = bin_dir / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "from view_fake import fake_gh_view_run\n"
        "try:\n"
        f"    print(fake_gh_view_run({routes!r})(sys.argv).stdout)\n"
        "except subprocess.CalledProcessError as error:\n"
        "    sys.exit(error.stderr)\n"
    )
    gh.chmod(0o755)

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    root = Path(__file__).resolve().parents[3]
    resources = ExitStack()
    request.addfinalizer(resources.close)
    sockets = resources.enter_context(tempfile.TemporaryDirectory(prefix="wd-zellij-"))
    for env_key, value in {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "ZELLIJ_CONFIG_FILE": str(zellij_config),
        "ZELLIJ_SOCKET_DIR": sockets,
        "WORKDASH_HOST": f"127.0.0.1:{port}",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "PYTHONPATH": os.pathsep.join(map(str, (root / "src", root / "tests"))),
    }.items():
        monkeypatch.setenv(env_key, value)
    name = resources.enter_context(live_zellij_session(monkeypatch))

    def run_action(*args, timeout=5):
        return subprocess.run(
            ["zellij", "--session", name, "action", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def action(*args):
        result = run_action(*args)
        assert result.returncode == 0, result.stderr
        return result.stdout

    def wait_for_terminals(expected, predicate, *, timeout=12):
        deadline = time.monotonic() + timeout
        last = "no response"
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                result = run_action("list-panes", "--json", "--all", timeout=min(5, remaining))
            except subprocess.TimeoutExpired as error:
                last = f"timed out; stdout={error.stdout!r}; stderr={error.stderr!r}"
            else:
                last = (
                    f"exit={result.returncode}; stdout={result.stdout!r}; stderr={result.stderr!r}"
                )
                if result.returncode == 0:
                    try:
                        panes = json.loads(result.stdout)
                    except json.JSONDecodeError:
                        panes = None
                    if isinstance(panes, list):
                        terminals = [p for p in panes if not p["is_plugin"]]
                        if terminals and predicate(terminals):
                            return terminals
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise AssertionError(f"Session {name}: expected {expected}; last list-panes: {last}")

    def screen_containing(expected, *args, without=None):
        screen = ""
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                screen = action("dump-screen", *args)
            except AssertionError as error:
                screen = str(error)
            if expected in screen and (without is None or without not in screen):
                return screen
            time.sleep(0.1)
        raise AssertionError(f"Expected {expected!r} on screen with {without!r} absent: {screen}")

    (dashboard,) = wait_for_terminals("one startup terminal", lambda panes: len(panes) == 1)
    dashboard_id = f"terminal_{dashboard['id']}"
    action("write-chars", "--pane-id", dashboard_id, "workdash")
    action("send-keys", "--pane-id", dashboard_id, "Enter")
    screen = screen_containing("(v)iew", "--pane-id", dashboard_id)
    title = scenario_state["title"]
    assert title in screen, screen
    if scenario_state.get("select_second"):
        assert preceding_title in screen and screen.index(preceding_title) < screen.index(title), (
            screen
        )
        action("send-keys", "--pane-id", dashboard_id, "Down")
    scenario_state.update(
        action=action,
        screen_containing=screen_containing,
        dashboard_id=dashboard_id,
        dashboard=dashboard,
        wait_for_terminals=wait_for_terminals,
        browser_log=browser_log,
    )
    _choose_view_menu(scenario_state, "Esc" if key == "Escape" else key)


@when("I open the selected item in my browser from the view menu")
def _open_selected_item(scenario_state):
    _choose_view_menu(scenario_state, "o")


def _choose_view_menu(scenario_state, choice):
    action = scenario_state["action"]
    pane = ("--pane-id", scenario_state["dashboard_id"])
    action("send-keys", *pane, "v")
    menu = scenario_state["screen_containing"]("(o) open", *pane)
    assert "(v) view" in menu
    action("send-keys", *pane, choice)


@then(parsers.parse('I can read "{text}" in a floating viewer in this session'))
def _read_in_pane(scenario_state, text):
    dashboard_id = scenario_state["dashboard"]["id"]
    panes = scenario_state["wait_for_terminals"](
        "the dashboard and viewer terminals",
        lambda panes: len(panes) >= 2 and any(p["id"] == dashboard_id for p in panes),
    )
    (viewer,) = (p for p in panes if p["id"] != dashboard_id)
    assert viewer["is_floating"], viewer
    pane_id = f"terminal_{viewer['id']}"
    screen_containing = scenario_state["screen_containing"]
    screen_containing(text, "--pane-id", pane_id)
    screen_containing("(v)iew", "--pane-id", scenario_state["dashboard_id"])
    scenario_state["viewer_opened"] = True


@when("I press q in the viewer")
def _quit_viewer(scenario_state):
    scenario_state["action"]("send-keys", "q")


@then("I return to the dashboard")
def _return_to_dashboard(scenario_state):
    if scenario_state.get("viewer_opened"):
        dashboard_id = scenario_state["dashboard"]["id"]
        scenario_state["wait_for_terminals"](
            "only the dashboard terminal after closing the viewer",
            lambda panes: [p["id"] for p in panes] == [dashboard_id],
            timeout=10,
        )
    scenario_state["screen_containing"]("(v)iew", without="View menu:")


@then("neither viewer opens")
def _nothing_opens(scenario_state):
    panes = scenario_state["wait_for_terminals"]("a terminal listing", bool)
    assert [p["id"] for p in panes] == [scenario_state["dashboard"]["id"]]
    assert not scenario_state["browser_log"].exists()


@then(parsers.parse('my browser opens "{url}"'))
def _browser_opens(scenario_state, url):
    log = scenario_state["browser_log"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if log.exists():
            opened_url = log.read_text().strip()
            if opened_url:
                assert opened_url == url, f"Expected browser to open {url}, got {opened_url}"
                scenario_state["screen_containing"](
                    "(v)iew", "--pane-id", scenario_state["dashboard_id"]
                )
                return
        time.sleep(0.05)
    raise AssertionError(f"The browser did not open {url}")


@then("the dashboard confirms that the item was opened")
def _opened_confirmation(scenario_state):
    scenario_state["screen_containing"](
        "Opened issue owner/repo#12", "--pane-id", scenario_state["dashboard_id"]
    )
    panes = scenario_state["wait_for_terminals"]("a terminal listing", bool)
    assert [p["id"] for p in panes] == [scenario_state["dashboard"]["id"]]
