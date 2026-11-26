"""Exercise Workdash pane launches against a real Zellij session."""

import errno
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
from typing import NamedTuple
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
    pane_groups = {}
    interrupted = False

    def defer_interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    # Defer Ctrl+C in pytest; run cleanup commands outside its foreground group.
    previous_handler = signal.signal(signal.SIGINT, defer_interrupt)
    try:
        try:
            # Record descendants before Zellij can hang them up and orphan them.
            discovery_deadline = time.monotonic() + 1
            while True:
                try:
                    current = _session_processes(
                        zellij, env, by_name, pane_groups=pane_groups, deadline=discovery_deadline
                    )
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= discovery_deadline:
                        raise
            owned.update(current)
            # Stop startup/attach clients so they cannot launch a server during deletion.
            for (pid, _), name in current.items():
                if pid in by_name[name]:
                    with suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
            owned.update(_session_processes(zellij, env, by_name, pane_groups=pane_groups))
            for name in by_name:
                with suppress(subprocess.TimeoutExpired):
                    subprocess.run(
                        [zellij, "delete-session", "--force", name],
                        env=env,
                        capture_output=True,
                        timeout=1,
                        start_new_session=True,
                    )
            owned.update(_session_processes(zellij, env, by_name))
        finally:
            _kill_and_verify_owned_processes(owned, pane_groups)
    finally:
        try:
            for _, pid, fd in sessions:
                with suppress(ChildProcessError):
                    os.waitpid(pid, os.WNOHANG)
                os.close(fd)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            if interrupted:
                signal.raise_signal(signal.SIGINT)


def _kill_and_verify_owned_processes(owned, pane_groups=None):
    deadline = time.monotonic() + 1
    signaled = set()
    remaining = 1
    survivors = {}
    while True:
        try:
            live = _live_processes(timeout=min(0.5, remaining))
        except subprocess.TimeoutExpired as error:
            timeout = error
        else:
            timeout = None
            # An orphan still belongs to its pane group after the leader exits.
            # A different live leader means the group ID has been reused.
            live_groups = {
                pgid: name
                for (pgid, started), name in (pane_groups or {}).items()
                if pgid not in live or live[pgid].started == started
            }
            survivors = {}
            for pid, process in live.items():
                identity = pid, process.started
                name = owned.get(identity) or live_groups.get(process.pgid)
                if name is not None:
                    survivors[identity] = f"{name}: PID {pid} ({process.command})"
            if not survivors:
                break
            for pid, started in survivors.keys() - signaled:
                with suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
                signaled.add((pid, started))
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
            remaining = deadline - time.monotonic()
        if remaining <= 0:
            if survivors:
                raise AssertionError(
                    "Zellij cleanup left live processes: " + "; ".join(survivors.values())
                ) from timeout
            raise timeout


