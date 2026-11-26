@feature:F-STARTUP-PREFLIGHT
Feature: Sign in to GitHub before opening the dashboard

  @id:F-STARTUP-PREFLIGHT-S003
  Scenario: Sign in to GitHub and reopen Workdash
    Given the system is running inside a Zellij session
    And the GitHub CLI is installed but not authenticated
    And the user has a tracked issue titled "Fix the issue"
    When the user runs `workdash`
    Then Workdash tells the user to sign in with `gh auth login`
    When the user signs in with `gh auth login`
    And the user runs `workdash` again
    Then the dashboard opens in the current pane and shows "Fix the issue"
