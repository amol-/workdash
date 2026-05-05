@feature:F-BROWSE-OPEN
Feature: Open a work item in the browser

  For quick inspection or human action that goes beyond what the TUI
  offers, the user can jump straight to the GitHub page for the selected
  work item.

  Rules:
    - Pressing "o" in the TUI opens the selected work item's GitHub URL in the user's default browser.
    - The TUI reports the outcome of the open action to the user.

  @id:F-BROWSE-OPEN-S001
  Scenario: Open the selected item in the browser
    Given the TUI has a work item selected
    When the user presses "o"
    Then the selected work item's GitHub URL is opened in the user's default browser
    And the TUI reports that the item was opened
