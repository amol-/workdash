@feature:F-CONFIG-SHOW-CONFIG
Feature: Find configured agents and the server address

  @id:F-CONFIG-SHOW-CONFIG-S002
  Scenario: Find which agents and server address to use before starting the dashboard
    Given no server-backed Workdash session is running
    And the user has configured Codex for analysis and pi for coding
    And `WORKDASH_HOST` is set to `127.0.0.1:18765`
    When the user runs `workdash show-config`
    Then the command lists `codex` for analysis and `pi` for coding
    And the command shows `127.0.0.1:18765` as the server address to use
