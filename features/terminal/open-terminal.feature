@feature:F-TERMINAL-OPEN
Feature: Open a terminal in the work item's worktree

  Sometimes the user wants a plain shell rooted in the work item's code
  rather than a coding agent. The system offers a one-keystroke way to
  get there.

  Rules:
    - Choosing "t" in the TUI code menu opens a terminal whose working directory is the worktree of the selected work item.
    - If the worktree does not yet exist, the system prepares it before opening the terminal.
    - The TUI focuses an existing Workdash terminal in the worktree even when it is running a program or its shell changed directory. It also reuses a live shell, including the user's configured shell, but not an agent, diff pane, or other program. A terminal in another worktree under the configured working directory is not reused even if the directories have the same name. Outside that directory, a terminal carrying this worktree's Workdash title is treated as this worktree's terminal.
    - Looking for an existing terminal is advisory: when the system cannot check, it opens a new terminal instead of failing.
    - No coding agent is started by this action.
    - The system reports the outcome of the action to the user.
    - Terminal-backed work actions open panes in the current Zellij session.
    - Terminal-backed work actions require an active Zellij session, even when the dashboard was started with `--direct`.
    - Terminal-backed work actions do not create, inspect, attach, or resurrect shared sessions.
    - If preparing the worktree or launching the terminal subprocess fails, the system closes the progress overlay and reports the error details to the user.

  @id:F-TERMINAL-OPEN-S001
  Scenario: Open a terminal in the selected item's worktree
    Given the TUI has a work item selected
    When the user chooses "t" from the code menu
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

  @id:F-TERMINAL-OPEN-S004
  Scenario: Worktree preparation failure reports the error details
    Given the TUI has a work item selected
    And the next worktree preparation will fail
    When the user chooses "t" from the code menu
    Then the system reports the worktree error details to the user
    And no terminal is opened
    And no dialog or progress overlay remains

  @id:F-TERMINAL-OPEN-S005
  Scenario: Terminal launch failure reports the error details
    Given the TUI has a work item selected
    And the next terminal launch will fail
    When the user chooses "t" from the code menu
    Then the system reports the terminal launch error details to the user
    And no terminal is opened
    And no dialog or progress overlay remains

  @id:F-TERMINAL-OPEN-S006
  Scenario: Focus the existing terminal for the selected work item
    Given the TUI has a work item selected
    And a plain shell is already open in that worktree alongside a coding agent
    When the user chooses "t" from the code menu
    Then the existing terminal is focused without opening another

  @id:F-TERMINAL-OPEN-S008
  Scenario: Focus an existing Workdash terminal running a program
    Given the TUI has a work item selected
    And a Workdash terminal is running a program in that worktree
    When the user chooses "t" from the code menu
    Then the existing terminal is focused without opening another

  @id:F-TERMINAL-OPEN-S007
  Scenario: Programs in the worktree are not mistaken for a shell
    Given the TUI has a work item selected
    And an editor and a test runner are open in that worktree
    When the user chooses "t" from the code menu
    Then a terminal is opened in that worktree

  @id:F-TERMINAL-OPEN-S010
  Scenario: A terminal in another worktree under the working directory with the same directory name is not reused
    Given the TUI has a work item selected
    And a terminal is open in another worktree under the working directory whose directory has the same name
    When the user chooses "t" from the code menu
    Then a terminal is opened in that worktree

  @id:F-TERMINAL-OPEN-S011
  Scenario: Focus a Workdash terminal whose shell changed directory
    Given the TUI has a work item selected
    And a Workdash terminal in that worktree changed to another directory
    When the user chooses "t" from the code menu
    Then the existing terminal is focused without opening another

  @id:F-TERMINAL-OPEN-S009
  Scenario: A failed check for an existing terminal still opens a terminal
    Given the TUI has a work item selected
    And the next check for an existing terminal will fail
    When the user chooses "t" from the code menu
    Then a terminal is opened in that worktree
