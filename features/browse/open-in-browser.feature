@feature:F-BROWSE-OPEN
Feature: Open a work item in the browser

  For quick inspection or human action that goes beyond what the TUI
  offers, the user can jump straight to the GitHub page for the selected
  work item.

  Rules:
    - Pressing "o" in the TUI opens the selected work item's GitHub URL in the user's default browser.
    - The TUI reports the outcome of the open action to the user.
    - If the browser command fails, the system closes the progress overlay and reports the error details to the user.
    - If the browser command does not respond promptly, the system closes the progress overlay and reports that opening a browser may not be supported from this session.

  @id:F-BROWSE-OPEN-S001
  Scenario: Open the selected item in the browser
    Given the TUI has a work item selected
    When the user presses "o"
    Then the selected work item's GitHub URL is opened in the user's default browser
    And the TUI reports that the item was opened

  @id:F-BROWSE-OPEN-S002
  Scenario: Browser command failure reports the error details
    Given the TUI has a work item selected
    And the next browser open will fail
    When the user presses "o"
    Then the system reports the browser error details to the user
    And no dialog or progress overlay remains

  @id:F-BROWSE-OPEN-S003
  Scenario: Unresponsive browser command reports that browser opening may not be supported
    Given the TUI has a work item selected
    And the next browser open will not respond
    When the user presses "o"
    Then the system reports that browser opening may not be supported from this session
    And no dialog or progress overlay remains
