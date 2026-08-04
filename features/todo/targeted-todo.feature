@feature:F-TODO-TARGET
Feature: Targeted todo work actions

  A todo that names a target repository is work to be done in that repository,
  even though the issue itself lives in the todo repository. The system
  presents it as an item of the target repository and runs code, terminal, and
  analysis actions against the target's code, while keeping the GitHub issue
  where it was created.

  Rules:
    - A todo item without a target is an ordinary issue item of the todo repository, with an ordinary issue Workdash item ID and an ordinary issue worktree.
    - A todo item with a target is listed under the target repository.
    - A targeted todo item's Workdash item ID is `<target>#ISSUE-WT<number>`, where the number is the todo issue number in the todo repository, so it never collides with the target repository's own issue of the same number.
    - Opening a targeted todo item in the browser opens the todo issue in the todo repository.
    - Analysis context for a targeted todo item is read from the todo issue in the todo repository.
    - Code, terminal, and analysis actions for a targeted todo item run in a worktree of the target repository.
    - A targeted todo item's worktree is checked out on a branch named "wt-<number>", created from the target repository's current default branch.
    - A targeted todo item keeps a worktree of its own, separate from the target repository's issue and pull request worktrees.

  @id:F-TODO-TARGET-S001
  Scenario: A targeted todo is listed as an item of the target repository
    Given the dashboard includes a todo issue number 110 with target `owner/repo`
    When the user lists work items
    Then the item is shown for repository `owner/repo`
    And the item's Workdash item ID is `owner/repo#ISSUE-WT110`

  @id:F-TODO-TARGET-S002
  Scenario: A targeted todo opens its own issue in the browser
    Given the dashboard includes a todo issue number 110 with target `owner/repo`
    When the user opens the item in the browser
    Then the system opens the todo issue in the todo repository

  @id:F-TODO-TARGET-S003
  Scenario: A targeted todo works on the target repository's code
    Given the dashboard includes a todo issue number 110 with target `owner/repo`
    When the system prepares the item's worktree
    Then the worktree belongs to the `owner/repo` main clone
    And the worktree is checked out on a branch named "wt-110"
    And that branch was created from the target repository's current default branch

  @id:F-TODO-TARGET-S004
  Scenario: A coding session for a targeted todo starts in the target worktree
    Given the dashboard includes a todo issue number 110 with target `owner/repo`
    When the user launches a coding session for that item
    Then the session starts in the target repository's todo worktree
    And the session context describes the todo issue

  @id:F-TODO-TARGET-S005
  Scenario: A todo without a target stays an item of the todo repository
    Given the dashboard includes a todo issue number 111 with no target
    When the user lists work items
    Then the item is shown for the todo repository
    And the item's Workdash item ID is the ordinary issue ID for that repository and number
