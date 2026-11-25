@feature:F-VIEW-LINKS
Feature: Follow links from the built-in Markdown viewer

  @id:F-VIEW-LINKS-S001
  Scenario: Read a linked GitHub issue without leaving the viewer
    Given the viewer is showing a link to a GitHub issue
    When I click the link in the viewer
    Then the viewer says it is loading the issue
    And I can read the issue "Crash on startup" in the same viewer

  @id:F-VIEW-LINKS-S002
  Scenario: Read a linked draft pull request in the viewer
    Given the viewer is showing a link to a pull request
    When I click the link in the viewer
    Then I can read the draft pull request in the same viewer

  @id:F-VIEW-LINKS-S003
  Scenario: Open a linked web page outside the viewer
    Given the viewer is showing a link to a web page
    When I click the link in the viewer
    Then my browser opens the linked web page and the viewer names the opener

  @id:F-VIEW-LINKS-S007
  Scenario: Read a linked Markdown document and return to the original
    Given the viewer is showing a relative link to "linked.md"
    When I click the link in the viewer
    Then I can read "linked body" instead of the original document, and Back is offered
    When I press b to return
    Then I can read the original document instead of the linked document, and Back is no longer offered

  @id:F-VIEW-LINKS-S009
  Scenario: Jump to a heading in the current document
    Given the viewer is showing a link to a heading in the current document
    When I click the link in the viewer
    Then the heading "Destination" is on screen

  @id:F-VIEW-LINKS-S012
  Scenario: Read a particular section of another document
    Given the viewer is showing a link to the Part section of another Markdown document
    When I click the link in the viewer
    Then I can read "section body" at the Part heading in the linked document

  @id:F-VIEW-LINKS-S013
  Scenario: Quit the viewer instead of waiting for a slow document
    Given the viewer is showing a link to a slow Markdown document
    When I click the link and press q while the viewer says it is loading
    Then I return from the viewer without waiting for the document
