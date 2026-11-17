@feature:F-AUTODISPATCH
Feature: Automatically dispatch ready work

  An autodispatch session dispatches one ready issue at a time. By default each
  task uses its implementation repository's group; an explicit group overrides it.
  After each agent pane disappears, the session reviews the pull request selected
  for that dispatch if it remains open, or a currently linked open pull request
  otherwise, before selecting more ready work. A pane counts as gone only once two
  consecutive checks miss it, so a listing that omits a live pane does not end
  supervision. It finishes when no ready work remains. A user can stop a session
  without closing its agent pane. If a review is already running when stopped,
  the dashboard refreshes the issue again after that review ends, including on failure.
  If an in-progress dispatch launches after stopping, the dashboard refreshes its issue.
  Autodispatch leaves pull requests open by default; opt-in automerge merges a
  pull request directly after an explicitly successful review; failed or unconfirmed
  review postings fail the session and prevent merging, without scheduling a merge or
  enabling GitHub auto-merge. It reviews the current pull request head and fails
  safely if the review checkout has unpublished work or does not match it before or
  after review; Workdash artifacts do not count as unpublished work. It merges only the unchanged reviewed
  head when feedback is resolved, checks pass or are absent, and GitHub reports it mergeable.
  If the complete review feedback cannot be verified, or any review thread remains unresolved,
  the session fails instead of merging unless actionable feedback is dispatched for repair.
  Actionable feedback, conflicts or failing checks are dispatched for repair;
  while a requested re-review, checks or mergeability are pending or unknown, the
  session remains active and rechecks until it can safely merge or dispatch repairs.
  Stopping or disabling automerge ends this wait. A review without a known starting head
  is followed by a fresh review if automerge was enabled during it. If the head
  changes during review or before the merge, including a force-push, the new head is
  reviewed before merging, with a bounded number of attempts. Existing local commits
  and files are preserved when reviewing the new head. Unpublished work produced during a
  retry review is preserved, and the failure reports its checkout path for recovery.
  If merging fails because the pull request
  has conflicts, the session continues and selects ready work again; other merge
  failures, including permission errors on conflicting pull requests, fail the session. If a merge finishes after stopping, the dashboard
  refreshes the issue again, including on failure.
  A selected row with a running session displays merge control instead of dispatch;
  pressing d still starts or replaces the session for the selected target.
  When several sessions match a row, merge control refuses to choose one; the user can
  inspect and stop sessions by ID until only the intended session remains.
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
  Human-readable `workdash info` shows the same progress alongside session status and full failure details.
  JSON and the control API retain the underlying progress fields.

  Rules:
    - Pressing "d" on an issue starts an autodispatch session scoped to that issue, replacing a session for the same target. For selected work with a running session, "m" controls automerge and is displayed instead of "d".
    - Pressing "d" on an authored pull request scopes the session to a linked issue, regardless of its repository or readiness. If there is no linked issue, the TUI reports an error.
    - A pull request that closes several issues is dispatched as the lowest one by repository name, then by issue number, which is the issue coding the same pull request would code.
    - When GitHub cannot report a pull request's linked issues, the TUI reports that lookup failure instead of claiming the pull request has no linked issue.
    - Pressing "d" on a pull request that is not authored by you reports that dispatch is unavailable and starts no session.
    - After three reminders and a further idle window without a pull request, a session remains blocked and visible in the dashboard and info. Pane activity postpones the next reminder or block but does not reset the reminder limit. A change in blocked pane content or successful input through Workdash immediately resumes supervision with a fresh limit, even if the screen stays unchanged or GitHub is temporarily unavailable. If a later pull request merges, the pane closes and the session continues to the next ready task.
    - The selected progress entry is marked with ">" only while progress has focus; the marker is hidden while a dialog is open. Leaving and returning to progress preserves the selection.
    - With progress focused, the command hint reflects the selected progress session: running sessions offer their automerge state, blocked sessions offer stop, and failed sessions offer neither. Stopping closes its pane without changing the worktree. Starting another session for the same scope also closes the blocked pane before replacing the session. If the pane content has changed before either action closes it, the session still stops or is replaced, but that action leaves the recovered pane open. An exact-issue restart can still close it to dispatch ready work.
    - The dashboard shows running and blocked sessions, the three most recently started failed sessions, and the three most recently started sessions that had no work at their first check. Their outcomes remain visible as other sessions finish; failure summaries are kept to one line each and full details remain available through info. Empty-start and failure status messages and progress rows lead with the outcome and reason so they remain visible on narrow screens, and identify the session's scope when space permits.
    - When an autodispatch refresh updates a pull request row, its current linked issues determine later refreshes and worktree selection.

  @id:F-AUTODISPATCH-S020
  Scenario: A blocked session is visible and resumes when its pane changes
    Given an autodispatch session has blocked after three reminders without a pull request
    Then the blocked session is visible in info and on the dashboard
    When its agent pane content changes
    Then the session resumes running

  @id:F-AUTODISPATCH-S022
  Scenario: Input through Workdash resumes a blocked session
    Given an autodispatch session has blocked after three reminders without a pull request
    When the user sends input to its agent pane through Workdash
    Then the session resumes running

  @id:F-AUTODISPATCH-S021
  Scenario: Stop a blocked session from the dashboard
    Given an autodispatch session has blocked after three reminders without a pull request
    When the user stops the blocked session from the dashboard
    Then its agent pane closes and its worktree is preserved

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
    And the dashboard reports that no work is available to dispatch

  @id:F-AUTODISPATCH-S027
  Scenario: Report when an issue has no ready work because of a dependency
    Given an issue blocked by an open dependency selected in the dashboard
    When the user presses d to start autodispatch
    Then an autodispatch session starts for the selected issue
    And the dashboard reports that no work is available to dispatch

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

  @id:F-AUTODISPATCH-S019
  Scenario: Info reports autodispatch progress
    Given an autodispatch session developing an issue in a selected group
    When the user runs workdash info for that session
    Then info shows the group, scope, current item, stage, launch reason, iteration and elapsed time

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

  @id:F-AUTODISPATCH-S020
  Scenario: Opt into merging completed work
    Given an autodispatch session with automerge enabled
    When its completed pull request has been reviewed successfully
    Then the pull request is merged

  @id:F-AUTODISPATCH-S025
  Scenario: A review finding cannot be posted
    Given an autodispatch session with automerge enabled
    When the review cannot post a major finding
    Then the session fails without merging the pull request

  @id:F-AUTODISPATCH-S026
  Scenario: Wait for a reviewer who was asked to review again
    Given an autodispatch session with automerge enabled
    When a reviewer has requested changes and has been asked to review again
    Then the session waits without merging or dispatching again
    When the reviewer approves the pull request
    Then the pull request is merged

  @id:F-AUTODISPATCH-S024
  Scenario: Review changed work before merging
    Given an autodispatch session with automerge enabled
    When the pull request changes during review
    Then the new head is reviewed before the pull request is merged

  @id:F-AUTODISPATCH-S021
  Scenario: Toggle automerge from the dashboard
    Given an autodispatch session for the selected dashboard work
    When the user toggles automerge
    Then the dashboard reports and displays the new automerge state

  @id:F-AUTODISPATCH-S028
  Scenario: Toggle automerge for the selected progress entry
    Given the dashboard has different sessions selected in the work table and progress
    When the user presses m with progress focused
    Then only the selected progress session changes and the dashboard identifies it
    And the command hint shows the selected session's automerge state

  @id:F-AUTODISPATCH-S022
  Scenario Outline: The dashboard toggle controls merging completed work
    Given an autodispatch session for the selected dashboard work
    When the user toggles automerge <times>
    And its completed pull request has been reviewed successfully
    Then after toggling, the pull request is <outcome>

    Examples:
      | times | outcome    |
      | once  | merged     |
      | twice | left open  |
