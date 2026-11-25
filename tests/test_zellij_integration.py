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
import tempfile
import termios
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext, suppress
from pathlib import Path
from unittest.mock import Mock

import pytest


def _temporary_home(tmp_path) -> str:
    """Isolate Zellij and give dashboard harnesses an existing config file."""

    config_dir = tmp_path / "home" / ".config"
    (config_dir / "zellij").mkdir(parents=True)
    (config_dir / "workdash").mkdir()
    (config_dir / "workdash" / "config.json").write_text("{}", encoding="utf-8")
    return str(tmp_path / "home")


def _cleanup_sessions(zellij, env, sessions, *, previously_owned=None):
    if not sessions:
        return
    by_name = {}
    for name, pid, fd in sessions:
        by_name.setdefault(name, set()).add(pid)
    owned = dict(previously_owned or {})
    try:
        # Record descendants before Zellij can hang them up and orphan them.
        current = _session_processes(env, by_name)
        owned.update(current)
        # Stop startup/attach clients so they cannot launch a server during deletion.
        for (pid, _), name in current.items():
            if pid in by_name[name]:
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        owned.update(_session_processes(env, by_name))
        for name in by_name:
            with suppress(subprocess.TimeoutExpired):
                subprocess.run(
                    [zellij, "delete-session", "--force", name],
                    env=env,
                    capture_output=True,
                    timeout=1,
                )

        owned.update(_session_processes(env, by_name))
        live = _live_processes()
        for pid, started in owned:
            if pid in live and live[pid][1] == started:
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)

        deadline = time.monotonic() + 1
        while True:
            live = _live_processes()
            survivors = [
                f"{name}: PID {pid} ({live[pid][2]})"
                for (pid, started), name in owned.items()
                if pid in live and live[pid][1] == started
            ]
            if not survivors:
                return
            if time.monotonic() >= deadline:
                raise AssertionError("Zellij cleanup left live processes: " + "; ".join(survivors))
            time.sleep(0.05)
    finally:
        for _, pid, fd in sessions:
            with suppress(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
            os.close(fd)


def _session_processes(env, sessions):
    """Find the server by its private socket path, even if Workdash started it."""
    live = _live_processes()
    socket_dir = Path(env["ZELLIJ_SOCKET_DIR"]).resolve()
    listing = subprocess.run(
        ["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=", "-o", "args="],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
        timeout=1,
    ).stdout
    candidates = set()
    for line in listing.splitlines():
        if " --server " in line and f"/{socket_dir.name}/" in line:
            pid = line.split(maxsplit=1)[0]
            if pid.isdecimal() and int(pid) in live:
                candidates.add(int(pid))

    servers = {}
    for pid in candidates:
        # Re-read candidates by PID: multiline arguments can look like rows in the bulk listing.
        args = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "args="],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=1,
        ).stdout.strip()
        executable, flag, socket_arg = args.partition(" --server ")
        if not flag:
            continue
        socket = Path(socket_arg)
        if (
            socket.name in sessions
            and socket.is_absolute()
            and socket.resolve().parent.is_relative_to(socket_dir)
            # ps flattens argv; reject a match embedded in another command's arguments.
            and os.path.isabs(executable)
            and os.path.isfile(executable)
        ):
            servers[socket.name] = pid

    owned = {}
    for name, clients in sessions.items():
        pids = {pid for pid in clients if pid in live and live[pid][0] == os.getpid()}
        if name in servers:
            pids.add(servers[name])
        while descendants := {pid for pid, (parent, _, _) in live.items() if parent in pids} - pids:
            pids.update(descendants)
        owned.update({(pid, live[pid][1]): name for pid in pids})
    return owned


def _live_processes():
    # Start times keep snapshots taken before a mid-test kill from targeting reused PIDs.
    result = subprocess.run(
        [
            "ps",
            "-U",
            str(os.getuid()),
            "-o",
            "pid=",
            "-o",
            "ppid=",
            "-o",
            "stat=",
            "-o",
            "lstart=",
            "-o",
            "ucomm=",
        ],
        env={**os.environ, "LC_ALL": "C"},
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
        timeout=1,
    )
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=8)
        if len(fields) >= 8 and fields[0].isdigit() and fields[1].isdigit():
            pid, parent, state, *started = fields[:8]
            if not state.startswith("Z"):
                processes[int(pid)] = (
                    int(parent),
                    tuple(started),
                    fields[8] if len(fields) == 9 else "",
                )
    return processes


def _private_socket_dir():
    # Unix socket paths have a short length limit, even when TMPDIR is long.
    return tempfile.TemporaryDirectory(prefix="wd-zellij-", dir="/tmp")


@contextmanager
def _private_zellij_env(monkeypatch):
    with _private_socket_dir() as sockets, monkeypatch.context() as env:
        for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
            env.delenv(key, raising=False)
        env.setenv("ZELLIJ_SOCKET_DIR", sockets)
        env.setenv("XDG_CACHE_HOME", os.path.join(sockets, "cache"))
        yield env


@pytest.fixture
def zellij_socket_dir(monkeypatch):
    with _private_zellij_env(monkeypatch):
        yield


