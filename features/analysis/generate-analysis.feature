@feature:F-ANALYSIS-GENERATE
Feature: Generate an analysis

  The system can produce an AI-assisted analysis of a work item, storing
  it locally so the user can re-read it instantly and open it in a
  browser-friendly format.

  Rules:
    - A fresh analysis can be generated with any supported coding agent configured for analysis.
    - Before generating an analysis, the system prepares the work item's worktree so the agent has access to the code.
    - While the analysis runs, the system shows the user that work is in progress.
    - After a successful fresh analysis, the system caches the result and opens the rendered analysis in the user's default browser.
    - Opening a cached analysis also renders it and opens it in the user's default browser.
    - When an analysis fails or produces no content, the system closes any progress overlay, reports the failure details to the user, and leaves any previous cached analysis untouched.

  @id:F-ANALYSIS-GENERATE-S001
  Scenario: Fresh analysis is cached and opened in the browser
    Given the user has the analyze dialog open on a work item
    When the user chooses to generate a fresh analysis with a supported coding agent
    Then the system prepares the work item's worktree
    And the system shows that the analysis is in progress
    And the generated analysis is cached
    And the rendered analysis is opened in the user's default browser

  @id:F-ANALYSIS-GENERATE-S002
  Scenario: Opening a cached analysis renders and opens it
    Given the selected work item has a cached analysis
    When the user chooses to open the cached analysis
    Then the cached analysis is rendered
    And the rendered analysis is opened in the user's default browser

  @id:F-ANALYSIS-GENERATE-S003
  Scenario: Failing analysis reports to the user and preserves the cache
    Given the user has the analyze dialog open on a work item with a cached analysis
    And the next fresh analysis will fail
    When the user chooses to generate a fresh analysis
    Then the system reports the failure to the user
    And no dialog or progress overlay remains
    And the previously cached analysis is preserved
