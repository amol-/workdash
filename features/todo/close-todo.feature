@feature:F-TODO-CLOSE
Feature: Close a todo
  A user can close an issue in the configured todo repository from the CLI or JSON API.
  Confirmation uses the targeted todo's Workdash ID when the todo is resolved;
  otherwise it uses the issue ID in the todo repository.

  @id:F-TODO-CLOSE-S001
  Scenario Outline: Close a todo by reference
    Given the user has a configured todo repository containing issue 42
    When the user closes the todo using `<reference>`
    Then issue 42 is closed in the todo repository
    And the closed todo is removed from the dashboard
    And the user receives confirmation of the closed issue

    Examples:
      | reference                            |
      | 42                                   |
      | #42                                  |
      | owner/todos#ISSUE-42                 |
      | https://github.com/owner/todos/issues/42 |

  @id:F-TODO-CLOSE-S002
  Scenario: Close a todo through the JSON API
    Given the user has a configured todo repository containing issue 42
    When a client requests to close todo `42`
    Then issue 42 is closed in the todo repository
    And the closed todo is removed from the dashboard
    And the API confirms the closed issue
