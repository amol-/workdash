@feature:F-TODO-CREATE
Feature: Capture a todo
  Capture work without leaving the dashboard. The issue lives in the configured
  todo repository even when the work belongs to a different repository.

  @id:F-TODO-CREATE-S001
  Scenario: Capture a generic todo from the TUI
    Given the user has a configured todo repository
    When the user presses "w" and submits "Fix the flaky test" with the target blank
    Then the capture dialog has asked for a title and an optional target repository
    And the dashboard immediately shows todo "Fix the flaky test" under "testuser/todos"

  @id:F-TODO-CREATE-S002
  Scenario: Capture a todo for another repository
    Given the user has a configured todo repository
    When the user captures "Fix the flaky test" with "w" for target "owner/repo"
    Then the dashboard immediately shows todo "Fix the flaky test" under "owner/repo"

  @id:F-TODO-CREATE-S004
  Scenario: Retry capture after creating the todo repository
    Given the configured todo repository does not exist on GitHub
    When the user submits "Fix the flaky test" with "w" and follows the guidance to create "testuser/todos" on GitHub and retry
    Then no todo titled "Fix the flaky test" was added by the failed attempt
    And the dashboard immediately shows todo "Fix the flaky test" under "testuser/todos"

  @id:F-TODO-CREATE-S006
  Scenario: Find a previously captured todo on refresh
    Given an earlier todo titled "Fix the flaky test" targets "owner/repo" but is not on the dashboard
    When the user refreshes the dashboard with "r"
    Then the dashboard shows todo "Fix the flaky test" under "owner/repo"
