@feature:F-TERMINAL-OPEN
Feature: Work in an item's terminal

  Open a plain shell in an item's worktree without starting a coding agent.

  @id:F-TERMINAL-OPEN-S001
  Scenario: Open a terminal in the selected item's worktree
    Given the TUI has a work item selected
    And the selected item does not have a worktree yet
    When the user presses "c" then "t" to open a terminal
    Then a plain shell opens in the selected item's new worktree
    And the TUI reports that a terminal was opened

  @id:F-TERMINAL-OPEN-S006
  Scenario: Return to the existing shell for the selected item
    Given the TUI has a work item selected
    And a plain shell is already open in that item's worktree
    When the user presses "c" then "t" to open a terminal
    Then the existing terminal is focused without opening another

  @id:F-TERMINAL-OPEN-S012
  Scenario: Close a Workdash terminal by exiting its shell
    Given a Workdash terminal is open in a worktree
    When the user exits the shell
    Then that terminal pane closes
