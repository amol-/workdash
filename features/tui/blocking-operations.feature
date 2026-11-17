@feature:F-TUI-BLOCKING
Feature: Report blocking operations in the dashboard

  Long-running operations can hold the dashboard until they finish. While that
  happens the dashboard shows a blocking dialog naming the operation, and clears
  it when the operation ends. Starting autodispatch returns immediately and is
  announced in the status bar; the dialog then covers the work from the start
  request until the first dispatch begins, and the dashboard stays usable once
  that dispatch begins.

  Rules:
    - An operation that blocks the dashboard shows a blocking dialog naming it.
    - The dialog clears when the operation finishes, whether it succeeds or fails.
    - Keys do not reach the dashboard while the dialog is shown.
    - A failure that ends a blocking operation is reported to the caller.
    - Starting autodispatch returns immediately and is announced in the status bar.
    - The work from the start request until the first dispatch begins blocks the dashboard, and the dashboard stays usable once that dispatch begins.
    - The dashboard stays usable while autodispatch works in the background, and the dispatches that follow do not block it.
    - A dialog the user opened keeps receiving keys while an operation holds the session.

  @id:F-TUI-BLOCKING-S001
  Scenario Outline: An operation that blocks the dashboard names itself
    Given Workdash has work items to show
    When <operation> is requested through the API while the dashboard is open
    Then the dashboard shows a blocking dialog saying "<message>"
    And the dashboard clears the dialog when the operation finishes

    Examples:
      | operation             | message                         |
      | a readiness check     | Checking ready work...          |
      | an analysis           | Analyzing owner/repo#ISSUE-1... |
      | an autodispatch start | Starting autodispatch...        |

  @id:F-TUI-BLOCKING-S002
  Scenario: Keys do not reach the dashboard while the dialog is shown
    Given Workdash has work items to show
    When a readiness check is requested through the API while the user presses keys on the dashboard
    Then the dashboard shows a blocking dialog saying "Checking ready work..."
    And the dashboard ignores the keys pressed while the dialog is shown
    And the dashboard opens the search dialog once the dialog clears

  @id:F-TUI-BLOCKING-S003
  Scenario: A failed operation clears the dialog and reports the failure
    Given Workdash has work items to show
    And the next GitHub lookup fails
    When a readiness check is requested through the API while the dashboard is open
    Then the dashboard shows a blocking dialog saying "Checking ready work..."
    And the dashboard clears the dialog when the operation finishes
    And the API reports that the readiness check failed

  @id:F-TUI-BLOCKING-S004
  Scenario: Starting autodispatch returns immediately and is announced in the status bar
    Given Workdash has work items to show
    And autodispatch can launch the ready issue
    When a background autodispatch session is started through the API while the dashboard is open
    Then the API reports the started autodispatch session
    And the API answered while the session was still starting
    And the dashboard reports in the status bar that autodispatch started
    And the dashboard shows no blocking dialog while the session works in the background

  @id:F-TUI-BLOCKING-S005
  Scenario: The dashboard stays usable while autodispatch works in the background
    Given Workdash has a work item to search
    And autodispatch can launch the ready issue
    When a background autodispatch session is started through the API while the user searches the dashboard
    Then the dashboard shows the matching work item
    And the dashboard shows no blocking dialog while the session works in the background

  @id:F-TUI-BLOCKING-S006
  Scenario: A dialog the user opened keeps receiving keys while an operation holds the session
    Given Workdash has work items to show
    When the user opens search and types while a readiness check is requested through the API
    Then the dashboard shows the typed text in the search box
    And the dashboard shows no blocking dialog over the search dialog
