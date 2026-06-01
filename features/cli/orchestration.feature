@feature:F-CLI-ORCHESTRATION
Feature: CLI orchestration commands

  Automation clients can use Workdash as the source of GitHub work-item
  context while using Zellij as the source of live terminal pane state.

  Rules:
    - CLI commands that return information support `--json`.
    - Workdash-owned Zellij sessions are active sessions whose names start with `workdash`.
    - Orchestration commands require an active Workdash-owned Zellij session and do not create one.
    - When multiple Workdash-owned sessions exist, commands do not guess unless the user passes `--session`.
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
    - `workdash info --all` also reports live non-plugin panes in the selected Workdash-owned session, but extra panes are `unknown` kind with unknown item mapping.

  @id:F-CLI-ORCHESTRATION-S003
  Scenario: Info inspects the only active Workdash-owned session
    Given exactly one active Workdash-owned Zellij session exists
    And that session has a `code_owner_repo_1` pane in a known worktree
    And that session has a `terminal_owner_repo_1` pane in the same known worktree
    When the user runs `workdash info`
    Then the system reports the Workdash-owned session name
    And the system reports the agent pane with its Zellij pane identifier, title, cwd, command, tab, and state
    And the system reports the terminal pane with its Zellij pane identifier, title, cwd, command, tab, and state
    And both panes are mapped to the matching Workdash item

  @id:F-CLI-ORCHESTRATION-S004
  Scenario: Info requires an explicit session when multiple Workdash-owned sessions exist
    Given multiple active Workdash-owned Zellij sessions exist
    When the user runs `workdash info`
    Then the system does not choose a session
    And the system lists the candidate Workdash-owned sessions
    And the system asks the user to pass `--session`
    And the system exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S005
  Scenario: Info returns machine-readable pane state
    Given exactly one active Workdash-owned Zellij session exists
    And that session has Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S006
  Scenario: Info keeps unmapped live panes visible
    Given exactly one active Workdash-owned Zellij session exists
    And that session has a Workdash-named pane whose cwd does not match a known worktree
    When the user runs `workdash info --json`
    Then the system reports the raw pane information
    And the pane item mapping is marked unknown

  @id:F-CLI-ORCHESTRATION-S007
  Scenario: Info ignores exited or held Workdash panes
    Given exactly one active Workdash-owned Zellij session exists
    And that session has live, exited, and held Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the system returns JSON pane records
    And the system does not report exited or held panes

  @id:F-CLI-ORCHESTRATION-S008
  Scenario: Top-level JSON flag applies to info
    Given exactly one active Workdash-owned Zellij session exists
    And that session has Workdash terminal-backed panes
    When the user runs `workdash --json info`
    Then the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S009
  Scenario: Analyze resolves a current work item from the CLI
    Given exactly one active Workdash-owned Zellij session exists
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the system analyzes the current item with the selected configured agent
    And the system returns JSON with the item id, selected agent, analysis path, and cache status

  @id:F-CLI-ORCHESTRATION-S010
  Scenario: Orchestration commands require a Workdash-owned Zellij session
    Given no active Workdash-owned Zellij session exists
    When the user runs an orchestration command
    Then the system reports that an active Workdash-owned Zellij session is required
    And the system exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S011
  Scenario: Analyze reports a malformed configured agent command
    Given exactly one active Workdash-owned Zellij session exists
    And the current Workdash items include an assigned issue without cached analysis
    And the configured Codex analyze command is malformed
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the system reports the malformed agent command with config context
    And the system does not prepare a worktree
    And the system exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S012
  Scenario: Info can include ordinary live panes on request
    Given exactly one active Workdash-owned Zellij session exists
    And that session has Workdash terminal-backed panes
    And that session has ordinary live, exited, held, and plugin panes
    When the user runs `workdash info --all --json`
    Then the system returns JSON pane records
    And the system reports the ordinary live pane as unknown kind with raw pane information
    And the system does not report exited, held, or plugin panes
