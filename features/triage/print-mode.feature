@feature:F-TRIAGE-PRINT
Feature: Print mode listing

  Print mode emits the same dashboard content as a plain text listing
  without starting the TUI. It is intended to let automation and coding
  agents inspect the user's work queue programmatically, iterate over it,
  and drive the same triage decisions a human would make from the TUI.

  Rules:
    - Running the system with "--print" emits a non-interactive listing and exits.
    - Print mode never starts the TUI and requires no keyboard input.
    - Print mode emits one row per work item sorted by last update, most recently updated first.
    - Each row reports the item type, Workdash item ID, last update date, and title.
    - Workdash item IDs are formatted as `repo#ISSUE-N`, `repo#PR-N`, or `repo#REVIEW-N`.
    - Print mode supports `--json` for machine-readable work item records.
    - The suggested item's title is prefixed with "* ".
    - When no work items match, print mode emits an explicit empty-result line.

  @id:F-TRIAGE-PRINT-S001
  Scenario: Print mode lists work items without launching the TUI
    Given the user has open work items
    When the user runs the system with "--print"
    Then the system emits one line per work item to standard output
    And the TUI is not started
    And the system exits with a zero status

  @id:F-TRIAGE-PRINT-S002
  Scenario: Print mode marks the suggested item
    Given the user has open work items and a suggested item exists
    When the user runs the system with "--print"
    Then the suggested item's line has its title prefixed with "* "

  @id:F-TRIAGE-PRINT-S003
  Scenario: Print mode reports an empty result explicitly
    Given the user has no open work items
    When the user runs the system with "--print"
    Then the system prints that no work items were found

  @id:F-TRIAGE-PRINT-S004
  Scenario: Print mode shows copy/paste Workdash item IDs
    Given the dashboard has issue, pull request, and review work items
    When the user lists work items with `workdash --print`
    Then each row includes a Workdash item ID
    And the issue row can be copied as `owner/repo#ISSUE-1`
    And the pull request row can be copied as `owner/repo#PR-2`
    And the review row can be copied as `owner/repo#REVIEW-3`

  @id:F-TRIAGE-PRINT-S005
  Scenario: Print mode returns machine-readable work items
    Given the dashboard has work items
    When the user lists work items with `workdash --print --json`
    Then the system returns JSON work item records
    And each record includes the Workdash item ID, type, kind, repository, number, title, URL, timestamps, and suggested status