def _session_processes(zellij, env, sessions, *, pane_groups=None, deadline=None):
    """Find the server by its private socket path, even if Workdash started it."""

    def ps_timeout():
        if deadline is None:
            return 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("Zellij process discovery", 1)
        return min(0.3, remaining)

    live = _live_processes(timeout=ps_timeout())
    socket_dir = Path(env["ZELLIJ_SOCKET_DIR"]).resolve()
    listing = subprocess.run(
        ["ps", "-ww", "-U", str(os.getuid()), "-o", "pid=", "-o", "args="],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
        timeout=ps_timeout(),
        start_new_session=True,
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
        if sys.platform == "linux":
            # ps can rewrite non-ASCII paths and lose argument boundaries.
            try:
                argv = Path(f"/proc/{pid}/cmdline").read_bytes().removesuffix(b"\0").split(b"\0")
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
            if len(argv) < 3 or argv[1] != b"--server" or not os.path.isabs(argv[0]):
                continue
            socket_arg = os.fsdecode(argv[2])
        else:
            args = subprocess.run(
                ["ps", "-ww", "-p", str(pid), "-o", "args="],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=ps_timeout(),
                start_new_session=True,
            ).stdout.strip()
            executable, flag, socket_arg = args.partition(" --server ")
            # With flattened argv, require the known binary to reject embedded matches.
            if not flag or executable not in {os.path.abspath(zellij), os.path.realpath(zellij)}:
                continue
        socket = Path(socket_arg)
        if (
            socket.name in sessions
            and socket.is_absolute()
            and socket.resolve().parent.is_relative_to(socket_dir)
        ):
            servers[socket.name] = pid

    owned = {}
    for name, clients in sessions.items():
        roots = {pid for pid in clients if pid in live and live[pid].parent == os.getpid()}
        if name in servers:
            roots.add(servers[name])
        pids = roots.copy()
        while descendants := {pid for pid, proc in live.items() if proc.parent in pids} - pids:
            pids.update(descendants)
        owned.update({(pid, live[pid].started): name for pid in pids})
        if pane_groups is not None:
            # Only follow groups led by owned descendants, not the client's or server's group.
            pane_groups.update(
                {(pid, live[pid].started): name for pid in pids - roots if live[pid].pgid == pid}
            )
    return owned


class _LiveProcess(NamedTuple):
    parent: int
    started: tuple[str, ...]
    command: str
    pgid: int


def _live_processes(*, timeout=1):
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
            "pgid=",
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
        timeout=timeout,
        start_new_session=True,
    )
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=9)
        if len(fields) >= 9 and all(field.isdigit() for field in fields[:3]):
            pid, parent, pgid, state, *started = fields[:9]
            if not state.startswith("Z"):
                processes[int(pid)] = _LiveProcess(
                    int(parent), tuple(started), fields[9] if len(fields) == 10 else "", int(pgid)
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
                stdout = f"{unrelated_pid} 1 1 S Mon Jan  1 00:00:00 2024 sleep\n"
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
    if sys.platform == "linux":
        monkeypatch.setattr(Path, "read_bytes", lambda path: b"/bin/sleep\x00120\0")
    monkeypatch.setattr(os, "kill", kill)
    fd = os.open(os.devnull, os.O_RDONLY)
    _cleanup_sessions("zellij", env, [(socket.name, os.getpid(), fd)])
    kill.assert_not_called()


@pytest.mark.parametrize(
    "timeout_at", ["initial-discovery", "discovery", "kill", "persistent", "survivor"]
)
def test_zellij_cleanup_kills_and_verifies_recorded_pids_after_ps_timeout(
    tmp_path, monkeypatch, timeout_at
):
    pid, reused_pid = 12345, 12346
    started = ("Mon", "Jan", "1", "00:00:00", "2024")
    session = "test-session"
    deleted = discovered = matched = killed = verified = False
    elapsed = 0.0
    timeouts = []

    def advance(seconds):
        nonlocal elapsed
        elapsed += seconds

    def run(command, **kwargs):
        nonlocal deleted, discovered, matched, verified
        stdout = ""
        if command[0] == "zellij":
            assert command[1:] == ["delete-session", "--force", session]
            deleted = True
        elif (timeout_at == "initial-discovery" and elapsed < 1) or (
            deleted
            and (
                (timeout_at == "discovery" and not timeouts)
                or (
                    discovered
                    and (timeout_at == "persistent" or (timeout_at == "kill" and not timeouts))
                )
                or (timeout_at == "survivor" and killed)
            )
        ):
            timeout = subprocess.TimeoutExpired(command, kwargs["timeout"])
            timeouts.append(timeout)
            advance(timeout.timeout)
            raise timeout
        elif "ucomm=" in command:
            stdout = f"{reused_pid} 1 1 S Mon Jan  1 00:00:01 2024 unrelated\n"
            if killed:
                verified = True
            else:
                matched = timeout_at == "initial-discovery" or (
                    deleted and (discovered or bool(timeouts))
                )
                stdout += f"{pid} 1 1 S Mon Jan  1 00:00:00 2024 pane\n"
        elif deleted:
            discovered = True
        return subprocess.CompletedProcess(command, 0, stdout)

    def kill(target, sig):
        nonlocal killed
        assert matched
        assert (target, sig) == (pid, signal.SIGKILL)
        killed = True

    monkeypatch.setattr(time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(time, "sleep", advance)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(os, "kill", kill)
    fd = os.open(os.devnull, os.O_RDONLY)
    if timeout_at == "kill":
        expected = nullcontext()
    elif timeout_at == "survivor":
        expected = pytest.raises(AssertionError, match=rf"{session}: PID {pid} \(pane\)")
    else:
        expected = pytest.raises(subprocess.TimeoutExpired)
    with expected as error:
        _cleanup_sessions(
            "zellij",
            {"ZELLIJ_SOCKET_DIR": str(tmp_path)},
            [(session, os.getpid(), fd)],
            previously_owned={(pid, started): session, (reused_pid, started): session},
        )
    if timeout_at == "survivor":
        assert error.value.__cause__ is timeouts[-1]
        assert killed and elapsed == pytest.approx(1)
        return
    if error is not None:
        assert error.value is timeouts[-1]
    if timeout_at == "persistent":
        assert len(timeouts) > 1
        assert elapsed == pytest.approx(1)
        assert not killed and not verified
    elif timeout_at == "initial-discovery":
        assert len(timeouts) > 2
        assert sum(timeout.timeout for timeout in timeouts) == pytest.approx(1)
        assert not deleted
        assert killed and verified
    else:
        assert len(timeouts) == 1
        assert killed and verified
        if timeout_at == "kill":
            assert elapsed < 1


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "failure",
        "ctrl-c",
        "ctrl-c-delete",
        "ctrl-c-scan",
        "ctrl-c-first-ps",
        "ctrl-c-verify",
        "late-child",
        "two-initial-ps-timeouts",
        "survivor",
        "renamed-binary",
        "long-tmpdir",
        pytest.param(
            "debug-server",
            marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux server argv"),
        ),
        pytest.param(
            "non-ascii-binary-dir",
            marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux ps C locale"),
        ),
    ],
)
def test_zellij_cleanup_kills_hup_ignoring_pane(tmp_path, monkeypatch, case):
    zellij = shutil.which("zellij")
    if case in {"renamed-binary", "non-ascii-binary-dir"}:
        bin_dir = tmp_path / "bin"
        if case == "non-ascii-binary-dir":
            bin_dir = tmp_path / "bin-è"
            monkeypatch.setenv("LC_ALL", "C")
        bin_dir.mkdir()
        binary = bin_dir / "terminal-multiplexer"
        shutil.copy2(zellij, binary)
        link = bin_dir / "zellij"
        link.symlink_to(binary)
        monkeypatch.setenv("PATH", str(bin_dir), prepend=os.pathsep)
        zellij = str(link)
    elif case == "debug-server":
        wrapper = tmp_path / "zellij"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$#" -eq 2 ] && [ "$1" = --session ]; then set -- "$@" --debug; fi\n'
            f'exec {shlex.quote(zellij)} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o700)
        monkeypatch.setenv("PATH", str(tmp_path), prepend=os.pathsep)
        zellij = str(wrapper)
    elif case == "long-tmpdir":
        long_tmpdir = tmp_path / ("long-" * 24)
        long_tmpdir.mkdir()
        monkeypatch.setenv("TMPDIR", str(long_tmpdir))
        monkeypatch.setattr(tempfile, "tempdir", None)

    body_error = {"failure": AssertionError, "ctrl-c": KeyboardInterrupt}.get(case)
    leave_survivor = case == "survivor"
    interrupt_cleanup = case.startswith("ctrl-c-")
    scans = initial_timeouts = 0
    sent_interrupt = post_delete_scan = deleted = False
    ready = tmp_path / "pane-pids"
    release = tmp_path / "fork-late-child"
    late_ready = tmp_path / "late-child-pid"
    owned = {}
    fallback = {}
    kill = os.kill
    run = subprocess.run
    previous_socket_dir = os.environ.get("ZELLIJ_SOCKET_DIR")

    def kill_except_pane(pid, sig):
        if (pid, sig) != (pane_pid, signal.SIGKILL):
            kill(pid, sig)

    def run_during_teardown(command, **kwargs):
        nonlocal scans, sent_interrupt, post_delete_scan, initial_timeouts, deleted
        ps = command[0] == "ps"
        if case == "two-initial-ps-timeouts" and ps and initial_timeouts < 2:
            initial_timeouts += 1
            time.sleep(kwargs["timeout"])
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        delete = command == [zellij, "delete-session", "--force", name]
        scan = ps and "-U" in command and "args=" in command
        if scan:
            scans += 1
            post_delete_scan |= deleted
        if not sent_interrupt and (
            (case == "ctrl-c-first-ps" and ps)
            or (case == "ctrl-c-scan" and scan and scans == 2)
            or (case == "ctrl-c-delete" and delete)
            or (case == "ctrl-c-verify" and post_delete_scan and "ucomm=" in command)
        ):
            sent_interrupt = True
            kill(os.getpid(), signal.SIGINT)
        if delete and case == "late-child":
            # The pane cannot fork this child until cleanup has taken its last snapshot.
            release.touch()
            deadline = time.monotonic() + 5
            while not late_ready.exists():
                assert time.monotonic() < deadline, "late child did not start"
                time.sleep(0.01)
            late_pid = int(late_ready.read_text())
            live = _live_processes()
            fallback[late_pid, live[late_pid].started] = name
            assert live[late_pid].parent == pane_pid
            assert os.getpgid(late_pid) == os.getpgid(pane_pid)
        result = run(command, **kwargs)
        if delete:
            deleted = result.returncode == 0
            if case == "late-child":
                deadline = time.monotonic() + 5
                while server_pid in (live := _live_processes()):
                    assert time.monotonic() < deadline, "Zellij server did not exit"
                    time.sleep(0.01)
                assert (late_pid, live[late_pid].started) in fallback
        return result

    def previous_interrupt_handler(signum, frame):
        assert signal.getsignal(signum) is previous_interrupt_handler
        raise KeyboardInterrupt("delivered to prior handler")

    if case == "ctrl-c-verify":
        expected = pytest.raises(KeyboardInterrupt, match="delivered to prior handler")
    elif interrupt_cleanup:
        expected = pytest.raises(KeyboardInterrupt)
    elif leave_survivor:
        expected = pytest.raises(AssertionError, match="Zellij cleanup left live processes")
    elif body_error:
        expected = pytest.raises(body_error, match="interrupted test body")
    else:
        expected = nullcontext()

    original_handler = signal.getsignal(signal.SIGINT)
    prior_handler = (
        previous_interrupt_handler if case == "ctrl-c-verify" else signal.default_int_handler
    )
    try:
        with (
            expected as cleanup_error,
            monkeypatch.context() as fault,
            live_zellij_session(monkeypatch) as name,
        ):
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
                    "child = os.fork()\n"
                    "if child:\n"
                    f"    ready = Path({str(ready)!r})\n"
                    "    pending = ready.with_suffix('.tmp')\n"
                    "    pending.write_text(f'{os.getpid()} {child}')\n"
                    "    pending.rename(ready)\n"
                    f"    if {case == 'late-child'}:\n"
                    f"        while not Path({str(release)!r}).exists(): time.sleep(0.01)\n"
                    "        late = os.fork()\n"
                    "        if late:\n"
                    f"            ready = Path({str(late_ready)!r})\n"
                    "            pending = ready.with_suffix('.tmp')\n"
                    "            pending.write_text(str(late))\n"
                    "            pending.rename(ready)\n"
                    "time.sleep(120)\n",
                ],
                check=True,
                capture_output=True,
                timeout=5,
            )
            deadline = time.monotonic() + 10
            while not ready.exists():
                assert time.monotonic() < deadline, "pane did not start"
                time.sleep(0.05)
            pane_pid, child_pid = map(int, ready.read_text().split())
            live = _live_processes()
            # Zellij starts the pane directly; discovery must not supply the expected server PID.
            server_pid = live[pane_pid].parent
            assert server_pid > 1
            fallback = {(pid, live[pid].started): name for pid in (server_pid, pane_pid, child_pid)}
            if case == "debug-server":
                argv = Path(f"/proc/{server_pid}/cmdline").read_bytes().split(b"\0")
                assert b"--debug" in argv[3:]
            owned = _session_processes(zellij, os.environ, {name: set()})
            if interrupt_cleanup or case in {"two-initial-ps-timeouts", "late-child"}:
                fault.setattr(subprocess, "run", run_during_teardown)
            if interrupt_cleanup:
                signal.signal(signal.SIGINT, prior_handler)
            if leave_survivor:
                fault.setattr(os, "kill", kill_except_pane)
            if body_error:
                raise body_error("interrupted test body")
        assert os.environ.get("ZELLIJ_SOCKET_DIR") == previous_socket_dir
        assert not os.path.exists(socket_dir)
        if interrupt_cleanup:
            assert sent_interrupt
            assert signal.getsignal(signal.SIGINT) is prior_handler
        if leave_survivor:
            assert f"{name}: PID {pane_pid} (" in str(cleanup_error.value)
        if case == "two-initial-ps-timeouts":
            assert initial_timeouts == 2 and deleted
        live = _live_processes()
        assert {server_pid, pane_pid, child_pid} <= {pid for pid, _ in owned}
        survivors = {
            pid for pid, started in owned | fallback if pid in live and live[pid].started == started
        }
        assert survivors == ({pane_pid} if leave_survivor else set())
        if case == "late-child":
            assert deleted and len(fallback) == 4
    finally:
        if interrupt_cleanup:
            signal.signal(signal.SIGINT, original_handler)
        if owned or fallback:
            _kill_and_verify_owned_processes(owned | fallback)


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
        "GitHubClient.linked_authored_open_pull_request_context = lambda *_args, **_kwargs: None\n"
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


