@feature:F-CLI-ORCHESTRATION
Feature: CLI orchestration commands

  Automation clients can use Workdash commands as thin clients for the local
  server-backed Workdash session. The commands format server responses for
  humans or pass structured JSON through for agents.

  Rules:
    - `workdash list`, `workdash info`, `workdash ready`, `workdash analyze`, `workdash code`, `workdash terminal`, `workdash todo`, `workdash read`, `workdash write`, and `workdash close` require a running `workdash --server` session.
    - All ten server-backed CLI commands accept `--host HOST[:PORT]` before or after the command, overriding `WORKDASH_HOST`; otherwise they use `WORKDASH_HOST` or `127.0.0.1:8765`. A host without a port uses 8765.
    - `--host` is rejected for server startup and commands other than the ten server-backed clients; `--version` exits immediately.
    - `WORKDASH_HOST` must be an IPv4 address with an optional port; an explicit `--host` may be a DNS hostname and overrides even an invalid `WORKDASH_HOST`. Host selection preserves positional item targets and todo targets, whether GitHub `owner/repo` names or git URLs.
    - Server-backed CLI commands do not load configuration, run GitHub preflight, inspect Zellij, or fetch GitHub directly.
    - Server-backed CLI commands report a clear error naming the selected host when the Workdash server is not reachable.
    - CLI commands that return information support `--json`.
    - `workdash info` reports live pane state from the server-backed Workdash session.
    - Live Zellij pane state is authoritative; pane identifiers are discovered on demand, not persisted.
    - Pane item mappings are based on `pane_cwd`, not pane title.
    - A pane whose cwd is a known worktree or a descendant of one maps to that Workdash item; the most specific matching worktree wins.
    - Planned worktree path names are not known worktrees by name alone; local git metadata must prove the repository relationship.
    - Known worktrees are discovered from Workdash-style per-item directory candidates, then local git metadata must prove the candidate is the repository root and has the expected repository relationship.
    - Scanned PR worktree candidates with a same-name origin under another owner are not known worktrees.
    - An authored pull request that shares the worktree of the issue it closes maps that worktree to the pull request, so its agent pane is reported against the pull request and not as an unknown item.
    - A fork or renamed fork directory must have local upstream metadata matching the base repository before it maps to that repository's item.
    - Manually renamed worktrees outside Workdash-style per-item candidates are unknown; Workdash may prepare its own worktree later.
    - `workdash info` reports only live panes whose titles prove Workdash launched them: `code_` agent panes and `terminal_` terminal panes.
    - Zellij exited or held panes are not live panes.
    - `workdash info --all` also reports live non-plugin panes in the server-backed Workdash session, but extra panes are `unknown` kind with unknown item mapping.
    - Missing optional agent commands mean that agent operation is not configured, not that the whole config is invalid.
    - `workdash analyze` accepts only configured analysis agents reported by `workdash show-config`.
    - `workdash code` launches only configured coding agents reported by `workdash show-config`.
    - `workdash code --group NAME` asks the server to open a new agent in the named Zellij tab, creating it if needed; without the option it opens in the current tab.
    - Code results report the actual tab of the agent pane, including an already-running pane, in human and JSON output.
    - `workdash code` always asks the server to autostart the agent, so the new agent works autonomously from its briefing; there is no `--autostart` option and the TUI never asks for it.
    - `workdash code --autoclose` asks the server to supervise the new pane as well; the CLI polls nothing itself and `--autoclose` never asks for autoclose without autostart.
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
  Scenario: Info inspects panes through the local Workdash server
    Given a server-backed Workdash session is running
    And that session has a `code_owner_repo_1` pane in a known worktree
    And that session has a `terminal_owner_repo_1` pane in the same known worktree
    When the user runs `workdash info`
    Then the command requests pane information from the local Workdash server
    And the system reports the Workdash session name
    And the system reports the agent pane with its Zellij pane identifier, title, cwd, command, tab, and state
    And the system reports the terminal pane with its Zellij pane identifier, title, cwd, command, tab, and state
    And both panes are mapped to the matching Workdash item

  @id:F-CLI-ORCHESTRATION-S004
  Scenario: Info requires the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs `workdash info`
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S005
  Scenario: Info returns machine-readable pane state
    Given a server-backed Workdash session is running
    And that session has Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the command requests pane information from the local Workdash server
    And the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S006
  Scenario: Info keeps unmapped live panes visible
    Given a server-backed Workdash session is running
    And that session has a Workdash-named pane whose cwd does not match a known worktree
    When the user runs `workdash info --json`
    Then the system reports the raw pane information
    And the pane item mapping is marked unknown

  @id:F-CLI-ORCHESTRATION-S007
  Scenario: Info ignores exited or held Workdash panes
    Given a server-backed Workdash session is running
    And that session has live, exited, and held Workdash terminal-backed panes
    When the user runs `workdash info --json`
    Then the system returns JSON pane records
    And the system does not report exited or held panes

  @id:F-CLI-ORCHESTRATION-S008
  Scenario: Top-level JSON flag applies to info
    Given a server-backed Workdash session is running
    And that session has Workdash terminal-backed panes
    When the user runs `workdash --json info`
    Then the system returns JSON pane records
    And each record includes the session, tab, pane identifier, title, cwd, command, pane kind, state, and mapped item when known

  @id:F-CLI-ORCHESTRATION-S009
  Scenario: Analyze resolves a current work item through the local Workdash server
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the server analysis response includes markdown content as base64 with a file name
    And the system returns JSON with the item ID, selected agent, analysis URL, and cache status

  @id:F-CLI-ORCHESTRATION-S010
  Scenario: Orchestration commands require the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs a server-backed orchestration command
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S012
  Scenario: Info can include ordinary live panes on request
    Given a server-backed Workdash session is running
    And that session has Workdash terminal-backed panes
    And that session has ordinary live, exited, held, and plugin panes
    When the user runs `workdash info --all --json`
    Then the command requests pane information from the local Workdash server with ordinary panes included
    And the system returns JSON pane records
    And the system reports the ordinary live pane as unknown kind with raw pane information
    And the system does not report exited, held, or plugin panes

  @id:F-CLI-ORCHESTRATION-S013
  Scenario: Code launches an agent through the local Workdash server
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash code owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests code launch from the local Workdash server
    And the system returns JSON with status `launched`, the item ID, selected agent, selected session, cwd, pane title, and pane ID

  @id:F-CLI-ORCHESTRATION-S027
  Scenario: Terminal opens a plain terminal through the local Workdash server
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue
    When the user runs `workdash terminal owner/repo#ISSUE-1 --json`
    Then the command requests terminal launch from the local Workdash server
    And the system returns JSON with the item ID, session, cwd, pane title, and pane ID

  @id:F-CLI-ORCHESTRATION-S028
  Scenario: Terminal requires the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs `workdash terminal owner/repo#ISSUE-1`
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S014
  Scenario: Code does not expose non-terminal editors
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    When the user runs `workdash code owner/repo#ISSUE-1 --agent vscode --json`
    Then the command requests code launch from the local Workdash server
    And the system reports "Agent 'vscode' is not configured."
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S016
  Scenario: Analyze accepts a partial agent configuration reported by show-config
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    And `workdash show-config` reports only `codex` as an analysis agent
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the server analysis response includes markdown content as base64 with a file name
    And the system returns JSON with the item ID, selected agent, analysis URL, and cache status

  @id:F-CLI-ORCHESTRATION-S017
  Scenario: Server-backed commands skip local GitHub and Zellij preflight
    Given the local Workdash server is reachable
    And the client process cannot find GitHub CLI or Zellij on PATH
    When the user runs `workdash info`
    Then the command still sends the request to the local Workdash server
    And the command formats the server response

  @id:F-CLI-ORCHESTRATION-S019
  Scenario: Read shows pane text through the local Workdash server
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When the user runs `workdash read terminal_23`
    Then the command requests pane content from the local Workdash server
    And the system prints the pane text for direct agent use

  @id:F-CLI-ORCHESTRATION-S020
  Scenario: Read can request full scrollback and JSON output
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When the user runs `workdash read terminal_23 --full --json`
    Then the command requests full pane content from the local Workdash server
    And the system returns JSON with the pane ID, content, and full flag

  @id:F-CLI-ORCHESTRATION-S021
  Scenario: Write sends pane input through the local Workdash server
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When the user runs `workdash write terminal_23 "continue"`
    Then the command sends pane input through the local Workdash server
    And the system confirms that the pane input was accepted

  @id:F-CLI-ORCHESTRATION-S022
  Scenario: Write can send raw input and JSON output
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When the user runs `workdash write terminal_23 "continue" --raw --json`
    Then the command sends raw pane input through the local Workdash server
    And the system returns JSON with the pane ID, raw flag, and accepted status

  @id:F-CLI-ORCHESTRATION-S023
  Scenario: Close closes a pane through the local Workdash server
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_26`
    When the user runs `workdash close terminal_26`
    Then the command requests pane close from the local Workdash server
    And the system confirms that the pane close was accepted

  @id:F-CLI-ORCHESTRATION-S024
  Scenario: Close can return JSON output
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_26`
    When the user runs `workdash close terminal_26 --json`
    Then the command requests pane close from the local Workdash server
    And the system returns JSON with the pane ID and accepted status

  @id:F-CLI-ORCHESTRATION-S030
  Scenario Outline: Todo captures a work item through the local Workdash server
    Given a server-backed Workdash session is running
    When the user runs `workdash todo "Fix the flaky test" --target <target>`
    Then the command captures the todo through the local Workdash server
    And the system reports the created Workdash item ID and issue URL

    Examples:
      | target                                   |
      | owner/repo                               |
      | https://gitlab.com/owner/repo.git        |
      | https://codeberg.org/owner/repo.git      |

  @id:F-CLI-ORCHESTRATION-S031
  Scenario: Todo requires the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs `workdash todo "Fix the flaky test"`
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S032
  Scenario: Info maps the shared worktree of an authored pull request to that pull request
    Given a server-backed Workdash session is running
    And that session has an agent pane in the worktree of the issue an authored pull request closes
    When the user runs `workdash info`
    Then the pane is mapped to that authored pull request

  @id:F-CLI-ORCHESTRATION-S033
  Scenario: Analyze CLI reports the server analysis URL without local files
    Given a server-backed Workdash session has loaded dashboard items
    And the current Workdash items include an assigned issue without cached analysis
    And the generated analysis content is returned by the server
    When the user runs `workdash analyze owner/repo#ISSUE-1 --agent codex --json`
    Then the command requests analysis from the local Workdash server
    And the server analysis response includes markdown content as base64 with a file name
    And the system returns JSON with the item ID, selected agent, analysis URL, cache status, and no server file or content fields

  @id:F-CLI-ORCHESTRATION-S034
  Scenario: Todo selects a remote server without changing its target
    Given a server-backed Workdash session is running
    When the user runs `workdash todo "Fix the flaky test" --target owner/repo --host dashboard.lan`
    Then the command connects to host `dashboard.lan`
    And the command captures the todo through the local Workdash server
    And the system reports the created Workdash item ID and issue URL

  @id:F-CLI-ORCHESTRATION-S035
  Scenario: Todo captures long details separately from its title
    Given a server-backed Workdash session is running
    And the user has a multiline todo body longer than 256 characters
    When the user captures a short todo title with that body using the CLI
    Then the command captures the todo through the local Workdash server
    And the created issue keeps the short title and the complete body after its metadata
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
      | analyze | pi    | Agent 'pi' cannot analyze: it has no analyze command. |
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

  @id:F-CLI-ORCHESTRATION-S039
  Scenario: Item commands report a target GitHub cannot find
    Given a server-backed Workdash session has loaded dashboard items
    And GitHub answers item lookups with no item
    When the user runs `workdash terminal owner/repo#ISSUE-99 --json`
    Then the command requests terminal launch from the local Workdash server
    And the system reports that the work item is unknown
    And the command exits with a non-zero status

  @id:F-CLI-ORCHESTRATION-S040
  Scenario: Code always asks the server to autostart the agent
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash code owner/repo#ISSUE-1 --agent pi`
    Then the command asks the server to code with autostart enabled
    And the agent is briefed with the original prompt followed by the autonomous appendix once

  @id:F-CLI-ORCHESTRATION-S041
  Scenario: Code has no autostart option
    When the user runs `workdash code owner/repo#ISSUE-1 --autostart`
    Then the command exits with a non-zero status
    And the command asks no server to code

  @id:F-CLI-ORCHESTRATION-S042
  Scenario Outline: Host selection is rejected for non-client commands
    When the user runs `workdash --host 127.0.0.1:9001 <command>`
    Then the command rejects `--host` as a client-only option
    And the command exits with a non-zero status

    Examples:
      | command     |
      | --server    |
      | show-config |

  @id:F-CLI-ORCHESTRATION-S043
  Scenario: A remote client launches an agent in a group
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #1
    When the user runs `workdash code owner/repo#ISSUE-1 --group epic --host dashboard.lan --json`
    Then the command connects to host `dashboard.lan`
    And the result reports the agent in tab `epic`

  @id:F-CLI-ORCHESTRATION-S044
  Scenario: Code with autoclose asks the server to supervise the new pane
    Given a server-backed Workdash session is ready to answer
    And GitHub repository `owner/repo` has open issue #42
    When the user runs `workdash code owner/repo#ISSUE-42 --agent pi --autoclose`
    Then the command asks the server to code with autostart and autoclose enabled
