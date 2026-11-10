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

## Autodispatch sessions

```bash
workdash autodispatch isocelot/substack-drafter --group substack --agent pi
workdash info --json                  # sessions, current task, elapsed and status
workdash autodispatch-stop SESSION_ID # does not close the agent pane
```

The server dispatches one task at a time and waits for its pane to disappear.
The group is required. Multiple independent sessions can run concurrently; the
non-modal TUI header shows running sessions. A session ends when no work is
ready, or fails if dispatch or pane lookup fails. It does not merge PRs. Session
state disappears when the server stops; after restart, relaunch on the same
scope to check readiness again.

## Find ready work

`workdash ready SCOPE` previews what can be worked on next, straight from
GitHub rather than the dashboard. It claims and launches nothing. Use the same
scope with `dispatch` to select and supervise one of these tasks.

```bash
workdash ready owner/repo
workdash ready owner/repo#42
workdash ready todo
workdash ready any --json
workdash ready owner/repo#ISSUE-42 --host dashboard.lan --json
```

Scopes include a repository, `todo`, `any`, or an issue ID, targeted todo ID, or
issue URL. Issue scopes include the issue and its native descendants recursively
across repositories; even a closed parent can scope open descendants. An open
parent is eligible once it has no open descendants, so its overall goal can be
verified. Pull requests are never ready items.

Results are issue IDs in priority order: issues with actionable authored pull
requests first, then unstarted issues, oldest first within each group. The linked
pull request is context. Its reasons are ordered `address_review`,
`fix_merge_conflicts`, `fix_ci`; all applicable reasons are reported. An issue
with somebody else's open pull request, or a pull request only waiting for CI,
review, or merge, is not ready. Open blockers and live code panes also exclude
work. `--json` includes CI, mergeability, head and base SHAs, and feedback.
`No ready work found.` means an empty result; GitHub or Zellij failures are errors.

## Dispatch one ready issue

Use `dispatch` when Workdash should choose the next task and supervise its
agent; use `code ITEM` for an explicitly chosen task with its pane left open.
`ready` previews the same ordered candidates without launching anything.

```bash
workdash dispatch owner/repo
workdash dispatch owner/repo#42 --agent pi --json
workdash dispatch owner/repo#42 --group project --host dashboard.lan --json
```

Dispatch selects the first still-eligible issue, rechecking readiness and
skipping claims by other agents. It launches at most one autonomous agent and
returns immediately; it does not wait for completion, create sub-issues, merge,
or run a scheduler. An actionable authored pull request is resumed on its existing branch,
with all reasons in the briefing. An open parent with no open descendants can
be dispatched to verify its goal. By default the Zellij group is the basename
of the implementation repository, not the parent issue's repository; `--group`
overrides it.

`status=launched` includes the issue, action, reasons, and actual `pane_id`.
The server owns supervision and automatic pane closure after the client exits;
supervision stops if the server restarts and is heuristic. `status=no_ready_task`
is a successful empty or exhausted scope; GitHub or Zellij failures and
unconfirmed launches are errors, not empty results. Remote `--host` commands
still require an existing server: never start one yourself.

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

## Code starts autonomous work

`workdash code ITEM` launches an autonomous but still interactive agent on an
exact issue or pull request: it implements the work, commits, pushes, and opens
a pull request linked to the issue (or updates the existing one), then checks
the CI of the pull request's current head. It never merges a pull request or
closes an issue, and its Zellij pane stays open at the agent's normal prompt
when it reports back. There is no `--autostart` option: the CLI always asks for
it. The TUI's `c` key does not; it keeps the human-directed, plan-first briefing
and never commits, pushes, or opens a pull request on its own.

Use `code ITEM` when you already know the exact task. Use `workdash ready` first
to see what is actionable; `code` itself does not check readiness.

```bash
workdash code owner/repo#42 --agent pi --json
workdash code owner/repo#PR-51 --agent pi --json            # your PR with merge conflicts, failing CI or review feedback
workdash code owner/repo#42 --agent pi --host dashboard.lan --json
```

The agent is briefed from GitHub's current state of the work, the same
classification `ready` reports:

- An issue without an open pull request is implemented, and a pull request
  closing it is created.
- An issue that an open pull request of yours already closes resumes that pull
  request in place, keeping the issue link; no second pull request is created. A
  pull request of somebody else closing the issue does not change the task.
- A pull request needing work is repaired in place on its existing branch,
  whatever the reason: review feedback, merge conflicts, or failing CI. Every
  applicable reason is given to the agent, review feedback first.
- A pull request waiting for CI, review, or merge is described as such; the
  agent invents no work and opens no new pull request.
- A pull request of somebody else stays a review: the agent starts reviewing
  without asking for confirmation and never edits, commits, or pushes.

An authored pull request linked to an issue is normalized to that issue for
implementation, using its cached issue analysis when available and continuing
the existing branch. An authored pull request without a linked issue remains the
target. A review of somebody else's pull request uses cached pull request
analysis instead; linked issues are background, not implementation tasks.

Successful work leaves the pull request open for a human to review and merge.
The agent may report completion only when the current head's CI is `passing` or
GitHub reports no required or observed checks (`no_checks`) and the pull request
merges cleanly. `no_checks` is a legitimate outcome reported as "no checks
required or reported", never as CI passed; it differs from configured CI whose
status cannot be read (`unknown`), after which the agent reports the work as
incomplete. If GitHub cannot report the work state, `code` fails with
`github_error` instead of guessing.

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
  was started and `pane_id` is that existing pane. Nothing is injected into it:
  its prompt and mode stay as they were, whether it was started by the TUI or the
  CLI. Reuse it to read or follow the work instead of starting another; it may
  belong to another orchestrator, so do not take it over. `agent` is null because
  the running agent is unknown.

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
5. Copy `pane_id` from `code` or `workdash info --json`. The agent works on its
   own; read its pane for the pull request URL and result. On `already_running`,
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