def dashboard_address_recovery(tmp_path, sockets):
    script, env = _dashboard_startup_environment(tmp_path, sockets)
    original_address = env["WORKDASH_HOST"]
    sessions = []

    def start(name, terminal_env):
        pid, fd = _start_dashboard_terminal(script, terminal_env, "--session", name)
        sessions.append((name, pid, fd))
        return fd

    def info(address):
        return subprocess.run(
            [script, "info", "--host", address], env=env, capture_output=True, text=True, timeout=5
        )

    def wait_for_dashboard(name, address, fd):
        deadline = time.monotonic() + 15
        while True:
            result = info(address)
            if result.returncode == 0 and f"Session: {name}" in result.stdout.splitlines():
                return
            if time.monotonic() >= deadline:
                pytest.fail(f"Dashboard {name} did not start: {result.stdout}{result.stderr}")
            if select.select([fd], [], [], 0.1)[0]:
                os.read(fd, 65536)

    def try_project():
        return subprocess.run(
            [script, "--session", "project"], env=env, capture_output=True, text=True, timeout=10
        )

    def restart_project_on_free_address():
        address = _free_dashboard_address()
        return address, start("project", {**env, "WORKDASH_HOST": address})

    def verify_both_dashboards(project):
        address, fd = project
        wait_for_dashboard("project", address, fd)
        result = info(original_address)
        assert result.returncode == 0, result.stderr
        assert "Session: other" in result.stdout.splitlines()

    try:
        wait_for_dashboard("other", original_address, start("other", env))
        yield original_address, try_project, restart_project_on_free_address, verify_both_dashboards
    finally:
        _cleanup_sessions(shutil.which("zellij"), env, sessions)


