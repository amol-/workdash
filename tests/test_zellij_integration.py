"""Exercise Workdash pane launches against a real Zellij session."""

import fcntl
import json
import os
import pty
import select
import shlex
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import uuid

import pytest


def _cleanup_sessions(zellij, env, sessions):
    for name, pid, fd in sessions:
        for command in (["delete-session", "--force", name], ["kill-session", name]):
            try:
                result = subprocess.run(
                    [zellij, *command], env=env, capture_output=True, timeout=10
                )
                if result.returncode == 0:
                    break
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            print(
                f"Warning: could not confirm cleanup of Zellij session {name}; it may have exited already.",
                file=sys.stderr,
            )
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        finally:
            os.close(fd)


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_two_server_dashboards_use_their_own_real_zellij_sessions(tmp_path):
    import socket
    import sys

    from workdash.control import WorkdashControlClient, WorkdashControlError

    zellij = shutil.which("zellij")
    pane_result = tmp_path / "pane-result.json"
    pane_error = tmp_path / "pane-error.txt"
    agent_result = tmp_path / "agent-result.json"
    pane_shell = tmp_path / "pane-shell"
    agent = tmp_path / "agent"
    for script, result in ((pane_shell, pane_result), (agent, agent_result)):
        script.write_text(
            "#!/bin/sh\n"
            + ('if [ "$1" = "-ic" ]; then exec /bin/sh "$@"; fi\n' if script == pane_shell else "")
            + f"{shlex.quote(sys.executable)} -m workdash list --json > {shlex.quote(str(result))}.tmp "
            + f"2> {shlex.quote(str(pane_error))}\n"
            + f"mv {shlex.quote(str(result))}.tmp {shlex.quote(str(result))}\n"
            + "sleep 120\n"
        )
        script.chmod(0o755)
    harness = tmp_path / "dashboard.py"
    harness.write_text(
        "import os\n"
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "import workdash.workdash as cli\n"
        "import workdash.control as control\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        "from workdash.models import WorkItem, WorkItemKind, WorkItemType\n"
        f"os.environ['SHELL'] = {str(pane_shell)!r}\n"
        "item = WorkItem(kind=WorkItemKind.ASSIGNED_ISSUE, item_type=WorkItemType.ISSUE, "
        "repo='owner/repo', number=1, title=os.environ['ZELLIJ_SESSION_NAME'], "
        "created_at=datetime.now(UTC), updated_at=datetime.now(UTC), url='https://example.com/1')\n"
        f"control.ensure_worktree = lambda *_args, **_kwargs: {str(tmp_path)!r}\n"
        "control.get_merge_base = lambda *_args, **_kwargs: None\n"
        f"control.SERVER_LOG_PATH = Path({str(tmp_path / 'workdash.log')!r})\n"
        "from workdash.github_client import GitHubClient\n"
        "GitHubClient.work_pull_request = lambda *_args, **_kwargs: None\n"
        "control.prepare_launch_agent_prompt = lambda *_args, **_kwargs: 'test prompt'\n"
        "cli.GitHubClient.check_authentication = lambda self: None\n"
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
        "analysis_viewer={'command': 'workdash mdview', 'format': 'markdown', 'needs_pane': True}, "
        f"agents={{'agent': AgentConfig(launch={shlex.quote(str(agent))!r})}}, repositories=('owner/repo',), "
        f"workdir={str(tmp_path)!r}, todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([item], {})\n"
        "raise SystemExit(cli.main(['--direct', '--server']))\n"
    )
    shell = tmp_path / "dashboard-shell"
    shell.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(harness))}\n")
    shell.chmod(0o755)
    config = tmp_path / "config.kdl"
    config.write_text("")
    sessions = []
    ports = []
    zellij_env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        zellij_env.pop(key, None)
    zellij_env.update(TERM="xterm-256color", ZELLIJ_CONFIG_FILE=str(config), SHELL=str(shell))
    try:
        for prefix in ("workdash-test", "demo-test"):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            assert port not in ports
            ports.append(port)
            name = f"{prefix}-{uuid.uuid4().hex}"
            env = {**zellij_env, "WORKDASH_HOST": f"127.0.0.1:{port}"}
            pid, fd = pty.fork()
            if pid == 0:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                os.execve(zellij, [zellij, "--session", name], env)
            sessions.append((name, pid, fd))
            client = WorkdashControlClient(f"127.0.0.1:{port}")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    result = client.request("info")
                    break
                except WorkdashControlError:
                    time.sleep(0.1)
            else:
                os.set_blocking(fd, False)
                try:
                    screen = os.read(fd, 65536).decode(errors="replace")
                except BlockingIOError:
                    screen = ""
                pytest.fail(f"server {name} did not start: {screen}")
            assert result["session"] == name
        for port, (name, _, _) in zip(ports, sessions):
            client = WorkdashControlClient(f"127.0.0.1:{port}")
            assert client.request("info")["session"] == name
            assert client.request("list")["items"][0]["title"] == name
        result = WorkdashControlClient(f"127.0.0.1:{ports[1]}").request(
            "terminal", {"target": "owner/repo#ISSUE-1"}
        )
        assert result["session"] == sessions[1][0]
        deadline = time.monotonic() + 15
        while not pane_result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert pane_result.exists(), (
            pane_error.read_text() if pane_error.exists() else "pane did not run"
        )
        assert json.loads(pane_result.read_text())["items"][0]["title"] == sessions[1][0]
        result = WorkdashControlClient(f"127.0.0.1:{ports[1]}").request(
            "code", {"target": "owner/repo#ISSUE-1"}
        )
        assert result["session"] == sessions[1][0]
        deadline = time.monotonic() + 15
        while not agent_result.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert agent_result.exists(), (
            pane_error.read_text() if pane_error.exists() else "agent did not run"
        )
        assert json.loads(agent_result.read_text())["items"][0]["title"] == sessions[1][0]
    finally:
        _cleanup_sessions(zellij, zellij_env, sessions)


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_launch_pane_and_wait_blocks_until_the_pane_command_exits_in_its_group_tab(tmp_path):
    import threading

    from workdash.launcher import zellij as zellij_client

    zellij = shutil.which("zellij")
    name = f"workdash-wait-test-{uuid.uuid4().hex}"
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(TERM="xterm-256color", SHELL="/bin/sh")
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(zellij, [zellij, "--session", name], env)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if zellij_client.load_zellij_panes(name):
                    break
            except RuntimeError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Zellij did not start")

        def wait_for(command, group):
            path = tmp_path / uuid.uuid4().hex
            path.mkdir()
            zellij_client.launch_pane_and_wait(
                str(path),
                ["/bin/sh", "-c", command],
                context="test",
                work_action="review",
                zellij_session=name,
                group=group,
            )
            return path

        # The call returns only once the pane command has run to completion.
        ran = wait_for("echo ran > ran.txt", None)
        assert (ran / "ran.txt").read_text().strip() == "ran"

        # The pane closes on exit, so the tab is inspected while the pane still runs.
        release = tmp_path / "release"
        grouped = []
        thread = threading.Thread(
            target=lambda: grouped.append(
                wait_for(
                    f"while [ ! -e {release} ]; do sleep 0.05; done; echo grouped > grouped.txt",
                    "project",
                )
            )
        )
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                tabs = {
                    pane.get("tab_name")
                    for pane in zellij_client.load_zellij_panes(name)
                    if not pane.get("is_plugin")
                }
                if "project" in tabs:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("the grouped pane did not open in its tab")
        finally:
            release.touch()
            thread.join(20)
        assert not thread.is_alive()
        assert (grouped[0] / "grouped.txt").read_text().strip() == "grouped"
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("tab_name", ["epic", "-foo"])
def test_grouped_agent_launches_in_new_and_existing_tabs(tmp_path, monkeypatch, tab_name):
    from workdash.agent_execution import executor
    from workdash.launcher import zellij as zellij_client

    zellij = shutil.which("zellij")
    name = f"workdash-group-test-{uuid.uuid4().hex}"
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(TERM="xterm-256color", SHELL="/bin/sh")
    monkeypatch.setenv("SHELL", "/bin/sh")
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(zellij, [zellij, "--session", name], env)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                if zellij_client.load_zellij_panes(name):
                    break
            except RuntimeError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Zellij did not start")
        for repo, group in [("first", tab_name), ("second", tab_name), ("third", None)]:
            path = tmp_path / repo
            path.mkdir()
            launch = executor.launch(
                str(path), "hello", ["/bin/sh", "-c", "sleep 120"], zellij_session=name, group=group
            )
            panes = zellij_client.load_zellij_panes(name)
            pane = next(
                p
                for p in panes
                if p.get("id") == int(launch.pane_id.removeprefix("terminal_"))
                and not p.get("is_plugin")
            )
            if group:
                assert pane["tab_name"] == group
            else:
                assert pane["tab_name"] != tab_name
        assert len({p["tab_id"] for p in panes if p.get("tab_name") == tab_name}) == 1
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("repeat", range(2))
def test_persistent_dashboard_quit_and_restart_in_real_zellij(tmp_path, repeat):
    zellij = shutil.which("zellij")
    name = "workdash" if repeat == 0 else f"wd-{uuid.uuid4().hex[:8]}"
    script = tmp_path / "workdash"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import workdash.workdash as cli\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        'cli.GitHubClient.check_authentication = lambda self: ("expired" if os.getenv("TEST_EXPIRED") else None)\n'
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
        "analysis_viewer={'command': 'workdash mdview', 'format': 'markdown', 'needs_pane': True}, "
        "agents={'agent': AgentConfig(launch='sh')}, repositories=('owner/repo',), "
        "workdir='/tmp', todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([], {})\n"
        "raise SystemExit(cli.main())\n"
    )
    script.chmod(0o755)
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(
        TERM="xterm-256color",
        SHELL="/bin/sh",
        ZELLIJ_SOCKET_DIR=str(tmp_path / "sockets"),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
    )
    clients = []

    def start(*args, expired=False):
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.execve(
                str(script),
                [str(script), *([] if repeat == 0 else ["--session", name]), *args],
                {**env, **({"TEST_EXPIRED": "1"} if expired else {})},
            )
        clients.append((pid, fd))

    def action(*args):
        return subprocess.run(
            [zellij, "--session", name, "action", *args], capture_output=True, text=True, env=env
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = action("list-panes", "--json", "--all")
            if result.stdout.strip().startswith("["):
                panes = json.loads(result.stdout)
                if predicate(panes):
                    return panes
            time.sleep(0.1)
        output = []
        for _, fd in clients:
            os.set_blocking(fd, False)
            try:
                output.append(os.read(fd, 65536).decode(errors="replace"))
            except BlockingIOError:
                pass
        processes = subprocess.run(
            [
                "ps",
                "-o",
                "pid,ppid,state,wchan,args",
                "--forest",
                "-p",
                ",".join(str(pid) for pid, _ in clients),
            ],
            capture_output=True,
            text=True,
        ).stdout
        pytest.fail(
            f"session {name} did not reach expected pane state: {result.stdout} {result.stderr}; "
            f"client output: {output}; processes: {processes}"
        )

    try:
        start()
        time.sleep(2)
        panes = wait_for(lambda panes: any(p.get("title") == "workdash tui" for p in panes))
        dashboard = next(p for p in panes if p.get("title") == "workdash tui")
        shell = next(p for p in panes if not p["is_plugin"] and p["id"] != dashboard["id"])
        assert dashboard["tab_name"] == "dashboard" and dashboard["pane_y"] < shell["pane_y"]
        assert {p.get("plugin_url") for p in panes} >= {"zellij:tab-bar", "zellij:status-bar"}
        pane_id = f"terminal_{dashboard['id']}"
        wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui" and "--direct" in str(p.get("pane_command"))
                for p in panes
            )
        )
        start("--server", expired=True)
        output = b""
        deadline = time.monotonic() + 10
        while b"options are ignored" not in output and time.monotonic() < deadline:
            if select.select([clients[-1][1]], [], [], 0.1)[0]:
                output += os.read(clients[-1][1], 65536)
        assert b"options are ignored" in output
        panes = wait_for(lambda panes: any(p.get("title") == "workdash tui" for p in panes))
        assert sum(p.get("title") == "workdash tui" for p in panes) == 1
        time.sleep(2)
        assert len(action("list-clients").stdout.splitlines()) >= 2
        assert action("write-chars", "--pane-id", pane_id, "q").returncode == 0
        wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui" and p.get("pane_command") == "/bin/sh -i"
                for p in panes
            )
        )
        assert "Ctrl+Q" in action("dump-screen", "--pane-id", pane_id).stdout
        start("--debug", expired=True)
        _, status = os.waitpid(clients[-1][0], 0)
        assert os.waitstatus_to_exitcode(status) == 1
        os.close(clients.pop()[1])
        assert (
            len(
                [
                    p
                    for p in json.loads(action("list-panes", "--json", "--all").stdout)
                    if p.get("title") == "workdash tui"
                ]
            )
            == 1
        )
        start("--debug")
        panes = wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui" and "--debug" in str(p.get("pane_command"))
                for p in panes
            )
        )
        old = next(p for p in panes if p.get("id") == dashboard["id"] and not p["is_plugin"])
        assert old["title"] == "workdash old" and old["pane_command"] == "/bin/sh -i"
        assert next(p for p in panes if p.get("title") == "workdash tui")["tab_name"] == "dashboard"
        assert action("new-tab", "--name", "keep").returncode == 0
        tab_id = next(p for p in panes if p.get("title") == "workdash tui")["tab_id"]
        assert action("close-tab", "--tab-id", str(tab_id)).returncode == 0
        start("--refresh")
        panes = wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui" and "--refresh" in str(p.get("pane_command"))
                for p in panes
            )
        )
        assert next(p for p in panes if p.get("title") == "workdash tui")["tab_name"] == "dashboard"
        assert any(p.get("tab_name") == "keep" for p in panes)
        recovered = next(p for p in panes if p.get("title") == "workdash tui")
        time.sleep(2)
        assert (
            action("write-chars", "--pane-id", f"terminal_{recovered['id']}", "q").returncode == 0
        )
        wait_for(
            lambda panes: any(
                p.get("id") == recovered["id"] and p.get("pane_command") == "/bin/sh -i"
                for p in panes
            )
        )
        keep = next(p for p in panes if p.get("tab_name") == "keep" and not p["is_plugin"])
        keep_id = f"terminal_{keep['id']}"
        assert action("write-chars", "--pane-id", keep_id, str(script)).returncode == 0
        assert action("send-keys", "--pane-id", keep_id, "Enter").returncode == 0
        panes = wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui" and p.get("tab_name") == "keep" for p in panes
            )
        )
        inside = next(
            p for p in panes if p.get("title") == "workdash tui" and p.get("tab_name") == "keep"
        )
        assert inside["id"] == keep["id"]
        start("--debug", expired=True)
        output = b""
        deadline = time.monotonic() + 10
        while b"options are ignored" not in output and time.monotonic() < deadline:
            if select.select([clients[-1][1]], [], [], 0.1)[0]:
                output += os.read(clients[-1][1], 65536)
        assert b"options are ignored" in output
        panes = json.loads(action("list-panes", "--json", "--all").stdout)
        assert sum(p.get("title") == "workdash tui" for p in panes) == 1
        old = next(p for p in panes if p.get("id") == recovered["id"] and not p["is_plugin"])
        assert old["title"] == "workdash old" and old["pane_command"] == "/bin/sh -i"
        assert action("write-chars", "--pane-id", f"terminal_{inside['id']}", "q").returncode == 0
        time.sleep(1)
        assert "Ctrl+Q" in action("dump-screen", "--pane-id", f"terminal_{inside['id']}").stdout

    finally:
        if clients:
            _cleanup_sessions(zellij, env, [(name, *clients[0])])
        for pid, fd in clients[1:]:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
            os.close(fd)


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_analysis_viewer_renders_and_closes_fullscreen_pane(tmp_path, monkeypatch):
    from workdash import launcher

    zellij = shutil.which("zellij")
    name = f"workdash-viewer-test-{uuid.uuid4().hex}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "workdash"
    executable.write_text(
        f"#!{sys.executable}\nfrom workdash.workdash import main\nraise SystemExit(main())\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    document = tmp_path / "analysis with spaces.md"
    document.write_text(
        "# Analysis heading\n\n- First item\n- Second item\n\n"
        "| Name | Value |\n| --- | --- |\n| Alpha | Beta |\n\n"
        "```python\nprint('code example')\n```\n\n"
        + "\n\n".join(f"Paragraph {i}" for i in range(40))
        + "\n\n## End of analysis\n"
    )
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(TERM="xterm-256color", SHELL="/bin/sh", PATH="/usr/bin:/bin")
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(zellij, [zellij, "--session", name], env)

    def action(*args):
        return subprocess.run(
            [zellij, "--session", name, "action", *args], capture_output=True, text=True, env=env
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = action("list-panes", "--json", "--all")
            if result.stdout.strip().startswith("["):
                panes = json.loads(result.stdout)
                if predicate(panes):
                    return panes
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            time.sleep(0.1)
        status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        os.set_blocking(fd, False)
        try:
            output = os.read(fd, 65536).decode(errors="replace")
        except (BlockingIOError, OSError):
            output = ""
        pytest.fail(
            f"Zellij pane state not reached (client status: {status}): "
            f"{result.stdout} {result.stderr}; {output}"
        )

    try:
        initial = wait_for(lambda panes: any(not p["is_plugin"] for p in panes))
        initial_ids = {p["id"] for p in initial if not p["is_plugin"]}
        launcher.open_markdown(
            str(document),
            "workdash mdview",
            format="markdown",
            needs_pane=True,
            zellij_session=name,
        )
        panes = wait_for(
            lambda panes: any(
                not p["is_plugin"] and p["id"] not in initial_ids and p["is_fullscreen"]
                for p in panes
            )
        )
        viewer = next(p for p in panes if not p["is_plugin"] and p["id"] not in initial_ids)
        pane_id = f"terminal_{viewer['id']}"
        deadline = time.monotonic() + 15
        screen = ""
        while time.monotonic() < deadline:
            screen = action("dump-screen", "--pane-id", pane_id).stdout
            if "code example" in screen:
                break
            time.sleep(0.1)
        for text in (
            "Analysis heading",
            "First item",
            "Second item",
            "Alpha",
            "Beta",
            "code example",
        ):
            assert text in screen
        assert "# Analysis heading" not in screen
        assert "```" not in screen
        assert action("send-keys", "--pane-id", pane_id, "End").returncode == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            screen = action("dump-screen", "--pane-id", pane_id).stdout
            if "End of analysis" in screen:
                break
            time.sleep(0.1)
        assert "End of analysis" in screen
        assert action("write-chars", "--pane-id", pane_id, "q").returncode == 0
        panes = wait_for(
            lambda panes: all(p["is_plugin"] or p["id"] != viewer["id"] for p in panes)
        )
        assert initial_ids <= {p["id"] for p in panes if not p["is_plugin"]}
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])
