@feature:F-CLI-ORCHESTRATION
Feature: CLI orchestration commands

  Automation clients can use Workdash commands as thin clients for the local
  server-backed Workdash session. The commands format server responses for
  humans or pass structured JSON through for agents.

  Rules:
    - `workdash list`, `workdash info`, `workdash ready`, `workdash dispatch`, `workdash analyze`, `workdash code`, `workdash terminal`, `workdash todo`, `workdash read`, `workdash write`, and `workdash close` require a running `workdash` session.
    - All eleven server-backed CLI commands accept `--host HOST[:PORT]` before or after the command, overriding `WORKDASH_HOST`; otherwise they use `WORKDASH_HOST` or `127.0.0.1:8765`. A host without a port uses 8765.
    - `--host` is rejected for server startup and commands other than the eleven server-backed clients; `--version` exits immediately.
    - `WORKDASH_HOST` must be an IPv4 address with an optional port; an explicit `--host` may be a DNS hostname and overrides even an invalid `WORKDASH_HOST`. Host selection preserves positional item targets and todo targets, whether GitHub `owner/repo` names or git URLs.
    - Server-backed CLI commands do not load configuration, run GitHub preflight, inspect Zellij, or fetch GitHub directly.
    - Server-backed CLI commands report a clear error naming the selected host when the Workdash server is not reachable.
    - CLI commands that return information support `--json`.
    - `workdash info` reports live pane state from the server-backed Workdash session.
    - Live Zellij pane state is authoritative; pane identifiers are discovered on demand, not persisted.
    - An agent pane maps to its launch worktree's item even after changing directory. If the launch title does not identify one known worktree and item, the mapping is unknown.
    - A terminal pane whose cwd is a known worktree or a descendant of one maps to that Workdash item; the most specific matching worktree wins.
    - Planned worktree path names are not known worktrees by name alone; local git metadata must prove the repository relationship.
    - Known worktrees are discovered from Workdash-style per-item directory candidates, then local git metadata must prove the candidate is the repository root and has the expected repository relationship.
    - Scanned PR worktree candidates with a same-name origin under another owner are not known worktrees.
    - An authored pull request can be identified by its agent pane in an issue-numbered worktree when it is the only known item for that worktree.
    - A fork or renamed fork directory must have local upstream metadata matching the base repository before it maps to that repository's item.
    - Manually renamed worktrees outside Workdash-style per-item candidates are unknown; Workdash may prepare its own worktree later.
    - `workdash info` reports only live panes whose titles prove Workdash launched them: `code_` agent panes and `terminal_` terminal panes.
    - Zellij exited or held panes are not live panes.
    - `workdash info --all` also reports live non-plugin panes in the server-backed Workdash session, but extra panes are `unknown` kind with unknown item mapping.
    - Missing optional agent commands mean that agent operation is not configured, not that the whole config is invalid.
    - `workdash analyze` accepts only configured analysis agents reported by `workdash show-config`.
    - `workdash analyze --group NAME` and `workdash review --group NAME` ask the server to open the analysis or review pane in the named Zellij tab, creating it if needed; without the option the pane opens in the tab named after the code repository's basename.
    - `workdash code` launches only configured coding agents reported by `workdash show-config`.
    - `workdash code --group NAME` asks the server to open a new agent in the named Zellij tab, creating it if needed; without the option it opens in the current tab.
    - Code results report the actual tab of the agent pane, including an already-running pane, in human and JSON output.
    - `workdash code` launches a plan-first coding session by default. Use `--autostart` to have the new agent work autonomously from its briefing.
    - `workdash code --autoclose` implies `--autostart` and asks the server to supervise the new pane; the CLI polls nothing itself.
    - A configured agent that lacks the command for the requested action is reported as unable to do that action, not as unconfigured.
    - Without `--agent`, `workdash analyze` and `workdash code` use the first configured agent for that action, in configuration order.
    - `workdash analyze`, `workdash code`, and `workdash terminal` accept any explicit target the JSON API accepts, including items `workdash list` does not show; the server resolves it.
    - `workdash code` reports status `launched` with the created Zellij pane ID, or status `already_running` with the live agent pane already working in the item's worktree; pane IDs come from live session state and are never persisted.
    - There is no claim command: the live agent pane started by `workdash code` is the claim.
    - `workdash terminal` opens a plain terminal in the item's worktree and reports the created Zellij pane ID.
    - `workdash terminal` does not accept an agent parameter.
    - `workdash code` does not expose non-terminal editors such as VSCode in V0.
    - `workdash read` returns pane text from the server-backed pane content API, with `--full` requesting full scrollback.
    - `workdash write` sends pane input through the server-backed pane send API, appending Enter unless raw input is requested.
    - `workdash close` closes a pane through the server-backed pane close API.
    - `workdash analyze` returns markdown analysis content as base64 in the server response with `content_type`, `file_name`, and `file_content`.
    - `workdash analyze` reports the server-provided analysis `url` and writes no local file; the server path stays internal.
    - `workdash todo` captures a todo through the server, taking the title as its text argument, an optional `--target` GitHub `owner/repo` name or git URL, and optional long details via `--body BODY`.
    - `workdash todo` reports the created Workdash item ID and the created issue URL.

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

  @id:F-CLI-ORCHESTRATION-S013
  Scenario: Code launches an agent through the local Workdash server
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash code owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests code launch from the local Workdash server
    And the system returns JSON with status `launched`, the item ID, selected agent, selected session, cwd, pane title, and pane ID

  @id:F-CLI-ORCHESTRATION-S027
  Scenario: Open a shell to inspect an issue's checkout
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue
    When the user runs `workdash terminal owner/repo#ISSUE-1`
    Then the command identifies the new shell's pane and checkout for `owner/repo#ISSUE-1`

  @id:F-CLI-ORCHESTRATION-S014
  Scenario: Code does not expose non-terminal editors
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash code owner/repo#ISSUE-1 --agent vscode --json`
    Then the command requests code launch from the local Workdash server
    And the system reports "Agent 'vscode' is not configured."
    And the command exits with a non-zero status

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

  @id:F-CLI-ORCHESTRATION-S036
  Scenario Outline: A configured agent without the command for the action is reported as unable to do it
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    And the configured agents are `pi` with only a launch command and `aider` with only an analyze command
    When the user runs `workdash <action> owner/repo#ISSUE-1 --agent <agent>`
    Then the system reports "<error>"
    And the command exits with a non-zero status

    Examples:
      | action  | agent | error                                                 |
      | code    | aider | Agent 'aider' cannot code: it has no launch command.  |

  @id:F-CLI-ORCHESTRATION-S037
  Scenario: A remote client codes on an issue outside the dashboard
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items do not include `owner/repo#ISSUE-99`
    And GitHub has the accessible issue `owner/repo#ISSUE-99`
    And the client process cannot find GitHub CLI or Zellij on PATH
    When the user runs `workdash code owner/repo#99 --agent pi --host dashboard.lan --json`
    Then the command connects to host `dashboard.lan`
    And the system returns JSON with status `launched` for `owner/repo#ISSUE-99` and the new pane ID

  @id:F-CLI-ORCHESTRATION-S038
  Scenario: Code reports an already running agent instead of starting another
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And the worktree of `owner/repo#ISSUE-1` already exists
    And an agent pane is running in that worktree
    When the user runs `workdash code owner/repo#ISSUE-1 --agent pi`
    Then the system prints status `already_running` and the running pane ID
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
  Scenario: A remote client launches an agent in a group
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash code owner/repo#ISSUE-1 --group epic --host dashboard.lan --json`
    Then the command connects to host `dashboard.lan`
    And the result reports the agent in tab `epic`

  @id:F-CLI-ORCHESTRATION-S046
  Scenario: Start a pull request review
    Given an open pull request is available for review
    When the user runs `workdash review owner/repo#PR-2`
    Then the command reports that the review started and where to find its document
