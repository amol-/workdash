# Glossary

Canonical product vocabulary used by the feature files.

## Terms

- User: The developer using `workdash` to triage their GitHub work.
- System: `workdash`, the text-based GitHub work triage dashboard.
- Work item: An issue or pull request surfaced by the system for triage.
- Item type: The category of a work item. One of `ISSUE`, `PR`, or `REVIEW`.
- REVIEW item: A pull request where the user is directly requested as a reviewer, or has already reviewed.
- Authored PR: A pull request whose author is the user.
- Assigned issue: An open issue where the user is an assignee.
- Tracked item: An open issue or pull request that appears in a tracked repository regardless of the user's direct involvement.
- Tracked repository: A repository listed in the user's configuration, either as `owner/repo` or expanded from an `owner/*` selector.
- Repository selector: An entry in the `repositories` configuration list. Either `owner/repo` or `owner/*`.
- Suggested item: The single work item the system highlights as the recommended next thing to pick up.
- Work directory: The local directory where the system keeps per-repository clones and per-item worktrees.
- Main clone: The local clone of a tracked repository that lives directly under the work directory.
- Worktree: A per-work-item git worktree used for analysis and coding sessions, rooted under the work directory.
- Analysis: A cached AI-generated summary and recommendation for a work item, viewable as rendered HTML.
- Analysis cache: The local store of previously generated analyses, keyed so that GitHub updates invalidate stale entries.
- Coding session: An interactive session with a supported coding agent, launched in the item's worktree and preloaded with work item context.
- Supported coding agent: One of Claude, ChatGPT Codex, or VSCode Copilot.
- Configuration: The user settings stored at `~/.config/workdash/config.json`.
- Configuration wizard: The interactive flow started by `--configure` that fills in any missing configuration fields.
- TUI: The interactive terminal interface shown by default when the system starts.
- Print mode: The non-interactive listing emitted when the system is started with `--print`, intended for automation and for agents that need to read the dashboard without driving the TUI.
- Recent activity: A work item whose last update is within the last 24 hours.
