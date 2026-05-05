@feature:F-STARTUP-PREFLIGHT
Feature: Startup preflight checks

  The system depends on an authenticated GitHub CLI and a complete
  configuration file. When a core component is missing, the system bails
  out early with a clear error instead of starting the TUI or producing
  partial results.

  Rules:
    - When the GitHub CLI is not available on PATH, the system exits with a non-zero status and reports that the GitHub CLI is required.
    - When the configuration file is missing required fields, the system exits with a non-zero status, lists the missing fields, and tells the user to re-run the configuration wizard.
    - Preflight checks run before the system starts the TUI, the refresh, or the print listing.

  @id:F-STARTUP-PREFLIGHT-S001
  Scenario: Missing GitHub CLI aborts startup
    Given the GitHub CLI is not installed on PATH
    When the user runs the system
    Then the system reports that the GitHub CLI is required
    And the system exits with a non-zero status

  @id:F-STARTUP-PREFLIGHT-S002
  Scenario: Incomplete configuration aborts startup
    Given the configuration file is missing a required field
    When the user runs the system
    Then the system lists the missing fields
    And the system tells the user to run the configuration wizard
    And the system exits with a non-zero status
