@feature:F-TUI-AUTODISPATCH
Feature: Start autodispatch from the dashboard

  @id:F-AUTODISPATCH-S013
  Scenario: Start autodispatch from an issue in the dashboard
    Given a ready issue selected in the dashboard and another item to triage
    When the user presses d, chooses agent 1, and uses / to find the other item once work begins
    Then an autodispatch session launches that issue
    And the dashboard has announced the start and shows the other item while autodispatch continues
