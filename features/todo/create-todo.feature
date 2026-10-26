@feature:F-TODO-CREATE
Feature: Capture a todo

  Ideas and chores show up while the user is triaging other work. The system
  lets the user capture one as a todo item in a single configured todo
  repository, without asking them to prepare that repository in any special
  way. A todo is an ordinary GitHub issue, so it is visible, searchable, and
  usable from GitHub itself.

  Rules:
    - The todo repository is a required configuration field naming one `owner/repo`.
    - A todo is created as an open GitHub issue in the todo repository.
    - The todo text is the issue title.
    - The issue body starts with a system-owned JSON metadata block recording the todo format version and the todo target when one was given.
    - A todo issue is assigned to the user, so it appears in the dashboard through the assigned-issue source without any extra configuration.
    - A todo issue is labeled `workdash-todo`.
    - The system creates the `workdash-todo` label in the todo repository when it does not exist yet.
    - A todo may name a target as a GitHub `owner/repo` or a git URL on any hosting service; the target is optional and empty by default.
    - URL targets accept HTTPS, SSH, and scp-like git remote forms and are preserved in the issue body metadata.
    - Existing `owner/repo` todos keep working without edits or migration.
    - The system rejects a todo whose text is empty or whose target is neither `owner/repo` nor a supported git URL, without creating an issue.
    - When the todo repository does not exist or cannot be written to, the system reports the GitHub failure and tells the user to create the todo repository.
    - A created todo becomes part of the current dashboard state immediately, without waiting for the next refresh.
    - CLI and JSON API capture accept optional body content, preserved after the metadata block; omitting it keeps the metadata-only body unchanged.
    - Todo capture is available from the TUI, from the command line, and from the local JSON API.

  @id:F-TODO-CREATE-S001
  Scenario: Capture a generic todo from the TUI
    Given the user has a configured todo repository
    When the user asks the TUI to capture a todo
    Then the system asks for the todo text and an optional target repository
    And submitting text with an empty target creates an open issue in the todo repository
    And that issue is titled with the todo text
    And that issue is assigned to the user
    And that issue is labeled `workdash-todo`
    And the new todo appears in the dashboard as an item of the todo repository

  @id:F-TODO-CREATE-S002
  Scenario Outline: Capture a todo with a target repository
    Given the user has a configured todo repository
    When the user captures a todo with target `<target>`
    Then the system creates the issue in the todo repository
    And the issue body metadata records `<target>` as the todo target
    And the new todo appears in the dashboard as an item of `<repository>`

    Examples:
      | target                                   | repository      |
      | owner/repo                               | owner/repo      |
      | https://gitlab.com/owner/repo.git        | owner/repo      |
      | https://codeberg.org/owner/repo.git      | owner/repo      |

  @id:F-TODO-CREATE-S003
  Scenario: The todo label is created on first use
    Given the user has a configured todo repository
    And the todo repository has no `workdash-todo` label
    When the user captures a todo
    Then the system creates the `workdash-todo` label in the todo repository
    And the created issue is labeled `workdash-todo`

  @id:F-TODO-CREATE-S004
  Scenario: A missing todo repository is reported to the user
    Given the configured todo repository does not exist on GitHub
    When the user captures a todo
    Then the system reports the GitHub failure
    And the system tells the user to create the todo repository
    And no dashboard item is added

  @id:F-TODO-CREATE-S005
  Scenario: Invalid todo input is rejected before touching GitHub
    Given the user has a configured todo repository
    When the user captures a todo with empty text
    Then the system reports that the todo text is required
    And no issue is created
    When the user captures a todo with target `not-a-repo`
    Then the system reports that the target must be a GitHub `owner/repo` or a supported git URL
    And no issue is created

  @id:F-TODO-CREATE-S006
  Scenario Outline: Todo items are recognized again on later refreshes
    Given the user captured a todo with target `<target>` in an earlier session
    When the system refreshes dashboard items
    Then the open `workdash-todo` issues of the todo repository are recognized as todo items
    And each todo item's target is read from its issue body metadata
    And the refreshed todo is listed under `<repository>`

    Examples:
      | target                                   | repository      |
      | owner/repo                               | owner/repo      |
      | https://gitlab.com/owner/repo.git        | owner/repo      |
      | https://codeberg.org/owner/repo.git      | owner/repo      |
