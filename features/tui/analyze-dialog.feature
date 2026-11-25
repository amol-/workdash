@feature:F-ANALYSIS-DIALOG
Feature: Cancel an analysis choice

  @id:F-ANALYSIS-DIALOG-S003
  Scenario: Return to triage without analyzing the item
    Given a work item is selected
    When the user presses "a" and then Esc in the analysis menu
    Then the user is back at the work table without opening an analysis
