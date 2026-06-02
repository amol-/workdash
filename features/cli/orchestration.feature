@feature:F-CLI-ORCHESTRATION
Feature: CLI orchestration commands

  Automation clients can use Workdash commands as thin clients for the local
  server-backed Workdash session. The commands format server responses for
  humans or pass structured JSON through for agents.

  Rules:
    - `workdash list`, `workdash info`, `workdash analyze`, and `workdash code` require a running `workdash --server` session.
    - Server-backed CLI commands connect to the local Workdash JSON API at `127.0.0.1:8765`.
    - Server-backed CLI commands do not load configuration, run GitHub preflight, inspect Zellij, or fetch GitHub directly.
    - Server-backed CLI commands report a clear error when the local Workdash server is not reachable.
    - CLI commands that return information support `--json`.
    - `workdash info` reports live pane state from the server-backed Workdash session.
    - Live Zellij pane state is authoritative; pane identifiers are discovered on demand, not persisted.
    - Pane item mappings are based on `pane_cwd`, not pane title.
    - A pane whose cwd is a known worktree or a descendant of one maps to that Workdash item; the most specific matching worktree wins.
    - Planned worktree path names are not known worktrees by name alone; local git metadata must prove the repository relationship.
    - Known worktrees are discovered from Workdash-style per-item directory candidates, then local git metadata must prove the candidate is the repository root and has the expected repository relationship.
    - Scanned PR worktree candidates with a same-name origin under another owner are not known worktrees.
    - A fork or renamed fork directory must have local upstream metadata matching the base repository before it maps to that repository's item.
    - Manually renamed worktrees outside Workdash-style per-item candidates are unknown; Workdash may prepare its own worktree later.
    - `workdash info` reports only live panes whose titles prove Workdash launched them: `code_` agent panes and `terminal_` terminal panes.
    - Zellij exited or held panes are not live panes.
    - `workdash info --all` also reports live non-plugin panes in the server-backed Workdash session, but extra panes are `unknown` kind with unknown item mapping.
    - Missing optional agent commands mean that agent operation is not configured, not that the whole config is invalid.
    - `workdash analyze` accepts only configured analysis agents reported by `workdash show-config`.
    - `workdash code` launches only configured terminal-backed coding agents reported by `workdash show-config`.
    - `workdash code` reports the created Zellij pane ID from live session state without persisting it.
    - `workdash code` does not expose non-terminal editors such as VSCode in V0.

  @id:F-CLI-ORCHESTRATION-S003
  Scenario: Info inspects panes through the local Workdash server
    Given a server-backed Workdash session is running
    And that session has a `code_owner_repo_1` pane in a known worktree
    And that session has a `terminal_owner_repo_1` pane in the same known worktree
    When the user runs `workdash info`
    Then the command requests pane information from the local Workdash server
    And the system reports the Workdash session name
    And the system reports the agent pane with its Zellij pane identifier, title, cwd, command, tab, and state
    And the system reports the terminal pane with its Zellij pane identifier, title, cwd, command, tab, and state
    And both panes are mapped to the matching Workdash item

  @id:F-CLI-ORCHESTRATION-S004
  Scenario: Info requires the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs `workdash info`
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S005
  Scenario: Info returns machine-readable pane state
    Given a server-backed Workdash session is running
    And that session has Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the command requests pane information from the local Workdash server
    And the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S006
  Scenario: Info keeps unmapped live panes visible
    Given a server-backed Workdash session is running
    And that session has a Workdash-named pane whose cwd does not match a known worktree
    When the user runs `workdash info --json`
    Then the system reports the raw pane information
    And the pane item mapping is marked unknown

  @id:F-CLI-ORCHESTRATION-S007
  Scenario: Info ignores exited or held Workdash panes
    Given a server-backed Workdash session is running
    And that session has live, exited, and held Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the system returns JSON pane records
    And the system does not report exited or held panes

  @id:F-CLI-ORCHESTRATION-S008
  Scenario: Top-level JSON flag applies to info
    Given a server-backed Workdash session is running
    And that session has Workdash terminal-backed panes
    When the user runs `workdash --json info`
    Then the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S009
  Scenario: Analyze resolves a current work item through the local Workdash server
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the system returns JSON with the item ID, selected agent, analysis path, and cache status

  @id:F-CLI-ORCHESTRATION-S010
  Scenario: Orchestration commands require the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs a server-backed orchestration command
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S011
  Scenario: Analyze reports server-side agent command errors
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    And the configured Codex analyze command is malformed
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the system reports the server-side malformed agent command error
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S012
  Scenario: Info can include ordinary live panes on request
    Given a server-backed Workdash session is running
    And that session has Workdash terminal-backed panes
    And that session has ordinary live, exited, held, and plugin panes
    When the user runs `workdash info --all --json`
    Then the command requests pane information from the local Workdash server with ordinary panes included
    And the system returns JSON pane records
    And the system reports the ordinary live pane as unknown kind with raw pane information
    And the system does not report exited, held, or plugin panes

  @id:F-CLI-ORCHESTRATION-S013
  Scenario: Code launches a terminal-backed agent through the local Workdash server
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash code owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests code launch from the local Workdash server
    And the system returns JSON with the item ID, selected agent, selected session, cwd, pane title, and pane ID

  @id:F-CLI-ORCHESTRATION-S014
  Scenario: Code does not expose non-terminal editors
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash code owner/repo#ISSUE-1 --agent vscode --json`
    Then the command requests code launch from the local Workdash server
    And the system reports that the coding agent is not a configured terminal-backed agent
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S015
  Scenario: Code reports server-side agent command errors
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    And the configured Codex launch command is malformed
    When the user runs `workdash code owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests code launch from the local Workdash server
    And the system reports the server-side malformed agent command error
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S016
  Scenario: Analyze accepts a partial agent configuration reported by show-config
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    And `workdash show-config` reports only `codex` as an analysis agent
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the system returns JSON with the item ID, selected agent, analysis path, and cache status

  @id:F-CLI-ORCHESTRATION-S017
  Scenario: Server-backed commands skip local GitHub and Zellij preflight
    Given the local Workdash server is reachable
    And the client process cannot find GitHub CLI or Zellij on PATH
    When the user runs `workdash info`
    Then the command still sends the request to the local Workdash server
    And the command formats the server response

  @id:F-CLI-ORCHESTRATION-S018
  Scenario: Analyze rejects an item outside the current dashboard state
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items do not include `owner/repo#ISSUE-99`
    When the user runs `workdash analyze owner/repo#ISSUE-99 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the system reports that the work item is unknown
    And the command exits with a non-zero status
