@feature:F-TRIAGE-REFRESH
Feature: Refresh work items

  @id:F-TRIAGE-REFRESH-S001
  Scenario: See new GitHub work without leaving the dashboard
    Given the TUI is open with a list of work items
    And a new issue titled "Fix the log viewer" is available on GitHub
    When the user presses "r"
    Then the dashboard lists "Fix the log viewer"
    And the system reports how many work items are now shown