def test_zellij_cleanup_does_not_signal_pid_from_multiline_args(tmp_path, monkeypatch):
    unrelated_pid = 12345
    socket = tmp_path / "contract_version_1" / "test-session"
    env = {"ZELLIJ_SOCKET_DIR": str(tmp_path)}

    def run(command, **kwargs):
        stdout = ""
        if command[0] == "ps":
            if "ucomm=" in command:
                stdout = f"{unrelated_pid} 1 S Mon Jan  1 00:00:00 2024 sleep\n"
            elif "-p" in command:
                assert command[command.index("-p") + 1] == str(unrelated_pid)
                stdout = "/bin/sleep 120\n"
            else:
                stdout = (
                    "12346 pane-command multiline argument:\n"
                    f"{unrelated_pid} {sys.executable} --server {socket}\n"
                    ": end of argument\n"
                )
        return subprocess.CompletedProcess(command, 0, stdout)

    kill = Mock(side_effect=AssertionError("cleanup signaled the unrelated process"))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "kill", kill)
    fd = os.open(os.devnull, os.O_RDONLY)
    _cleanup_sessions("zellij", env, [(socket.name, os.getpid(), fd)])
    kill.assert_not_called()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize(
    "case",
    ["normal", "failure", "ctrl-c", "survivor", "renamed-binary", "long-tmpdir"],
)
def test_zellij_cleanup_kills_hup_ignoring_pane(tmp_path, monkeypatch, case):
    zellij = shutil.which("zellij")
    if case == "renamed-binary":
        binary = tmp_path / "terminal-multiplexer"
        shutil.copy2(zellij, binary)
        link = tmp_path / "zellij"
        link.symlink_to(binary)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
        zellij = str(link)
    elif case == "long-tmpdir":
        long_tmpdir = tmp_path / ("long-" * 24)
        long_tmpdir.mkdir()
        monkeypatch.setenv("TMPDIR", str(long_tmpdir))
        monkeypatch.setattr(tempfile, "tempdir", None)

    body_error = {"failure": AssertionError, "ctrl-c": KeyboardInterrupt}.get(case)
    leave_survivor = case == "survivor"
    ready = tmp_path / "pane-pid"
    pane_pid = None
    kill = os.kill
    previous_socket_dir = os.environ.get("ZELLIJ_SOCKET_DIR")

    def kill_except_pane(pid, sig):
        if (pid, sig) != (pane_pid, signal.SIGKILL):
            kill(pid, sig)

    if leave_survivor:
        expected = pytest.raises(AssertionError, match="Zellij cleanup left live processes")
    elif body_error:
        expected = pytest.raises(body_error, match="interrupted test body")
    else:
        expected = nullcontext()

    try:
        with expected as cleanup_error, live_zellij_session(monkeypatch) as name:
            socket_dir = os.environ["ZELLIJ_SOCKET_DIR"]
            assert socket_dir != previous_socket_dir
            subprocess.run(
                [
                    zellij,
                    "--session",
                    name,
                    "action",
                    "new-pane",
                    "--",
                    sys.executable,
                    "-c",
                    "import os, signal, time\nfrom pathlib import Path\n"
                    "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
                    f"Path({str(ready)!r}).write_text(str(os.getpid()))\n"
                    "time.sleep(120)\n",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            deadline = time.monotonic() + 10
            while not ready.exists() or not ready.read_text():
                assert time.monotonic() < deadline, "pane did not start"
                time.sleep(0.05)
            pane_pid = int(ready.read_text())
            if leave_survivor:
                monkeypatch.setattr(os, "kill", kill_except_pane)
            if body_error:
                raise body_error("interrupted test body")
        assert os.environ.get("ZELLIJ_SOCKET_DIR") == previous_socket_dir
        assert not os.path.exists(socket_dir)
        if leave_survivor:
            assert f"{name}: PID {pane_pid} (" in str(cleanup_error.value)
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pane_pid)], capture_output=True, text=True
        ).stdout.strip()
        assert bool(state and not state.startswith("Z")) == leave_survivor, state
    finally:
        if pane_pid in _live_processes():
            with suppress(ProcessLookupError):
                kill(pane_pid, signal.SIGKILL)


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_two_server_dashboards_use_their_own_real_zellij_sessions(tmp_path, zellij_socket_dir):
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
        "analysis_viewer={'command': 'workdash view', 'format': 'markdown', 'needs_pane': True}, "
        f"agents={{'agent': AgentConfig(launch={shlex.quote(str(agent))!r})}}, repositories=('owner/repo',), "
        f"workdir={str(tmp_path)!r}, todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([item], {})\n"
        "raise SystemExit(cli.main([]))\n"
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
    zellij_env.update(
        TERM="xterm-256color",
        HOME=_temporary_home(tmp_path),
        ZELLIJ_CONFIG_FILE=str(config),
        SHELL=str(shell),
    )
    try:
        for prefix in ("workdash-test", "demo-test"):
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            assert port not in ports
            ports.append(port)
            name = f"{prefix}-{uuid.uuid4().hex[:8]}"
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
def test_launch_pane_and_wait_blocks_until_the_pane_command_exits_in_its_group_tab(
    tmp_path, zellij_socket_dir
):
    import threading

    from workdash.launcher import zellij as zellij_client

    zellij = shutil.which("zellij")
    name = f"workdash-wait-{uuid.uuid4().hex[:8]}"
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

        reported = []

        def wait_for(command, group, pane_callback=None):
            path = tmp_path / uuid.uuid4().hex
            path.mkdir()
            zellij_client.launch_pane_and_wait(
                str(path),
                ["/bin/sh", "-c", command],
                context="test",
                work_action="review",
                zellij_session=name,
                group=group,
                pane_callback=pane_callback,
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
                    reported.append,
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
                if "project" in tabs and reported:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("the grouped pane did not open in its tab")
            panes = zellij_client.load_zellij_panes(name)
            assert any(f"terminal_{pane['id']}" == reported[0] for pane in panes)
        finally:
            release.touch()
            thread.join(20)
        assert reported[-1] is None
        assert not thread.is_alive()
        assert (grouped[0] / "grouped.txt").read_text().strip() == "grouped"
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])


@contextmanager
def live_zellij_session(monkeypatch):
    from workdash.launcher import zellij as zellij_client

    zellij = shutil.which("zellij")
    if zellij is None:
        pytest.skip("Zellij is not installed")
    with _private_zellij_env(monkeypatch) as session_env:
        session_env.setenv("SHELL", "/bin/sh")
        env = {**os.environ, "TERM": "xterm-256color"}
        name = f"workdash-pane-test-{uuid.uuid4().hex[:8]}"
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
            yield name
        finally:
            _cleanup_sessions(zellij, env, [(name, pid, fd)])


