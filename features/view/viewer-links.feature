@feature:F-VIEW-LINKS
Feature: Follow links from the built-in Markdown viewer

  The built-in Markdown viewer shows analyses and work items, and the
  links in those documents lead to more of the same work.

  Rules:
    - Activating a GitHub issue or pull request link opens a viewer for that item.
    - Inside a Zellij session the item opens in a new full-size floating pane that closes when the viewer exits; outside Zellij the current process becomes the new viewer with normal terminal settings restored.
    - Activating any other link runs the configured `open` command without waiting for it to exit, and the viewer names the command it used.
    - An immediate launch failure is reported to the user and the current document stays open.
    - Links to headings within the document scroll to those headings.

  @id:F-VIEW-LINKS-S001
  Scenario: A GitHub issue link opens a viewer for that issue
    Given the built-in Markdown viewer shows a document linking to a GitHub issue
    And the user is inside a Zellij session
    When the user activates the issue link
    Then a viewer for the issue opens in a new full-size floating pane
    And the current document stays open

  @id:F-VIEW-LINKS-S002
  Scenario: A GitHub link outside Zellij replaces the viewer
    Given the built-in Markdown viewer shows a document linking to a pull request
    And the user is not inside a Zellij session
    When the user activates the pull request link
    Then the viewer process becomes a viewer for the pull request

  @id:F-VIEW-LINKS-S003
  Scenario: Any other link opens with the configured command
    Given the built-in Markdown viewer shows a document linking to a web page
    When the user activates the web page link
    Then the configured open command receives the web page URL
    And the viewer reports the command it used

  @id:F-VIEW-LINKS-S004
  Scenario: A failing open is reported
    Given the built-in Markdown viewer shows a document linking to a web page
    And the configured open command will fail
    When the user activates the web page link
    Then the viewer reports that the link could not be opened
    And the current document stays open
