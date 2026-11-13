@feature:F-AUTODISPATCH
Feature: Automatically dispatch ready work

  An autodispatch session dispatches one ready issue at a time. By default each
  task uses its implementation repository's group; an explicit group overrides it.
  After each agent pane disappears, the session reviews the pull request selected
  for that dispatch if it remains open, or a currently linked open pull request
  otherwise, before selecting more ready work. A pane counts as gone only once two
  consecutive checks miss it, so a listing that omits a live pane does not end
  supervision. It finishes when no ready work remains. A user can stop a session
  without closing its agent pane. Autodispatch does not merge work.
  A tenth consecutive launch of the same issue fails without another launch;
  work claimed by another agent is skipped rather than counted. A target has at most
  one session: starting a session for a target that already has one stops and replaces
  it, whether that session was running or failed, even when the target is referenced in
  another form, including a targeted todo ID and its underlying todo issue URL. A failed
  session stays visible in progress with its error collapsed onto a single truncated
  line, while `workdash info` reports the full error. Starting a session for an exact issue
  that still has ready work closes any coding pane already working on that issue
  before dispatching, interrupting that agent; the rare race between its stop and the
  new launch is accepted. A scope without ready work leaves the coding pane working on
  it untouched, because nothing would replace it.
  The dashboard shows the selected group (or automatic until known), scope,
  current issue, stage, launch reason during development, and iteration with elapsed
  time. While checking for another task, it retains the last selected group until
  the next task selects a group. Issue URLs are shortened, and an issue shown as both scope and current
  item appears only once, even when the issue number has leading zeroes; different scope and current item are labeled.

  Rules:
    - Pressing "d" on an issue starts an autodispatch session scoped to that issue.
    - Pressing "d" on an authored pull request scopes the session to a linked issue, regardless of its repository or readiness. If there is no linked issue, the TUI reports an error.
    - A pull request that closes several issues is dispatched as the lowest one by repository name, then by issue number, which is the issue coding the same pull request would code.
    - When GitHub cannot report a pull request's linked issues, the TUI reports that lookup failure instead of claiming the pull request has no linked issue.
    - Pressing "d" on a pull request that is not authored by you reports that dispatch is unavailable and starts no session.
    - The dashboard shows running sessions and the three most recently started failed sessions, with failure summaries kept to one line each; full details remain available through info.

  @id:F-AUTODISPATCH-S013
  Scenario: Start autodispatch from an issue in the dashboard
    Given a ready issue selected in the dashboard
    When the user presses d to start autodispatch
    Then an autodispatch session launches that issue

  @id:F-AUTODISPATCH-S009
  Scenario: Dashboard chooses an agent that can both code and review
    Given a ready issue selected in the dashboard
    And the default coding agent cannot analyze but another agent can code and analyze
    When the user presses d to start autodispatch
    Then an autodispatch session launches that issue with the compatible agent

  @id:F-AUTODISPATCH-S005
  Scenario: Start autodispatch from an authored pull request linked to an issue in another repository
    Given an authored pull request selected in the dashboard linked only to an issue in another repository
    When the user presses d to start autodispatch
    Then an autodispatch session launches that linked issue

  @id:F-AUTODISPATCH-S008
  Scenario: An authored pull request without a linked issue cannot start a session
    Given an authored pull request without a linked issue in the dashboard
    When the user presses d to start autodispatch
    Then the dashboard reports that the pull request has no linked issue and starts no session

  @id:F-AUTODISPATCH-S010
  Scenario: A pull request closing several issues is dispatched as the lowest one
    Given an authored pull request selected in the dashboard linked to issues in several repositories
    When the user presses d to start autodispatch
    Then an autodispatch session launches the lowest linked issue

  @id:F-AUTODISPATCH-S011
  Scenario: A linked issue lookup failure is reported instead of a missing linked issue
    Given an authored pull request selected in the dashboard whose linked issues GitHub denies
    When the user presses d to start autodispatch
    Then the dashboard reports the linked issue lookup failure and starts no session

  @id:F-AUTODISPATCH-S012
  Scenario Outline: Dispatch is unavailable for a pull request that is not authored by you
    Given a pull request of somebody else selected in the dashboard that is <kind>
    When the user presses d to start autodispatch
    Then the dashboard reports that dispatch is unavailable and starts no session

    Examples:
      | kind                    |
      | waiting for my review   |
      | in a tracked repository |

  @id:F-AUTODISPATCH-S007
  Scenario: Start a session for a linked issue that is not ready
    Given an authored pull request linked to an issue that is not ready
    When the user presses d to start autodispatch
    Then an autodispatch session starts for the linked issue

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

  @id:F-AUTODISPATCH-S006
  Scenario: A failed autodispatch session remains visible in the dashboard
    Given a ready issue selected in the dashboard
    When the user's autodispatch session fails
    Then the dashboard shows the failed session and its error

  @id:F-AUTODISPATCH-S014
  Scenario: A failed session remains visible until restarted
    Given an autodispatch session has failed
    When the user inspects autodispatch progress
    Then the failure is visible in autodispatch progress
    When the user restarts autodispatch for the same scope
    Then the old failure is no longer shown

  @id:F-AUTODISPATCH-S015
  Scenario: Starting an exact issue closes the coding agent already working on it
    Given a coding agent is already running for issue `owner/repo#ISSUE-1`
    And issue `owner/repo#ISSUE-1` has ready work
    When the user starts autodispatch for issue `owner/repo#ISSUE-1`
    Then that coding agent pane is closed before the issue is dispatched

  @id:F-AUTODISPATCH-S016
  Scenario: Starting an exact issue without ready work leaves its agent running
    Given a coding agent is already running for issue `owner/repo#ISSUE-1`
    And issue `owner/repo#ISSUE-1` has no ready work
    When the user starts autodispatch for issue `owner/repo#ISSUE-1`
    Then that coding agent pane stays open

  @id:F-AUTODISPATCH-S017
  Scenario: Starting a repository scope leaves coding panes running
    Given a coding agent is already running for issue `owner/repo#ISSUE-1`
    And issue `owner/repo#ISSUE-1` has ready work
    When the user starts autodispatch for repository `owner/repo`
    Then that coding agent pane stays open

  @id:F-AUTODISPATCH-S018
  Scenario: Starting a target that is already running replaces its session
    Given an autodispatch session is running for `owner/repo`
    When the user starts autodispatch again for `owner/repo`
    Then the previous session is stopped and replaced
