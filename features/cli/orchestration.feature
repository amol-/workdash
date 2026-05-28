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
    - A directory whose name only ends with the item number is not a known worktree unless local git metadata plausibly relates it to the item's repository.
    - Scanned PR worktree candidates with a same-name origin under another owner are not known worktrees.
    - A fork or renamed fork directory must have local upstream metadata matching the base repository before it maps to that repository's item.

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
  Scenario: Info ignores exited Workdash panes
    Given exactly one active Workdash-owned Zellij session exists
    And that session has live and exited Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the system returns JSON pane records
    And the system does not report exited panes

  @id:F-CLI-ORCHESTRATION-S008
  Scenario: Top-level JSON flag applies to info
    Given exactly one active Workdash-owned Zellij session exists
    And that session has Workdash terminal-backed panes
    When the user runs `workdash --json info`
    Then the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S010
  Scenario: Orchestration commands require a Workdash-owned Zellij session
    Given no active Workdash-owned Zellij session exists
    When the user runs an orchestration command
    Then the system reports that an active Workdash-owned Zellij session is required
    And the system exits with a non-zero status
