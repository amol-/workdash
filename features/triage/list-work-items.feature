@feature:F-TRIAGE-LIST
Feature: List work items

  The user opens the system to see the GitHub issues and pull requests
  that matter to them across their tracked repositories, ranked so the
  most relevant item to pick up next is easy to spot.

  Rules:
    - The list includes open pull requests the user authored, open pull requests where the user is a REVIEW item, open issues assigned to the user, open issues and pull requests across tracked repositories, and items the user has explicitly included by URL.
    - A pull request is a REVIEW item when the user is directly requested as a reviewer, or when the user has already reviewed it.
    - Review requests addressed only to a team the user belongs to do not make the pull request a REVIEW item; only direct user review requests do.
    - Each TUI entry's Type column shows its item type (ISSUE, PR, or REVIEW) followed immediately by its GitHub number (for example, `ISSUE#123`), alongside the owning repository, last update date, item's age, and title.
    - Tracked repositories can hold years of open work, so the number of discovered work items is capped and the oldest ones beyond that cap are dropped. Todo items and items the user included by URL are asked for by hand, so they are always listed even when they are older than everything else.
    - An authored pull request's Type column is prefixed with a one-character symbol for the latest CI result GitHub reports on it: passing, failing, or still running. Every other row, including issues and pull requests the user did not author, is prefixed with a blank instead so the Type labels stay aligned.
    - The Repo column is capped at the width of `posit-dev/rsconnect-python`, leaving the title more room. A longer repository is truncated on its left so the repository name itself stays readable, and the leading character is replaced with an ellipsis to show the owner was cut.
    - Entries are sorted by last update, most recently updated first.
    - The same GitHub issue or pull request never appears twice in the list.
    - When the same item qualifies for multiple sources, the strongest relationship wins in this order: authored pull request, then REVIEW pull request, then assigned issue, then plain tracked item.
    - When GitHub does not return a repository or item while loading a specific work source, either because further repository authorization is required or because GitHub can no longer resolve it, the system warns the user, skips that inaccessible repository or item, and keeps loading other work.
    - When no work items match, the system reports that no work items were found.
    - Scrolling the TUI list keeps each visual row tied to the same GitHub issue or pull request; included items may show a "+" type suffix but must not create stale, duplicate, or empty visual rows.
    - The dashboard is keyboard-driven: it never captures mouse input, so mouse behavior (text selection, wheel scrolling) stays with the terminal.

  @id:F-TRIAGE-LIST-S001
  Scenario: Authored, review, assigned, and tracked items appear together
    Given the user has open work across all supported sources
    When the user opens the dashboard
    Then authored pull requests appear as PR items
    And pull requests requiring the user's review appear as REVIEW items
    And issues assigned to the user appear as ISSUE items
    And other open issues and pull requests in tracked repositories appear as ISSUE or PR items

  @id:F-TRIAGE-LIST-S008
  Scenario: Type column shows the GitHub number
    Given the dashboard has issue, pull request, and review work items
    When the user opens the dashboard
    Then the Type column shows `ISSUE#1`, `PR#2`, and `REVIEW#3`

  @id:F-TRIAGE-LIST-S002
  Scenario: Only direct user review requests are treated as REVIEW items
    Given a pull request has requested only a team the user belongs to
    And a separate pull request has requested the user directly
    When the user opens the dashboard
    Then the team-only pull request does not appear as a REVIEW item
    And the directly requested pull request appears as a REVIEW item

  @id:F-TRIAGE-LIST-S003
  Scenario: Items are sorted by most recently updated first
    Given the user has several open work items with different last update times
    When the user opens the dashboard
    Then the most recently updated item is listed first
    And older items follow in decreasing order of last update

  @id:F-TRIAGE-LIST-S004
  Scenario: A pull request the user authored is not duplicated from tracked sources
    Given a pull request the user authored also lives in a tracked repository
    When the user opens the dashboard
    Then the pull request appears exactly once
    And it is classified as an authored pull request

  @id:F-TRIAGE-LIST-S005
  Scenario: Empty result is reported explicitly
    Given the user has no open work items matching any source
    When the user opens the dashboard
    Then the system reports that no work items were found

  @id:F-TRIAGE-LIST-S006
  Scenario: Repository authorization failure skips only that tracked repository
    Given one tracked repository requires additional GitHub authorization
    And another tracked repository has open work
    When the user opens the dashboard
    Then the accessible repository's work items appear
    And the system warns that the unauthorized repository was skipped

  @id:F-TRIAGE-LIST-S009
  Scenario: Authored pull requests show their latest CI result
    Given the user has authored pull requests whose CI is passing, failing, and still running
    When the user opens the dashboard
    Then each authored pull request's Type column carries the symbol for its CI result
    And an issue's Type column carries no CI symbol

  @id:F-TRIAGE-LIST-S007
  Scenario: Repository authorization failure skips one review-requested pull request
    Given one review-requested pull request requires additional GitHub authorization
    And another review-requested pull request has requested the user directly
    When the user opens the dashboard
    Then the authorized review-requested pull request appears
    And the system warns that the unauthorized review-requested pull request was skipped
