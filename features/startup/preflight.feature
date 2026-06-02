@feature:F-STARTUP-PREFLIGHT
Feature: Startup preflight checks

  The system depends on an authenticated GitHub CLI and a complete
  configuration file. When a core component is missing, the system bails
  out early with a clear error instead of starting the TUI or producing
  partial results.

  Rules:
    - Before checking for GitHub CLI, the system appends ~/.config/workdash/bin to PATH so a global GitHub CLI is preferred when present and a locally downloaded GitHub CLI is used as the fallback.
    - When the GitHub CLI is not available on PATH after that extension, the system exits with a non-zero status and reports that the GitHub CLI is required.
    - When the GitHub CLI is available but `gh auth status` fails, the system exits with a non-zero status and tells the user to authenticate with `gh auth login`.
    - When the configuration file is missing required fields, the system exits with a non-zero status, lists the missing fields, and tells the user to re-run the configuration wizard.
    - Binary availability and authentication preflight checks run before the system replaces itself with Zellij.
    - Preflight checks run before the system starts the TUI, the refresh, the list command, command-specific Zellij session checks, or orchestration commands.

  @id:F-STARTUP-PREFLIGHT-S001
  Scenario: Missing GitHub CLI aborts startup
    Given the GitHub CLI is not installed on PATH
    When the user runs the system
    Then the system reports that the GitHub CLI is required
    And the system does not replace itself with Zellij
    And the system exits with a non-zero status

  @id:F-STARTUP-PREFLIGHT-S003
  Scenario: Unauthenticated GitHub CLI aborts startup
    Given the GitHub CLI is installed but not authenticated
    When the user runs the system
    Then the system tells the user to authenticate GitHub CLI
    And the system exits with a non-zero status

  @id:F-STARTUP-PREFLIGHT-S002
  Scenario: Incomplete configuration aborts startup
    Given the configuration file is missing a required field
    When the user runs the system
    Then the system lists the missing fields
    And the system tells the user to run the configuration wizard
    And the system exits with a non-zero status
