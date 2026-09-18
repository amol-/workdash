@feature:F-BRANCHINFO-VIEW
Feature: Report branch info in standalone CLI command

  Users need a quick summary of the pull request tied to their current branch
  and the issues it closes without opening a browser. The `workdash branchinfo`
  command reports both from GitHub metadata and works in any git repository
  directory, standalone from the interactive dashboard.

  Rules:
    - The `workdash branchinfo` command works in any git repository directory.
    - It reports the pull request for the checked-out branch: title, url, and a
      CI/review status symbol.
    - It reports every issue that pull request closes in the same repository,
      one per line: title and url.
    - Merged or closed pull requests and issues are still reported, marked with
      their state, so work that continues after a merge stays visible.
    - When no pull request, or no same-repository closing issue, can be
      resolved from GitHub metadata alone, that field is reported as "unknown".

  @id:F-BRANCHINFO-S001
  Scenario: Branchinfo shows a passing, approved pull request and the issue it closes
    Given the current directory is a git repository on a branch with an open pull request
    And that pull request is passing CI and approved, and closes an issue in the same repository
    When the user runs "workdash branchinfo"
    Then the command reports the pull request's title, url, and a passing, approved symbol
    And the command reports the closed issue's title and url

  @id:F-BRANCHINFO-S002
  Scenario: Branchinfo reports no pull request as unknown
    Given the current directory is a git repository on a branch with no pull request
    When the user runs "workdash branchinfo"
    Then the command reports the pull request as unknown

  @id:F-BRANCHINFO-S003
  Scenario: Branchinfo reports a closing issue in another repository as unknown
    Given the current directory is a git repository on a branch with an open pull request
    And that pull request closes an issue in a different repository
    When the user runs "workdash branchinfo"
    Then the command reports the issue as unknown

  @id:F-BRANCHINFO-S004
  Scenario: Branchinfo command handles non-git directory
    Given the current directory is not a git repository
    When the user runs "workdash branchinfo"
    Then the command reports an error
    And exits with non-zero status

  @id:F-BRANCHINFO-S005
  Scenario: Branchinfo command works from a repository subdirectory
    Given the current directory is a git repository on a branch with an open pull request
    And that pull request is passing CI and approved, and closes an issue in the same repository
    When the user runs "workdash branchinfo" from a subdirectory of the repository
    Then the command reports the pull request's title, url, and a passing, approved symbol
    And the command reports the closed issue's title and url

  @id:F-BRANCHINFO-S006
  Scenario: Branchinfo marks a merged pull request and its closed issue with their state
    Given the current directory is a git repository on a branch whose pull request is merged
    And that pull request closes an already closed issue in the same repository
    When the user runs "workdash branchinfo"
    Then the command reports the pull request marked as merged
    And the command reports the issue marked as closed

  @id:F-BRANCHINFO-S007
  Scenario: Branchinfo lists every issue the pull request closes
    Given the current directory is a git repository on a branch with an open pull request
    And that pull request closes two issues in the same repository
    When the user runs "workdash branchinfo"
    Then the command reports both issues' titles and urls

  @id:F-BRANCHINFO-S008
  Scenario: Branchinfo shows a pending reviewer's question mark alongside the CI symbol
    Given the current directory is a git repository on a branch with an open pull request
    And that pull request is passing CI with a reviewer requested who has not yet reviewed
    When the user runs "workdash branchinfo"
    Then the command reports the pull request's title, url, and a passing symbol followed by a question mark
