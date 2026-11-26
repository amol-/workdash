@feature:F-TUI-BRANCHDIFF-SPAWN
Feature: Review pull request changes from the dashboard

  @id:F-TUI-BRANCHDIFF-SPAWN-S001
  Scenario: Compare the selected pull request with its release branch
    Given the selected pull request updates the renderer on a release branch
    When I press c and then d to open branchdiff
    Then the new diff pane shows the release renderer and the proposed renderer side by side
