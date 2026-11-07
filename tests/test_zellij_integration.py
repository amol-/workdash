"""Exercise Workdash pane launches against a real Zellij session."""

import fcntl
import json
import os
import pty
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
        "import workdash.workdash as cli\n"
        "import workdash.control as control\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        "from workdash.models import WorkItem, WorkItemKind, WorkItemType\n"
        f"os.environ['SHELL'] = {str(pane_shell)!r}\n"
        "item = WorkItem(kind=WorkItemKind.ASSIGNED_ISSUE, item_type=WorkItemType.ISSUE, "
        "repo='owner/repo', number=1, title=os.environ['ZELLIJ_SESSION_NAME'], "
        "created_at=datetime.now(UTC), updated_at=datetime.now(UTC), url='https://example.com/1')\n"
        f"control.ensure_worktree = lambda *_args: {str(tmp_path)!r}\n"
        "control.get_merge_base = lambda *_args: None\n"
        "control.prepare_launch_agent_prompt = lambda *_args, **_kwargs: 'test prompt'\n"
        "cli._check_gh_preflight = lambda: None\n"
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
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
def test_grouped_agent_launches_in_new_and_existing_tabs(tmp_path, monkeypatch):
    from workdash.launcher import launch_agent_context, load_zellij_panes

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
                if load_zellij_panes(name):
                    break
            except RuntimeError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Zellij did not start")
        for repo, group in [("first", "epic"), ("second", "epic"), ("third", None)]:
            path = tmp_path / repo
            path.mkdir()
            launch = launch_agent_context(
                str(path), "hello", ["/bin/sh", "-c", "sleep 120"], zellij_session=name, group=group
            )
            panes = load_zellij_panes(name)
            pane = next(
                p
                for p in panes
                if p.get("id") == int(launch.pane_id.removeprefix("terminal_"))
                and not p.get("is_plugin")
            )
            if group:
                assert pane["tab_name"] == group
            else:
                assert pane["tab_name"] != "epic"
        assert len({p["tab_id"] for p in panes if p.get("tab_name") == "epic"}) == 1
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])