def _dashboard_startup_environment(tmp_path, sockets):
    script = tmp_path / "workdash"
    script.write_text(
        f"#!{sys.executable}\n"
        "import workdash.workdash as cli\n"
        "from workdash.config import WorkdashConfig, AgentConfig\n"
        "cli.GitHubClient.check_authentication = lambda self: None\n"
        "cli.load_config = lambda: WorkdashConfig(github_username='test', open_command='xdg-open', "
        "analysis_viewer={'command': 'workdash view', 'format': 'markdown', 'needs_pane': True}, "
        "agents={'agent': AgentConfig(launch='sh')}, repositories=('owner/repo',), "
        "workdir='/tmp', todo_repository='test/todos')\n"
        "cli.WorkdashBackend.load_items = lambda self, progress_callback=None: ([], {})\n"
        "raise SystemExit(cli.main())\n"
    )
    script.chmod(0o755)
    env = os.environ.copy()
    env["WORKDASH_HOST"] = _free_dashboard_address()
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        env.pop(key, None)
    config = tmp_path / "zellij.kdl"
    config.write_text("show_release_notes false\nshow_startup_tips false\n")
    env.update(
        TERM="xterm-256color",
        HOME=_temporary_home(tmp_path),
        SHELL="/bin/sh",
        PATH=f"{tmp_path}:{env['PATH']}",
        ZELLIJ_SOCKET_DIR=sockets,
        ZELLIJ_CONFIG_FILE=str(config),
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
    )
    return script, env


