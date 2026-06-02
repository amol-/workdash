# Glossary

Canonical product vocabulary used by the feature files.

## Terms

- User: The developer using `workdash` to triage their GitHub work.
- System: `workdash`, the text-based GitHub work triage dashboard.
- Work item: An issue or pull request surfaced by the system for triage.
- Workdash item ID: A copy/paste identifier for a work item, formatted as `<repo>#ISSUE-<number>`, `<repo>#PR-<number>`, or `<repo>#REVIEW-<number>`.
- Item type: The category of a work item. One of `ISSUE`, `PR`, or `REVIEW`.
- REVIEW item: A pull request where the user is directly requested as a reviewer, or has already reviewed.
- Authored PR: A pull request whose author is the user.
- Assigned issue: An open issue where the user is an assignee.
- Tracked item: An open issue or pull request that appears in a tracked repository regardless of the user's direct involvement.
- Tracked repository: A repository listed in the user's configuration, either as `owner/repo` or expanded from an `owner/*` selector.
- Included item: A work item the user has explicitly added by pasting its GitHub URL. Persisted across sessions, participates in every list source alongside authored pull requests, review-requested pull requests, assigned issues, and tracked items, and removed automatically when it closes, merges, or is resolved.
- Repository selector: An entry in the `repositories` configuration list. Either `owner/repo` or `owner/*`.
- Suggested item: The single work item the system highlights as the recommended next thing to pick up.
- Work directory: The local directory where the system keeps per-repository clones and per-item worktrees.
- Main clone: The local clone of a tracked repository that lives directly under the work directory.
- Worktree: A per-work-item git worktree used for analysis and coding sessions, rooted under the work directory.
- Analysis: A cached AI-generated summary and recommendation for a work item, viewable as rendered HTML.
- Analysis cache: The local store of previously generated analyses, keyed so that GitHub updates invalidate stale entries.
- Coding session: An interactive session with a supported coding agent, launched in the item's worktree and preloaded with work item context.
- Supported coding agent: One of Claude, ChatGPT Codex, VSCode Copilot, or pi.
- Terminal-backed work action: A work action that opens a shell or terminal-hosted coding agent for a work item.
- CLI orchestration command: A non-interactive command that lets a user or automation client inspect or launch Workdash-owned Zellij panes.
- Zellij session: A terminal multiplexer session used by the system to host terminal-backed work actions.
- Workdash Zellij session: A Zellij session whose name starts with `workdash`, used by the system as the live terminal surface for orchestration commands and terminal-backed work actions.
- Agent pane: A Zellij pane opened by the system for a terminal-hosted coding agent.
- Terminal pane: A Zellij pane opened by the system for a plain terminal in a work item's worktree.
- Configuration: The user settings stored at `~/.config/workdash/config.json`.
- Configuration wizard: The interactive flow started by `--configure` that fills in any missing configuration fields.
- TUI: The interactive terminal interface shown by default when the system starts.
- List command: The non-interactive listing emitted by `workdash list`, intended for automation and for agents that need to read the dashboard without driving the TUI.
- Recent activity: A work item whose last update is within the last 24 hours.
