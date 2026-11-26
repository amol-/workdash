@feature:F-TODO-CLOSE
Feature: Close a todo
  Close a captured issue when there is no more work to do on it.

  @id:F-TODO-CLOSE-S001
  Scenario: Close a todo by reference
    Given the dashboard lists todo `owner/todos#ISSUE-42`
    When the user runs `workdash todo --close owner/todos#ISSUE-42`
    Then the command confirms that `owner/todos#ISSUE-42` was closed
    And the closed todo is removed from the dashboard
