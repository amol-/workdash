@feature:F-BRANCHDIFF-VIEW
Feature: Review branch changes from the shell

  @id:F-BRANCHDIFF-S001
  Scenario: Review current changes against the default branch
    Given the default branch has the old renderer and my checkout has the new renderer
    When I run `workdash branchdiff`
    Then the viewer opens on the renderer file with the old and new lines side by side

  @id:F-BRANCHDIFF-S002
  Scenario: Review current changes against a release branch instead
    Given the default branch has the old renderer and my checkout has the new renderer
    And my work is based on the release branch, which has a different renderer
    When I run `workdash branchdiff release`
    Then the viewer compares the release renderer with my new renderer

  @id:F-BRANCHDIFF-S014
  Scenario: Use the whole Zellij pane to review changes and return to the tiled layout
    Given the default branch has the old renderer and my checkout has the new renderer
    And my shell is in a tiled Zellij pane
    When I run `workdash branchdiff` and press q to return to the shell
    Then the diff viewer fills the pane while I review
    And my shell returns to its original tiled layout
