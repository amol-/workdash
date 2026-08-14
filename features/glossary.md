# Glossary

Canonical product vocabulary used by the feature files.

## Terms

- User: The developer using `workdash` to triage their GitHub work.
- System: `workdash`, the text-based GitHub work triage dashboard.
- Work item: An issue or pull request surfaced by the system for triage.
- Workdash item ID: A copy/paste identifier for a work item, formatted as `<repo>#ISSUE-<number>`, `<repo>#PR-<number>`, `<repo>#REVIEW-<number>`, `<repo>#CHECK-<number>`, or `<target>#ISSUE-WT<number>` for a targeted todo item.
- Item type: The category of a work item shown in the Type column and used in its Workdash item ID. One of `ISSUE`, `PR`, `REVIEW`, or `CHECK`. An assigned or tracked issue is an `ISSUE`, a pull request the user authored is a `PR`, a pull request the user must review or has already reviewed is a `REVIEW`, and any other pull request is a `CHECK`.
- REVIEW item: A pull request where the user is directly requested as a reviewer, or has already reviewed.
- CHECK item: A pull request the user did not author and was neither asked to review nor has already reviewed, so it is still waiting to be looked at. This holds however the pull request reached the dashboard, including one the user included by URL.
- Authored PR: A pull request whose author is the user, shown as a `PR` item.
- Linked issue: The issue in a pull request's own repository that the pull request closes, as GitHub reports it. A pull request that closes several of them is linked to the lowest-numbered one. A pull request replaces every issue it closes on the dashboard, including issues in other repositories, but only a linked issue names its worktree.
- CI result: The combined state of the checks GitHub last ran on an authored pull request's most recent commit, shown as a one-character symbol in the Type column. A pull request with no checks configured has no CI result.
- Review decision: GitHub's rollup verdict on an authored pull request's reviews (for example approved, or changes requested). An authored pull request with a passing CI result and an approved review decision is prefixed with a double checkmark instead of the single passing symbol.
- Assigned issue: An open issue where the user is an assignee.
- Tracked item: An open issue or pull request that appears in a tracked repository regardless of the user's direct involvement.
- Tracked repository: A repository listed in the user's configuration, either as `owner/repo` or expanded from an `owner/*` selector.
- Todo item: A work item the user captured as an issue in the todo repository, assigned to themselves and labeled `workdash-todo`.
- Todo repository: The single configured `owner/repo` where captured todos are created as issues.
- Todo target: The optional repository a todo is about. A targeted todo is listed under its target and its work actions use a worktree of the target.
- Included item: A work item the user has explicitly added by pasting its GitHub URL. Persisted across sessions, participates in every list source alongside authored pull requests, review-requested pull requests, assigned issues, and tracked items, and removed automatically when it closes, merges, or is resolved.
- Repository selector: An entry in the `repositories` configuration list. Either `owner/repo` or `owner/*`.
- Suggested item: The single work item the system highlights as the recommended next thing to pick up.
- Search filter: Text the user typed in the TUI to narrow the listed work items to those whose Type, Repo, or Title column contains it. Lives only in the TUI and lasts until it is cleared or the loaded list changes.
- Work directory: The local directory where the system keeps per-repository clones and per-item worktrees.
- Main clone: The local clone of a tracked repository that lives directly under the work directory.
- Worktree: A per-work-item git worktree used for analysis and coding sessions, rooted under the work directory.
- Analysis: A cached AI-generated summary and recommendation for a work item, viewable as rendered HTML.
- Analysis cache: The local store of previously generated analyses, keyed so that GitHub updates invalidate stale entries.
- Coding session: An interactive session with a supported coding agent, launched in the item's worktree and preloaded with work item context.
- Supported coding agent: One of Claude, ChatGPT Codex, VSCode Copilot, or pi.
- Terminal-backed work action: A work action that opens a shell or terminal-hosted coding agent for a work item.
- CLI orchestration command: A non-interactive command that lets a user or automation client inspect or control the active server-backed Workdash session.
- Workdash server: The localhost JSON API server started by `workdash --server` in the same process as the TUI.
- Server-backed Workdash session: A Workdash TUI process started with `--server`, sharing one in-memory dashboard state with the local JSON API.
- Local Workdash JSON API: The HTTP JSON API exposed by the Workdash server on `127.0.0.1:8765`.
- API client: A local command or automation agent that sends JSON requests to the Workdash server.
- Pane ID: The copy/paste Zellij pane identifier reported by `workdash info`, used by pane content and send APIs.
- Zellij session: A terminal multiplexer session used by the system to host terminal-backed work actions.
- Workdash Zellij session: A Zellij session whose name starts with `workdash`, used by the system as the live terminal surface for orchestration commands and terminal-backed work actions.
- Agent pane: A Zellij pane opened by the system for a terminal-hosted coding agent.
- Terminal pane: A Zellij pane opened by the system for a plain terminal in a work item's worktree.
- Configuration: The user settings stored at `~/.config/workdash/config.json`.
- Configuration wizard: The interactive flow started by `--configure` that fills in any missing configuration fields.
- TUI: The interactive terminal interface shown by default when the system starts.
- List command: The non-interactive listing emitted by `workdash list`, intended for automation and for agents that need to read the active server-backed dashboard without driving the TUI.
- Show-config command: The command and API capability that reports configured analysis and coding agents plus the fixed V0 server address.
- Recent activity: A work item whose last update is within the last 24 hours.
