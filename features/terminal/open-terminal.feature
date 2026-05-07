@feature:F-TERMINAL-OPEN
Feature: Open a terminal in the work item's worktree

  Sometimes the user wants a plain shell rooted in the work item's code
  rather than a coding agent. The system offers a one-keystroke way to
  get there.

  Rules:
    - Pressing "t" in the TUI opens a terminal whose working directory is the worktree of the selected work item.
    - If the worktree does not yet exist, the system prepares it before opening the terminal.
    - No coding agent is started by this action.
    - The system reports the outcome of the action to the user.
    - Terminal-backed work actions open panes in the current Zellij session.
    - Terminal-backed work actions require an active Zellij session, even when the dashboard was started with `--direct`.
    - Terminal-backed work actions do not create, inspect, attach, or resurrect shared sessions.

  @id:F-TERMINAL-OPEN-S001
  Scenario: Open a terminal in the selected item's worktree
    Given the TUI has a work item selected
    When the user presses "t"
    Then the system ensures the worktree for that work item exists
    And a terminal is opened in that worktree
    And no coding agent is started
    And the TUI reports that a terminal was opened

  @id:F-TERMINAL-OPEN-S002
  Scenario: Work action launched from inside the dashboard uses the current Zellij session
    Given the system is running inside a Zellij session
    When the user launches a terminal-backed work action
    Then the work action opens in the current Zellij session
    And the system does not target the shared `workdash` Zellij session

  @id:F-TERMINAL-OPEN-S003
  Scenario: Direct dashboard outside Zellij reports the active-session requirement
    Given the system is not running inside a Zellij session
    And the dashboard was started with `--direct`
    When the user launches a terminal-backed work action
    Then the system reports that terminal-backed work actions require an active Zellij session
