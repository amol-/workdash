@feature:F-CODING-AUTOCLOSE
Feature: Supervise an autoclosed coding pane

  A user who asks for autoclose leaves a newly launched coding agent in its own
  interactive pane and lets the server decide, from the pane's output and the
  work's GitHub state, when to nudge it and when to close it.

  Rules:
    - `workdash code ITEM --autoclose` launches an ordinary interactive agent and asks the server to supervise that new pane, about once a minute.
    - Autoclose needs autostart: the CLI always autostarts, the TUI never asks for autoclose, and a request for autoclose without autostart is rejected.
    - Supervision never takes over a pane that `code` did not just launch, so `already_running` enables nothing.
    - Only the server supervises: the CLI is a thin client with no polling of its own, and every GitHub and Zellij call happens in the server process.
    - A pane is not judged while its output keeps changing.
    - Fifteen minutes without new output and without a Workdash message make the pane a candidate for a decision; every message Workdash sends restarts that wait.
    - The decision uses the same GitHub work classification as `ready`, and the message is the instruction that classification renders.
    - A pull request merged for the supervised work closes its pane on the next check, even while the agent is still writing; one that already merged before the pane was launched is not the work it was launched for, so the pane is left alone.
    - Finished work (current-head CI passing or verifiably absent, clean mergeability, no outstanding feedback, no local work GitHub does not have: an edited file, a new file the agent never added, or an unpushed commit) closes its pane after the wait, rechecked immediately before closing, and the reason is logged when unpublished work keeps the pane open. Only the worktree's `.workdash/` artifact directory is left out of that check.
    - A known pull request is cheap to check, so its state is read on every check; finding the pull request an issue now has and deciding what an idle pane needs cost a whole work state, so each happens at most every fifth check, and a pull request that opens and merges between two of them is found at the next one.
    - Pending, unknown, or unavailable GitHub answers never close a pane and never report success; failed lookups back off instead of messaging.
    - A pull request closed without merging is neither completion nor work to redo on its own: supervision leaves the pane open, stops watching it, and reports it as needing review.
    - A pane whose identity stops matching is dropped from supervision for good: the action is abandoned and it is never messaged or closed again.
    - Somebody else's pull request stays a review: its pane is never messaged and closes only once that pull request merges.
    - An unchanged GitHub state is never messaged twice; a new head, check result, review, base head, or mergeability is a new state.
    - An instruction whose text reached the pane is never typed twice: a failed Enter is retried on its own.
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
    When 15 minutes pass
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
    When 15 minutes pass
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
    And a minute passes
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S006
  Scenario: A supervised pane is judged only after fifteen idle minutes
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 14 minutes pass
    Then nothing is sent to the supervised pane
    When another minute passes
    Then the supervised pane receives the `fix_ci` instruction

  @id:F-CODING-AUTOCLOSE-S007
  Scenario: An unchanged GitHub state is never messaged twice
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 15 minutes pass
    Then the supervised pane receives the `fix_ci` instruction
    When 15 minutes pass
    Then nothing more is sent to the supervised pane

  @id:F-CODING-AUTOCLOSE-S008
  Scenario: New review feedback brings a new instruction
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 15 minutes pass
    Then the supervised pane receives the `fix_ci` instruction
    When a reviewer requests changes on pull request `owner/repo#51`
    And 15 minutes pass
    Then the supervised pane receives the `address_review` instruction

  @id:F-CODING-AUTOCLOSE-S009
  Scenario: New output postpones the instruction instead of interrupting the agent
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 14 minutes pass
    And the supervised pane prints new output
    And a minute passes
    Then nothing is sent to the supervised pane
    When 15 minutes pass
    Then the supervised pane receives the `fix_ci` instruction

  @id:F-CODING-AUTOCLOSE-S010
  Scenario: Finished work closes its pane after the wait
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has passing CI, no review feedback, and merges cleanly
    When 15 minutes pass
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S011
  Scenario: Unpublished local work keeps the pane open
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has passing CI, no review feedback, and merges cleanly
    And the supervised worktree has uncommitted changes
    When 15 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
    And the server reports the pane as waiting for unpublished work

  @id:F-CODING-AUTOCLOSE-S012
  Scenario Outline: An unfinished pull request never closes its pane
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` is <state>
    When 15 minutes pass
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
    When 15 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
    When the other program gives the pane back
    And 15 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed

  @id:F-CODING-AUTOCLOSE-S014
  Scenario: A pull request closed without merging is reported instead of reworked
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` is closed without being merged
    When 15 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
    And the server reports the pane as needing review

  @id:F-CODING-AUTOCLOSE-S015
  Scenario: A pull request with merge conflicts is told to resolve them
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has merge conflicts with its base branch
    When 15 minutes pass
    Then the supervised pane receives the `fix_merge_conflicts` instruction

  @id:F-CODING-AUTOCLOSE-S016
  Scenario: Somebody else's pull request is only closed once it merges
    Given a server-backed Workdash session is ready to answer
    And an open pull request `owner/repo#52` by somebody else with a failing check `tests`
    And a supervised coding pane for pull request `owner/repo#CHECK-52`
    When 15 minutes pass
    Then nothing is sent to the supervised pane
    When pull request `owner/repo#52` merges
    And a minute passes
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S017
  Scenario: A pull request without checks closes its pane after the wait
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has no checks, no review feedback, and merges cleanly
    When 15 minutes pass
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S018
  Scenario: A moved base branch or an unknown mergeability is a new state
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` is open
    And pull request `owner/repo#51` has a failing check `tests` on its current head
    When 15 minutes pass
    Then the supervised pane receives the `fix_ci` instruction
    When the base branch of pull request `owner/repo#51` moves
    And 15 minutes pass
    Then the supervised pane receives the `fix_ci` instruction again
    When pull request `owner/repo#51` is reporting unknown mergeability
    And 15 minutes pass
    Then the supervised pane receives the `fix_ci` instruction again
    When pull request `owner/repo#51` has merge conflicts with its base branch
    And 15 minutes pass
    Then the supervised pane receives the `fix_merge_conflicts` instruction

  @id:F-CODING-AUTOCLOSE-S019
  Scenario: Work with no pull request yet is told to implement once
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` with no pull request yet
    When 15 minutes pass
    Then the supervised pane receives the `implement` instruction
    When 15 minutes pass
    Then nothing more is sent to the supervised pane
    And no pane is closed

  @id:F-CODING-AUTOCLOSE-S020
  Scenario: Work that opens and merges between two discovery checks closes its pane
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` with no pull request yet
    When pull request `owner/repo#51` opens and merges for issue `owner/repo#42`
    And 6 minutes pass
    Then the supervised pane is closed
    And nothing is sent to the supervised pane

  @id:F-CODING-AUTOCLOSE-S021
  Scenario: A pull request that merged before the launch leaves a new pane alone
    Given a server-backed Workdash session is ready to answer
    And a supervised coding pane for issue `owner/repo#42` whose pull request `owner/repo#51` merged before the launch
    When 15 minutes pass
    Then nothing is sent to the supervised pane
    And no pane is closed
