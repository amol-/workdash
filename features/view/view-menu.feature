@feature:F-VIEW-MENU
Feature: Choose how to open the selected item

  The selected work item can be read inside Workdash or handed to the
  configured viewer, and the user picks which one after seeing both.

  Rules:
    - Pressing "v" in the TUI opens a menu offering `view` and `open`.
    - Choosing `view` shows the selected item as a document in a full-size floating pane in the current session, which closes when the viewer exits.
    - Choosing `open` opens the selected item's GitHub URL with the configured viewer.
    - The menu reports a failure to the user and leaves no dialog or progress overlay open.
    - Esc cancels the menu without opening anything.

  @id:F-VIEW-MENU-S001
  Scenario: View the selected item
    Given the TUI has a work item selected
    When the user chooses "view" from the view menu
    Then the item is shown as a document in a full-size floating pane in the current session
    And the TUI reports that the viewer was opened

  @id:F-VIEW-MENU-S002
  Scenario: Open the selected item
    Given the TUI has a work item selected
    When the user chooses to open the selected item
    Then the selected work item's GitHub URL is opened with the configured viewer

  @id:F-VIEW-MENU-S003
  Scenario: Esc cancels the menu
    Given the TUI has a work item selected
    When the user opens the view menu and cancels it
    Then nothing is opened
    And no dialog remains

  @id:F-VIEW-MENU-S004
  Scenario: A failing viewer reports to the user
    Given the TUI has a work item selected
    And the next viewer launch will fail
    When the user chooses "view" from the view menu
    Then the system reports the viewer error details to the user
    And no dialog or progress overlay remains
