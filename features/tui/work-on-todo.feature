@feature:F-TODO-TARGET
Feature: Work on a todo in its target repository

  Work on the target's code while keeping the task and its discussion in GitHub.

  @id:F-TODO-TARGET-S004
  Scenario: Start coding on a todo in its target repository
    Given the dashboard lists a todo "Fix the flaky test" targeting `owner/repo`
    When the user presses "c" and chooses a coding agent
    Then the coding agent opens in the target checkout with the todo as its task

  @id:F-TODO-TARGET-S008
  Scenario: Analyze a GitHub todo in the context of its Codeberg repository
    Given the dashboard lists a todo "Fix the flaky test" targeting `https://codeberg.org/owner/repo.git`
    And the todo has discussion on GitHub and source code in its target repository
    When the user presses "a" and chooses a fresh analysis
    Then the opened analysis draws on both the target code and the todo discussion
