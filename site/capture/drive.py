"""Record a real Zellij client connected to the isolated dashboard."""

import codecs
import contextlib
import fcntl
import json
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import termios
import time
import uuid

QUIET_PERIOD = 0.4
POLL_INTERVAL = 0.1
READINESS_TIMEOUT = 0.1


def capture(env, size, steps, output):
    name = "wds-" + uuid.uuid4().hex[:8]
    pid, fd = pty.fork()
    if pid == 0:
        fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", size[1], size[0], 0, 0))
        os.execvpe("zellij", ["zellij", "--session", name], env)
    start = time.monotonic()
    events = []
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        for action, value in steps:
            if action == "keys":
                os.write(fd, value.encode())
                continue
            marker, duration = value if action == "until" else (None, value)
            deadline = time.monotonic() + duration
            last = time.monotonic()
            tail = ""
            # A hold watches a running interaction, so it never ends early.
            while time.monotonic() < deadline:
                ready, _, _ = select.select(
                    [fd], [], [], min(POLL_INTERVAL, max(0, deadline - time.monotonic()))
                )
                if ready:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        break
                    if not data:
                        raise RuntimeError(
                            f"Zellij exited before the capture finished: {events[-1][2]!r}"
                        )
                    last = time.monotonic()
                    if text := decoder.decode(data):
                        events.append([round(last - start, 3), "o", text])
                        if marker:
                            tail += text
                            if marker in tail:
                                break
                            tail = tail[-len(marker) :]
                elif (
                    action == "wait"
                    and time.monotonic() - last >= QUIET_PERIOD
                    and _dashboard_ready(env)
                ):
                    break
            if marker and marker not in tail:
                raise RuntimeError(f"Timed out waiting for {marker!r} in Zellij")
        output.write_text(
            json.dumps({"version": 2, "width": size[0], "height": size[1], "timestamp": 0})
            + "\n"
            + "".join(json.dumps(event) + "\n" for event in events)
        )
    finally:
        try:
            subprocess.run(
                ["zellij", "delete-session", "--force", name],
                env=env,
                capture_output=True,
                timeout=10,
            )
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            os.close(fd)


def _dashboard_ready(env):
    # Zellij can be quiet while Workdash is still starting.
    try:
        with socket.create_connection(
            ("127.0.0.1", int(env["WORKDASH_HOST"].split(":")[-1])), timeout=READINESS_TIMEOUT
        ):
            return True
    except OSError:
        return False
