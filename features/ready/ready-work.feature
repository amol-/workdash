@feature:F-READY
Feature: Ready work

  Agents and users ask the Workdash server which GitHub work can be picked up
  next: open issues ready to implement or whose authored pull requests need
  new work. The answer comes from GitHub and
  the live Zellij panes, never from the dashboard list.

  Rules:
    - `workdash ready SCOPE` requires a running `workdash --server`; like the other server-backed commands it accepts `--host HOST` and `--json`, and it never reads configuration, runs `gh`, or inspects Zellij or worktrees on the client.
    - SCOPE is required: `owner/repo`, an issue (`owner/repo#42`, `owner/repo#ISSUE-42`, a targeted todo ID such as `owner/repo#ISSUE-WT7`, or a GitHub issue URL), `todo`, or `any`.
    - The server resolves `todo` and `any` from its own configuration: `todo` is the todo repository, `any` is every repository selected by `repositories`, with `owner/*` expanded to every unarchived repository of that owner, plus the todo repository.
    - A repository scope covers every open issue of the repository, including linked open pull requests in other repositories, and issues in other repositories linked to its actionable authored pull requests, however many the dashboard lists.
    - An issue scope covers the issue and its native sub-issue descendants recursively across repositories. A closed issue can scope open descendants. An open issue is considered only when it has no open descendants.
    - An issue is ready when it is open, has no open descendants or open `blocked by` issue, and no live Workdash agent pane works in its worktree. With no open linked pull request its action is `implement`; with an authored open linked pull request it is ready only if that pull request needs work. An issue with an open pull request by somebody else is not ready.
    - An authored linked pull request needs work when the server can push to its head branch and its current head needs work for at least one reason:
        - `address_review`: actionable review feedback on the current head.
        - `fix_merge_conflicts`: GitHub confirms the pull request conflicts with its current base branch.
        - `fix_ci`: a check or status on the current head failed.
    - An issue with an actionable authored pull request lists every reason, ordered `address_review`, `fix_merge_conflicts`, `fix_ci`; the first one is its `action`. An issue without a pull request has action `implement`.
    - Actionable review feedback is any of these on the current head:
        - a changes-requested verdict a reviewer gave on the current head commit and has not superseded;
        - a review body, an unresolved and not outdated review thread, or a pull request comment that a human other than the pull request author and the server's GitHub identity posted after the current head was pushed. A thread is judged by its latest comment that is not from a bot.
    - Settled feedback is not actionable: a review thread is settled by a later reply in it from the pull request author or the server's GitHub identity, a pull request comment by a later comment of either of them in the conversation, and a review body or pull request comment by a later approval of the current head from the same person.
    - Bot comments, comments of the pull request author or the server's GitHub identity, hidden comments, dismissed reviews, resolved or outdated threads, and feedback older than the current head are never actionable.
    - The CI status of the current head comes from GitHub metadata only: its checks and statuses, and the required checks of the base branch rulesets and branch protection. Repository files are never inspected.
        - `failing`: a check or status on the current head failed, was cancelled, timed out, or needs action.
        - `pending`: nothing failed and something is queued or running.
        - `passing`: everything finished successfully and every required check reported.
        - `no_checks`: GitHub reports no check or status on the current head and the base branch requires none.
        - `unknown`: anything else, including a required-checks lookup that failed and a required check that has not reported.
    - Mergeability is `conflicting` only when GitHub reports a conflict with the current base branch, `mergeable` when GitHub reports none, and `unknown` while GitHub has not computed it; `unknown` never creates `fix_merge_conflicts` work.
    - Issues with actionable authored pull requests are listed before unstarted issues, ordered by pull request creation time with stable pull request ties; unstarted issues are oldest first with stable ties.
    - Each issue is listed once, however many scopes or selectors include it; pull requests are never listed as ready items. When one actionable pull request links multiple eligible issues in the scope, its work is listed once, using the issue with the first repository name and issue number.
    - Each listed issue carries its Workdash item ID, accepted as is by `workdash code`, `workdash analyze`, and `workdash terminal`; an actionable linked pull request is reported as context.
    - `ready` only reports: it never launches panes or reserves work.
    - No ready work is a successful empty result. A GitHub or Zellij failure fails the whole command, except that a failed required-checks lookup only makes the CI status `unknown` and a todo repository that does not exist yet has no work.

  @id:F-READY-S001
  Scenario: Ready lists the open leaf issues of a repository the dashboard does not show
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issues #1 and #2 and closed issue #3
    And the dashboard lists none of them
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-1` and `owner/repo#ISSUE-2` with action `implement`
    And issue #3 is not listed
    And the dashboard list is unchanged
    And no pane is launched

  @id:F-READY-S002
  Scenario: An issue is ready after its descendants are closed
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#10` is open with open sub-issue #11 and closed sub-issue #12
    And GitHub issue `owner/repo#20` is open and all its sub-issues are closed
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-11` and `owner/repo#ISSUE-20`
    And issue `owner/repo#ISSUE-10` is not listed

  @id:F-READY-S003
  Scenario Outline: Open blockers hold an issue back
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#5` is open and blocked by a <blocker> issue
    When the user runs `workdash ready owner/repo --json`
    Then issue `owner/repo#ISSUE-5` is <listed>

    Examples:
      | blocker | listed     |
      | open    | not listed |
      | closed  | listed     |

  @id:F-READY-S004
  Scenario: Ready is a thin client of a remote server
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    And the client process cannot find GitHub CLI or Zellij on PATH
    When the user runs `workdash ready owner/repo --host dashboard.lan --json`
    Then the command connects to host `dashboard.lan`
    And the command requests ready work for scope `owner/repo` from the Workdash server
    And the result lists `owner/repo#ISSUE-1` with action `implement`

  @id:F-READY-S005
  Scenario: Ready requires a running server
    Given no Workdash server is running
    When the user runs `workdash ready any`
    Then the system reports that the Workdash server is not reachable
    And the command exits with a non-zero status

  @id:F-READY-S006
  Scenario: The todo scope covers the server's todo repository, including targeted todos
    Given a server-backed Workdash session is ready to answer
    And the server's todo repository `testuser/todos` has open todo #7 targeting `owner/other` and open todo #8 without a target
    When the user runs `workdash ready todo --json`
    Then the result lists `owner/other#ISSUE-WT7` and `testuser/todos#ISSUE-8` with action `implement`

  @id:F-READY-S007
  Scenario: The any scope combines every configured repository and the todo repository once
    Given a server-backed Workdash session is ready to answer
    And the server tracks repository selectors `owner/repo` and `team/*`
    And GitHub owner `team` has repositories `team/a` and `team/b`, each with one open leaf issue
    And GitHub repository `owner/repo` has open issue #1
    And the server's todo repository `testuser/todos` has open todo #9 closed by authored pull request `owner/repo#30` whose CI failed
    When the user runs `workdash ready any --json`
    Then the result lists `owner/repo#ISSUE-WT9` once with action `fix_ci` and linked pull request `owner/repo#PR-30`
    And the result lists `team/a`, `team/b`, and `owner/repo#ISSUE-1` issues once each

  @id:F-READY-S008
  Scenario Outline: A pull request that only waits for others hides itself and its issue
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And pull request #51 has <ci> CI, <mergeability> mergeability, and <reviews>
    And no agent pane works on either of them
    When the user runs `workdash ready owner/repo --json`
    Then neither `owner/repo#ISSUE-42` nor `owner/repo#PR-51` is listed

    Examples:
      | ci        | mergeability | reviews                  |
      | pending   | mergeable    | no reviews               |
      | passing   | mergeable    | no reviews               |
      | passing   | mergeable    | an approval              |
      | no_checks | mergeable    | no reviews               |
      | unknown   | mergeable    | no reviews               |
      | passing   | unknown      | no reviews               |

  @id:F-READY-S009
  Scenario: An issue with a pull request with failed CI is ready
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And check `tests` failed on the current head of pull request #51, which has no reviews
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-42` with action `fix_ci` and reasons `fix_ci`
    And that entry links pull request `owner/repo#PR-51`, reports CI status `failing` with failing check `tests`, and the current head SHA
    And pull request `owner/repo#PR-51` is not listed as a ready item

  @id:F-READY-S010
  Scenario Outline: Actionable review feedback makes a green pull request ready
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And pull request #51 has passing CI and <feedback>
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-42` with action `address_review` and reasons `address_review`
    And that entry references the actionable feedback and links pull request `owner/repo#PR-51`

    Examples:
      | feedback                                                         |
      | a reviewer requesting changes on the current head                |
      | a reviewer comment posted after the current head was pushed      |
      | an unresolved review thread a reviewer opened after the last push |

  @id:F-READY-S011
  Scenario Outline: A pull request needing several kinds of work is listed once with every reason
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And pull request #51 has <problems>
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-42` once with action `<action>` and reasons `<reasons>`
    And pull request `owner/repo#PR-51` is not listed as a ready item

    Examples:
      | problems                                                | action              | reasons                                    |
      | failed CI and new review feedback                       | address_review      | address_review,fix_ci                      |
      | merge conflicts and failed CI                           | fix_merge_conflicts | fix_merge_conflicts,fix_ci                 |
      | merge conflicts, failed CI, and new review feedback     | address_review      | address_review,fix_merge_conflicts,fix_ci  |
      | merge conflicts and pending CI                          | fix_merge_conflicts | fix_merge_conflicts                        |

  @id:F-READY-S012
  Scenario Outline: Handled, stale, or non-human feedback does not make a pull request ready
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And pull request #51 has passing CI and only <feedback>
    When the user runs `workdash ready owner/repo --json`
    Then neither `owner/repo#ISSUE-42` nor `owner/repo#PR-51` is listed

    Examples:
      | feedback                                                       |
      | a changes-requested review on an older head                    |
      | a dismissed changes-requested review                           |
      | a reviewer comment posted before the current head was pushed   |
      | a bot comment posted after the current head was pushed         |
      | an author reply posted after the current head was pushed       |
      | a resolved review thread                                       |
      | an outdated review thread                                      |

  @id:F-READY-S013
  Scenario Outline: CI is judged on the current head only
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51` whose CI failed
    When the user runs `workdash ready owner/repo --json`
    Then issue `owner/repo#ISSUE-42` is listed
    When a new head is pushed to pull request #51 and its CI is <current>
    And the user runs `workdash ready owner/repo --json`
    Then issue `owner/repo#ISSUE-42` is <listed>

    Examples:
      | current | listed     |
      | pending | not listed |
      | passing | not listed |
      | failing | listed     |

  @id:F-READY-S014
  Scenario: A pull request whose head branch the server cannot push to is not ready
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored pull request `owner/repo#51` from a fork the server cannot push to, and its CI failed
    When the user runs `workdash ready owner/repo --json`
    Then issue `owner/repo#ISSUE-42` is not listed

  @id:F-READY-S015
  Scenario: A live agent pane holds an issue and the pull request that closes it
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51` of another author whose CI failed
    And GitHub repository `owner/repo` has open issue #43
    And GitHub repository `owner/repo` has open issue #44
    And open pull request `owner/repo#52` of another author closes no issue and its CI failed
    And an agent pane is running in the worktree of issue `owner/repo#42`
    And an agent pane is running in the worktree of issue `owner/repo#44`
    And an agent pane is running in the worktree of pull request `owner/repo#52`
    And a terminal pane is running in the worktree of issue `owner/repo#43`
    When the user runs `workdash ready owner/repo --json`
    Then the result lists only `owner/repo#ISSUE-43`

  @id:F-READY-S016
  Scenario: Ready fails when the server cannot read its Zellij panes
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    And Zellij cannot list the panes of the Workdash session
    When the user runs `workdash ready owner/repo`
    Then the system reports that Zellij is unavailable
    And the command exits with a non-zero status

  @id:F-READY-S017
  Scenario: Ready fails when GitHub cannot answer
    Given a server-backed Workdash session is ready to answer
    And GitHub answers repository queries with an outage
    When the user runs `workdash ready owner/repo`
    Then the system reports that the GitHub lookup failed
    And the command exits with a non-zero status

  @id:F-READY-S018
  Scenario Outline: A closed pull request no longer covers its issue
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and was linked to pull request `owner/repo#51`, which is now <state>
    When the user runs `workdash ready owner/repo --json`
    Then the result lists only `owner/repo#ISSUE-42`

    Examples:
      | state           |
      | closed unmerged |
      | merged          |

  @id:F-READY-S019
  Scenario: Issues with actionable authored pull requests come first in pull request age order
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has these ready items:
      | item                                 | created    | PR created |
      | issue 1                              | 2026-01-01 | -          |
      | issue 2                              | 2026-01-03 | -          |
      | issue 60 with actionable authored PR | 2026-02-01 | 2026-03-02 |
      | issue 61 with actionable authored PR | 2026-02-02 | 2026-03-01 |
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-61`, `owner/repo#ISSUE-60`, `owner/repo#ISSUE-1`, `owner/repo#ISSUE-2` in this order
    And `workdash ready owner/repo` prints the same items in the same order with type, ID, action, title, and URL

  @id:F-READY-S020
  Scenario: No ready work is a successful empty result
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has no open issues or pull requests
    When the user runs `workdash ready owner/repo`
    Then the system prints `No ready work found.`
    And the system exits with a zero status

  @id:F-READY-S021
  Scenario Outline: An issue without sub-issues scopes itself or its pull request
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` <state>
    When the user runs `workdash ready owner/repo#42 --json`
    Then the result lists <result>

    Examples:
      | state                                                          | result                                  |
      | is open                                                        | only `owner/repo#ISSUE-42`              |
      | is closed                                                      | nothing                                 |
      | is open and blocked by an open issue                           | nothing                                 |
      | is open and closed by authored open pull request #51 with pending CI    | nothing                                 |
      | is open and closed by authored open pull request #51 whose CI failed    | only `owner/repo#ISSUE-42`                 |
      | is closed and closed by authored open pull request #51 whose CI failed  | nothing                                 |

  @id:F-READY-S022
  Scenario: An issue scope lists the ready work of its whole sub-issue tree only
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#99` is open with these sub-issues:
      | sub-issue          | parent             | state                                                     |
      | owner/repo#100     | owner/repo#99      | open, with sub-issues                                     |
      | owner/repo#101     | owner/repo#100     | open                                                      |
      | other/lib#7        | owner/repo#100     | open                                                      |
      | owner/repo#102     | owner/repo#99      | open and closed by authored open pull request #110 whose CI failed |
      | owner/repo#103     | owner/repo#99      | closed                                                    |
    And GitHub repository `owner/repo` also has open issue #200 and open pull request #210 whose CI failed, outside that tree
    When the user runs `workdash ready https://github.com/owner/repo/issues/99 --json`
    Then the result lists `owner/repo#ISSUE-102`, `other/lib#ISSUE-7`, `owner/repo#ISSUE-101` in this order
    And neither issue #99, #100, #103, #200 nor pull request #210 is listed

  @id:F-READY-S023
  Scenario Outline: A parent issue is a scope whatever its state
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#99` is <state> with open sub-issue #101 and closed sub-issue #102
    When the user runs `workdash ready owner/repo#ISSUE-99 --json`
    Then the result lists only `owner/repo#ISSUE-101`

    Examples:
      | state  |
      | open   |
      | closed |

  @id:F-READY-S024
  Scenario: A targeted todo can be an issue scope
    Given a server-backed Workdash session is ready to answer
    And the server's todo repository `testuser/todos` has open todo #7 targeting `owner/other`
    When the user runs `workdash ready owner/other#ISSUE-WT7 --json`
    Then the result lists only `owner/other#ISSUE-WT7`

  @id:F-READY-S025
  Scenario Outline: A scope that names no accessible issue is an error, never a broader scope
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash ready <scope> --json`
    Then the system reports error `<error>`
    And the command exits with a non-zero status

    Examples:
      | scope            | error                 |
      | owner/repo#404   | unknown_item          |
      | owner/repo#PR-51 | unsupported_item_type |
      | not a scope      | invalid_target        |

  @id:F-READY-S026
  Scenario Outline: Confirmed merge conflicts make a pull request ready whatever its CI and reviews
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And pull request #51 has <ci> CI, an approval, and GitHub reports it conflicts with base branch `main`
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-42` with action `fix_merge_conflicts`
    And that entry reports mergeability `conflicting`, base `main` with its head SHA, and the current head SHA

    Examples:
      | ci      |
      | passing |
      | pending |

  @id:F-READY-S027
  Scenario: A base branch update that brings conflicts makes an unchanged pull request ready
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored pull request `owner/repo#51` which has passing CI and is mergeable
    When the user runs `workdash ready owner/repo --json`
    Then issue `owner/repo#ISSUE-42` is not listed
    When base branch `main` moves to a new head and GitHub reports pull request #51 conflicts with it
    And the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-42` with action `fix_merge_conflicts` and the new base head SHA

  @id:F-READY-S028
  Scenario Outline: CI status tells no reported checks from unknown checks
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored pull request `owner/repo#51` with a reviewer comment posted after its current head was pushed
    And GitHub reports no check or status on its current head
    And the base branch required checks are <required>
    When the user runs `workdash ready owner/repo --json`
    Then the result lists `owner/repo#ISSUE-42` with action `address_review` and CI status `<ci_status>`

    Examples:
      | required                          | ci_status |
      | none in rulesets or protection    | no_checks |
      | `build` from a ruleset            | unknown   |
      | unreadable                        | unknown   |

  @id:F-READY-S029
  Scenario: An issue with somebody else's open pull request is not ready
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51` of another author whose CI failed
    When the user runs `workdash ready owner/repo --json`
    Then neither `owner/repo#ISSUE-42` nor `owner/repo#PR-51` is listed
