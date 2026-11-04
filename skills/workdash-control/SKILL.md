---
name: workdash-control
description: Coordinate Workdash-launched agents using only workdash subcommands against a running local Workdash server. Use when orchestrating panes, agents, analysis, and coding sessions through Workdash CLI control.
---

# Workdash Control

Use this skill when you may run `workdash` subcommands but should not inspect
Zellij, GitHub, config files, or worktrees directly.

## Required server

Depends on an existing `workdash --server` session.
Server-backed commands fail clearly if it is not running.
You MUST NEVER start a server yourself, if no server is running just tell so.
To know the configuration of the server, you can use `show-config` subcommand

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

- Item IDs from `workdash list`: `owner/repo#ISSUE-123`, `owner/repo#PR-123`, `owner/repo#REVIEW-123`, `owner/repo#CHECK-123`, and `owner/repo#ISSUE-WT123` for a todo targeting `owner/repo`.
- Pane IDs from `workdash info`: usually `terminal_N`.

`workdash list` is only the dashboard view, capped and filtered. Item commands
(`analyze`, `code`, `terminal`) also accept any issue or pull request the server
can access on GitHub, even one `list` does not show: a Workdash item ID,
`owner/repo#123` for an issue, or a GitHub issue or pull request URL. Using one
does not add it to `list`. Use a PR ID or URL for a pull request: `owner/repo#123`
naming a pull request fails with `unsupported_item_type`.

## Find ready work

`workdash ready SCOPE` answers "what can be worked on next?" straight from
GitHub, independent of what `workdash list` shows. It is read-only advice: it
claims nothing and launches nothing. Use it to pick the next target for
`workdash code`; use `workdash list` for dashboard triage instead.

```bash
workdash ready owner/repo              # open issues and pull requests of one repository
workdash ready owner/repo#42           # an issue and its whole sub-issue tree
workdash ready todo                    # the server's todo repository
workdash ready any                     # every configured repository plus the todo repository
workdash ready any --json
workdash ready owner/repo#ISSUE-42 --host dashboard.lan --json
```

`ready ISSUE` (`owner/repo#42`, `owner/repo#ISSUE-42`, a targeted todo ID, or an
issue URL) previews every actionable native sub-issue descendant, recursively and
across repositories, or the issue itself when it has no sub-issues. An issue
with sub-issues is never listed itself. A pull request ID or URL is not a scope.

Results come in priority order, so the first one is the next to pick:

- Pull requests needing work come first. `action` is `address_review`,
  `fix_merge_conflicts`, or `fix_ci`, and `reasons` lists every one of them. The
  pull request is the target: `workdash code` its `id` to resume its existing
  branch; `issue_id` names the issue it implements. `--json` adds `ci_status`,
  `failing_checks`, `mergeability`, `pr_head_sha`, `base_ref`, `base_head_sha`,
  and `feedback` references.
- New leaf issues follow, with action `implement`.
- Left out: blocked issues, issues with sub-issues or an open pull request, work
  a live code pane already holds, and pull requests merely awaiting CI, review,
  or merge.
- `No ready work found.` (an empty `items` list) means nothing is ready, not a failure.

Only the server looks at GitHub and Zellij, so `ready` needs it running like
every other command; never start a server just to run `ready`.

## Capture todos

```bash
workdash todo TEXT                     # capture a todo in the todo repository
workdash todo TEXT --target owner/repo # capture a todo for another repository
workdash todo "Plan the migration" --body "Long research findings and next steps..."
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

Todo `--target` accepts a GitHub `owner/repo` or a git URL on any hosting service,
for example `https://codeberg.org/owner/repo.git`. HTTPS (with or without `.git`),
`ssh://`, `git://`, and scp-like `git@host:owner/repo.git` targets are supported.
Code, terminal, and fresh analysis actions prepare the target clone and worktree
automatically. Todos and their discussion still live in the GitHub todo repository.
URL targets retain their original URL in metadata and API responses, but display
and item IDs use the final `owner/repo` path segments. Same-name targets can share
an ID; a conflicting local directory is reported without changing existing work.

Keep the title short and use `--body BODY` for long or multiline details instead
of editing the issue afterwards. The issue body always starts with Workdash's
metadata JSON block, followed by the user body with its formatting preserved.
Without `--body`, the metadata-only body is unchanged. The JSON API accepts the
same optional content as `body` on `POST /api/v0/todo`.

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
prints that path as `analysis_path`. `terminal` opens a plain terminal pane in the
item's worktree and prints the pane title and pane ID.

## Code is an exclusive claim

Use `workdash code ITEM` when you already know the issue or pull request to
work on, even if `workdash list` does not show it:

```bash
workdash code owner/repo#123 --agent pi --json
workdash code owner/repo#ISSUE-WT123 --json
workdash code https://github.com/owner/repo/pull/45 --agent pi --host dashboard.lan --json
```

At most one coding agent works in a worktree. The live Workdash `code_` pane
launched for that worktree, by its title, is the claim: there is no `workdash claim` command and no claim
file or database. Closing or exiting that pane releases the claim; it does not
mean the work is finished. Plain `terminal_` panes never hold a claim.

Check `status` in the result:

- `launched`: this call started the agent. Its `pane_id` is yours to drive.
- `already_running`: an agent was already working in that worktree, so nothing
  was started and `pane_id` is that existing pane. Reuse it to read or follow
  the work instead of starting another; it may belong to another orchestrator,
  so do not take it over. `agent` is null because the running agent is unknown.

`launch_unconfirmed` means a launch may have started an agent whose pane is not
visible yet: check `workdash info` instead of retrying in a loop. `agent_exited`
means the agent quit right after launch (e.g. a missing binary): read the pane it
names, fix the cause, then retry. If Zellij cannot report its panes, `code`
starts nothing and fails. VSCode Copilot is only offered by the TUI and holds no
claim.

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
2. `workdash ready SCOPE` (or `workdash list`) and choose one copy/paste item ID,
   or use an item you already know.
3. `workdash analyze ITEM --json` for context, then read `analysis_path` if needed.
4. `workdash code ITEM --agent pi --json` to launch a worker.
5. Copy `pane_id` from `code` or `workdash info --json`. On `already_running`,
   another agent already owns that item: follow its pane or pick other work.
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
