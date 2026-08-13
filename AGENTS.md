# Notes for agents working on this repository

Before starting any work, read these files in order:

1. **README.md** – Project overview, installation, and usage.
2. **CONTRIBUTING.md** – Core rules, especially BDD-first workflow and the distinction between user-facing BDD scenarios and implementation-detail tests.

## Verifying visual/TUI behavior

To reproduce or verify a rendering bug, drive a real running app inside a real
zellij session instead of guessing. Zellij can be scripted end to end:

- `zellij -s NAME action new-pane --cwd DIR -- CMD` – start the app in a pane.
- `zellij -s NAME action toggle-fullscreen` / `resize` – change the pane size.
- `zellij -s NAME action write-chars k` – send keystrokes to the app.
- `zellij -s NAME action dump-screen FILE` – dump what is actually on screen.

`dump-screen` gives the terminal contents as text, so a rendering bug becomes an
assertion (compare the dumped rows against what should be there) instead of a
screenshot. Run the session itself under a PTY (`pty.fork()` + `TIOCSWINSZ`) to
control its size headlessly.

When using this, check the harness varies what it claims to vary (e.g. that the
child process really receives the environment variable being tested), and run
each variant more than once before concluding.

## Quick reminders

- BDD scenarios describe user-visible, API-visible, or externally observable behavior only.
- Implementation details, corner cases, and internal mechanisms go in normal unit/integration tests.
- Keep changes minimal and focused.
- Match existing style; see `pyproject.toml` for lint/format config and run `ruff check` and `ruff format`.
