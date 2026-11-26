@feature:F-TUI-BRANCHDIFF-VIEW
Feature: Read changes in the branch diff viewer

  @id:F-BRANCHDIFF-S004
  Scenario: Review changes in a different file
    Given the diff viewer is open on the first of two changed files
    When I press k, press j, and then click the second file
    Then I see the second file's changes, the first file's changes, and the second file's changes again

  @id:F-BRANCHDIFF-S009
  Scenario: Read the end of a wide changed line
    Given the diff viewer is open on a changed line whose end is off screen
    When I press Right to read farther along the line and Left to return
    Then I see the end of the changed line before returning to its beginning
