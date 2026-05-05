@feature:F-ANALYSIS-CACHE
Feature: Analysis cache freshness

  Analyses are stored locally so repeated access is instant, but they
  must not lie to the user when the underlying work item on GitHub has
  moved on.

  Rules:
    - An analysis is considered cached for a work item only while the cache's recorded last-update timestamp matches the work item's current last-update timestamp on GitHub.
    - When a work item changes on GitHub, its cached analysis is treated as absent until a new analysis is generated.
    - The analyze dialog reflects the cache state it observed at the time the dashboard was last refreshed.

  @id:F-ANALYSIS-CACHE-S001
  Scenario: Updating a work item on GitHub invalidates its cached analysis
    Given a work item has a cached analysis produced before the item's last update on GitHub
    When the user opens the analyze dialog on that work item
    Then the dialog tells the user there is no previous analysis

  @id:F-ANALYSIS-CACHE-S002
  Scenario: Unchanged work items keep their cached analysis
    Given a work item has a cached analysis that matches its last update on GitHub
    When the user opens the analyze dialog on that work item
    Then the dialog offers to open the cached analysis
