@feature:F-CLI-AUTODISPATCH
Feature: Run and monitor autodispatch from the command line

  @id:F-AUTODISPATCH-S001
  Scenario: An autodispatch session continues until no work is ready
    Given an autodispatch server with two ready issues
    When the user runs `workdash autodispatch owner/repo --group project`
    Then `workdash info` shows the first issue developing in group `project`
    When the first agent pane exits
    Then `workdash info` shows the first issue being reviewed
    When the review completes
    Then `workdash info` shows the second issue developing in group `project`
    When the second agent pane exits
    Then `workdash info` shows the session has finished

  @id:F-AUTODISPATCH-S002
  Scenario: Independent sessions can be monitored and stopped
    Given an autodispatch server with two independent running sessions
    When the user runs `workdash info` to inspect autodispatch sessions
    Then both sessions show their group, scope and current task
    When the user runs `workdash autodispatch-stop` with one session's ID
    Then its agent pane remains open and the other session continues

  @id:F-AUTODISPATCH-S014
  Scenario: A failed session remains visible until restarted
    Given an autodispatch session has failed
    When the user runs `workdash info` to inspect autodispatch sessions
    Then the failed session and its error are visible
    When the user resolves the failure and runs `workdash autodispatch` for the same scope
    Then `workdash info` shows the replacement instead of the failure

  @id:F-AUTODISPATCH-S015
  Scenario: Starting an exact issue takes over from its running coding agent
    Given a coding agent is already working on ready issue `owner/repo#ISSUE-1`
    When the user runs `workdash autodispatch owner/repo#ISSUE-1`
    Then the existing coding pane closes and autodispatch takes over the issue

  @id:F-AUTODISPATCH-S018
  Scenario: Replace a running session to use a different group
    Given an autodispatch session is running for `owner/repo`
    When the user runs `workdash autodispatch owner/repo --group project` again
    Then `workdash info` shows only the replacement session in group `project`

  @id:F-AUTODISPATCH-S026
  Scenario: Opt into merging completed work once its reviewer approves
    Given ready work in `owner/repo` whose pull request will need a re-review
    When the user runs `workdash autodispatch owner/repo --automerge`
    And the completed pull request has been reviewed by Workdash
    Then the session waits without merging or dispatching again
    When the reviewer approves the pull request
    Then the pull request is merged
