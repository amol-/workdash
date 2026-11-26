@feature:F-VIEW-ITEM
Feature: Read a document with workdash view

  @id:F-VIEW-ITEM-S001
  Scenario: View an issue
    Given a GitHub issue with sub-issues and comments
    When I run `workdash view "https://github.com/owner/repo/issues/12"`
    Then the document shows the issue metadata, description, sub-issues and comments

  @id:F-VIEW-ITEM-S011
  Scenario: See what remains in an open project without a Workdash server
    Given an open project with a ready child, a blocked child, and a closed child
    And the ready child has the user's writable pull request with failing checks
    And the blocked child has a linked pull request with review feedback and failing checks
    And the Workdash server is unavailable
    When I run `workdash view "owner/repo#12"`
    Then the document shows the open parent as blocked by its open sub-issues
    And it shows the ready child as ready on GitHub with failing PR checks and the closed child as completed
    And the blocked child's status links its blocker and shows its pull request's review and checks
    And the document says it couldn't check whether Workdash is working on this

  @id:F-VIEW-ITEM-S012
  Scenario: See work already underway beneath a closed parent
    Given a closed project with an open child
    And Workdash is coding on the child's linked pull request, whose checks are failing
    When I run `workdash view "owner/repo#12"`
    Then the document keeps the parent closed and the child open
    And the child's linked pull request shows failing checks and the coding pane Workdash maps to it

  @id:F-VIEW-ITEM-S014
  Scenario: See why an open project has no work ready to pick up
    Given an open project whose only child has a clean linked pull request with a pending review request
    When I run `workdash view "owner/repo#12"`
    Then the parent and child remain open without any work marked ready
    And the child's linked pull request shows that it is awaiting review and its reported checks are passing

  @id:F-VIEW-ITEM-S002
  Scenario: View a pull request
    Given a GitHub pull request with checks, reviews and review threads
    When I run `workdash view "owner/repo#12"`
    Then the document shows the pull request metadata, description, CI, reviews, review threads and comments

  @id:F-VIEW-ITEM-S006
  Scenario: A local file path opens the file
    Given a local Markdown document
    When I run `workdash view "local notes.md"`
    Then the document shows the local content

  @id:F-VIEW-ITEM-S008
  Scenario: A remote document is read from its URL
    Given a document served at an HTTPS URL
    When I run `workdash view "https://docs.example/guide.md"`
    Then the document shows the remote content
