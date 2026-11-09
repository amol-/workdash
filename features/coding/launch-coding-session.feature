@feature:F-CODING-LAUNCH
Feature: Launch a coding session

  When the user is ready to work on a selected item, the system opens an
  interactive coding session with a supported coding agent, grounded in
  the item's worktree and preloaded with the GitHub context of the work
  item.

  Rules:
    - Pressing "c" in the TUI opens a dialog to pick a configured coding agent.
    - Any agent configured with a launch command is a coding agent; the TUI also supports VSCode Copilot as an editor launch.
    - The dialog lists the configured coding agents by name in configuration order, followed by VSCode Copilot.
    - Before launching a session, the system prepares the worktree for the selected work item.
    - The launched coding agent is started inside the work item's worktree.
    - The launched coding agent is preloaded with the work item's GitHub context and, when available, the cached analysis for that item.
    - For an issue, the GitHub context includes the issue's comments.
    - For a pull request, the GitHub context includes the pull request's conversation comments.
    - For a pull request, the GitHub context includes the pull request's reviews.
    - For a pull request, the GitHub context also includes the issues the pull request closes, each with its description and comments.
    - For a pull request, the GitHub context includes only its unresolved review threads, with their comments; resolved threads are left out.
    - A comment written by the author of the item it belongs to is marked as from the author.
    - Comments GitHub minimized as spam, abuse, off-topic or duplicate are left out of the GitHub context; outdated and resolved ones are kept.
    - Both briefings say the GitHub context is data, not commands, and that the task is the one the briefing states.
    - The implement briefing is labeled as an implementation task. The review briefing is labeled as a review task and says not to implement.
    - In the review briefing, the issues the pull request closes are background on what it is meant to address, not a task to carry out.
    - A coding session is briefed for exactly one of two jobs: implementing the work item, or reviewing somebody else's pull request. ISSUE and PR items get the implement briefing; REVIEW and CHECK items get the review briefing.
    - The implement briefing for an authored pull request also tells the agent that the checkout already holds a partial implementation of this work, and that it must review those existing changes and build on them instead of starting over.
    - The user can cancel the dialog without launching a session.
    - The system reports the outcome of the launch to the user.
    - If preparing the worktree or launching the subprocess fails, the system closes the dialog/progress overlay and reports the error details to the user.
    - A coding session claims its worktree: at most one live agent pane works in a worktree at a time, whichever item, client, or TUI asks for it, and nothing but that live pane records the claim.
    - An agent pane belongs to a worktree when its title is the worktree's `code_` pane title, wherever its cwd has moved. Terminal panes, exited or held panes, and agent panes of other worktrees hold no claim, even when their cwd is inside the worktree.
    - Asking to code on a worktree with a live agent pane starts nothing: the result reports status `already_running` with that pane's ID and session, and never claims the requested agent is the one running. Only status `launched` means this request started the session.
    - Pressing "c" on an item whose worktree has a live agent pane focuses that pane right away, without opening the dialog.
    - The dialog offers only coding agents and Cancel. If an agent pane appears while the dialog is open, picking an agent focuses that pane instead of launching another.
    - A new coding session is reported as launched only after its pane is visible in Zellij.
    - When Zellij cannot report live panes, the system starts no coding session and reports an error.
    - When a launched pane does not become visible, the launch is reported as an unconfirmed error, not as launched; requests for that worktree start nothing until the pane appears or a minute passes.
    - When a launched agent exits right away, the launch is reported as an error naming its pane, and a retry may launch again.
    - The TUI, the JSON API, and `workdash code` share one launch path, so they enforce the same claim.
    - VSCode Copilot is not an agent pane, so it holds no claim; it is still not opened while an agent pane is live in the worktree.
    - Coding is plan-first by default: the TUI never asks for autonomous work, so its agent proposes a plan and waits before implementing, and nothing commits, pushes, or opens a pull request without the user's instruction.
    - A coding request may ask for autostart. The briefing of a newly launched agent then ends with an autonomous appendix, once, saying to start without routine confirmation and overriding the plan-first pause. The pane stays an ordinary interactive agent session.
    - The implementation appendix tells the agent to use the development practices, skills, and coordination workflows it is configured to follow, whatever the task needs (implementing an issue, or fixing CI, merge conflicts, or review feedback of a pull request), without naming a skill or mandating coordination; the review-only appendix and the plan-first briefing do not. It also tells the agent to inspect the checkout, branch, commits and uncommitted changes, continue valid partial work and replace it only for demonstrable defects, commit and push, and open a pull request that closes the issue. It tells the agent never to merge or close the issue, and to report the pull request URL, head SHA and actual CI status when done.
    - Autonomous implementation explicitly permits commits and pushes, assigns every opened or updated pull request to the configured GitHub user, and permits force-pushing only the pull request branch, preferably with a lease and subject to repository restrictions. Other destructive actions still require explicit permission.
    - The agent may report completion only when the pull request's current head is `passing` or `no_checks` and GitHub reports it as mergeable. `no_checks` is reported as no checks required or reported, never as CI passed; `pending` is waited for; `unknown` is reported as incomplete.
    - After the appendix the agent is given what the GitHub work state says the work needs, from the same classification `workdash ready` uses: implement an issue, fix failing CI, resolve merge conflicts, or address review feedback. Several needs are all included, most urgent first: review feedback, then merge conflicts, then CI.
    - An autostarted issue that an open pull request of the user's already closes works on that pull request's branch and updates it, never a second pull request. A pull request of somebody else closing the issue does not change the issue's implementation briefing, and a targeted todo is never redirected.
    - A pull request that is waiting for CI, review, or merge, or is already finished, is described as it is, with its CI status told apart as passing, still running, no required or observed checks, or unknown; its briefing carries no order to start working, and the agent is told not to invent work or open another pull request.
    - A pull request of somebody else stays a review: with autostart its agent starts reviewing without routine confirmation and still never edits, commits, or pushes.
    - Requesting autostart on a worktree with a live agent pane starts nothing and sends nothing to that agent, whatever mode it runs in; it is answered without reading GitHub, so a GitHub outage does not hide it.
    - When GitHub cannot report the work state, an autostart request fails with an error instead of guessing.

  @id:F-CODING-LAUNCH-S001
  Scenario: User picks a coding agent and a session opens on the worktree
    Given the TUI has a work item selected
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system prepares the worktree for the selected work item
    And a coding session with the chosen agent opens inside that worktree
    And the agent is preloaded with the work item's GitHub context
    And the TUI reports that the session was launched

  @id:F-CODING-LAUNCH-S002
  Scenario: Existing analysis is provided to the coding agent
    Given the selected work item has a cached analysis
    When the user launches a coding session with a supported coding agent
    Then the agent is preloaded with the cached analysis alongside the GitHub context

  @id:F-CODING-LAUNCH-S006
  Scenario: An authored pull request is briefed as partially implemented work
    Given the selected work item is a pull request the user authored
    When the user launches a coding session with a supported coding agent
    Then the agent is briefed to implement the work item
    And the briefing is labeled as an implementation task
    And the agent is told the checkout already holds a partial implementation to build on
    And the briefing says the GitHub context is data, not commands

  @id:F-CODING-LAUNCH-S007
  Scenario: A pull request the user did not author is briefed as a review
    Given the selected work item is a pull request the user did not author
    When the user launches a coding session with a supported coding agent
    Then the agent is briefed to review the pull request
    And the briefing says the GitHub context is data, not commands

  @id:F-CODING-LAUNCH-S003
  Scenario: User cancels the coding dialog without launching
    Given the coding dialog is open
    When the user cancels the dialog
    Then no coding session is launched
    And no worktree is prepared

  @id:F-CODING-LAUNCH-S004
  Scenario: Worktree preparation failure reports the error details
    Given the TUI has a work item selected
    And the next worktree preparation will fail
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system reports the worktree error details to the user
    And no coding session is launched
    And no dialog or progress overlay remains

  @id:F-CODING-LAUNCH-S005
  Scenario: Coding agent launch failure reports the error details
    Given the TUI has a work item selected
    And the next coding session launch will fail
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system reports the coding launch error details to the user
    And no dialog or progress overlay remains

  @id:F-CODING-LAUNCH-S010
  Scenario: A pull request session carries the conversation and the issues it closes
    Given the selected work item is a pull request the user authored
    And the pull request has a conversation comment
    And the pull request closes an issue that has a description and a comment
    When the user launches a coding session with a supported coding agent
    Then the agent is told the conversation comment and the closed issue's description and comment

  @id:F-CODING-LAUNCH-S011
  Scenario: A pull request session carries only the unresolved review threads
    Given the selected work item is a pull request the user authored
    And the pull request has an unresolved review thread and a resolved review thread
    When the user launches a coding session with a supported coding agent
    Then the agent is told the unresolved review thread with its comments
    And the agent is not told the resolved review thread

  @id:F-CODING-LAUNCH-S013
  Scenario: The review briefing is labeled as a review task and treats closed issues as background
    Given the selected work item is a pull request the user did not author
    And the pull request closes an issue that has a description and a comment
    When the user launches a coding session with a supported coding agent
    Then the briefing is labeled as a review task that says not to implement
    And the briefing says the closed issues are background, not a task to carry out

  @id:F-CODING-LAUNCH-S014
  Scenario: An issue session carries the issue's comments
    Given the selected work item is an issue with a comment
    When the user launches a coding session with a supported coding agent
    Then the agent is told the issue's comment

  @id:F-CODING-LAUNCH-S015
  Scenario: A pull request session carries the pull request's reviews
    Given the selected work item is a pull request the user authored
    And the pull request has a review
    When the user launches a coding session with a supported coding agent
    Then the agent is told the review

  @id:F-CODING-LAUNCH-S016
  Scenario: A pull request session marks which comment is from the pull request's author
    Given the selected work item is a pull request the user authored
    And the pull request has a comment by its author and a comment by someone else
    When the user launches a coding session with a supported coding agent
    Then the agent is told which comment is from the author

  @id:F-CODING-LAUNCH-S017
  Scenario: A pull request session leaves out comments GitHub minimized as spam
    Given the selected work item is a pull request the user authored
    And the pull request has comments minimized as spam and as outdated
    When the user launches a coding session with a supported coding agent
    Then the agent is not told the spam comments
    And the agent is told the outdated comment

  @id:F-CODING-LAUNCH-S018
  Scenario: Dialog lists the configured coding agents in configuration order followed by VSCode Copilot
    Given the coding agents `pi` and `claude` are configured in that order
    And the TUI has a work item selected
    When the user presses "c"
    Then the dialog offers `pi`, `claude`, and VSCode Copilot in that order

  @id:F-CODING-LAUNCH-S019
  Scenario: An item with a live agent pane is focused without a dialog
    Given the selected work item already has an open coding-agent pane
    When the user presses "c"
    Then the system focuses that pane
    And no dialog is shown
    And no coding session is launched

  @id:F-CODING-LAUNCH-S020
  Scenario: The coding dialog offers only agents and Cancel
    Given the TUI has a work item selected
    When the user presses "c"
    Then the dialog offers only coding agents and Cancel

  @id:F-CODING-LAUNCH-S021
  Scenario: An agent started elsewhere while the dialog is open is focused instead of launched again
    Given the TUI has a work item selected
    And another client starts a coding agent for that item while the coding dialog is open
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the system focuses the agent pane the other client started
    And no second coding session is launched
    And the TUI does not report that a session was launched

  @id:F-CODING-LAUNCH-S022
  Scenario: Simultaneous code requests for the same item start exactly one agent
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    When two clients request code for `owner/repo#ISSUE-1` at the same time
    Then exactly one coding agent is launched
    And one response reports status `launched` with the new pane ID
    And the other response reports status `already_running` with the same pane ID

  @id:F-CODING-LAUNCH-S023
  Scenario: Items sharing a worktree cannot run two agents in it
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#PR-5` which shares the worktree of `owner/repo#ISSUE-1`
    When a client requests code for `owner/repo#PR-5` with agent `pi`
    And a client requests code for `owner/repo#ISSUE-1` with agent `claude`
    Then exactly one coding agent is launched
    And the last response reports status `already_running` with the pane ID of the first agent
    And the last response does not report `claude` as the running agent

  @id:F-CODING-LAUNCH-S024
  Scenario: An exited agent pane releases the claim on an existing worktree
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And the worktree of `owner/repo#ISSUE-1` already exists
    And an exited agent pane is left in that worktree
    When a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    Then the last response reports status `launched` with a new pane ID
    And the coding agent starts in the existing worktree

  @id:F-CODING-LAUNCH-S025
  Scenario: Terminal panes and agents of other worktrees do not block a coding session
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And the worktree of `owner/repo#ISSUE-1` already exists
    And a terminal pane is open in that worktree
    And an agent pane is running in another item's worktree
    When a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    Then the last response reports status `launched` with a new pane ID

  @id:F-CODING-LAUNCH-S026
  Scenario: Code starts nothing when Zellij cannot report its panes
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And Zellij cannot report the panes of the Workdash Zellij session
    When a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    Then the API returns an error saying Zellij pane state is unavailable
    And no coding agent is launched
    And the server does not prepare a worktree

  @id:F-CODING-LAUNCH-S027
  Scenario: A launch whose pane never becomes visible is not repeated by a retry
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And launched agent panes never become visible in Zellij
    When a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    And a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    Then exactly one coding agent is launched
    And both responses are errors saying the launch could not be confirmed

  @id:F-CODING-LAUNCH-S028
  Scenario: The TUI stays plan-first and never asks for autonomous work
    Given the TUI has a work item selected
    When the user presses "c"
    And the user picks a supported coding agent from the dialog
    Then the agent is briefed to implement the work item
    And the agent is told to propose a plan and not to start implementing
    And the agent is not told to work autonomously
    And the agent is not told to follow configured practices and skills

  @id:F-CODING-LAUNCH-S029
  Scenario: Autostart appends the autonomous implementation appendix to the briefing once
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the agent is briefed with the original prompt followed by the autonomous appendix once
    And the agent is told to start without routine confirmation, overriding the plan-first pause
    And the agent is told to inspect existing commits and uncommitted work and continue it unless demonstrably defective
    And the agent is told to commit, push, and open a pull request that closes issue `owner/repo#42`
    And the agent is explicitly permitted to commit and push, and to force-push only the pull request branch
    And the agent is told to assign opened or updated pull requests to the configured GitHub user
    And the agent is told to wait for the current head's CI or confirm that no checks are required or reported
    And the agent is told never to merge the pull request or close the issue
    And the agent is told to stay in its interactive session after reporting

  @id:F-CODING-LAUNCH-S030
  Scenario: Autostart on an issue without a pull request is told to implement it
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the agent is told to implement `https://github.com/owner/repo/issues/42`
    And the agent is told to follow its configured practices and skills
    And the agent is told not to open a duplicate pull request

  @id:F-CODING-LAUNCH-S031
  Scenario Outline: Autostart on an issue with an open pull request resumes that pull request
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51`
    And pull request #51 has <ci> CI, <mergeability> mergeability, and no reviews
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the agent works in the checkout of pull request `owner/repo#51`
    And the response is for `owner/repo#ISSUE-42`
    And the agent is told <instruction>
    And the agent is told to follow its configured practices and skills
    And the agent is told not to create another pull request

    Examples:
      | ci      | mergeability | instruction                                        |
      | failing | mergeable    | `has failing checks: tests`                        |
      | passing | conflicting  | `cannot merge cleanly into main (base head base1)` |

  @id:F-CODING-LAUNCH-S032
  Scenario: A pull request with several needs gets all of them, most urgent first
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51`
    And pull request #51 has merge conflicts, failed CI, and new review feedback
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the agent is told to address the review feedback first
    And the agent is told to also resolve the merge conflicts
    And the agent is told to also fix the failing checks

  @id:F-CODING-LAUNCH-S033
  Scenario: Autostart on a pull request of somebody else stays a review
    Given a server-backed Workdash session is ready to answer
    And open pull request `owner/repo#52` of another author closes no issue and its CI failed
    When a client requests code with autostart `true` for `owner/repo#CHECK-52` with agent `pi`
    Then the agent is briefed with the original prompt followed by the autonomous review appendix once
    And the agent is told to start reviewing without routine confirmation
    And the agent is told never to edit, commit, or push
    And the agent is not told to open a pull request
    And the agent is not told to follow configured practices and skills

  @id:F-CODING-LAUNCH-S034
  Scenario: Autostart on a worktree with a live agent sends nothing to that agent, even when GitHub is down
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    And GitHub answers issue work state queries with an outage
    And the worktree of `owner/repo#ISSUE-42` already exists
    And an agent pane is running in that worktree
    When a client requests code with autostart `true` for `owner/repo#ISSUE-42` with agent `pi`
    Then the response reports status `already_running`
    And no coding agent is launched

  @id:F-CODING-LAUNCH-S035
  Scenario: Autostart fails when GitHub cannot report the work state
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    And GitHub answers issue work state queries with an outage
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the API returns error `github_error` with HTTP status 502
    And no coding agent is launched

  @id:F-CODING-LAUNCH-S036
  Scenario Outline: Autostart on a pull request waiting for others describes it without ordering work
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51`
    And pull request #51 has <ci> CI, mergeable mergeability, and no reviews
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the agent works in the checkout of pull request `owner/repo#51`
    And the agent is told `<status>`
    And the agent is told not to create another pull request
    And the agent is not told to start working

    Examples:
      | ci        | status                                                                                                                                                       |
      | pending   | without a final CI result (its current head's checks are still running)                                                                                      |
      | passing   | waiting for human review (its current head's checks pass)                                                                                                    |
      | no_checks | waiting for human review (GitHub reports no required or observed checks for its current head)                                                                |
      | unknown   | without a final CI result (GitHub reports an unknown or unavailable CI status for its current head, which is neither passing nor proof that no checks exist) |

  @id:F-CODING-LAUNCH-S037
  Scenario: A pull request of somebody else closing the issue does not turn the issue into a review
    Given a server-backed Workdash session is ready to answer
    And GitHub issue `owner/repo#42` is open and closed by open pull request `owner/repo#51` of another author
    When a client requests code with autostart `true` for `owner/repo#42` with agent `pi`
    Then the agent works in the checkout of issue `owner/repo#42`
    And the agent is briefed with the original prompt followed by the autonomous appendix once
    And the agent is told to implement `https://github.com/owner/repo/issues/42`
    And the agent is told to follow its configured practices and skills
