@feature:F-CODING-LAUNCH
Feature: Launch a coding session

  When the user is ready to work on a selected item, the system opens an
  interactive coding session with a supported coding agent, grounded in
  the item's worktree and preloaded with the GitHub context of the work
  item.

  Rules:
    - Pressing "c" in the TUI opens a dialog to pick a supported coding agent.
    - The supported coding agents are Claude, ChatGPT Codex, and VSCode Copilot.
    - Before launching a session, the system prepares the worktree for the selected work item.
    - The launched coding agent is started inside the work item's worktree.
    - The launched coding agent is preloaded with the work item's GitHub context and, when available, the cached analysis for that item.
    - The user can cancel the dialog without launching a session.
    - The system reports the outcome of the launch to the user.

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

  @id:F-CODING-LAUNCH-S003
  Scenario: User cancels the coding dialog without launching
    Given the coding dialog is open
    When the user cancels the dialog
    Then no coding session is launched
    And no worktree is prepared
