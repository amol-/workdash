@feature:F-AUTODISPATCH
Feature: Automatically dispatch ready work

  An autodispatch session dispatches one ready issue at a time. By default each
  task uses its implementation repository's group; an explicit group overrides it.
  After each agent pane disappears, the session reviews the pull request selected
  for that dispatch if it remains open, or a currently linked open pull request
  otherwise, before selecting more ready work. It finishes when no ready work remains. A user can stop a session without
  closing its agent pane. Autodispatch does not merge work.
  A tenth consecutive launch of the same issue fails without another launch;
  work claimed by another agent is skipped rather than counted.
  The dashboard shows the selected group (or automatic until known), scope,
  current issue, stage, launch reason during development, and iteration with elapsed
  time. While checking for another task, it retains the last selected group until
  the next task selects a group. Issue URLs are shortened, and an issue shown as both scope and current
  item appears only once, even when the issue number has leading zeroes; different scope and current item are labeled.

  @id:F-AUTODISPATCH-S001
  Scenario: An autodispatch session continues until no work is ready
    Given an autodispatch server with two ready issues
    When the user starts autodispatch in group `project`
    Then the first issue is dispatched in group `project`
    When its agent pane disappears
    Then the first issue is reviewed before the next dispatch
    And the second issue is dispatched in group `project`
    When its agent pane disappears
    Then the autodispatch session finishes after no ready work remains

  @id:F-AUTODISPATCH-S004
  Scenario: The dashboard shows compact progress for an issue-scoped session
    Given an autodispatch session for a targeted todo issue URL in group `project`
    When the user views its progress while checking and developing
    Then the dashboard shows the group, stage, iteration and launch reason without repeating the issue

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
