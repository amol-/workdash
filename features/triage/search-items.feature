@feature:F-TRIAGE-SEARCH
Feature: Search work items

  A busy dashboard lists more work than fits on screen. The user narrows
  the list to the items they care about right now by typing a fragment of
  a number, repository, or title, and gets the full list back with a
  single keystroke.

  Rules:
    - The search action is the first entry in the TUI action bar and is bound to "/".
    - Pressing "/" with no active search filter opens a search box; submitting a non-empty string filters the list to matching items.
    - The search filter matches when the typed text appears, ignoring case, anywhere in the entry's Type column (which includes the item number), its Repo column, or its Title column. The typed text is matched literally, including spaces.
    - The Repo column is matched on the full repository name, so an owner cut off by the column's width is still searchable.
    - The Age and Last Update columns are never searched.
    - Pressing "/" while a search filter is active clears the filter and shows the full list again.
    - Closing the search box without submitting leaves the list unchanged.
    - Any action that changes the loaded work item list clears the search filter, so a newly arrived item is never hidden: refreshing, including an item by URL, and capturing a todo all show the full list again.
    - While a search filter is active, the action bar's search entry is emphasized; the action bar never shows the filter text itself.
    - After filtering, the system reports how many of the loaded work items matched.
    - Work actions apply to the entry the cursor is on in the filtered list.
    - The search filter is a view over the already loaded work items: it never changes what the system fetches from GitHub, and it does not affect the list command or the JSON API.

  @id:F-TRIAGE-SEARCH-S001
  Scenario: A search filters the list to matching titles
    Given the TUI is open with a list of work items
    When the user searches for a fragment of one item's title
    Then only the work items whose title contains that fragment are listed
    And the system reports how many of the loaded work items matched

  @id:F-TRIAGE-SEARCH-S002
  Scenario: A search matches the item number shown in the Type column
    Given the TUI is open with a list of work items
    When the user searches for one item's number
    Then only that work item is listed

  @id:F-TRIAGE-SEARCH-S003
  Scenario: A search matches the repository
    Given the TUI is open with work items from two repositories
    When the user searches for one repository's name
    Then only the work items from that repository are listed

  @id:F-TRIAGE-SEARCH-S004
  Scenario: Searching again clears the active filter
    Given the TUI is open with an active search filter
    When the user presses "/"
    Then no search box is shown
    And all loaded work items are listed again

  @id:F-TRIAGE-SEARCH-S005
  Scenario: Refreshing clears the active filter
    Given the TUI is open with an active search filter
    When the user presses "r"
    Then all refreshed work items are listed

  @id:F-TRIAGE-SEARCH-S008
  Scenario: Including an item clears the active filter
    Given the TUI is open with an active search filter
    When the user includes a work item by URL
    Then all loaded work items are listed again
    And the included work item is listed

  @id:F-TRIAGE-SEARCH-S006
  Scenario: The action bar shows search first and marks an active filter
    Given the TUI is open with a list of work items
    Then the action bar lists the search action first
    When the user searches for a fragment of one item's title
    Then the action bar emphasizes the search action
    And the action bar does not show the filter text

  @id:F-TRIAGE-SEARCH-S007
  Scenario: A search that matches nothing reports an empty result
    Given the TUI is open with a list of work items
    When the user searches for text that no work item contains
    Then no work items are listed
    And the system reports that no work items matched
