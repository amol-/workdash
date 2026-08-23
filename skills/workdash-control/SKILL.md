---
name: workdash-control
description: Coordinate Workdash-launched agents using only workdash subcommands against a running local Workdash server. Use when orchestrating panes, agents, analysis, and coding sessions through Workdash CLI control.
---

# Workdash Control

Use this skill when you may run `workdash` subcommands but should not inspect
Zellij, GitHub, config files, or worktrees directly.

## Required server

Start from an existing `workdash --server` session. Server-backed commands fail
clearly if it is not running.

```bash
workdash show-config          # configured agents + fixed server address
workdash show-config --json
```

## Discover work and panes

```bash
workdash list                 # current item rows
workdash list --refresh       # ask server to refresh GitHub data
workdash list --json

workdash info                 # Workdash-owned live panes
workdash info --all           # include ordinary live non-plugin panes
workdash info --json
```

Copy IDs exactly:

- Item IDs from `workdash list`: `owner/repo#ISSUE-123`, `owner/repo#PR-123`, `owner/repo#REVIEW-123`, `owner/repo#CHECK-123`.
- Pane IDs from `workdash info`: usually `terminal_N`.

## Capture todos

```bash
workdash todo TEXT                     # capture a todo in the todo repository
workdash todo TEXT --target owner/repo # capture a todo for another repository
workdash todo TEXT --json              # machine-readable output
```

Use `workdash todo` to create **private task tracking items** that are not part of
any specific repository or that should not be public. A todo is stored as a
GitHub issue in your configured todo repository (assigned to you, labeled
`workdash-todo`), so it appears in your dashboard like any other work item.

Use `--target owner/repo` when you need to create a task for another repository but
want to keep the task itself non-public (the todo issue lives in your todo
repository, not the target). The target is recorded in the issue body metadata
and the todo appears in the dashboard under its target repository.

`todo` prints the created Workdash item ID, todo repository, target, issue
number, and issue URL.

## Start useful work

```bash
workdash analyze ITEM --agent codex
workdash analyze ITEM --agent codex --json

workdash code ITEM --agent pi
workdash code ITEM --agent codex --json

workdash terminal ITEM
workdash terminal ITEM --json
```

`analyze` receives markdown content from the server as base64 (`content_type`,
`file_name`, `file_content`), writes it to a secure temporary local file, and
prints that path as `analysis_path`. `code` prints the new pane title and pane ID.
`terminal` opens a plain terminal pane in the item's worktree and prints the
pane title and pane ID.

## Talk to panes

```bash
workdash read PANE_ID                 # visible pane text
workdash read PANE_ID --full          # full scrollback
workdash read PANE_ID --json

workdash write PANE_ID "continue"     # sends text plus Enter
workdash write PANE_ID "question" --raw
workdash write PANE_ID "question" --no-enter --json

workdash close PANE_ID            # close a pane
workdash close PANE_ID --json
```

Human `read` output is just the pane text so it can be read directly.

## Typical orchestration loop

1. `workdash show-config` to know usable agents.
2. `workdash list` and choose one copy/paste item ID.
3. `workdash analyze ITEM --json` for context, then read `analysis_path` if needed.
4. `workdash code ITEM --agent pi --json` to launch a worker.
5. Copy `pane_id` from `code` or `workdash info --json`.
6. Poll with `workdash read PANE_ID --full`.
7. Send concise instructions with `workdash write PANE_ID "..."`.
8. Repeat `read`/`write` until the pane reports completion.
9. Close finished panes with `workdash close PANE_ID` when done.

## Safety notes

- Treat pane IDs and item IDs as opaque; copy/paste them exactly.
- Prefer `--json` when another program or agent parses output.
- Do not run shell commands in panes unless the task explicitly requires it.
- Use `--raw`/`--no-enter` only for partial input; default Enter is safer for normal messages.
- Do not assume a pane title maps to an item; use `workdash info` item mappings.
