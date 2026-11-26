@feature:F-CLI-ORCHESTRATION
Feature: CLI orchestration commands

  Use the command line to check work, start or find an agent, and manage panes
  and todos in a running Workdash session, locally or on a remote dashboard.

  @id:F-CLI-ORCHESTRATION-S003
  Scenario: Find the agent and terminal for an issue
    Given a server-backed Workdash session is running
    And that session has an agent and a terminal for `owner/repo#ISSUE-1`
    When the user runs `workdash info`
    Then the system reports the Workdash session name
    And the listing identifies `terminal_1` as the agent and `terminal_2` as the terminal for `owner/repo#ISSUE-1`

  @id:F-CLI-ORCHESTRATION-S004
  Scenario: Start Workdash when info cannot reach it
    Given no server-backed Workdash session is running
    When the user runs `workdash info`
    Then the command tells the user to start it with `workdash`
    When the user starts `workdash` and retries `workdash info`
    Then the system reports the Workdash session name

  @id:F-CLI-ORCHESTRATION-S009
  Scenario: Get an analysis report for an assigned issue
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex`
    Then the command shows the Markdown report link for `owner/repo#ISSUE-1` from `codex`

  @id:F-CLI-ORCHESTRATION-S012
  Scenario: Find an ordinary shell alongside work panes
    Given a server-backed Workdash session is running
    And that session has an agent and a terminal for `owner/repo#ISSUE-1`
    And the user has an ordinary shell in the `scratch` tab
    When the user runs `workdash info`
    Then the listing does not show `terminal_11`
    When the user runs `workdash info --all`
    Then the listing identifies the shell in `scratch` as `terminal_11`

  @id:F-CLI-ORCHESTRATION-S027
  Scenario: Open a shell to inspect an issue's checkout
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue
    When the user runs `workdash terminal owner/repo#ISSUE-1`
    Then the command identifies the new shell's pane and checkout for `owner/repo#ISSUE-1`

  @id:F-CLI-ORCHESTRATION-S019
  Scenario: Check the output of a work terminal
    Given a server-backed Workdash session is running
    And a work terminal `terminal_23` displays "Local checks finished."
    When the user runs `workdash info`
    Then the listing shows `terminal_23`
    When the user runs `workdash read terminal_23`
    Then the command displays "Local checks finished."

  @id:F-CLI-ORCHESTRATION-S023
  Scenario: Close an unneeded work terminal
    Given a server-backed Workdash session is running
    And work terminals `terminal_26` and `terminal_27` are open
    When the user runs `workdash info`
    Then the listing shows `terminal_26`
    When the user runs `workdash close terminal_26`
    Then the command confirms that it accepted the close
    When the user runs `workdash info`
    Then the listing does not show `terminal_26`
    And the listing shows `terminal_27`

  @id:F-CLI-ORCHESTRATION-S030
  Scenario: Capture a follow-up for a repository
    Given a server-backed Workdash session is running
    When the user runs `workdash todo "Fix the flaky test" --target owner/repo`
    Then the system reports the created Workdash item ID and issue URL

  @id:F-CLI-ORCHESTRATION-S034
  Scenario: File a todo from a client of a remote dashboard
    Given the user's dashboard is running on `dashboard.lan` instead of locally
    When the user runs `workdash todo "Fix the flaky test" --target owner/repo --host dashboard.lan`
    Then the new todo appears under `owner/repo` in `workdash list --host dashboard.lan`

  @id:F-CLI-ORCHESTRATION-S035
  Scenario: Keep detailed notes separate from a todo's title
    Given a server-backed Workdash session is running
    When the user runs `workdash todo "Fix the flaky test" --target owner/repo --body` with these notes:
      """
      The test fails when run after the export tests.

      Compare the saved settings before and after export and document the result.
      """
    Then the created issue is titled "Fix the flaky test" and includes those notes
    And the system reports the created Workdash item ID and issue URL

  @id:F-CLI-ORCHESTRATION-S037
  Scenario: Start coding an unlisted issue on a remote dashboard
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items do not include `owner/repo#ISSUE-99`
    And GitHub has the accessible issue `owner/repo#ISSUE-99`
    When the user runs `workdash code owner/repo#99 --agent pi --host dashboard.lan`
    Then the command shows the new pane for `owner/repo#ISSUE-99` running `pi`

  @id:F-CLI-ORCHESTRATION-S038
  Scenario: Find the agent already working on an issue
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And the worktree of `owner/repo#ISSUE-1` already exists
    And an agent pane is running in that worktree
    When the user runs `workdash code owner/repo#ISSUE-1 --agent pi`
    Then the command shows that pane `terminal_7` is already running for `owner/repo#ISSUE-1`
    And no coding agent is launched

  @id:F-CLI-ORCHESTRATION-S040
  Scenario: Code launches a plan-first coding session by default
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash code owner/repo#ISSUE-1 --agent pi`
    Then the command reports that pi launched in a pane for `owner/repo#ISSUE-1`
    And the agent is asked to propose a plan rather than start implementation

  @id:F-CODING-LAUNCH-S031
  Scenario: Continue an issue's open pull request from the command line
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51` whose CI failed
    And pull request #51 has an existing worktree on branch `pr-branch`
    When the user runs `workdash code owner/repo#42 --agent pi --autostart`
    Then the command reports that pi launched in a pane for `owner/repo#ISSUE-42`
    And the command shows the existing pull request checkout as the pane's working directory

  @id:F-CLI-ORCHESTRATION-S047
  Scenario: Coding a pull request follows its newly linked issue
    Given a server-backed Workdash session has loaded dashboard items
    And authored pull request `owner/repo#50` has a branch but no linked issue
    When the user runs `workdash code https://github.com/owner/repo/pull/50`
    Then code opens in the checkout for `owner_repo_50`
    When the author links pull request `owner/repo#50` to issue `owner/repo#1`
    And the user runs `workdash code https://github.com/owner/repo/pull/50`
    Then code opens in the checkout for `owner_repo_1`
    And the original pull request checkout is left untouched

  @id:F-CLI-ORCHESTRATION-S041
  Scenario: Code with autostart launches an autonomous coding session
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash code owner/repo#ISSUE-1 --agent pi --autostart`
    Then the command reports that pi launched in a pane for `owner/repo#ISSUE-1`
    And the agent is asked to start implementing without routine confirmation

  @id:F-CLI-ORCHESTRATION-S043
  Scenario: Start a coding agent in a named tab on a remote dashboard
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash code owner/repo#ISSUE-1 --group epic --host dashboard.lan`
    Then the command reports a new agent in tab `epic`

  @id:F-CLI-ORCHESTRATION-S046
  Scenario: Start a pull request review
    Given an open pull request is available for review
    When the user runs `workdash review owner/repo#PR-2`
    Then the command reports that the review started and where to find its document

  @id:F-CLI-ORCHESTRATION-S044 @id:F-CODING-AUTOCLOSE-S010
  Scenario: Close an idle coding pane when the work is already ready to merge
    Given a server-backed Workdash session is ready to answer
    And issue `owner/repo#42` has an open pull request `owner/repo#51` ready to merge with all changes published
    When the user runs `workdash code owner/repo#ISSUE-42 --agent pi --autoclose`
    Then Workdash reports the new agent pane with autoclose enabled
    When the agent goes idle
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S005
  Scenario: Close an active coding pane when its pull request merges
    Given a server-backed Workdash session is ready to answer
    And issue `owner/repo#42` has an open pull request `owner/repo#51`
    When the user runs `workdash code owner/repo#ISSUE-42 --agent pi --autoclose`
    And pull request `owner/repo#51` merges while the agent is still producing output
    Then the supervised pane is closed

  @id:F-CODING-AUTOCLOSE-S006 @id:F-CODING-AUTOCLOSE-S008
  Scenario: Keep an idle agent working as failing checks and review feedback arrive
    Given a server-backed Workdash session is ready to answer
    And issue `owner/repo#42` has an open pull request `owner/repo#51`
    When the user runs `workdash code owner/repo#ISSUE-42 --agent pi --autoclose`
    And pull request `owner/repo#51` develops a failing check `tests`
    And the agent goes idle
    Then the agent receives a message to fix the failing checks
    When a reviewer requests changes on pull request `owner/repo#51`
    And the agent goes idle
    Then the agent receives a message to address the review feedback
