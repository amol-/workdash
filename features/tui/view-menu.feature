@feature:F-VIEW-MENU
Feature: Choose how to open the selected item

  @id:F-VIEW-MENU-S001
  Scenario: Read the selected item without leaving the dashboard session
    Given I have pressed Down to select the second issue "Crash on startup"
    When I press v then v on the dashboard
    Then I can read "Reproduce with #34 and #35." in a floating viewer in this session
    When I press q in the viewer
    Then I return to the dashboard
    When I open the selected item in my browser from the view menu
    Then my browser opens "https://github.com/owner/repo/issues/12"

  @id:F-VIEW-MENU-S002
  Scenario: Open the selected item in my browser
    Given the dashboard has selected the issue "Crash on startup"
    And my configured browser opens links outside the terminal
    When I press v then o on the dashboard
    Then my browser opens "https://github.com/owner/repo/issues/12"
    And the dashboard confirms that the item was opened

  @id:F-BROWSE-OPEN-S004
  Scenario: Read the GitHub page with a terminal browser
    Given the dashboard has selected the issue "Crash on startup"
    And my configured browser needs its own terminal
    When I press v then o on the dashboard
    Then I can read "https://github.com/owner/repo/issues/12" in a floating viewer in this session

  @id:F-VIEW-MENU-S003
  Scenario: Back out of the view menu
    Given I have pressed Down to select the second issue "Crash on startup"
    When I press v then Escape on the dashboard
    Then I return to the dashboard
    And neither viewer opens
    When I open the selected item in my browser from the view menu
    Then my browser opens "https://github.com/owner/repo/issues/12"

  @id:F-TODO-TARGET-S002
  Scenario: Read the original GitHub issue for a todo shown under its target
    Given the dashboard lists a todo "Fix the flaky test" targeting `owner/repo`
    When the user presses "v" then "o" on the todo
    Then the browser opens `https://github.com/testuser/todos/issues/110`, not an issue in `owner/repo`
