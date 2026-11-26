@feature:F-BRANCHINFO-VIEW
Feature: Identify the work for the checked-out branch

  @id:F-BRANCHINFO-S001
  Scenario: Check whether the branch's pull request is passing CI and approved
    Given I am in a repository subdirectory on a branch with an open pull request
    And that pull request is passing CI and approved, and closes an issue in the same repository
    When I run `workdash branchinfo`
    Then I see the pull request's title, URL, and passing and approved indicators
    And I see the title and URL of the issue it closes

  @id:F-BRANCHINFO-S006
  Scenario: Look up completed work from a branch that is still checked out
    Given I am still on the branch of a merged pull request
    And that pull request closes an already closed issue in the same repository
    When I run `workdash branchinfo`
    Then the output identifies the pull request as merged and the issue as closed

  @id:F-BRANCHINFO-S009
  Scenario: Identify the issue before opening a pull request
    Given I am on a Workdash issue branch with no pull request
    When I run `workdash branchinfo`
    Then the pull request is reported as unknown
    And I see the title and URL of the branch's issue
