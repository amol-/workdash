@feature:F-API-JSON-CONTROL
Feature: Local JSON control API

  A server-backed Workdash session exposes a JSON API so CLI commands
  and trusted LAN automation agents can inspect and control the same live dashboard
  state as the TUI.

  Rules:
    - `workdash --server` starts the normal TUI and a JSON API server in the same process.
    - The API server starts only after the initial dashboard items have loaded successfully.
    - The API server runs in the background and stops when the TUI process exits.
    - At most one server-backed Workdash session can be active in V0.
    - The server always listens on `127.0.0.1:8765`; repeatable `--listen HOST@IP1,IP2` adds listeners and requires `--server`.
    - Each listener requires a nonempty list of exact source IPs, not hostnames or subnets; missing, malformed, or empty entries are rejected.
    - Listener hosts may be IPs or DNS hostnames; wildcard binds and custom ports are not supported. The port is always 8765.
    - All listeners share the union of accepted source IPs, automatically including localhost.
    - Actual peer source IPs determine access, never forwarding headers; unlisted peers receive a forbidden JSON error without executing actions.
    - If any listener cannot bind, startup fails without leaving other listeners active; exiting the TUI stops every listener.
    - The API is intentionally unauthenticated plain HTTP for trusted LAN use only.
    - V0 exposes JSON APIs only, not an HTML browser UI.
    - API requests and responses use JSON objects.
    - V0 capabilities are exposed as `POST /api/v0/list`, `POST /api/v0/info`, `POST /api/v0/analyze`, `POST /api/v0/code`, `POST /api/v0/terminal`, `POST /api/v0/ready`, `POST /api/v0/todo`, `POST /api/v0/show-config`, `POST /api/v0/pane/content`, `POST /api/v0/pane/send`, and `POST /api/v0/pane/close`.
    - `POST /api/v0/todo` accepts an optional target as a GitHub `owner/repo` name or a git URL on any hosting service.
    - `POST /api/v0/todo` accepts optional body text as long details separate from the title, appended unchanged after the metadata block.
    - Successful API responses use `{ "ok": true, "result": ... }`.
    - Failed API responses use an appropriate HTTP error status and `{ "ok": false, "error": { "code": ..., "message": ... } }`.
    - Work item actions take an explicit target: a Workdash item ID as emitted by `workdash list`, `owner/repo#<number>` for an issue, or a GitHub issue or pull request URL.
    - An explicit target is any issue or pull request GitHub lets the server access, whether or not the dashboard lists it: older than the dashboard keeps, hidden because a pull request closes it, closed, or in an untracked repository.
    - A pull request outside the dashboard accepts any of its `PR`, `REVIEW`, or `CHECK` IDs and is reported with the ID its GitHub data implies.
    - Acting on a target outside the dashboard neither adds it to the dashboard list nor to the included items.
    - Target errors are distinct: a malformed target is `invalid_target` (400), an issue or pull request GitHub cannot find or the server cannot access is `unknown_item` (404), an issue reference naming a pull request or the reverse is `unsupported_item_type` (400), and a failing GitHub request is `github_error` (502).
    - `POST /api/v0/ready` takes a required `scope` and returns the ready work as an ordered `items` list; a pull request ID or URL is not an issue scope and is `unsupported_item_type` (400).
    - Code responses include `status`: `launched` when the request started a coding agent, `already_running` when a live agent pane already works in the item's worktree.
    - Analyze and code requests without an agent use the first configured agent for that action, in configuration order.
    - A refresh request may arrive before the TUI event loop is running; it still succeeds and updates shared state, and the first TUI render uses that shared state.
    - Pane actions accept pane IDs exactly as emitted by `workdash info`.
    - Analyze responses include `content_type`, `file_name`, and base64 `file_content`; there is no `content_encoding` field because content is always base64.
    - CLI analyze treats a missing or invalid base64 `file_content` as an invalid server response instead of falling back to the server cache path.

  @id:F-API-JSON-CONTROL-S001
  Scenario: Server-backed startup exposes a local JSON API with the TUI
    Given no server-backed Workdash session is already running
    When the user starts Workdash with `--server`
    Then the system loads the initial dashboard items
    And the system starts the JSON API on `127.0.0.1:8765`
    And the system starts the TUI using the same dashboard state

  @id:F-API-JSON-CONTROL-S002
  Scenario: A second server-backed session is rejected
    Given a server-backed Workdash session is already running on `127.0.0.1:8765`
    When the user starts another Workdash session with `--server`
    Then the system reports that the Workdash server port is already in use
    And the second session exits with a non-zero status

  @id:F-API-JSON-CONTROL-S003
  Scenario: Server-backed startup keeps the Zellij wrapper behavior
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts Workdash with `--server`
    Then the system replaces itself with a Zellij process
    And the Zellij process runs the dashboard with `--direct --server`
    And the JSON API belongs to the dashboard process inside Zellij

  @id:F-API-JSON-CONTROL-S004
  Scenario: List API returns current dashboard items
    Given a server-backed Workdash session has loaded dashboard items
    When a client requests the list API without refresh
    Then the API returns the current in-memory work items
    And each item includes its Workdash item ID, API type, display type, kind, repository, number, title, URL, timestamps, and suggested status
    And the API does not fetch GitHub before responding

  @id:F-API-JSON-CONTROL-S005
  Scenario: List API can refresh the shared dashboard state
    Given a server-backed Workdash session is running
    When a client requests the list API with refresh enabled
    Then the server refreshes dashboard items from GitHub
    And the API returns the refreshed work items
    And the refreshed work items become the shared dashboard state
    And the live TUI reflects the refreshed state when it can safely repaint

  @id:F-API-JSON-CONTROL-S006
  Scenario: Info API returns current pane state
    Given a server-backed Workdash session is running
    And the Workdash Zellij session has live Workdash-owned panes
    When a client requests the info API
    Then the API returns pane records from the live Zellij session
    And each pane record includes the session, tab, pane ID, title, cwd, command, pane kind, state, and mapped Workdash item when known

  @id:F-API-JSON-CONTROL-S007
  Scenario: Analyze API runs for a known dashboard item
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    When a client requests analysis for `owner/repo#ISSUE-1` with agent `codex`
    Then the server analyzes the known item with the selected configured agent
    And the API returns the item ID, selected agent, analysis path, cache status, and base64 markdown content

  @id:F-API-JSON-CONTROL-S024
  Scenario: Analyze API without an agent uses the first configured analysis agent
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    And the analysis agents `pi` and `claude` are configured in that order
    When a client requests analysis for `owner/repo#ISSUE-1` without an agent
    Then the server analyzes the known item with `pi`
    And the API returns `pi` as the selected agent

  @id:F-API-JSON-CONTROL-S009
  Scenario: Code API launches for a known dashboard item
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    When a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    Then the server launches the selected configured agent for the known item
    And the API returns status `launched` with the item ID, selected agent, selected session, cwd, pane title, and pane ID

  @id:F-API-JSON-CONTROL-S018
  Scenario: Terminal API opens a terminal for a known dashboard item
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    When a client requests terminal for `owner/repo#ISSUE-1`
    Then the server opens a plain terminal in the item's worktree
    And the API returns the item ID, session, cwd, pane title, and pane ID

  @id:F-API-JSON-CONTROL-S015
  Scenario Outline: Todo API captures a todo in the configured todo repository
    Given a server-backed Workdash session has loaded dashboard items
    When a client requests a todo with text `Fix the flaky test` and target `<target>`
    Then the server creates the todo issue in the configured todo repository
    And the new todo becomes part of the shared dashboard state
    And the API returns the Workdash item ID, todo repository, target, issue number, and issue URL

    Examples:
      | target                                   |
      | owner/repo                               |
      | https://gitlab.com/owner/repo.git        |
      | https://codeberg.org/owner/repo.git      |

  @id:F-API-JSON-CONTROL-S016
  Scenario: Todo API rejects invalid input
    Given a server-backed Workdash session has loaded dashboard items
    When a client requests a todo with an invalid target
    Then the API returns an error saying the target must be a GitHub `owner/repo` or a supported git URL
    And the server does not create a GitHub issue

  @id:F-API-JSON-CONTROL-S010
  Scenario: Pane content API returns the visible viewport by default
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When a client requests pane content for `terminal_23`
    Then the server asks Zellij for the current visible pane content
    And the API returns the pane ID and captured content

  @id:F-API-JSON-CONTROL-S011
  Scenario: Pane content API can return full scrollback
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When a client requests full pane content for `terminal_23`
    Then the server asks Zellij for the pane content including scrollback
    And the API returns the pane ID and captured content

  @id:F-API-JSON-CONTROL-S012
  Scenario: Pane send API appends Enter by default
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When a client sends `pwd` to pane `terminal_23`
    Then the server sends `pwd` to that pane
    And the server sends a trailing Enter to that pane
    And the API reports that the input was accepted

  @id:F-API-JSON-CONTROL-S013
  Scenario: Pane send API can send raw input
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_23`
    When a client sends raw `pwd` to pane `terminal_23`
    Then the server sends exactly `pwd` to that pane
    And the server does not send a trailing Enter
    And the API reports that the input was accepted

  @id:F-API-JSON-CONTROL-S014
  Scenario: Pane APIs report Zellij failures as JSON errors
    Given a server-backed Workdash session is running
    When a client requests a pane action for a pane ID that Zellij rejects
    Then the API returns an error with an appropriate HTTP status
    And the error message includes the Zellij failure in user-readable form

  @id:F-API-JSON-CONTROL-S017
  Scenario: Pane close API closes the target pane
    Given a server-backed Workdash session is running
    And `workdash info` reports pane ID `terminal_26`
    When a client requests pane close for `terminal_26`
    Then the server asks Zellij to close pane `terminal_26`
    And the API returns the pane ID and accepted status

  @id:F-API-JSON-CONTROL-S020
  Scenario: Pane close API reports Zellij failures as JSON errors
    Given a server-backed Workdash session is running
    When a client requests pane close for a pane ID that Zellij rejects
    Then the API returns an error with an appropriate HTTP status
    And the error message includes the Zellij failure in user-readable form

  @id:F-API-JSON-CONTROL-S021
  Scenario: Explicit source IPs are shared across listeners
    Given listeners accept source IPs `192.0.2.10` and `192.0.2.11`
    When a client at `192.0.2.11` requests the list API
    Then the API returns the current in-memory work items

  @id:F-API-JSON-CONTROL-S022
  Scenario: Forwarding headers cannot grant access
    Given listeners accept source IPs `192.0.2.10` and `192.0.2.11`
    When a client at `192.0.2.12` requests a todo with a forwarding header claiming `192.0.2.10`
    Then the API returns a forbidden error
    And the server does not create a GitHub issue

  @id:F-API-JSON-CONTROL-S023
  Scenario: Todo API captures long details separately from its title
    Given a server-backed Workdash session has loaded dashboard items
    And the user has a multiline todo body longer than 256 characters
    When a client requests a short todo title with that body and target `owner/repo`
    Then the server creates the todo issue in the configured todo repository
    And the created issue keeps the short title and the complete body after its metadata
    And the new todo becomes part of the shared dashboard state
    And the API returns the Workdash item ID, todo repository, target, issue number, and issue URL

  @id:F-API-JSON-CONTROL-S025
  Scenario Outline: Work item actions run on an accessible item outside the dashboard
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items do not include `<item_id>`
    And GitHub has the accessible <kind> `<item_id>`
    When a client requests <request>
    Then the server runs <action> for `<item_id>` in its worktree
    And the dashboard list and the included items are unchanged

    Examples:
      | action   | kind         | request                                                         | item_id             |
      | analysis | issue        | analysis for `other/tool#7` with agent `codex`                  | other/tool#ISSUE-7  |
      | code     | issue        | code for `owner/repo#ISSUE-99` with agent `pi`                  | owner/repo#ISSUE-99 |
      | terminal | issue        | terminal for `https://github.com/owner/repo/issues/42`          | owner/repo#ISSUE-42 |
      | code     | pull request | code for `owner/repo#REVIEW-5` with agent `pi`                  | owner/repo#CHECK-5  |
      | code     | pull request | code for `https://github.com/owner/repo/pull/5` with agent `pi` | owner/repo#CHECK-5  |

  @id:F-API-JSON-CONTROL-S026
  Scenario: An issue hidden behind the pull request that closes it can still be targeted
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include a pull request that closes `owner/repo#42`
    And GitHub has the accessible issue `owner/repo#ISSUE-42`
    When a client requests terminal for `owner/repo#42`
    Then the server runs terminal for `owner/repo#ISSUE-42` in its worktree

  @id:F-API-JSON-CONTROL-S027
  Scenario Outline: Every issue reference form names the same issue
    Given a server-backed Workdash session has loaded dashboard items
    And GitHub has the accessible issue `owner/repo#ISSUE-42`
    When a client requests terminal for `<target>`
    Then the server runs terminal for `owner/repo#ISSUE-42` in its worktree

    Examples:
      | target                                  |
      | owner/repo#42                           |
      | owner/repo#ISSUE-42                     |
      | https://github.com/owner/repo/issues/42 |

  @id:F-API-JSON-CONTROL-S028
  Scenario Outline: Unusable targets are reported with distinct errors
    Given a server-backed Workdash session has loaded dashboard items
    And GitHub answers item lookups with <answer>
    When a client requests terminal for `<target>`
    Then the API returns error `<code>` with HTTP status <status>
    And the server does not prepare a worktree

    Examples:
      | target              | answer         | code                  | status |
      | owner/repo#banana   | an issue       | invalid_target        | 400    |
      | owner/repo#404      | no item        | unknown_item          | 404    |
      | owner/repo#5        | a pull request | unsupported_item_type | 400    |
      | owner/repo#ISSUE-77 | an outage      | github_error          | 502    |