@pytest.mark.parametrize("tab_name", ["epic", "-foo"])
def test_grouped_agent_launches_in_new_and_existing_tabs(tmp_path, monkeypatch, tab_name):
    from workdash.agent_execution import executor
    from workdash.launcher import zellij as zellij_client

    with live_zellij_session(monkeypatch) as name:
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


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("repeat", range(2))
def test_persistent_dashboard_quit_and_restart_in_real_zellij(tmp_path, repeat, zellij_socket_dir):
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
        "analysis_viewer={'command': 'workdash view', 'format': 'markdown', 'needs_pane': True}, "
        "agents={'agent': AgentConfig(launch='sh')}, repositories=('owner/repo',), "
        "workdir='/tmp', todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([], {})\n"
        "raise SystemExit(cli.main())\n"
    )
    script.chmod(0o755)
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ.copy()
    env["WORKDASH_HOST"] = f"127.0.0.1:{port}"
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(
        TERM="xterm-256color",
        HOME=_temporary_home(tmp_path),
        SHELL="/bin/sh",
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
        from workdash.control import WorkdashControlClient, WorkdashControlError

        deadline = time.monotonic() + 10
        while True:
            try:
                result = WorkdashControlClient(f"127.0.0.1:{port}").request("list")
                break
            except WorkdashControlError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
        assert result["items"] == []
        pane_id = f"terminal_{dashboard['id']}"
        wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui"
                and "--persistent-pane" in str(p.get("pane_command"))
                for p in panes
            )
        )
        start("--refresh", expired=True)
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
        assert old["title"] == "workdash tui" and old["pane_command"] == "/bin/sh -i"
        restarted = next(
            p for p in panes if "--debug" in str(p.get("pane_command")) and not p["is_plugin"]
        )
        assert restarted["tab_name"] == "dashboard"
        assert action("write-chars", "--pane-id", pane_id, "exit 23").returncode == 0
        assert action("send-keys", "--pane-id", pane_id, "Enter").returncode == 0
        panes = wait_for(
            lambda panes: not any(p["id"] == dashboard["id"] and not p["is_plugin"] for p in panes)
        )
        assert any(p["id"] == shell["id"] and not p["is_plugin"] for p in panes)
        assert action("new-tab", "--name", "keep").returncode == 0
        assert action("close-tab", "--tab-id", str(restarted["tab_id"])).returncode == 0
        start("--refresh")
        panes = wait_for(
            lambda panes: any(
                p.get("title") == "workdash tui" and "--refresh" in str(p.get("pane_command"))
                for p in panes
            )
        )
        recovered = next(p for p in panes if "--refresh" in str(p.get("pane_command")))
        assert recovered["tab_name"] == "dashboard"
        assert any(p.get("tab_name") == "keep" for p in panes)
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
        panes_before = json.loads(action("list-panes", "--json", "--all").stdout)
        start("--debug", expired=True)
        output = b""
        deadline = time.monotonic() + 10
        while b"options are ignored" not in output and time.monotonic() < deadline:
            if select.select([clients[-1][1]], [], [], 0.1)[0]:
                output += os.read(clients[-1][1], 65536)
        assert b"options are ignored" in output
        panes = json.loads(action("list-panes", "--json", "--all").stdout)
        assert {p["id"] for p in panes} == {p["id"] for p in panes_before}
        old = next(p for p in panes if p.get("id") == recovered["id"] and not p["is_plugin"])
        assert old["title"] == "workdash tui" and old["pane_command"] == "/bin/sh -i"
        assert action("write-chars", "--pane-id", f"terminal_{inside['id']}", "q").returncode == 0
        time.sleep(1)
        assert "Ctrl+Q" in action("dump-screen", "--pane-id", f"terminal_{inside['id']}").stdout

    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd) for pid, fd in clients])


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_analysis_viewer_renders_and_closes_full_size_floating_pane(tmp_path, monkeypatch):
    from workdash import launcher

    zellij = shutil.which("zellij")
    sockets = _private_socket_dir()
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", sockets.name)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = tmp_path / "config.kdl"
    config.write_text("")
    monkeypatch.setenv("ZELLIJ_CONFIG_FILE", str(config))
    name = f"workdash-viewer-{uuid.uuid4().hex[:8]}"
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
            [zellij, "--session", name, "action", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        output = b""
        result = None
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0)[0]:
                try:
                    output += os.read(fd, 65536)
                except OSError:
                    break
            result = action("list-panes", "--json", "--all")
            if result.stdout.strip().startswith("["):
                panes = json.loads(result.stdout)
                if predicate(panes):
                    return panes
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            time.sleep(0.1)
        status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        pytest.fail(
            f"Zellij pane state not reached (client status: {status}): {result}; PTY: {output!r}"
        )

    try:
        initial = wait_for(lambda panes: any(not p["is_plugin"] for p in panes))
        initial_ids = {p["id"] for p in initial if not p["is_plugin"]}
        launcher.open_markdown(
            str(document),
            "workdash view",
            format="markdown",
            needs_pane=True,
            zellij_session=name,
            focus=True,
        )
        panes = wait_for(
            lambda panes: any(
                not p["is_plugin"] and p["id"] not in initial_ids and p["is_floating"]
                for p in panes
            )
        )
        viewer = next(p for p in panes if not p["is_plugin"] and p["id"] not in initial_ids)
        original = next(p for p in initial if not p["is_plugin"])
        assert launcher.is_zellij_shell_pane(original)
        for field in ("pane_x", "pane_y", "pane_columns", "pane_rows"):
            assert viewer[field] == original[field], (field, viewer, original)
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
        running = json.loads(action("list-panes", "--json", "--all").stdout)
        viewer = next(p for p in running if not p["is_plugin"] and p["id"] == viewer["id"])
        assert not launcher.is_zellij_shell_pane(viewer), viewer["pane_command"]
        assert action("send-keys", "End").returncode == 0
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
        sockets.cleanup()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_cli_and_api_analysis_preserve_focus_in_real_zellij(tmp_path, monkeypatch):
    import socket
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from workdash.backend import WorkdashBackend
    from workdash.config import AgentConfig, WorkdashConfig
    from workdash.control import WorkdashControlClient, WorkdashControlServer, WorkdashSession
    from workdash.models import WorkItem, WorkItemKind, WorkItemType
    from workdash.workdash import main

    zellij = shutil.which("zellij")
    sockets = _private_socket_dir()
    monkeypatch.setenv("ZELLIJ_SOCKET_DIR", sockets.name)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = tmp_path / "config.kdl"
    config.write_text("")
    monkeypatch.setenv("ZELLIJ_CONFIG_FILE", str(config))
    name = f"workdash-analysis-focus-{uuid.uuid4().hex[:8]}"
    document = tmp_path / "analysis.md"
    document.write_text("# Analysis")
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(TERM="xterm-256color", SHELL="/bin/sh")
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(zellij, [zellij, "--session", name], env)

    def action(*args):
        return subprocess.run(
            [zellij, "--session", name, "action", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        output = b""
        result = None
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0)[0]:
                try:
                    output += os.read(fd, 65536)
                except OSError:
                    break
            result = action("list-panes", "--json", "--all")
            if result.stdout.strip().startswith("["):
                panes = json.loads(result.stdout)
                if predicate(panes):
                    return panes
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            time.sleep(0.1)
        status = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        pytest.fail(
            f"Zellij pane state not reached (client status: {status}): {result}; PTY: {output!r}"
        )

    try:
        initial = wait_for(lambda panes: any(not p["is_plugin"] for p in panes))
        original = next(p for p in initial if not p["is_plugin"])
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
        item = WorkItem(
            kind=WorkItemKind.TRACKED_ISSUE,
            item_type=WorkItemType.ISSUE,
            repo="owner/repo",
            number=1,
            title="Analysis",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            url="https://github.com/owner/repo/issues/1",
            analysis="# Analysis",
        )
        backend = MagicMock()
        backend.analyze_item.return_value = str(document)
        session = WorkdashSession(
            config=WorkdashConfig(),
            backend=backend,
            work_items=[item],
            suggestion_markers={},
            zellij_session=name,
        )
        server = WorkdashControlServer(session)
        server.start()
        try:
            client = WorkdashControlClient(f"127.0.0.1:{port}")
            assert action("focus-pane-id", f"terminal_{original['id']}").returncode == 0
            for index, request in enumerate(
                (
                    lambda: client.request("analyze", {"target": "owner/repo#ISSUE-1"})[
                        "cache_used"
                    ],
                    lambda: main(["analyze", "owner/repo#ISSUE-1", "--json"]) == 0,
                )
            ):
                assert request()
                marker = f"CACHED_FOCUS_{index}"
                assert action("write-chars", f"echo {marker}\n").returncode == 0
                time.sleep(0.2)
                assert (
                    marker
                    in action("dump-screen", "--pane-id", f"terminal_{original['id']}").stdout
                )
        finally:
            server.stop()

        release = tmp_path / "release-analysis"
        started = tmp_path / "analysis-started"
        agent = tmp_path / "analysis-agent"
        agent.write_text(
            f"#!/bin/sh\nsleep 1\ntouch {shlex.quote(str(started))}\n"
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.05; done\n"
            "echo 'Fresh analysis result'\n"
        )
        agent.chmod(0o755)
        config = WorkdashConfig(agents={"test": AgentConfig(analyze=str(agent))})
        github = MagicMock()
        github.fetch_analysis_context.return_value = {"body": "Issue context"}
        backend = WorkdashBackend(
            cache_root=tmp_path / "cache", github_client=github, config=config
        )
        monkeypatch.setattr(
            "workdash.control.analysis_worktree", lambda *a, **kw: nullcontext(str(tmp_path))
        )
        item.analysis = None
        session = WorkdashSession(
            config=config,
            backend=backend,
            work_items=[item],
            suggestion_markers={},
            zellij_session=name,
        )
        server = WorkdashControlServer(session)
        server.start()
        try:
            client = WorkdashControlClient(f"127.0.0.1:{port}")
            for index, request in enumerate(
                (
                    lambda: (
                        client.request("analyze", {"target": "owner/repo#ISSUE-1"})["status"]
                        == "generated"
                    ),
                    lambda: main(["analyze", "owner/repo#ISSUE-1", "--json"]) == 0,
                )
            ):
                release.unlink(missing_ok=True)
                started.unlink(missing_ok=True)
                backend.analysis_cache.build_cache_path(item).unlink(missing_ok=True)
                item.analysis = None
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(request)
                    try:
                        wait_for(
                            lambda panes: any(
                                p.get("title") == f"analyse_{tmp_path.name}" for p in panes
                            )
                        )
                        deadline = time.monotonic() + 15
                        while (
                            not started.exists()
                            and not future.done()
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.05)
                        if not started.exists():
                            panes = action("list-panes", "--json", "--all")
                            screen = action("dump-screen")
                            outcome = repr(future.exception()) if future.done() else "still running"
                            pytest.fail(
                                f"analysis agent {index} did not start; request: {outcome}; "
                                f"panes: {panes.stdout!r} {panes.stderr!r}; "
                                f"screen: {screen.stdout!r} {screen.stderr!r}"
                            )
                        marker = f"RUNNING_FOCUS_{index}"
                        assert action("write-chars", f"echo {marker}\n").returncode == 0
                        time.sleep(0.2)
                        assert (
                            marker
                            in action(
                                "dump-screen", "--pane-id", f"terminal_{original['id']}"
                            ).stdout
                        )
                    finally:
                        release.touch()
                    assert future.result(timeout=15)
                    assert (
                        backend.analysis_cache.build_analysis_path(item).read_text().strip()
                        == "Fresh analysis result"
                    )
                    marker = f"FINISHED_FOCUS_{index}"
                    assert action("write-chars", f"echo {marker}\n").returncode == 0
                    time.sleep(0.2)
                    assert (
                        marker
                        in action("dump-screen", "--pane-id", f"terminal_{original['id']}").stdout
                    )
        finally:
            server.stop()

    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])
        sockets.cleanup()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("selected", ["custom", "unset"])
