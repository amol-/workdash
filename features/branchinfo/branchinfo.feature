@feature:F-BRANCHINFO-VIEW
Feature: Report branch info in standalone CLI command

  Users need a quick summary of the pull request tied to their current branch
  and the issue it closes without opening a browser. The `workdash branchinfo`
  command reports both from GitHub metadata and works in any git repository
  directory, standalone from the interactive dashboard.

  Rules:
    - The `workdash branchinfo` command works in any git repository directory.
    - It reports the open pull request for the checked-out branch: title, url,
      and a CI/review status symbol.
    - It reports the issue that pull request closes in the same repository,
      when GitHub metadata names one: title and url.
    - When no open pull request, or no same-repository closing issue, can be
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
    Given the current directory is a git repository on a branch with no open pull request
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