def _free_dashboard_address():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"127.0.0.1:{probe.getsockname()[1]}"


def _start_dashboard_terminal(script, env, *args):
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        os.execve(str(script), [str(script), *args], env)
    os.set_blocking(fd, False)
    return pid, fd


def dashboard_restart(tmp_path, sockets):
    zellij = shutil.which("zellij")
    name = "workdash"
    script, env = _dashboard_startup_environment(tmp_path, sockets)
    clients = []

    def start(*args):
        clients.append(_start_dashboard_terminal(script, env, *args))

    def action(*args, check=True):
        return subprocess.run(
            [zellij, "--session", name, "action", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
            check=check,
        ).stdout

    def pane_response():
        return action("list-panes", "--json", "--all", check=False)

    def terminal_panes(response):
        try:
            panes = json.loads(response)
        except json.JSONDecodeError:
            return []
        return [p for p in panes if not p["is_plugin"]] if isinstance(panes, list) else []

    def wait_for(read, matches, description):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                result = read()
            except subprocess.TimeoutExpired as error:
                result = error
            else:
                if matches(result):
                    return result
            for _, fd in clients:
                if select.select([fd], [], [], 0)[0]:
                    try:
                        os.read(fd, 65536)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
            time.sleep(0.1)
        pytest.fail(f"{name}: {description}; last result: {result!r}")

    def screen(pane):
        return action("dump-screen", "--pane-id", pane, check=False)

    def type_in(pane, text, *, enter=False):
        action("write-chars", "--pane-id", pane, text)
        if enter:
            action("send-keys", "--pane-id", pane, "Enter")

    def dashboard_pane(option):
        response = wait_for(
            pane_response,
            lambda text: any(option in str(p.get("pane_command")) for p in terminal_panes(text)),
            f"dashboard with {option} did not start",
        )
        panes = terminal_panes(response)
        return next(p for p in panes if option in str(p.get("pane_command"))), panes

    def filter_items(pane):
        wait_for(
            lambda: screen(pane), lambda text: "Loaded 0 item(s)." in text, "dashboard not ready"
        )
        type_in(pane, "/")
        wait_for(
            lambda: screen(pane),
            lambda text: "Filter items by type" in text,
            "dashboard did not open the filter",
        )
        action("send-keys", "--pane-id", pane, "Enter")
        wait_for(
            lambda: screen(pane),
            lambda text: "Filter items by type" not in text,
            "dashboard did not close the filter",
        )

    def launch_dashboard():
        start()
        dashboard, panes = dashboard_pane("--persistent-pane")
        shell = next(p for p in panes if p["tab_name"] == dashboard["tab_name"] and p != dashboard)
        return f"terminal_{dashboard['id']}", f"terminal_{shell['id']}"

    def verify_dashboard_and_shell(panes):
        dashboard, shell = panes
        filter_items(dashboard)
        type_in(shell, "draft=work-in-progress; echo SHELL_READY", enter=True)
        wait_for(
            lambda: screen(shell),
            lambda text: "SHELL_READY" in text.splitlines(),
            "sibling shell did not accept work",
        )

    def refresh_from_shell(panes):
        _, shell = panes
        type_in(shell, "workdash --refresh", enter=True)

    def verify_original_dashboard(panes):
        dashboard, shell = panes
        wait_for(
            lambda: screen(shell),
            lambda text: "new options are ignored" in text,
            "sibling invocation did not report the running dashboard",
        )
        response = wait_for(pane_response, terminal_panes, "could not list terminal panes")
        assert {f"terminal_{p['id']}" for p in terminal_panes(response)} == set(panes)
        filter_items(dashboard)

    def reopen_dashboard(panes):
        dashboard, shell = panes
        type_in(dashboard, "q")
        wait_for(
            lambda: screen(dashboard),
            lambda text: "Ctrl+Q" in text,
            "dashboard did not quit with session-exit guidance",
        )
        start("--debug")
        reopened, panes = dashboard_pane("--debug")
        assert reopened["tab_name"] == "dashboard"
        old = next(p for p in panes if f"terminal_{p['id']}" == dashboard)
        assert old["title"] == "workdash tui" and old["pane_command"] == "/bin/sh -i"
        remaining = f"terminal_{reopened['id']}", shell
        type_in(dashboard, "exit 23", enter=True)
        wait_for(
            pane_response,
            lambda text: {f"terminal_{p['id']}" for p in terminal_panes(text)} == set(remaining),
            "exit 23 did not close only the original dashboard shell",
        )
        return remaining

    def verify_reopened_dashboard_and_shell(panes):
        dashboard, shell = panes
        filter_items(dashboard)
        type_in(shell, 'printf "DRAFT:%s\\n" "$draft"', enter=True)
        wait_for(
            lambda: screen(shell),
            lambda text: "DRAFT:work-in-progress" in text.splitlines(),
            "sibling shell lost its draft",
        )

    try:
        yield {
            "launch_dashboard": launch_dashboard,
            "verify_dashboard_and_shell": verify_dashboard_and_shell,
            "refresh_from_shell": refresh_from_shell,
            "verify_original_dashboard": verify_original_dashboard,
            "reopen_dashboard": reopen_dashboard,
            "verify_reopened_dashboard_and_shell": verify_reopened_dashboard_and_shell,
        }
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
                        pane_title = f"analyse_{tmp_path.name}"
                        panes = wait_for(
                            lambda panes: any(p.get("title") == pane_title for p in panes)
                        )
                        pane = next(p for p in panes if p.get("title") == pane_title)
                        assert pane["tab_name"] == "repo"
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
            previously_owned.update(
                _session_processes(zellij, env, {name: {pid for pid, _ in clients}})
            )
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


@contextmanager
def running_dashboard_return(
    tmp_path, *, has_pane=True, duplicate_tab=False, new_last_input=False, last_detach=False
):
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
        WORKDASH_HOST=_free_dashboard_address(),
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
    output = bytearray()
    returning_output = bytearray()
    stop_draining = threading.Event()
    drain_threads = []

    def drain(fd, capture):
        while not stop_draining.is_set():
            try:
                if select.select([fd], [], [], 0.1)[0]:
                    capture.extend(os.read(fd, 65536))
            except OSError:
                return

    def start_terminal(capture, executable, *args):
        pid, fd = _start_dashboard_terminal(executable, env, *args)
        thread = threading.Thread(target=drain, args=(fd, capture), daemon=True)
        thread.start()
        drain_threads.append(thread)
        return pid, fd

    def action(*args, check=True):
        return subprocess.run(
            [zellij, "--session", name, "action", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
            check=check,
        )

    def wait_for(predicate):
        deadline = time.monotonic() + 15
        result = None
        while time.monotonic() < deadline:
            try:
                result = action("list-panes", "--json", "--all", "--tab", check=False)
                if result.returncode == 0:
                    panes = json.loads(result.stdout)
                    if isinstance(panes, list) and predicate(panes):
                        return panes
            except subprocess.TimeoutExpired as error:
                result = error
            except json.JSONDecodeError:
                pass
            time.sleep(0.1)
        process = subprocess.run(
            ["ps", "-o", "pid,ppid,state,wchan,args", "-p", str(sessions[0][1])],
            capture_output=True,
            text=True,
        ).stdout
        pytest.fail(
            f"Zellij state not reached; last pane response: {result!r}; PTY: {output[-3000:]!r}; {process}"
        )

    def wait_for_screen(pane_id, text, *, seconds=10, interval=0.1, whole_line=False):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            screen = action("dump-screen", "--pane-id", pane_id).stdout
            if text in (screen.splitlines() if whole_line else screen):
                return
            time.sleep(interval)
        pytest.fail(f"{pane_id} did not show {text!r}:\n{screen}")

    def shell_command(command):
        action("write-chars", "--pane-id", work_pane, command)
        action("send-keys", "--pane-id", work_pane, "Enter")

    def leave_draft():
        shell_command("draft=work-in-progress; echo ready")
        wait_for_screen(work_pane, "ready", seconds=5, whole_line=True)

    def detach():
        action("detach")
        deadline = time.monotonic() + 5
        while action("list-clients").stdout.splitlines()[1:]:
            assert time.monotonic() < deadline, "last client did not detach"
            time.sleep(0.01)

    def attach():
        nonlocal returning_fd, returning_client
        args = ["-m", "workdash", "--session", name]
        if new_last_input:
            args.append("--refresh")
        child, returning_fd = start_terminal(returning_output, sys.executable, *args)
        sessions.append((name, child, returning_fd))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            clients = action("list-clients").stdout.splitlines()[1:]
            new_clients = [
                line.split() for line in clients if last_detach or line.split()[0] != client_id
            ]
            if len(new_clients) == 1 and (last_detach or new_clients[0][1] == work_pane):
                returning_client = new_clients[0][0]
                return returning_output
            time.sleep(0.01)
        pytest.fail(f"new client did not attach: {clients}; CLI PTY: {returning_output[-3000:]!r}")

    def send_input_before_focus():
        deadline = time.monotonic() + 5
        while not (tmp_path / "focus-pending").exists():
            assert time.monotonic() < deadline, "focus action was not issued"
            time.sleep(0.01)
        clients = action("list-clients").stdout.splitlines()[1:]
        assert any(line.split()[:2] == [client_id, work_pane] for line in clients)
        os.write(returning_fd, b"printf 'NEW_CLIENT_%s\\n' INPUT_PROOF\n")
        wait_for_screen(work_pane, "NEW_CLIENT_INPUT_PROOF", seconds=5, interval=0.01)
        (tmp_path / "focus-release").touch()

    def wait_for_client_on_pane(pane_id, *, client=None):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = action("list-clients", check=False)
            if result.returncode == 0:
                for row in result.stdout.splitlines()[1:]:
                    fields = row.split()
                    if fields[1:2] == [pane_id] and (client is None or fields[0] == client):
                        return fields[0]
            time.sleep(0.1)
        pytest.fail(
            f"client {client or '(original)'} did not focus {pane_id}; last response: {result!r}"
        )

    def press_filter():
        wait_for_client_on_pane(dashboard_pane, client=returning_client)
        os.write(returning_fd, b"/")

    def verify_original_dashboard():
        wait_for_screen(dashboard_pane, "Filter items by type")
        live_panes = wait_for(bool)
        assert [p["id"] for p in live_panes if p["title"] == "workdash tui"] == [pane["id"]]

    def verify_shell_draft():
        shell_command('printf "DRAFT:%s\\n" "$draft"')
        wait_for_screen(work_pane, "DRAFT:work-in-progress", seconds=5, whole_line=True)

    def verify_focus_and_input():
        focused_pane = dashboard_pane if has_pane else work_pane
        wait_for_client_on_pane(focused_pane, client=client_id)
        if has_pane:
            os.write(fd, b"/")
            wait_for_screen(focused_pane, "Filter items by type")
            os.write(fd, b"FOCUSED_INPUT_PROOF")
        else:
            os.write(returning_fd, b"printf 'FOCUSED_%s\\n' INPUT_PROOF\n")
        wait_for_screen(focused_pane, "FOCUSED_INPUT_PROOF")
        if has_pane:
            clients = action("list-clients").stdout.splitlines()[1:]
            assert any(line.split()[:2] == [returning_client, work_pane] for line in clients)
            os.write(returning_fd, b"printf 'NEW_CLIENT_%s\\n' INPUT_PROOF\n")
            wait_for_screen(work_pane, "NEW_CLIENT_INPUT_PROOF")

    returning_fd = returning_client = None
    try:
        pid, fd = start_terminal(output, zellij, "--session", name)
        sessions.append((name, pid, fd))
        panes = wait_for(lambda ps: any(not p["is_plugin"] for p in ps))
        if duplicate_tab:
            action("rename-tab", "renamed")
            action("new-tab", "--name", "dashboard")
            panes = wait_for(lambda ps: any(not p["is_plugin"] and p["id"] == 1 for p in ps))
        record = tmp_path / "home" / ".config" / "workdash" / "sessions" / f"{name}.json"
        deadline = time.monotonic() + 20
        while not record.exists():
            assert time.monotonic() < deadline, f"dashboard did not start: {output[-4000:]!r}"
            time.sleep(0.1)
        panes = wait_for(lambda ps: any(p["title"] == "workdash tui" for p in ps))
        pane = next(p for p in panes if not p["is_plugin"] and p["title"] == "workdash tui")
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
        work_pane = f"terminal_{other['id']}"
        dashboard_pane = f"terminal_{pane['id']}"
        client_id = wait_for_client_on_pane(work_pane)
        os.write(fd, b"\n")
        yield {
            "leave_draft": leave_draft,
            "detach": detach,
            "attach": attach,
            "send_input_before_focus": send_input_before_focus,
            "press_filter": press_filter,
            "verify_original_dashboard": verify_original_dashboard,
            "verify_shell_draft": verify_shell_draft,
            "verify_focus_and_input": verify_focus_and_input,
        }
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
@pytest.mark.parametrize(
    "has_pane,duplicate_tab",
    [(False, False), (True, False), (True, True)],
)
def test_attach_running_dashboard_focuses_its_pane(tmp_path, attempt, has_pane, duplicate_tab):
    with running_dashboard_return(
        tmp_path, has_pane=has_pane, duplicate_tab=duplicate_tab
    ) as dashboard:
        dashboard["attach"]()
        dashboard["verify_focus_and_input"]()


@pytest.mark.skipif(shutil.which("zellij") is None, reason="Zellij is not installed")
@pytest.mark.parametrize("attempt", range(2))
def test_attach_after_last_client_detaches_from_other_tab(tmp_path, attempt):
    with running_dashboard_return(tmp_path, last_detach=True) as dashboard:
        dashboard["leave_draft"]()
        dashboard["detach"]()
        dashboard["attach"]()
        dashboard["press_filter"]()
        dashboard["verify_original_dashboard"]()
        dashboard["verify_shell_draft"]()
