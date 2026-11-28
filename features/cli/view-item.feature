@feature:F-VIEW-ITEM
Feature: Read a document with workdash view

  With a running Workdash server, the viewer starts coding or autodispatch on
  the issue or pull request the document is about. A file or a URL names that
  item with `--target`.

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

  @id:F-VIEW-ITEM-S015
  Scenario: Start coding from an issue document
    Given a GitHub issue with sub-issues and comments
    And the Workdash server offers a coding agent for the viewer
    When I run `workdash view "https://github.com/owner/repo/issues/12"` and choose an agent with "c"
    Then the viewer reports the launched coding pane

  @id:F-VIEW-ITEM-S016
  Scenario: Start autodispatch from a local report about an issue
    Given a local Markdown document
    And the Workdash server offers an agent that can code and analyze
    When I run `workdash view "local notes.md" --target "owner/repo#12"` and choose an agent with "d"
    Then the document shows the local content
    And the viewer reports the autodispatch session for the issue

  @id:F-VIEW-ITEM-S017
  Scenario: A pull request document offers coding but not dispatch
    Given a GitHub pull request with checks, reviews and review threads
    And the Workdash server offers a coding agent for the viewer
    When I run `workdash view "owner/repo#12"`
    Then the viewer offers coding but not dispatch

  @id:F-VIEW-ITEM-S018
  Scenario: A document that names no item offers no launch actions
    Given a local Markdown document
    When I run `workdash view "local notes.md"`
    Then the document shows the local content
    And the viewer offers no launch actions because the document names no item

  @id:F-VIEW-ITEM-S019
  Scenario: A document about an issue offers no launch actions without a server
    Given a local Markdown document
    When I run `workdash view "local notes.md" --target "owner/repo#12"`
    Then the document shows the local content
    And the viewer offers no launch actions because no server is reachable

  @id:F-VIEW-ITEM-S020
  Scenario: An item argument conflicts with --target
    When I run `workdash view "owner/repo#ISSUE-3" --target "owner/repo#12"` expecting failure
    Then the command reports the conflict between the document and --target
