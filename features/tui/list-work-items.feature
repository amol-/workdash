@feature:F-TRIAGE-LIST
Feature: Triage work on the dashboard

  @id:F-TRIAGE-LIST-S001
  Scenario: Pick up the suggested work from one dashboard
    Given the user has an authored pull request, a review request, an assigned issue, and other tracked work
    When the user opens the dashboard and uses "v" then "o" on the starred row
    Then all of that work is visible together
    And the starred item's GitHub page opens

  @id:F-TRIAGE-LIST-S018
  Scenario: Monitor the work autodispatch is doing
    Given the dashboard shows a leaf issue being worked on by autodispatch
    When autodispatch stops working on the leaf issue after its title changes
    Then the leaf issue shows its updated title and closed status without the working highlight

  @id:F-TRIAGE-LIST-S012
  Scenario: Find a pull request again after reviewing it voluntarily
    Given the dashboard lists a pull request as VR
    And the user has since reviewed that pull request on GitHub
    When the user presses "r" to reload the reviewed pull request
    Then the same pull request is listed as RV

  @id:F-TRIAGE-LIST-S013
  Scenario: Return to work using the pull request instead of its issue
    Given an open pull request closes an assigned issue
    And both the pull request and that issue are open work for the user
    When the user returns to the dashboard
    Then the pull request appears on the dashboard
    And the issue it closes does not appear on the dashboard

  @id:F-TRIAGE-LIST-S020
  Scenario: Hide repositories from the dashboard
    Given work items in `team/old`, `team/current`, and `archive/project`
    And the configuration excludes `Team/OLD` and `Archive/*`
    When the user opens the dashboard with these exclusions
    Then only `team/current` appears in the work table
    And no work item is marked as suggested
