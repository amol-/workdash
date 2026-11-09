@feature:F-DISPATCH
Feature: Dispatch one ready issue

  A user can ask the server to choose and launch one ready issue without
  running a scheduler or waiting for the work to finish.

  Rules:
    - `workdash dispatch TARGET [--agent NAME] [--group NAME] [--host HOST] [--json]` is a thin client of the running server. TARGET is a repository or issue scope accepted by `ready`, including targeted todos and issue URLs.
    - The server selects the first still-eligible issue from the same ordered results as `ready TARGET`, using the same scope resolution, readiness and GitHub metadata-only CI classification. It rechecks eligibility before launch, including current base mergeability.
    - The server uses the same code operation and instruction renderer as explicit code and autoclose, targeting the issue with autostart and autoclose enabled. When multiple authored pull requests are linked, the oldest actionable one is resumed, matching readiness. An actionable authored linked pull request, including one linked to a targeted todo, is resumed on its existing branch with all its reasons in the instruction, ordered review, conflicts, then CI.
    - Dispatch resumes a targeted todo's pushed worktree branch even if it still tracks the default branch, preserving local work. Dispatch fails without launching if it cannot verify that the selected pull request's checkout is on its head branch in its head repository; it does not switch an existing checkout to another branch.
    - A newly launched agent's default group is the basename of the implementation repository, not the parent issue's repository. `--group` overrides it.
    - If the selected issue remains ready with an actionable pull request when rechecked, dispatch uses its currently actionable pull request and reasons, even if they changed, and reports its current action and state. If a selected pull request disappears without a replacement, another caller claims a candidate, or the selected work is no longer eligible, the server retries the next still-eligible candidate, bounded by the candidates. At most one confirmed pane is launched per dispatch; uncertainty or an upstream failure is an error, never a false empty result or a second launch.
    - A successful launch returns immediately with status `launched`, issue, action, reasons, and actual pane ID; the server owns the agent and its autoclose supervision after the client exits. An empty or exhausted scope returns success with status `no_ready_task`.
    - Dispatch does not create sub-issues, merge, wait for completion, or run a scheduler. There is no `autocode` command.

  @id:F-DISPATCH-S001
  Scenario: Dispatch selects the first issue previewed by ready and returns immediately
    Given a server-backed Workdash session is ready to answer
    And `owner/repo` has an older unstarted issue #1 and issue #42 with authored pull request #51 whose CI failed
    And pull request #51 has an existing worktree on branch `pr-branch`
    When the user runs `workdash ready owner/repo --json`
    Then issue `owner/repo#ISSUE-42` precedes issue `owner/repo#ISSUE-1`
    When the user runs `workdash dispatch owner/repo --agent pi --json`
    Then the result reports status `launched`, issue `owner/repo#ISSUE-42`, action `fix_ci`, and the actual pane ID
    And exactly one agent is launched on pull request #51's existing branch
    And the agent is told to fix the failing checks using the same instruction as autoclose
    And the server supervises the pane after the command returns

  @id:F-DISPATCH-S002
  Scenario: An issue scope dispatches its own unstarted work
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42 with no sub-issues or pull request
    When the user runs `workdash dispatch owner/repo#42 --json`
    Then the result reports status `launched` and issue `owner/repo#ISSUE-42` with action `implement`
    And exactly one agent is told to implement issue #42

  @id:F-DISPATCH-S003
  Scenario: A closed parent scopes descendants and the implementation repository selects the group
    Given a server-backed Workdash session is ready to answer
    And closed issue `owner/parent#42` has open nested descendant `owner/child#7` with no open descendants
    And an unrelated issue in `owner/parent` is also ready
    When the user runs `workdash dispatch owner/parent#42 --json`
    Then the result reports status `launched` and issue `owner/child#ISSUE-7`
    And exactly one agent starts in group `child`
    And no agent is launched for the parent or unrelated issue

  @id:F-DISPATCH-S004
  Scenario: An open parent becomes ready once its descendants are closed
    Given a server-backed Workdash session is ready to answer
    And open issue `owner/repo#42` has only closed sub-issues
    When the user runs `workdash dispatch owner/repo#42 --json`
    Then the result reports status `launched` and issue `owner/repo#ISSUE-42`
    And the agent is told to verify the parent's workflow and prove it with a test if already implemented by the sub-issues

  @id:F-DISPATCH-S005
  Scenario: Dispatch carries all actionable reasons into one briefing
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by authored pull request `owner/repo#51` with merge conflicts, failed CI, and new review feedback
    And pull request #51 has an existing worktree on branch `pr-branch`
    When the user runs `workdash dispatch owner/repo#42 --json`
    Then exactly one agent starts on pull request #51's existing branch
    And the result reports issue `owner/repo#ISSUE-42` with action `address_review` and all three reasons
    And the briefing includes the feedback references, merge conflicts, and failing checks in that order
    And no new pull request is created by dispatch

  @id:F-DISPATCH-S006
  Scenario: Waiting or externally authored pull requests do not make issues dispatchable
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` has an authored open pull request waiting only for review
    And GitHub issue `owner/repo#43` has an open pull request of another author with failed CI
    When the user runs `workdash dispatch owner/repo --json`
    Then the result reports status `no_ready_task`
    And no coding agent is launched

  @id:F-DISPATCH-S007
  Scenario: A racing dispatch retries the next available issue
    Given a server-backed Workdash session is ready to answer
    And issues `owner/repo#ISSUE-1` and `owner/repo#ISSUE-2` are ready in that order
    When two clients dispatch `owner/repo` at the same time and both initially select issue #1
    Then each response reports one launched pane for a different issue
    And exactly two coding agents are launched

  @id:F-DISPATCH-S008
  Scenario: Stale readiness is rechecked before launch
    Given a server-backed Workdash session is ready to answer
    And issue `owner/repo#42` has an authored pull request whose only problem is a merge conflict
    And the conflict is resolved after ready selection but before dispatch launch
    When the user runs `workdash dispatch owner/repo#42 --json`
    Then the result reports status `no_ready_task`
    And no coding agent is launched

  @id:F-DISPATCH-S009
  Scenario Outline: Dispatch fails closed on unavailable state or unconfirmed launch
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    And <failure>
    When the user runs `workdash dispatch owner/repo --json`
    Then the command reports an error and exits with a non-zero status
    And the result does not report `no_ready_task` or a confirmed launch
    And dispatch does not start a second agent

    Examples:
      | failure                                         |
      | GitHub cannot report the work state             |
      | Zellij cannot report live panes                 |
      | a launched pane cannot be confirmed             |

  @id:F-DISPATCH-S010
  Scenario: A remote dispatch uses the server and honors a group override
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    And the client process cannot find GitHub CLI or Zellij on PATH
    When the user runs `workdash dispatch owner/repo --host dashboard.lan --agent pi --group epic --json`
    Then the command connects to host `dashboard.lan`
    And the result reports status `launched`, issue `owner/repo#ISSUE-42`, agent `pi`, group `epic`, and the actual pane ID
    And the server supervises the launched pane
