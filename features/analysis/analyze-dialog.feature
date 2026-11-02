@feature:F-ANALYSIS-DIALOG
Feature: Analyze dialog

  When the user asks to analyze a work item, the system opens a dialog
  summarising the current analysis state and offering the available
  analyze actions.

  Rules:
    - Pressing "a" in the TUI opens the analyze dialog for the selected work item.
    - If an analysis already exists for the item, the dialog shows how long ago it was produced and offers to open it.
    - If no analysis exists, the dialog tells the user there is no previous analysis.
    - The dialog offers fresh analysis only for configured analysis agents.
    - Each configured analysis agent is offered as "Analyze with <name>", numbered from 1 in configuration order.
    - The user can cancel the dialog without triggering any action.

  @id:F-ANALYSIS-DIALOG-S001
  Scenario: Dialog offers to open a cached analysis
    Given the selected work item has a cached analysis
    When the user presses "a"
    Then the dialog shows how long ago the analysis was produced
    And the dialog offers to open the cached analysis
    And the dialog offers to generate a fresh analysis

  @id:F-ANALYSIS-DIALOG-S002
  Scenario: Dialog reports when no analysis exists
    Given the selected work item has no cached analysis
    When the user presses "a"
    Then the dialog tells the user there is no previous analysis
    And the dialog offers to generate a fresh analysis

  @id:F-ANALYSIS-DIALOG-S004
  Scenario: Dialog offers each configured analysis agent in configuration order
    Given the selected work item has no cached analysis
    And the analysis agents `pi` and `claude` are configured in that order
    When the user presses "a"
    Then the dialog offers "Analyze with pi" as option 1
    And the dialog offers "Analyze with claude" as option 2

  @id:F-ANALYSIS-DIALOG-S003
  Scenario: User cancels the dialog without side effects
    Given the analyze dialog is open
    When the user cancels the dialog
    Then no analysis is generated
    And no analysis is opened
