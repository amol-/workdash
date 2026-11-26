@feature:F-READY
Feature: Find ready work

  Use `workdash ready` to find what to pick up next without starting work.

  @id:F-READY-S001
  Scenario: Find work in a repository outside the dashboard
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issues #1 and #2 and closed issue #3
    And the dashboard lists none of them
    When the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-1` and `owner/repo#ISSUE-2`

  @id:F-READY-S002
  Scenario: Pick up the parent after finishing its last sub-issue
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#10` is open with open sub-issue #11 and closed sub-issue #12
    When the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-11` but not `owner/repo#ISSUE-10`
    When the last open sub-issue `owner/repo#11` closes
    And the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-10` but not `owner/repo#ISSUE-11`

  @id:F-READY-S005
  Scenario: Start the dashboard and retry a ready check
    Given no Workdash server is running
    When the user runs `workdash ready any`
    Then the command reports that `workdash` must be running
    When the user starts a Workdash dashboard tracking `owner/repo`, where issue #1 is ready
    And the user runs `workdash ready any`
    Then ready shows `owner/repo#ISSUE-1`

  @id:F-READY-S006
  Scenario: Find captured todos ready to start
    Given a server-backed Workdash session is ready to answer
    And the server's todo repository `testuser/todos` has open todo #7 targeting `owner/other` and open todo #8 without a target
    When the user runs `workdash ready todo`
    Then ready shows `owner/other#ISSUE-WT7` and `testuser/todos#ISSUE-8`

  @id:F-READY-S007
  Scenario: Find ready work across projects and todos
    Given a server-backed Workdash session is ready to answer
    And the server tracks repository selectors `owner/repo` and `team/*`
    And GitHub owner `team` has repositories `team/a` and `team/b`, each with one open leaf issue
    And GitHub repository `owner/repo` has open issue #1
    And the server's todo repository `testuser/todos` has open todo #9 closed by authored pull request `owner/repo#30` whose CI failed
    When the user runs `workdash ready any`
    Then ready shows `owner/repo#ISSUE-1`, `team/a#ISSUE-1`, `team/b#ISSUE-1`, and `owner/repo#ISSUE-WT9`

  @id:F-READY-S009
  Scenario: Find failed CI to repair and recheck after pushing a fix
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And check `tests` failed on the current head of pull request #51, which has no reviews
    When the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-42` with action `fix_ci`
    When the user pushes a fix to pull request #51 and its CI passes
    And the user runs `workdash ready owner/repo`
    Then the system prints `No ready work found.`

  @id:F-READY-S030
  Scenario: Recheck work after asking for another review
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored open pull request `owner/repo#51`
    And pull request #51 has passing CI and a reviewer requested changes
    When the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-42` with action `address_review`
    When that reviewer is asked to review pull request #51 again
    And the user runs `workdash ready owner/repo`
    Then the system prints `No ready work found.`

  @id:F-READY-S019
  Scenario: Pick up existing work before starting a new issue
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has these ready items:
      | item                                 | created    | PR created |
      | issue 1                              | 2026-01-01 | -          |
      | issue 61 with actionable authored PR | 2026-02-02 | 2026-03-01 |
    When the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-61` before unstarted issue `owner/repo#ISSUE-1`

  @id:F-READY-S020
  Scenario: No ready work is a successful empty result
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has no open issues or pull requests
    When the user runs `workdash ready owner/repo`
    Then the system prints `No ready work found.`
    And the system exits with a zero status

  @id:F-READY-S022
  Scenario: Find remaining work from a parent issue URL
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#99` is open with these sub-issues:
      | sub-issue      | parent         | state |
      | owner/repo#100 | owner/repo#99  | open  |
      | owner/repo#101 | owner/repo#100 | open  |
      | other/lib#7    | owner/repo#100 | open  |
    And GitHub repository `owner/repo` has open issue #200
    When the user runs `workdash ready https://github.com/owner/repo/issues/99`
    Then ready shows `other/lib#ISSUE-7` and `owner/repo#ISSUE-101` but not unrelated issue `owner/repo#ISSUE-200`

  @id:F-READY-S024
  Scenario: Check one targeted todo by its Workdash ID
    Given a server-backed Workdash session is ready to answer
    And the server's todo repository `testuser/todos` has open todo #7 targeting `owner/other` and open todo #8 without a target
    When the user runs `workdash ready owner/other#ISSUE-WT7`
    Then ready shows only `owner/other#ISSUE-WT7`

  @id:F-READY-S027
  Scenario: Find work made ready by new base branch conflicts
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored pull request `owner/repo#51` which has passing CI and is mergeable
    When the user runs `workdash ready owner/repo`
    Then the system prints `No ready work found.`
    When base branch `main` moves to a new head and GitHub reports pull request #51 conflicts with it
    And the user runs `workdash ready owner/repo`
    Then ready shows `owner/repo#ISSUE-42` with action `fix_merge_conflicts`
