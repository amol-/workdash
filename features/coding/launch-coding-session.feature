@feature:F-CODING-LAUNCH
Feature: Launch a coding session

  When the user is ready to work on a selected item, the system opens an
  interactive coding session with a supported coding agent, grounded in
  the item's worktree and preloaded with the GitHub context of the work
  item.

  Rules:
    - Pressing "c" in the TUI opens a dialog to pick a configured coding agent.
    - The configured coding agents can be Claude, ChatGPT Codex, and pi; the TUI also supports VSCode Copilot as an editor launch.
    - Before launching a session, the system prepares the worktree for the selected work item.
    - The launched coding agent is started inside the work item's worktree.
    - The launched coding agent is preloaded with the work item's GitHub context and, when available, the cached analysis for that item.
    - A coding session is briefed for exactly one of two jobs: implementing the work item, or reviewing somebody else's pull request. ISSUE and PR items get the implement briefing; REVIEW and CHECK items get the review briefing.
    - The implement briefing for an authored pull request also tells the agent that the checkout already holds a partial implementation of this work, and that it must review those existing changes and build on them instead of starting over.
    - The user can cancel the dialog without launching a session.
    - The system reports the outcome of the launch to the user.
    - If preparing the worktree or launching the subprocess fails, the system closes the dialog/progress overlay and reports the error details to the user.
    - When the selected work item already has an open coding-agent pane, the dialog also offers to focus that pane instead of only offering to start a new session.
    - Choosing to focus the active pane switches to it and starts no new coding session.

  @id:F-CODING-LAUNCH-S001
  Scenario: User picks a coding agent and a session opens on the worktree
    Given the TUI has a work item selected
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system prepares the worktree for the selected work item
    And a coding session with the chosen agent opens inside that worktree
    And the agent is preloaded with the work item's GitHub context
    And the TUI reports that the session was launched

  @id:F-CODING-LAUNCH-S002
  Scenario: Existing analysis is provided to the coding agent
    Given the selected work item has a cached analysis
    When the user launches a coding session with a supported coding agent
    Then the agent is preloaded with the cached analysis alongside the GitHub context

  @id:F-CODING-LAUNCH-S006
  Scenario: An authored pull request is briefed as partially implemented work
    Given the selected work item is a pull request the user authored
    When the user launches a coding session with a supported coding agent
    Then the agent is briefed to implement the work item
    And the agent is told the checkout already holds a partial implementation to build on

  @id:F-CODING-LAUNCH-S007
  Scenario: A pull request the user did not author is briefed as a review
    Given the selected work item is a pull request the user did not author
    When the user launches a coding session with a supported coding agent
    Then the agent is briefed to review the pull request

  @id:F-CODING-LAUNCH-S003
  Scenario: User cancels the coding dialog without launching
    Given the coding dialog is open
    When the user cancels the dialog
    Then no coding session is launched
    And no worktree is prepared

  @id:F-CODING-LAUNCH-S004
  Scenario: Worktree preparation failure reports the error details
    Given the TUI has a work item selected
    And the next worktree preparation will fail
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system reports the worktree error details to the user
    And no coding session is launched
    And no dialog or progress overlay remains

  @id:F-CODING-LAUNCH-S005
  Scenario: Coding agent launch failure reports the error details
    Given the TUI has a work item selected
    And the next coding session launch will fail
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system reports the coding launch error details to the user
    And no dialog or progress overlay remains

  @id:F-CODING-LAUNCH-S008
  Scenario: An item with an already open agent pane offers to focus it
    Given the selected work item already has an open coding-agent pane
    When the user presses "c"
    Then the dialog offers to focus the active agent pane

  @id:F-CODING-LAUNCH-S009
  Scenario: User focuses the already open agent pane instead of starting a new one
    Given the selected work item already has an open coding-agent pane
    When the user presses "c"
    And the user chooses to focus the active agent pane
    Then the system focuses that pane
    And no coding session is launched
