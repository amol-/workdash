@feature:F-TRIAGE-SEARCH
Feature: Find work on the dashboard

  @id:F-TRIAGE-SEARCH-S001
  Scenario: Narrow the dashboard to work with a matching title
    Given the TUI is open with a list of work items
    When the user presses "/" and searches for part of an item's title
    Then only the work items whose title contains that fragment are listed
    And the system reports how many of the loaded work items matched

  @id:F-TRIAGE-SEARCH-S004
  Scenario: Get back to the full dashboard after an unsuccessful search
    Given the dashboard has no matches for the active search
    When the user presses "/"
    Then no search box is shown
    And all loaded work items are listed again
