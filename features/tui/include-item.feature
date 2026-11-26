@feature:F-TRIAGE-INCLUDE
Feature: Include a work item by URL

  Keep GitHub work from outside your tracked repositories on the dashboard
  until it is resolved.

  @id:F-TRIAGE-INCLUDE-S001
  Scenario: Add off-dashboard work from its GitHub URL
    Given the dashboard is filtered to an unrelated issue
    When the user presses "i", pastes the off-dashboard pull request's URL, and confirms
    Then the dashboard selects the pull request and marks it as included

  @id:F-TRIAGE-INCLUDE-S003
  Scenario: Jump to work already on the dashboard by URL
    Given the dashboard shows a pull request below the selected issue
    When the user presses "i", pastes the listed pull request's URL, and confirms
    Then the dashboard selects the listed pull request

  @id:F-TRIAGE-INCLUDE-S005
  Scenario: Find included work after reopening the dashboard
    Given the user included an off-dashboard pull request in a previous dashboard session
    When the user reopens the dashboard
    Then the included pull request is still listed

  @id:F-TRIAGE-INCLUDE-S006
  Scenario: Stop seeing closed included work after refreshing
    Given the dashboard shows an included pull request that has since closed on GitHub
    When the user presses "r" to refresh the dashboard
    Then the closed pull request is no longer listed
