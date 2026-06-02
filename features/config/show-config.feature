@feature:F-CONFIG-SHOW-CONFIG
Feature: Show configured automation options

  Users and automation clients can discover which agents and server address are
  valid before choosing arguments for analysis, coding, or direct API calls.

  Rules:
    - `workdash show-config` is a local command and does not require a running Workdash server.
    - `workdash show-config` reports configured agents, not a live executable health check.
    - The show-config API reports the same configured agents from the server-backed session.
    - Configured analysis agents are agents with an analyze command configured.
    - Configured coding agents are terminal-backed agents with a launch command configured.
    - V0 coding agents do not include non-terminal editors such as VSCode.
    - The server address is reported as `127.0.0.1:8765`.
    - JSON output is available for automation clients.

  @id:F-CONFIG-SHOW-CONFIG-S001
  Scenario: Local show-config reports configured agents and server address
    Given the configuration has Codex analysis configured
    And the configuration has Codex and pi coding configured
    When the user runs `workdash show-config --json`
    Then the system reports `codex` as an analysis agent
    And the system reports `codex` and `pi` as coding agents
    And the system reports the server host `127.0.0.1`
    And the system reports the server port `8765`

  @id:F-CONFIG-SHOW-CONFIG-S002
  Scenario: Local show-config does not require a server
    Given no server-backed Workdash session is running
    When the user runs `workdash show-config`
    Then the system reports the configured automation options
    And the command exits with a zero status

  @id:F-CONFIG-SHOW-CONFIG-S003
  Scenario: Show-config API reports configured agents to HTTP clients
    Given a server-backed Workdash session is running
    When a client requests the show-config API
    Then the API returns the configured analysis agents
    And the API returns the configured coding agents
    And the API returns the server host and port

  @id:F-CONFIG-SHOW-CONFIG-S004
  Scenario: Missing optional agent commands are omitted from show-config
    Given the configuration has Codex analysis configured
    And the configuration has no Claude analyze command
    And the configuration has no pi launch command
    When the user runs `workdash show-config --json`
    Then the system reports `codex` as an analysis agent
    And the system does not report `claude` as an analysis agent
    And the system does not report `pi` as a coding agent
