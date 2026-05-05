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

  @id:F-TERMINAL-OPEN-S001
  Scenario: Open a terminal in the selected item's worktree
    Given the TUI has a work item selected
    When the user presses "t"
    Then the system ensures the worktree for that work item exists
    And a terminal is opened in that worktree
    And no coding agent is started
    And the TUI reports that a terminal was opened
