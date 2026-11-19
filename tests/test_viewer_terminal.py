import os
import pty
import subprocess
import sys


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
view.show_document = lambda document: print(document.content)
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


def test_quitting_during_a_load_returns_to_the_shell(tmp_path):
    """`q` while a document loads must not wait for the read to finish."""
    script = tmp_path / "quit_during_load.py"
    script.write_text(
        """import asyncio
import threading

from workdash import view


def read(target):
    threading.Event().wait()


view.load_document = read
view._MarkdownApp._set_status = lambda self, message: None


async def main():
    app = view._MarkdownApp(view.Document("# First", "file:///first.md"), "xdg-open", False)
    loading = asyncio.create_task(app._load("file:///second.md"))
    await asyncio.sleep(0.2)
    loading.cancel()
    await asyncio.wait([loading])


asyncio.run(main())
print("quit")
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, timeout=15
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("quit")
