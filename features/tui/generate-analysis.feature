@feature:F-ANALYSIS-GENERATE
Feature: Read an analysis

  @id:F-ANALYSIS-GENERATE-S001
  Scenario: Analyze a work item for the first time
    Given a work item is selected with no saved analysis
    When the user presses "a" and then "1" to analyze with pi
    Then the dashboard shows analysis in progress
    And the configured viewer opens the fresh analysis and receives focus

  @id:F-ANALYSIS-GENERATE-S002
  Scenario Outline: Reopen a saved analysis in the configured viewer
    Given a work item is selected with a saved analysis
    And the configured viewer reads <format> without a server
    When the user presses "a" to open the analysis menu and "a" again to open the saved analysis
    Then the menu offers to open the saved analysis or run a fresh one
    And the configured viewer opens the saved analysis without running the agent

    Examples:
      | format   |
      | Markdown |
      | HTML     |

  @id:F-ANALYSIS-GENERATE-S009
  Scenario: Copy text from a Markdown document
    Given a Markdown document is open in the viewer
    When the user selects text and presses Ctrl+C
    Then the selected text is copied
