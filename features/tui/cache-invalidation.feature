@feature:F-ANALYSIS-CACHE
Feature: Analyze changed work

  @id:F-ANALYSIS-CACHE-S001
  Scenario: Get a current analysis after a GitHub update
    Given a work item is selected with a saved analysis
    And the work item changes on GitHub
    When the user refreshes with "r", opens the analysis menu with "a", and presses "1" to analyze with pi
    Then the menu does not offer the previous analysis
    And the configured viewer opens the updated analysis
