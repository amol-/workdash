@feature:F-TRIAGE-LIST-COMMAND
Feature: List command

  The list command prints the work items known to the active server-backed
  Workdash session. It is intended to let automation and coding agents inspect
  the same dashboard state that the TUI is using, without driving the TUI.

  Rules:
    - `workdash list` is a thin client for the local Workdash JSON API.
    - `workdash list` requires a running `workdash --server` session.
    - The list command does not load configuration, run GitHub preflight, inspect Zellij, or fetch GitHub directly.
    - Running `workdash list` emits a non-interactive listing and exits.
    - The list command never starts the TUI and requires no keyboard input.
    - Without `--refresh`, the list command emits the server's current in-memory dashboard items.
    - With `--refresh`, the list command asks the server to refresh dashboard items before returning them.
    - The list command emits one row per work item sorted by last update, most recently updated first.
    - Each row reports the item type, Workdash item ID, last update date, and title.
    - Workdash item IDs are formatted as `repo#ISSUE-N`, `repo#PR-N`, or `repo#REVIEW-N`.
    - The list command supports `--json` for machine-readable work item records.
    - The suggested item's title is prefixed with "* ".
    - When no work items match, the list command emits an explicit empty-result line.

  @id:F-TRIAGE-LIST-COMMAND-S001
  Scenario: List command lists server dashboard items without launching the TUI
    Given a server-backed Workdash session has loaded dashboard items
    When the user runs `workdash list`
    Then the command requests the current item list from the local Workdash server
    And the system emits one line per work item to standard output
    And the TUI is not started by the command
    And the command exits with a zero status

  @id:F-TRIAGE-LIST-COMMAND-S002
  Scenario: List command marks the suggested item
    Given a server-backed Workdash session has open work items and a suggested item exists
    When the user runs `workdash list`
    Then the suggested item's line has its title prefixed with "* "

  @id:F-TRIAGE-LIST-COMMAND-S003
  Scenario: List command reports an empty result explicitly
    Given a server-backed Workdash session has no open work items
    When the user runs `workdash list`
    Then the system prints that no work items were found

  @id:F-TRIAGE-LIST-COMMAND-S004
  Scenario: List command shows copy/paste Workdash item IDs
    Given a server-backed Workdash session has issue, pull request, and review work items
    When the user lists work items with `workdash list`
    Then each row includes a Workdash item ID
    And the issue row can be copied as `owner/repo#ISSUE-1`
    And the pull request row can be copied as `owner/repo#PR-2`
    And the review row can be copied as `owner/repo#REVIEW-3`

  @id:F-TRIAGE-LIST-COMMAND-S005
  Scenario: List command returns machine-readable work items
    Given a server-backed Workdash session has work items
    When the user lists work items with `workdash list --json`
    Then the command requests the current item list from the local Workdash server
    And the system returns JSON work item records
    And each record includes the Workdash item ID, type, kind, repository, number, title, URL, timestamps, and suggested status

  @id:F-TRIAGE-LIST-COMMAND-S006
  Scenario: List command can refresh the shared dashboard state
    Given a server-backed Workdash session is running
    When the user runs `workdash list --refresh`
    Then the command asks the local Workdash server to refresh dashboard items
    And the system emits the refreshed work items
    And the refreshed work items become the shared dashboard state used by the TUI and API

  @id:F-TRIAGE-LIST-COMMAND-S007
  Scenario: List command requires the local Workdash server
    Given no server-backed Workdash session is running
    When the user runs `workdash list`
    Then the command reports that `workdash --server` must be running
    And the command exits with a non-zero status
