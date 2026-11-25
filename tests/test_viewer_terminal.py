import os
import pty
import subprocess
import sys


def test_item_viewer_pane_keeps_lookup_error_until_dismissed(monkeypatch, tmp_path):
    import select

    from workdash import launcher

    executable = tmp_path / "workdash"
    executable.write_text(
        '#!/bin/sh\nif [ "$2" = "owner/repo#1" ]; then echo "lookup failed" >&2; exit 1; fi\n'
    )
    executable.chmod(0o755)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: str(executable))
    launched = []
    monkeypatch.setattr(
        launcher.zellij,
        "open_viewer",
        lambda session, command, *, executable: launched.append(command),
    )

    for target in ("owner/repo#1", "owner/repo#2"):
        launcher.open_item_document(target, zellij_session="test")
        command = launched[-1]
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

from textual.widgets import Markdown, Static

from workdash import view


def read(target):
    threading.Event().wait()


view.load_document = read


async def main():
    document = view.Document("# First\\n\\n[Next](second.md)\\n", "file:///first.md")
    app = view._MarkdownApp(document, "xdg-open", False)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        asyncio.create_task(pilot.click(Markdown, offset=(2, 4)))
        while not app.query_one("#status", Static).render().plain.startswith("Loading "):
            await pilot.pause()
        await pilot.press("q")
        assert not app.is_running, "q did not quit the viewer"


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
