# Notes for agents working on this repository

Before starting any work, read these files in order:

1. **README.md** – Project overview, installation, and usage.
2. **CONTRIBUTING.md** – Core rules, especially BDD-first workflow and the distinction between user-facing BDD scenarios and implementation-detail tests.
3. **DESIGN.md** – Design direction; all solutions must adhere to it.

## Repository and worktree assumptions

- Use the repository's current name. Users only interact with the new name after a rename; do not add rename-specific handling.
- Assume Workdash operates only in worktrees it creates and names. Do not add support for hand-made or manually renamed worktrees.

## Issue and pull request assumptions

- Coding an authored PR that closes an issue is the same as coding the issue. A PR with no linked issue uses its own worktree. If an issue is later linked, plan-first coding uses the issue worktree and leaves the old PR worktree alone.
- For plan-first coding, if the issue has no worktree, mention only the most recently created open PR you authored that closes it in the briefing. The issue chooses the checkout.

## Zellij session assumptions

- Workdash runs inside a Zellij session. Its actions operate only in that same session; cross-session capabilities are out of scope.
- When Workdash quits, it no longer manages any remaining panes. On a later run, Workdash can discover panes it created in the current session and should generally reuse them rather than create duplicates.

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

- Feature files describe only the user experience and externally observable behavior, never how the software works internally.
- Implementation details, corner cases, and internal mechanisms go in normal unit/integration tests.
- Keep changes minimal and focused.
- For rare or obscure cases outside the expected flow, fail gracefully with a clear error instead of adding complex recovery heuristics.
- If GitHub can't provide issue or PR data, treat it as unavailable rather than adding complex heuristics to reconstruct it.
- Match existing style; see `pyproject.toml` for lint/format config and run `ruff check` and `ruff format`.