@pytest.mark.parametrize("repeat", range(2))
def test_restart_uses_current_host_in_existing_zellij_session(tmp_path, selected, repeat):
    import socket

    from workdash.control import WorkdashControlClient, WorkdashControlError

    zellij = shutil.which("zellij")
    sockets = _private_socket_dir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        old_port = probe.getsockname()[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    name = f"wd-{uuid.uuid4().hex[:8]}"
    received = tmp_path / "received"
    dashboard = tmp_path / "dashboard.py"
    dashboard.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "import workdash.workdash as cli\n"
        "import workdash.control as control\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        f"Path({str(received)!r}).write_text(os.environ.get('WORKDASH_HOST', '<unset>'))\n"
        f"control.SERVER_LOG_PATH = Path({str(tmp_path / 'workdash.log')!r})\n"
        + (f"control.SERVER_PORT = {port}\n" if selected == "unset" else "")
        + "cli.GitHubClient.check_authentication = lambda self: None\n"
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
        "analysis_viewer={'command': 'workdash view', 'format': 'markdown', 'needs_pane': True}, "
        "agents={'agent': AgentConfig(launch='sh')}, repositories=('owner/repo',), "
        f"workdir={str(tmp_path)!r}, todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([], {})\n"
        "raise SystemExit(cli.main(['--persistent-pane']))\n"
    )
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import sys\n"
        "import workdash.control as control\n"
        "from workdash import launcher\n"
        f"launcher._build_direct_workdash_command = lambda args: [{sys.executable!r}, {str(dashboard)!r}]\n"
        "launcher.os.execvp = lambda *args: sys.exit(0)\n"
        # The dashboard under test uses this default address too.
        + (f"control.SERVER_PORT = {port}\n" if selected == "unset" else "")
        + f"launcher.zellij.exec_zellij_wrapped_workdash(['--session', {name!r}, '--debug'])\n"
    )
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    config = tmp_path / "config.kdl"
    config.write_text("")
    env.update(
        TERM="xterm-256color",
        HOME=_temporary_home(tmp_path),
        SHELL="/bin/sh",
        WORKDASH_HOST=f"127.0.0.1:{old_port}",
        ZELLIJ_CONFIG_FILE=str(config),
        ZELLIJ_SOCKET_DIR=sockets.name,
    )
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(zellij, [zellij, "--session", name], env)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = subprocess.run(
                [zellij, "list-sessions", "--no-formatting"],
                env=env,
                capture_output=True,
                text=True,
            )
            if name in result.stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail("Zellij session did not start")
        current = env.copy()
        if selected == "unset":
            del current["WORKDASH_HOST"]
        else:
            current["WORKDASH_HOST"] = f"127.0.0.1:{port}"
        subprocess.run([sys.executable, str(wrapper)], env=current, check=True, timeout=15)
        deadline = time.monotonic() + 15
        client = WorkdashControlClient(f"127.0.0.1:{port}")
        while time.monotonic() < deadline:
            try:
                result = client.request("info")
                break
            except WorkdashControlError:
                time.sleep(0.1)
        else:
            pytest.fail(
                f"dashboard did not listen on {port}; received {received.read_text() if received.exists() else 'nothing'}"
            )
        assert received.read_text() == ("<unset>" if selected == "unset" else f"127.0.0.1:{port}")
        assert result["session"] == name
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])
        sockets.cleanup()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
