@feature:F-AUTODISPATCH
Feature: Automatically dispatch ready work

  A server-owned session dispatches one ready issue at a time. By default each
  task uses its implementation repository's group; an explicit group overrides it.
  It checks about every 30 seconds for each launched pane to disappear before
  dispatching again, and finishes when dispatch finds no ready work. Stopping
  a session does not wait for the next check. It does not merge work.

  @id:F-AUTODISPATCH-S001
  Scenario: An autodispatch session continues until no work is ready
    Given an autodispatch server with two ready issues
    When the user starts autodispatch in group `project`
    Then the first issue is dispatched in group `project`
    When its agent pane disappears
    Then the second issue is dispatched in group `project`
    When its agent pane disappears
    Then the autodispatch session finishes after no ready work remains

  @id:F-AUTODISPATCH-S003
  Scenario: An autodispatch session uses the implementation repository groups by default
    Given an autodispatch server with ready issues in different implementation repositories
    When the user starts autodispatch without a group
    Then each issue is dispatched in its implementation repository's group

  @id:F-AUTODISPATCH-S002
  Scenario: Independent sessions can be monitored and stopped
    Given an autodispatch server with two independent running sessions
    When the user inspects autodispatch progress
    Then both sessions show their group, target, current task and elapsed time
    When the user stops one autodispatch session
    Then its agent pane remains open and the other session continues
