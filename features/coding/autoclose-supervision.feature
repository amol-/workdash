@feature:F-CODING-AUTOCLOSE
Feature: Supervise an autoclosed coding pane

  A user who asks for autoclose leaves a newly launched coding agent in its own
  interactive pane and lets the server decide, from the pane's output and the
  work's GitHub state, when to nudge it and when to close it.

  Rules:
    - `workdash code ITEM --autoclose` launches an ordinary interactive agent and asks the server to supervise that new pane, about every 30 seconds.
    - Autoclose needs autostart: the CLI always autostarts, the TUI never asks for autoclose, and a request for autoclose without autostart is rejected.
    - Supervision never takes over a pane that `code` did not just launch, so `already_running` enables nothing.
    - A pane is not judged while its output keeps changing, including output during a GitHub lookup before a blocked decision.
    - Five minutes without new output and without a Workdash message make the pane a candidate for a decision; every message Workdash sends restarts that wait.
    - The decision uses the same GitHub work classification as `ready`; an actionable message includes the autonomous publishing rules and tells the agent to make reasonable decisions guided by the work goal to unblock progress.
    - A pull request merged for the supervised work closes its pane on the next check, even while the agent is still writing or a detailed work lookup is unavailable. A pull request merged before launch does not finish a reopened issue; a pane explicitly targeting that merged pull request is left alone. A merge during pane startup closes the pane only when GitHub's timestamp establishes that it happened after launch; a merge in the same second as launch is ambiguous and leaves the pane open.
    - Finished work (current-head CI passing or verifiably absent, clean mergeability, no outstanding feedback, no local work GitHub does not have: an edited file, a new file the agent never added, or an unpushed commit) closes its pane after the wait, rechecked immediately before closing, and the reason is logged when unpublished work keeps the pane open. Only the worktree's `.workdash/` artifact directory is left out of that check.
    - A pull request that opens and merges between checks is found within about five minutes and its pane is closed.
    - Pending, unknown, or unavailable GitHub answers never close a pane and never report success; failed lookups back off instead of messaging.
    - A pull request closed without merging is neither completion nor work to redo on its own: supervision leaves the pane open, stops watching it, and reports it as needing review.
    - A pane whose identity stops matching is dropped from supervision for good: the action is abandoned and it is never messaged or closed again. Other Zellij panes do not interrupt supervision of the launched agent.
    - Somebody else's pull request stays a review: its pane is never messaged and closes only once that pull request merges. An issue with somebody else's open linked pull request does not receive no-pull-request reminders or become blocked for lack of a pull request.
    - Except when no pull request exists, an unchanged GitHub state is never messaged twice; a new head, check result, review, base head, or mergeability is a new state.
    - For work without a pull request, any change in pane content postpones the next reminder or block, regardless of what caused the change. Only recovery from a blocked pane resets the reminder count. A blocked pane is still closed when its pull request merges.
    - A failed Enter for instruction text that reached the pane is retried on its own, without typing the text again.
    - Supervising a pane that keeps failing its checks reports the stall once, and a stall after a recovery is reported again.
    - Supervision is a heuristic: a silent long-running command or a pending question looks exactly like a finished agent.
    - Supervision lives in the server process and does not survive a server restart.

  @id:F-CODING-AUTOCLOSE-S001
  Scenario: Code with autoclose supervises the pane it launched
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When a client requests autoclosed code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the response reports the new agent pane as `launched`
    And the response reports monitoring `enabled`

  @id:F-CODING-AUTOCLOSE-S002
  Scenario: Code without autoclose enables no monitoring
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the response reports monitoring `off`
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed

  @id:F-CODING-AUTOCLOSE-S003
  Scenario: An agent pane that was already running is never taken over
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    And the worktree of `owner/repo#ISSUE-42` already exists
    And an agent pane is running in that worktree
    When a client requests autoclosed code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the response reports status `already_running`
    And the response reports monitoring `off`
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed

  @id:F-CODING-AUTOCLOSE-S004
  Scenario: Autoclose without autostart is rejected
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When a client requests autoclosed code with autostart `false` for `owner/repo#42` with agent `pi`
    Then the API returns error `bad_request` with HTTP status 400
    And no coding agent is launched

  @id:F-CODING-AUTOCLOSE-S005
  Scenario: A merged pull request closes its pane without waiting for idleness
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And the supervised pane keeps printing new output
    When pull request `owner/repo#51` merges
    And 30 seconds pass
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S006
  Scenario: A supervised pane is judged only after five idle minutes
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 4 minutes pass
    Then nothing is sent to the supervised pane
    When another minute passes
    Then the supervised pane receives the `fix_ci` instruction
    And the instruction permits publishing
    And the instruction asks the agent to decide how to unblock the work using its goal

  @id:F-CODING-AUTOCLOSE-S007
  Scenario: An unchanged GitHub state is never messaged twice
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 5 minutes pass
    Then the supervised pane receives the `fix_ci` instruction
    When 5 minutes pass
    Then nothing more is sent to the supervised pane

  @id:F-CODING-AUTOCLOSE-S008
  Scenario: New review feedback brings a new instruction
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 5 minutes pass
    Then the supervised pane receives the `fix_ci` instruction
    When a reviewer requests changes on pull request `owner/repo#51`
    And 5 minutes pass
    Then the supervised pane receives the `address_review` instruction

  @id:F-CODING-AUTOCLOSE-S009
  Scenario: New output postpones the instruction instead of interrupting the agent
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 4 minutes pass
    And the supervised pane prints new output
    And a minute passes
    Then nothing is sent to the supervised pane
    When 5 minutes pass
    Then the supervised pane receives the `fix_ci` instruction

  @id:F-CODING-AUTOCLOSE-S010
  Scenario: Finished work closes its pane after the wait
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has passing CI, no review feedback, and merges cleanly
    When 5 minutes pass
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S011
  Scenario: Unpublished local work keeps the pane open
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has passing CI, no review feedback, and merges cleanly
    And the supervised worktree has uncommitted changes
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
    And the server reports the pane as waiting for unpublished work

  @id:F-CODING-AUTOCLOSE-S012
  Scenario Outline: An unfinished pull request never closes its pane
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` is <state>
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed

    Examples:
      | state                                  |
      | waiting on running checks              |
      | reporting unknown mergeability         |

  @id:F-CODING-AUTOCLOSE-S013
  Scenario: A pane that no longer matches its registered identity is dropped for good
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    And another program takes over the supervised pane
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
    When the other program gives the pane back
    And 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed

  @id:F-CODING-AUTOCLOSE-S014
  Scenario: A pull request closed without merging is reported instead of reworked
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` is closed without being merged
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
    And the server reports the pane as needing review

  @id:F-CODING-AUTOCLOSE-S015
  Scenario: A pull request with merge conflicts is told to resolve them
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has merge conflicts with its base branch
    When 5 minutes pass
    Then the supervised pane receives the `fix_merge_conflicts` instruction

  @id:F-CODING-AUTOCLOSE-S016
  Scenario: Somebody else's pull request is only closed once it merges
    Given a server-backed Workdash session is ready to answer
    And an open pull request `owner/repo#52` by somebody else with a failing check `tests`
    And a supervised coding pane for pull request `owner/repo#CHECK-52`
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    When pull request `owner/repo#52` merges
    And a minute passes
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S017
  Scenario: A pull request without checks closes its pane after the wait
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has no checks, no review feedback, and merges cleanly
    When 5 minutes pass
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S018
  Scenario: A moved base branch or an unknown mergeability is a new state
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 5 minutes pass
    Then the supervised pane receives the `fix_ci` instruction
    When the base branch of pull request `owner/repo#51` moves
    And 5 minutes pass
    Then the supervised pane receives the `fix_ci` instruction again
    When pull request `owner/repo#51` is reporting unknown mergeability
    And 5 minutes pass
    Then the supervised pane receives the `fix_ci` instruction again
    When pull request `owner/repo#51` has merge conflicts with its base branch
    And 5 minutes pass
    Then the supervised pane receives the `fix_merge_conflicts` instruction

  @id:F-CODING-AUTOCLOSE-S019
  Scenario: Work with no pull request yet is reminded three times
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` with no pull request yet
    When 5 minutes pass
    Then the supervised pane receives the `implement` instruction
    And the instruction asks the agent to decide how to unblock the work using its goal
    When 5 minutes pass
    Then the supervised pane receives the `implement` instruction again
    When 5 minutes pass
    Then the supervised pane receives the `implement` instruction again
    When 5 minutes pass
    Then nothing more is sent to the supervised pane
    And the supervised pane is blocked
    And no pane is closed
    When the user sends instructions to the supervised pane
    And a minute passes
    Then the supervised pane is no longer blocked
    And nothing more is sent to the supervised pane
    When 4 minutes pass
    Then nothing more is sent to the supervised pane
    When another minute passes
    Then the supervised pane receives the `implement` instruction again

  @id:F-CODING-AUTOCLOSE-S022
  Scenario: Activity between reminders postpones blocking but does not renew the reminder limit
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` with no pull request yet
    When 5 minutes pass
    Then the supervised pane receives the `implement` instruction
    When the supervised pane prints new output
    And a minute passes
    And 5 minutes pass
    Then the supervised pane receives the `implement` instruction again
    When 5 minutes pass
    Then the supervised pane receives the `implement` instruction again
    When 5 minutes pass
    Then nothing more is sent to the supervised pane
    And the supervised pane is blocked

  @id:F-CODING-AUTOCLOSE-S020
  Scenario: Work that opens and merges between two discovery checks closes its pane
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` with no pull request yet
    When pull request `owner/repo#51` opens and merges for issue `owner/repo#42`
    And 6 minutes pass
    Then the supervised pane is closed
    And nothing is sent to the supervised pane

  @id:F-CODING-AUTOCLOSE-S021
  Scenario: A reopened issue with an old merged pull request receives reminders
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` merged before the launch
    When 5 minutes pass
    Then the supervised pane receives the `implement` instruction
    And no pane is closed

  @id:F-CODING-AUTOCLOSE-S024
  Scenario: A reopened issue's new pull request merges after launch
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` merged before the launch
    When pull request `owner/repo#52` opens and merges for issue `owner/repo#42`
    And 6 minutes pass
    Then the supervised pane is closed
    And nothing is sent to the supervised pane

  @id:F-CODING-AUTOCLOSE-S023
  Scenario: A pull request explicitly targeted after it merged leaves its pane alone
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for pull request `owner/repo#PR-51` that merged before the launch
    When 5 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
