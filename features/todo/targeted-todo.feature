@feature:F-TODO-TARGET
Feature: Targeted todo work actions

  A todo that names a target repository is work to be done in that repository,
  even though the issue itself lives in the todo repository. The system
  presents it as an item of the target repository and runs code, terminal, and
  analysis actions against the target's code, while keeping the GitHub issue
  where it was created.

  Rules:
    - A todo item without a target is an ordinary issue item of the todo repository, with an ordinary issue Workdash item ID and an ordinary issue worktree.
    - A todo item with a target is listed under the target repository's `owner/name`; for a URL target, this comes from its last two path segments without a `.git` suffix.
    - A targeted todo item's Workdash item ID is `<owner/name>#ISSUE-WT<number>`, where the number is the todo issue number in the todo repository, so it never collides with the target repository's own issue of the same number.
    - Opening a targeted todo item in the browser opens the todo issue in the todo repository.
    - A targeted todo's analysis combines the target repository's code with the title, body, and comments of the GitHub todo issue.
    - Free-form body text or JSON naming other repositories never overrides the metadata target, including after refresh.
    - Code, terminal, and analysis actions for a targeted todo item run in a worktree of the target repository.
    - A targeted todo item's worktree is checked out on a branch named "wt-<number>", created from the target repository's current default branch.
    - A targeted todo item keeps a worktree of its own, separate from the target repository's issue and pull request worktrees.
    - URL targets need no manual clone or worktree preparation; their clone and worktree names follow the same `owner_name` and `owner_name_todo_<number>` conventions as GitHub targets.
    - Repositories on different hosts or at different repository paths are distinct, even when their displayed `owner/name` is the same.
    - If the required local clone or worktree directory belongs to another repository, the action reports the conflict and leaves existing work untouched.

  @id:F-TODO-TARGET-S001
  Scenario Outline: A targeted todo is listed as an item of the target repository
    Given the dashboard includes a todo issue number 110 with target `<target>`
    When the user lists work items
    Then the item is shown for repository `<repository>`
    And the item's Workdash item ID is `<repository>#ISSUE-WT110`

    Examples:
      | target                                   | repository      |
      | owner/repo                               | owner/repo      |
      | https://gitlab.com/owner/repo.git        | owner/repo      |
      | https://codeberg.org/owner/repo.git      | owner/repo      |

  @id:F-TODO-TARGET-S002
  Scenario: A targeted todo opens its own issue in the browser
    Given the dashboard includes a todo issue number 110 with target `owner/repo`
    When the user opens the item in the browser
    Then the system opens the todo issue in the todo repository

  @id:F-TODO-TARGET-S003
  Scenario Outline: A targeted todo works on the target repository's code
    Given the dashboard includes a todo issue number 110 with target `<target>`
    And the target repository has no local clone or worktree
    When the system prepares the item's worktree
    Then the worktree belongs to the `<target>` main clone
    And the worktree directory is named `<directory>`
    And the worktree is checked out on a branch named "wt-110"
    And that branch was created from the target repository's current default branch

    Examples:
      | target                                   | directory                |
      | owner/repo                               | owner_repo_todo_110      |
      | https://gitlab.com/owner/repo.git        | owner_repo_todo_110      |
      | https://codeberg.org/owner/repo.git      | owner_repo_todo_110      |

  @id:F-TODO-TARGET-S004
  Scenario Outline: A coding session for a targeted todo starts in the target worktree
    Given the dashboard includes a todo issue number 110 with target `<target>`
    When the user launches a coding session for that item
    Then the session starts in the target repository's todo worktree
    And the session context describes the todo issue

    Examples:
      | target                                   |
      | owner/repo                               |
      | https://gitlab.com/owner/repo.git        |
      | https://codeberg.org/owner/repo.git      |

  @id:F-TODO-TARGET-S005
  Scenario: A todo without a target stays an item of the todo repository
    Given the dashboard includes a todo issue number 111 with no target
    When the user lists work items
    Then the item is shown for the todo repository
    And the item's Workdash item ID is the ordinary issue ID for that repository and number

  @id:F-TODO-TARGET-S006
  Scenario: Target metadata stays authoritative over body details after refresh
    Given the user has a configured todo repository
    When the user captures a targeted todo with body details naming a different repository
    And the system prepares the item's worktree
    Then the worktree belongs to the `owner/repo` main clone
    When the system refreshes that captured todo from GitHub
    And the system prepares the item's worktree
    Then the worktree belongs to the `owner/repo` main clone
    And the worktree is checked out on a branch named "wt-110"

  @id:F-TODO-TARGET-S007
  Scenario: A terminal opens on a URL target without manual worktree preparation
    Given the dashboard includes a todo issue number 110 with target `https://codeberg.org/owner/repo.git`
    And the target repository has no local clone or worktree
    When the user opens a terminal for that item
    Then the terminal starts in the target repository's todo worktree
    And the worktree directory is named `owner_repo_todo_110`

  @id:F-TODO-TARGET-S008
  Scenario: A URL-targeted todo is analyzed using target code and GitHub todo discussion
    Given the dashboard includes a todo issue number 110 with target `https://codeberg.org/owner/repo.git`
    And the target repository contains a source file `src/cards.py`
    And the todo issue has a description and comments on GitHub
    When the user generates a fresh analysis for that item
    Then the analysis agent starts inside the target repository's todo worktree
    And the agent can inspect `src/cards.py` in the target repository
    And the agent receives the todo issue's title, body, and comments from GitHub

  @id:F-TODO-TARGET-S009
  Scenario: A repository on another host is not reused for a URL target
    Given the dashboard includes a todo issue number 110 with target `https://codeberg.org/owner/repo.git`
    And the local clone directory `owner_repo` belongs to `https://github.com/owner/repo.git`
    When the user opens a terminal for that item
    Then the system reports that the local directory belongs to a different repository
    And no terminal is opened
    And the existing clone and its worktrees remain untouched