def test_startup_resurrects_exited_session_with_work_pane(tmp_path):
    import socket

    from workdash.control import WorkdashControlClient, WorkdashControlError

    zellij = shutil.which("zellij")
    name = f"wd-{uuid.uuid4().hex[:8]}"
    script = tmp_path / "workdash"
    script.write_text(
        f"#!{sys.executable}\n"
        "import workdash.workdash as cli\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        "cli.GitHubClient.check_authentication = lambda self: None\n"
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
        "analysis_viewer={'command': 'workdash view', 'format': 'markdown', 'needs_pane': True}, "
        "agents={'agent': AgentConfig(launch='sh')}, repositories=('owner/repo',), "
        f"workdir={str(tmp_path)!r}, todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([], {})\n"
        "raise SystemExit(cli.main())\n"
    )
    script.chmod(0o755)
    command = tmp_path / "saved-command"
    runs = tmp_path / "command-runs"
    command.write_text(f"#!/bin/sh\ntrap '' HUP\necho $$ >> {shlex.quote(str(runs))}\nsleep 120\n")
    command.chmod(0o755)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "config.kdl"
    config.write_text(
        "session_serialization false\ndisable_session_metadata true\nserialization_interval 1\n"
    )
    with _private_socket_dir() as sockets:
        env = os.environ.copy()
        for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
            env.pop(key, None)
        env.update(
            TERM="xterm-256color",
            HOME=_temporary_home(tmp_path),
            SHELL="/bin/sh",
            WORKDASH_HOST=f"127.0.0.1:{port}",
            ZELLIJ_SOCKET_DIR=sockets,
            ZELLIJ_CONFIG_FILE=str(config),
            XDG_CACHE_HOME=str(tmp_path / "cache"),
            XDG_DATA_HOME=str(tmp_path / "data"),
            XDG_CONFIG_HOME=str(tmp_path / "config"),
        )
        clients = []
        previously_owned = {}

        def start(command):
            pid, fd = pty.fork()
            if pid == 0:
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                os.execve(command[0], command, env)
            clients.append((pid, fd))

        def run(*args):
            return subprocess.run([zellij, *args], env=env, capture_output=True, text=True)

        def stop_for_resurrection():
            previously_owned.update(_session_processes(env, {name: {pid for pid, _ in clients}}))
            assert run("kill-session", name).returncode == 0
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                sessions = run("list-sessions", "--no-formatting").stdout
                if any(
                    line.startswith(f"{name} ") and "(EXITED" in line
                    for line in sessions.splitlines()
                ):
                    return
                time.sleep(0.1)
            pytest.fail(f"session did not exit: {sessions}")

        try:
            start([str(script), "--session", name])
            client = WorkdashControlClient(f"127.0.0.1:{port}")
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    client.request("info")
                    break
                except WorkdashControlError:
                    time.sleep(0.1)
            else:
                pytest.fail("initial dashboard did not start")
            assert (
                run(
                    "--session", name, "action", "new-tab", "--name", "work", "--cwd", str(tmp_path)
                ).returncode
                == 0
            )
            assert (
                run(
                    "--session",
                    name,
                    "action",
                    "new-pane",
                    "--name",
                    "saved-command",
                    "--",
                    str(command),
                ).returncode
                == 0
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                layouts = list((tmp_path / "cache").rglob("session-layout.kdl"))
                if (
                    runs.exists()
                    and runs.read_text()
                    and any(
                        'name="work"' in layout.read_text() and str(command) in layout.read_text()
                        for layout in layouts
                    )
                ):
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"work tab was not serialized: {layouts}")
            saved_pid = int(runs.read_text())
            stop_for_resurrection()
            assert saved_pid in _live_processes(), "the saved pane did not survive the session stop"
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    client.request("info")
                except WorkdashControlError:
                    break
                time.sleep(0.1)
            else:
                pytest.fail("original dashboard did not stop")
            start([str(script), "--session", name])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    info = client.request("info")
                    break
                except WorkdashControlError:
                    time.sleep(0.1)
            else:
                pytest.fail("resurrected dashboard did not start")
            assert info["session"] == name
            assert client.request("list")["items"] == []
            panes = json.loads(
                run("--session", name, "action", "list-panes", "--json", "--all").stdout
            )
            assert runs.read_text().splitlines() == [str(saved_pid)]
            assert any(p["title"] == "saved-command" and p["is_held"] for p in panes)
            work = next(
                p
                for p in panes
                if p["tab_name"] == "work" and not p["is_plugin"] and not p["is_held"]
            )
            pane_id = f"terminal_{work['id']}"
            assert (
                run(
                    "--session",
                    name,
                    "action",
                    "write-chars",
                    "--pane-id",
                    pane_id,
                    "echo preserved > preserved.txt",
                ).returncode
                == 0
            )
            assert (
                run(
                    "--session", name, "action", "send-keys", "--pane-id", pane_id, "Enter"
                ).returncode
                == 0
            )
            deadline = time.monotonic() + 10
            while not (tmp_path / "preserved.txt").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            assert (tmp_path / "preserved.txt").read_text().strip() == "preserved"
            assert (
                run("--session", name, "action", "new-tab", "--name", "later-work").returncode == 0
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                layouts = list((tmp_path / "cache").rglob("session-layout.kdl"))
                if any('name="later-work"' in layout.read_text() for layout in layouts):
                    break
                time.sleep(0.1)
            else:
                pytest.fail("tab added after recovery was not serialized")
            stop_for_resurrection()
            start([str(script), "--session", name])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    client.request("info")
                    panes = json.loads(
                        run("--session", name, "action", "list-panes", "--json", "--all").stdout
                    )
                    if any(p["tab_name"] == "later-work" for p in panes) and any(
                        p["title"] == "workdash tui" and not p["is_held"] for p in panes
                    ):
                        break
                except (WorkdashControlError, json.JSONDecodeError):
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("tab added after recovery did not survive the next recovery")
            assert runs.read_text().splitlines() == [str(saved_pid)]
            assert any(p["title"] == "saved-command" and p["is_held"] for p in panes)
            assert any(p["tab_name"] == "work" and not p["is_plugin"] for p in panes)
            dashboard = next(p for p in panes if p["title"] == "workdash tui" and not p["is_held"])
            dashboard_id = f"terminal_{dashboard['id']}"
            assert (
                run(
                    "--session", name, "action", "write-chars", "--pane-id", dashboard_id, "q"
                ).returncode
                == 0
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                screen = run(
                    "--session", name, "action", "dump-screen", "--pane-id", dashboard_id
                ).stdout
                if "Ctrl+Q" in screen:
                    break
                time.sleep(0.1)
            else:
                pytest.fail(f"dashboard did not respond to quit: {screen}")
        finally:
            _cleanup_sessions(
                zellij,
                env,
                [(name, pid, fd) for pid, fd in clients],
                previously_owned=previously_owned,
            )
        assert saved_pid not in _live_processes(), "cleanup missed the pane from the first stop"


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("choice", ["a", "1"])
@pytest.mark.parametrize("repeat", range(2))
def test_dashboard_analysis_focuses_viewer_in_real_zellij(tmp_path, choice, repeat):
    import socket

    sockets = _private_socket_dir()
    config = tmp_path / "config.kdl"
    config.write_text("show_release_notes false\nshow_startup_tips false\n")
    zellij = shutil.which("zellij")
    name = f"dashboard-focus-{uuid.uuid4().hex[:8]}"
    document = tmp_path / "analysis.md"
    document.write_text("# Dashboard analysis\n\nViewer has focus.\n")
    harness = tmp_path / "dashboard.py"
    harness.write_text(
        "from datetime import UTC, datetime\n"
        "from pathlib import Path\n"
        "import os\n"
        f"Path({str(tmp_path / 'host')!r}).write_text(os.environ['WORKDASH_HOST'])\n"
        "import workdash.workdash as cli\n"
        "import workdash.control as control\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        "from workdash.models import WorkItem, WorkItemKind, WorkItemType\n"
        "now = datetime.now(UTC)\n"
        "item = WorkItem(kind=WorkItemKind.ASSIGNED_ISSUE, item_type=WorkItemType.ISSUE, "
        "repo='owner/repo', number=1, title='Test issue', created_at=now, updated_at=now, "
        "url='https://example.com/1', analyzed_at=now)\n"
        "cli.GitHubClient.check_authentication = lambda self: None\n"
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
        f"analysis_viewer={{'command': '{sys.executable} -m workdash view', "
        "'format': 'markdown', 'needs_pane': True}, "
        "agents={'agent': AgentConfig(analyze='unused')}, repositories=('owner/repo',), "
        f"workdir={str(tmp_path)!r}, todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([item], {})\n"
        "def analyze(self, **kwargs):\n"
        f"    Path({str(tmp_path / 'choice')!r}).write_text(str(kwargs['prefer_cache']))\n"
        f"    return {{'path': {str(document)!r}}}\n"
        "control.WorkdashSession.analyze = analyze\n"
        f"control.SERVER_LOG_PATH = Path({str(tmp_path / 'workdash.log')!r})\n"
        "raise SystemExit(cli.main([]))\n"
    )
    shell = tmp_path / "shell"
    shell.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(harness))}\n")
    shell.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    env.update(
        TERM="xterm-256color",
        SHELL=str(shell),
        WORKDASH_HOST=f"127.0.0.1:{port}",
        ZELLIJ_SOCKET_DIR=sockets.name,
        HOME=_temporary_home(tmp_path),
        ZELLIJ_CONFIG_FILE=str(config),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
    )
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(zellij, [zellij, "--session", name], env)

    def action(*args):
        return subprocess.run(
            [zellij, "--session", name, "action", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    try:
        deadline = time.monotonic() + 20
        output = b""
        while time.monotonic() < deadline and b"Test issue" not in output:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    output += os.read(fd, 65536)
                except OSError:
                    break
        if b"Test issue" not in output:
            result = action("dump-screen")
            pytest.fail(f"dashboard did not start: stderr={result.stderr!r}, PTY={output!r}")
        assert action("write-chars", "a").returncode == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            screen = action("dump-screen").stdout
            if "Open analysis" in screen:
                break
            time.sleep(0.1)
        assert "Open analysis" in screen
        assert action("write-chars", choice).returncode == 0
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            screen = action("dump-screen").stdout
            if "Dashboard analysis" in screen:
                break
            time.sleep(0.1)
        assert "Dashboard analysis" in screen
        assert (tmp_path / "choice").read_text() == str(choice == "a")
        assert (tmp_path / "host").read_text() == env["WORKDASH_HOST"]
        assert action("write-chars", "q").returncode == 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            screen = action("dump-screen").stdout
            if "Test issue" in screen:
                break
            time.sleep(0.1)
        assert "Test issue" in screen
    finally:
        _cleanup_sessions(zellij, env, [(name, pid, fd)])
        sockets.cleanup()


def _attach_running_dashboard_focus_harness(
    tmp_path, has_pane, duplicate_tab=False, new_last_input=False, last_detach=False
):
    import socket

    zellij = shutil.which("zellij")
    name = f"workdash-focus-{uuid.uuid4().hex[:8]}"
    env = os.environ.copy()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID", "XDG_CONFIG_HOME"):
        env.pop(key, None)
    shell = tmp_path / "shell"
    dashboard = tmp_path / "dashboard.py"
    dashboard.write_text(
        "import workdash.workdash as cli\n"
        "cli.GitHubClient.check_authentication = lambda self: None\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([], {})\n"
        "raise SystemExit(cli.main())\n"
    )
    shell.write_text(
        f'#!/bin/sh\nif [ "$ZELLIJ_PANE_ID" = {1 if duplicate_tab else 0} ]; then exec {shlex.quote(sys.executable)} '
        f"{shlex.quote(str(dashboard))}; fi\necho DASHBOARD_PANE_$ZELLIJ_PANE_ID\nexec /bin/sh\n"
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    shell.chmod(0o755)
    sockets = _private_socket_dir()
    config = tmp_path / "config.kdl"
    config.write_text("show_release_notes false\nshow_startup_tips false\n")
    home = _temporary_home(tmp_path)
    (tmp_path / "home" / ".config" / "workdash" / "config.json").write_text(
        json.dumps(
            {
                "github_username": "test",
                "open": "xdg-open",
                "analysis_viewer": {
                    "command": "workdash view",
                    "format": "markdown",
                    "needs_pane": True,
                },
                "agents": {"agent": {"analyze": "", "launch": "sh"}},
                "repositories": ["owner/repo"],
                "workdir": "/tmp",
                "todo_repository": "test/todos",
            }
        ),
        encoding="utf-8",
    )
    env.update(
        TERM="xterm-256color",
        HOME=home,
        SHELL=str(shell),
        WORKDASH_HOST=f"127.0.0.1:{port}",
        ZELLIJ_SOCKET_DIR=sockets.name,
        ZELLIJ_CONFIG_FILE=str(config),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
    )
    if new_last_input:
        wrapper_dir = tmp_path / "bin"
        wrapper_dir.mkdir()
        wrapper = wrapper_dir / "zellij"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$3" = action ] && [ "$4" = go-to-tab-by-id ]; then\n'
            f"  touch {shlex.quote(str(tmp_path / 'focus-pending'))}\n"
            f"  while [ ! -e {shlex.quote(str(tmp_path / 'focus-release'))} ]; do sleep 0.01; done\n"
            "fi\n"
            f'exec {shlex.quote(zellij)} "$@"\n'
        )
        wrapper.chmod(0o755)
        env["PATH"] = f"{wrapper_dir}:{env['PATH']}"
    sessions = []
    drain_threads = []
    output = bytearray()
    returning_output = bytearray()
    stop_draining = threading.Event()

    def drain(fd, capture):
        while not stop_draining.is_set():
            try:
                if select.select([fd], [], [], 0.1)[0]:
                    capture.extend(os.read(fd, 65536))
            except OSError:
                return

    def action(*args):
        return subprocess.run(
            [zellij, "--session", name, "action", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        result = None
        while time.monotonic() < deadline:
            try:
                result = action("list-panes", "--json", "--all", "--tab")
                panes = json.loads(result.stdout)
                if predicate(panes):
                    return panes
            except (subprocess.SubprocessError, ValueError):
                pass
            time.sleep(0.1)
        process = subprocess.run(
            ["ps", "-o", "pid,ppid,state,wchan,args", "-p", str(sessions[0][1])],
            capture_output=True,
            text=True,
        ).stdout
        pytest.fail(f"Zellij state not reached: {result}; PTY: {output[-3000:]!r}; {process}")

    try:
        pid, fd = pty.fork()
        if pid == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.execve(zellij, [zellij, "--session", name], env)
        sessions.append((name, pid, fd))
        drain_thread = threading.Thread(target=drain, args=(fd, output), daemon=True)
        drain_thread.start()
        drain_threads.append(drain_thread)
        panes = wait_for(lambda ps: any(not p["is_plugin"] for p in ps))
        if duplicate_tab:
            action("rename-tab", "renamed")
            action("new-tab", "--name", "dashboard")
            panes = wait_for(lambda ps: any(not p["is_plugin"] and p["id"] == 1 for p in ps))
        pane = next(
            p for p in panes if not p["is_plugin"] and p["id"] == (1 if duplicate_tab else 0)
        )
        record = tmp_path / "home" / ".config" / "workdash" / "sessions" / f"{name}.json"
        deadline = time.monotonic() + 20
        while not record.exists():
            assert time.monotonic() < deadline, f"dashboard did not start: {output[-4000:]!r}"
            time.sleep(0.1)
        action("rename-tab", "renamed")
        action("new-pane")
        wait_for(lambda ps: sum(not p["is_plugin"] for p in ps) == (3 if duplicate_tab else 2))
        if last_detach:
            action("new-pane")
            wait_for(lambda ps: sum(not p["is_plugin"] for p in ps) == 3)
        if duplicate_tab:
            action("go-to-tab", "1")
            other = next(p for p in panes if not p["is_plugin"] and p["id"] == 0)
        else:
            action("new-tab", "--name", "other")
            panes = wait_for(lambda ps: any(p.get("tab_name") == "other" for p in ps))
            other = next(p for p in panes if p.get("tab_name") == "other" and not p["is_plugin"])
        if not has_pane:
            data = json.loads(record.read_text())
            data["dashboard"]["pane_id"] = None
            record.write_text(json.dumps(data))
        os.write(fd, b"\n")
        client_id = action("list-clients").stdout.splitlines()[1].split()[0]
        time.sleep(0.5)
        yield
        if last_detach:
            action("detach")
            deadline = time.monotonic() + 5
            while action("list-clients").stdout.splitlines()[1:]:
                assert time.monotonic() < deadline, "last client did not detach"
                time.sleep(0.01)
        output.clear()
        stop_draining.set()
        drain_thread.join()
        stop_draining.clear()
        child, child_fd = pty.fork()
        if child == 0:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.execve(sys.executable, [sys.executable, "-m", "workdash", "--session", name], env)
        sessions.append((name, child, child_fd))
        drain_thread = threading.Thread(target=drain, args=(fd, output), daemon=True)
        child_drain_thread = threading.Thread(
            target=drain, args=(child_fd, returning_output), daemon=True
        )
        drain_thread.start()
        drain_threads.append(drain_thread)
        child_drain_thread.start()
        drain_threads.append(child_drain_thread)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            clients = action("list-clients").stdout.splitlines()[1:]
            new_clients = [
                line.split() for line in clients if last_detach or line.split()[0] != client_id
            ]
            if len(new_clients) == 1 and (
                last_detach or new_clients[0][1] == f"terminal_{other['id']}"
            ):
                break
            time.sleep(0.01)
        assert len(new_clients) == 1 and (
            last_detach or new_clients[0][1] == f"terminal_{other['id']}"
        ), f"new client did not attach: {clients}; CLI PTY: {returning_output[-3000:]!r}"
        new_client_id = new_clients[0][0]
        if new_last_input:
            deadline = time.monotonic() + 5
            while not (tmp_path / "focus-pending").exists():
                assert time.monotonic() < deadline, "focus action was not issued"
                time.sleep(0.01)
            assert any(
                line.split()[:2] == [client_id, f"terminal_{other['id']}"] for line in clients
            )
            os.write(child_fd, b"printf 'NEW_CLIENT_%s\\n' INPUT_PROOF\n")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                screen = action("dump-screen", "--pane-id", f"terminal_{other['id']}").stdout
                if "NEW_CLIENT_INPUT_PROOF" in screen:
                    break
                time.sleep(0.01)
            assert "NEW_CLIENT_INPUT_PROOF" in screen
            (tmp_path / "focus-release").touch()
        yield returning_output
        focused = pane if has_pane else other
        focus_client = new_client_id if new_last_input or last_detach else client_id
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            clients = action("list-clients").stdout.splitlines()[1:]
            if any(
                line.split()[:2] == [focus_client, f"terminal_{focused['id']}"] for line in clients
            ):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"client {focus_client} did not focus pane {focused['id']}: {clients}")
        input_fd = child_fd if new_last_input or last_detach or not has_pane else fd
        if has_pane:
            os.write(input_fd, b"/")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                screen = action("dump-screen", "--pane-id", f"terminal_{focused['id']}").stdout
                if "Filter items by type" in screen:
                    break
                time.sleep(0.1)
            assert "Filter items by type" in screen
            if new_last_input or last_detach:
                return
            os.write(input_fd, b"FOCUSED_INPUT_PROOF")
        else:
            os.write(input_fd, b"printf 'FOCUSED_%s\\n' INPUT_PROOF\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            screen = action("dump-screen", "--pane-id", f"terminal_{focused['id']}").stdout
            if "FOCUSED_INPUT_PROOF" in screen:
                break
            time.sleep(0.1)
        assert "FOCUSED_INPUT_PROOF" in screen, (screen, action("list-clients").stdout)
        if has_pane and not new_last_input:
            clients = action("list-clients").stdout.splitlines()[1:]
            new_pane = next(line.split()[1] for line in clients if line.split()[0] == new_client_id)
            assert new_pane == f"terminal_{other['id']}"
            os.write(child_fd, b"printf 'NEW_CLIENT_%s\\n' INPUT_PROOF\n")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                screen = action("dump-screen", "--pane-id", new_pane).stdout
                if "NEW_CLIENT_INPUT_PROOF" in screen:
                    break
                time.sleep(0.1)
            assert "NEW_CLIENT_INPUT_PROOF" in screen
    finally:
        if new_last_input:
            (tmp_path / "focus-release").touch()
        stop_draining.set()
        for thread in drain_threads:
            thread.join()
        _cleanup_sessions(zellij, env, sessions)
        sockets.cleanup()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("attempt", range(2))
def test_attach_after_last_client_detaches_from_other_tab(tmp_path, attempt):
    harness = _attach_running_dashboard_focus_harness(tmp_path, True, last_detach=True)
    try:
        next(harness)
        next(harness)
        with pytest.raises(StopIteration):
            next(harness)
    finally:
        harness.close()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("attempt", range(2))
@pytest.mark.parametrize(
    "has_pane,duplicate_tab,new_last_input",
    [(False, False, False), (True, False, False), (True, True, False), (True, False, True)],
)
def test_attach_running_dashboard_focuses_its_pane(
    tmp_path, attempt, has_pane, duplicate_tab, new_last_input
):
    harness = _attach_running_dashboard_focus_harness(
        tmp_path, has_pane, duplicate_tab, new_last_input
    )
    try:
        next(harness)
        next(harness)
        with pytest.raises(StopIteration):
            next(harness)
    finally:
        harness.close()
