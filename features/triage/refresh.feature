@feature:F-TRIAGE-REFRESH
Feature: Refresh work items

  Work on GitHub changes while the TUI is open. The user can force a
  refresh without restarting the system, and can also request a refresh
  at startup from the command line.

  Rules:
    - Pressing "r" in the TUI re-fetches the work item list from GitHub and updates the dashboard in place.
    - Running the system with "--refresh" re-fetches the work item list at startup.
    - During a refresh, the system shows the user that work is in progress and prevents further actions until the refresh completes.
    - After a successful refresh, the TUI reports how many work items are now shown.
    - A refresh clears any active search filter.
    - If a refresh fails, the TUI closes the progress overlay, reports the failure details, and keeps the previous list visible.

  @id:F-TRIAGE-REFRESH-S001
  Scenario: Interactive refresh updates the dashboard in place
    Given the TUI is open with a list of work items
    When the user presses "r"
    Then the system shows that a refresh is in progress
    And the list updates to the latest state from GitHub
    And the system reports how many work items are now shown

  @id:F-TRIAGE-REFRESH-S002
  Scenario: A failing refresh preserves the current list
    Given the TUI is open with a list of work items
    And the next refresh will fail
    When the user presses "r"
    Then the system reports the failure to the user
    And no dialog or progress overlay remains
    And the previously shown list remains visible
