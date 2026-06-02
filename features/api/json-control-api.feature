@feature:F-API-JSON-CONTROL
Feature: Local JSON control API

  A server-backed Workdash session exposes a localhost JSON API so CLI commands
  and local automation agents can inspect and control the same live dashboard
  state as the TUI.

  Rules:
    - `workdash --server` starts the normal TUI and a JSON API server in the same process.
    - The API server starts only after the initial dashboard items have loaded successfully.
    - The API server runs in the background and stops when the TUI process exits.
    - At most one server-backed Workdash session can be active in V0.
    - The V0 server listens on fixed address `127.0.0.1:8765`.
    - The V0 server rejects requests whose client address is not localhost.
    - V0 has no authentication because the server is localhost-only.
    - V0 exposes JSON APIs only, not an HTML browser UI.
    - API requests and responses use JSON objects.
    - V0 capabilities are exposed as `POST /api/v0/list`, `POST /api/v0/info`, `POST /api/v0/analyze`, `POST /api/v0/code`, `POST /api/v0/show-config`, `POST /api/v0/pane/content`, and `POST /api/v0/pane/send`.
    - Successful API responses use `{ "ok": true, "result": ... }`.
    - Failed API responses use an appropriate HTTP error status and `{ "ok": false, "error": { "code": ..., "message": ... } }`.
    - Work item actions accept Workdash item IDs exactly as emitted by `workdash list`.
    - Pane actions accept pane IDs exactly as emitted by `workdash info`.

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
    And each item includes its Workdash item ID, type, kind, repository, number, title, URL, timestamps, and suggested status
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
  Scenario: Analyze API runs only for a known dashboard item
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    When a client requests analysis for `owner/repo#ISSUE-1` with agent `codex`
    Then the server analyzes the known item with the selected configured agent
    And the API returns the item ID, selected agent, analysis path, and cache status

  @id:F-API-JSON-CONTROL-S008
  Scenario: Analyze API rejects an unknown item
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items do not include `owner/repo#ISSUE-99`
    When a client requests analysis for `owner/repo#ISSUE-99` with agent `codex`
    Then the API returns an error saying the work item is unknown
    And the server does not fetch the item outside the current dashboard state
    And the server does not prepare a worktree

  @id:F-API-JSON-CONTROL-S009
  Scenario: Code API launches only for a known dashboard item
    Given a server-backed Workdash session has loaded dashboard items
    And the current dashboard items include `owner/repo#ISSUE-1`
    When a client requests code for `owner/repo#ISSUE-1` with agent `pi`
    Then the server launches the selected configured terminal-backed agent for the known item
    And the API returns the item ID, selected agent, selected session, cwd, pane title, and pane ID

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
