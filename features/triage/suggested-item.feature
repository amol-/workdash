@feature:F-TRIAGE-SUGGESTED
Feature: Suggested next item

  Alongside the full list, the system picks one work item to highlight as
  the recommended next thing to pick up, so the user does not have to
  reason about priorities to get started.

  Rules:
    - Exactly one work item is marked as suggested when the list is non-empty.
    - The suggested item is the oldest open work item by creation date.
    - When several items share the oldest creation date, pull requests are preferred over issues.
    - The suggested item's title is prefixed with "* " in both the TUI and list command.

  @id:F-TRIAGE-SUGGESTED-S001
  Scenario: The oldest item is suggested
    Given the user has several open work items with different creation dates
    When the user opens the dashboard
    Then exactly one work item is marked as suggested
    And the suggested item is the oldest by creation date

  @id:F-TRIAGE-SUGGESTED-S002
  Scenario: Pull requests win the tie-breaker against issues
    Given the oldest creation date is shared by a pull request and an issue
    When the user opens the dashboard
    Then the pull request is marked as suggested

  @id:F-TRIAGE-SUGGESTED-S003
  Scenario: No suggestion when there is no work
    Given the user has no open work items
    When the user opens the dashboard
    Then no item is marked as suggested
