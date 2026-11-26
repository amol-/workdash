@feature:F-DISPATCH
Feature: Dispatch one ready issue

  Use `workdash dispatch` to launch one ready issue and return to the command
  line while the server supervises the agent.

  @id:F-DISPATCH-S001
  Scenario: Preview the next issue and resume its pull request
    Given a server-backed Workdash session is ready to answer
    And `owner/repo` has an older unstarted issue #1 and issue #42 with authored pull request #51 whose CI failed
    And pull request #51 has an existing worktree
    When the user runs `workdash ready owner/repo`
    Then the preview lists `owner/repo#ISSUE-42` first as work to fix CI
    When the user runs `workdash dispatch owner/repo --agent pi`
    Then dispatch reports launching `owner/repo#ISSUE-42` to `fix_ci`
    And dispatch reports the existing pull request worktree as the agent's working directory
    And dispatch reports that autoclose is enabled

  @id:F-DISPATCH-S002
  Scenario: Start work on a specific unstarted issue
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42 with no sub-issues or pull request
    When the user runs `workdash dispatch owner/repo#42`
    Then dispatch reports launching `owner/repo#ISSUE-42` to `implement`

  @id:F-DISPATCH-S003
  Scenario: Dispatch remaining work from a closed parent
    Given a server-backed Workdash session is ready to answer
    And closed issue `owner/parent#42` has open nested descendant `owner/child#7` with no open descendants
    And an unrelated issue in `owner/parent` is also ready
    When the user runs `workdash dispatch owner/parent#42`
    Then dispatch reports launching `owner/child#ISSUE-7` to `implement`
    And dispatch reports the agent in tab `child`

  @id:F-DISPATCH-S004
  Scenario: Start the final verification after the parent's subtasks are done
    Given a server-backed Workdash session is ready to answer
    And open issue `owner/repo#42` has only closed sub-issues
    When the user runs `workdash dispatch owner/repo#42`
    Then dispatch reports launching `owner/repo#ISSUE-42` to `implement`

  @id:F-DISPATCH-S010
  Scenario: Dispatch ready work on a remote dashboard in a chosen tab
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When the user runs `workdash dispatch owner/repo --host dashboard.lan --agent pi --group epic`
    Then dispatch reports launching `owner/repo#ISSUE-42` to `implement`
    And dispatch reports agent `pi` in tab `epic`
    And dispatch reports that autoclose is enabled
