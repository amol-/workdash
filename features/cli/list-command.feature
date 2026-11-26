@feature:F-TRIAGE-LIST-COMMAND
Feature: List work items from another terminal

  @id:F-TRIAGE-LIST-COMMAND-S001
  Scenario: List the work already on the dashboard
    Given a server-backed Workdash session is running
    When the user runs `workdash list`
    Then the printed list includes "Fix the issue" with the ID `owner/repo#ISSUE-1`

  @id:F-TRIAGE-LIST-COMMAND-S006
  Scenario: List the latest work from GitHub
    Given a server-backed Workdash session is running
    And a new issue titled "Fix the log viewer" is available on GitHub
    When the user runs `workdash list --refresh`
    Then the printed list includes "Fix the log viewer" with the ID `owner/repo#ISSUE-99`

  @id:F-API-JSON-CONTROL-S031
  Scenario: List work from a dashboard on a custom address
    Given the dashboard is running on the custom address set in `WORKDASH_HOST`
    When the user runs `workdash list` from another terminal
    Then the printed list includes "Fix the issue" with the ID `owner/repo#ISSUE-1`

  @id:F-TODO-TARGET-S001
  @id:F-TODO-TARGET-S007
  Scenario: Find a todo and open a shell in its target repository
    Given the dashboard lists a todo "Fix the flaky test" targeting `https://codeberg.org/owner/repo.git`
    And the target repository has no local clone or worktree
    When the user runs `workdash list` to find the todo's ID
    Then the printed list identifies the todo as `owner/repo#ISSUE-WT110`
    When the user passes that ID to `workdash terminal`
    Then Workdash reports a shell in a new worktree of the Codeberg repository
