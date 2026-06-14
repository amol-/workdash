@feature:F-TRIAGE-INCLUDE
Feature: Include a work item by URL

  The user may encounter an issue or pull request outside of their
  configured tracked repositories that they still want to keep on the
  dashboard until it is resolved. Including an item by pasting its
  GitHub URL makes it participate in every subsequent refresh as an
  included item, alongside authored pull requests, review-requested
  pull requests, assigned issues, and tracked items.

  Rules:
    - Pressing "i" in the TUI opens a modal dialog with a text field that accepts a pasted GitHub URL.
    - Valid URLs point to a GitHub issue or pull request on github.com; trailing path, query, and fragment components are ignored.
    - On successful include the item is fetched, added to the dashboard, persisted to the included-items store, and the cursor moves to it.
    - An included item shows a "+" suffix on its type column in both the TUI and list command (for example "PR+", "ISSUE+", or "REVIEW+").
    - Including a URL that is already shown on the dashboard moves the cursor to that item without duplicating it.
    - Included items are a fetch-time source; downstream sorting, suggestion, and recent-activity rules treat them like any other item.
    - On every refresh the included-items store is re-read and its URLs are re-fetched; items that are no longer open are removed from the store.
    - Invalid URLs and non-GitHub URLs are reported to the user and not persisted.
    - A transient fetch failure during refresh retries the included item on the next refresh.
    - A missing or empty included-items store is not an error: the dashboard loads normally with no included items.

  @id:F-TRIAGE-INCLUDE-S001
  Scenario: Including a pull request URL adds it to the list and moves the cursor to it
    Given the TUI is open
    And the user pastes a pull request URL into the include dialog
    When the user confirms the include dialog
    Then the pull request appears on the dashboard as an included item
    And the cursor is positioned on that pull request
    And the URL is persisted in the included-items store

  @id:F-TRIAGE-INCLUDE-S002
  Scenario: Including an issue URL adds it to the list and moves the cursor to it
    Given the TUI is open
    And the user pastes an issue URL into the include dialog
    When the user confirms the include dialog
    Then the issue appears on the dashboard as an included item
    And the cursor is positioned on that issue
    And the URL is persisted in the included-items store

  @id:F-TRIAGE-INCLUDE-S003
  Scenario: Including a URL already shown on the dashboard moves the cursor without duplicating
    Given the TUI is open with a work item already visible
    And the user pastes that same work item's URL into the include dialog
    When the user confirms the include dialog
    Then the work item appears exactly once on the dashboard
    And the cursor is positioned on that work item

  @id:F-TRIAGE-INCLUDE-S004
  Scenario: Included items are distinguished by a "+" type suffix in TUI and list command
    Given the user has an included pull request, an included issue, and an included review-requested pull request
    When the user opens the dashboard
    Then the included pull request's type column reads "PR+"
    And the included issue's type column reads "ISSUE+"
    And the included review-requested pull request's type column reads "REVIEW+"
    And the same suffixes appear when the user runs `workdash list`

  @id:F-TRIAGE-INCLUDE-S005
  Scenario: Included items persist across restarts
    Given the included-items store contains a URL from a previous session
    When the user opens the dashboard
    Then the included item appears on the dashboard

  @id:F-TRIAGE-INCLUDE-S006
  Scenario: Included items that close, merge, or resolve are removed on the next refresh
    Given the included-items store contains a URL for an item that has since closed
    When the user opens the dashboard
    Then the item does not appear on the dashboard
    And the URL is no longer persisted in the included-items store

  @id:F-TRIAGE-INCLUDE-S007
  Scenario: Invalid or non-GitHub URLs are reported and not persisted
    Given the TUI is open
    And the user pastes a URL that is not a GitHub issue or pull request URL into the include dialog
    When the user confirms the include dialog
    Then the system reports that the URL is not valid
    And no URL is persisted in the included-items store

  @id:F-TRIAGE-INCLUDE-S009
  Scenario: A transient fetch failure retries the included item on the next refresh
    Given the included-items store contains a URL
    And the next fetch for that URL will fail transiently
    When the user opens the dashboard
    Then the system retries the included item on the next refresh

  @id:F-TRIAGE-INCLUDE-S010
  Scenario: A missing or empty included-items store loads normally
    Given the included-items store does not exist
    When the user opens the dashboard
    Then the dashboard loads without error
    And no included items appear on the dashboard
