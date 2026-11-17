import os
import pty
import subprocess
import sys


def test_viewer_restores_terminal_before_replacing_process(tmp_path):
    result = tmp_path / "terminal"
    script = """
import os, termios
from pathlib import Path
from workdash.markdownview import _MarkdownApp
before = termios.tcgetattr(0)
def replace(file, args):
    Path(os.environ["RESULT"]).write_text(str(termios.tcgetattr(0) == before))
    os._exit(0)
os.execvp = replace
app = _MarkdownApp("# Document", "xdg-open", False)
app.call_after_refresh(lambda: app.open_url("https://github.com/owner/repo/issues/12"))
app.run()
"""
    master, slave = pty.openpty()
    env = {**os.environ, "RESULT": str(result), "TERM": "xterm-256color"}
    env.pop("ZELLIJ_SESSION_NAME", None)
    try:
        subprocess.run(
            [sys.executable, "-c", script],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            timeout=10,
            check=True,
        )
        assert result.read_text() == "True"
    finally:
        os.close(master)
        os.close(slave)


def test_item_viewer_pane_keeps_lookup_error_until_dismissed(monkeypatch, tmp_path):
    import select

    from workdash import launcher

    executable = tmp_path / "workdash"
    executable.write_text(f"""#!{sys.executable}
import json, subprocess, sys
from workdash import view

def gh(command, **kwargs):
    if sys.argv[2] == "owner/repo#1":
        raise subprocess.CalledProcessError(1, command, stderr="lookup failed")
    data = [] if "?per_page=" in command[2] else {{
        "title": "Issue", "state": "open", "html_url": "https://github.com/owner/repo/issues/2",
        "updated_at": "2026-01-01T00:00:00Z",
    }}
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(data))

view.subprocess.run = gh
view.show_markdown = lambda document: print(document)
sys.exit(view.run_view(sys.argv[2]))
""")
    executable.chmod(0o755)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(executable))
    launched = []
    monkeypatch.setattr(
        launcher.zellij,
        "open_viewer",
        lambda session, command, **kwargs: launched.append((command, kwargs)),
    )

    for target in ("owner/repo#1", "owner/repo#2"):
        launcher.open_item_document(target, zellij_session="test")
        command, options = launched[-1]
        assert options["close_on_exit"] is True
        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave)
            if target == "owner/repo#1":
                output = b""
                while b"Press Enter" not in output:
                    assert select.select([master], [], [], 5)[0]
                    output += os.read(master, 4096)
                assert b"lookup failed" in output
                assert process.poll() is None
                os.write(master, b"\n")
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)
            os.close(slave)
