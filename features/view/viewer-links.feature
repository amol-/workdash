@feature:F-VIEW-LINKS
Feature: Follow links from the built-in Markdown viewer

  The built-in Markdown viewer shows analyses, work items and any Markdown
  document, and the links in those documents lead to more of the same work.

  Rules:
    - Activating a link to a Markdown document opens it in the same viewer, and the document being left becomes the previous document.
    - A Markdown file hosted on GitHub is a repository page rather than a document, so its link opens with the configured `open` command.
    - A relative link resolves against the directory of the viewed local file, or against the address of a remote document.
    - Activating a GitHub issue or pull request link opens that item in the same viewer as the item document.
    - Following a document or item link never opens a pane and never replaces the running viewer, with or without Zellij.
    - Activating any other link runs the configured `open` command without waiting for it to exit, and the viewer names the command it used in its status line.
    - While a document is fetched the viewer shows that it is loading.
    - A link that cannot be loaded or opened is reported in the viewer's status line, and the current document stays on screen.
    - `b` returns to the previous document and is offered only while there is a previous document.
    - Links to headings within the document scroll to those headings and are not previous documents.

  @id:F-VIEW-LINKS-S001
  Scenario: A GitHub issue link opens that issue in the same viewer
    Given the built-in Markdown viewer shows a document linking to a GitHub issue
    When the user activates the issue link
    Then the viewer shows the issue document
    And the viewer offers Back

  @id:F-VIEW-LINKS-S002
  Scenario: A pull request link opens in the same viewer inside Zellij
    Given the built-in Markdown viewer shows a document linking to a pull request
    And the user is inside a Zellij session
    When the user activates the pull request link
    Then the viewer shows the pull request document
    And no pane is opened
    And the viewer process is not replaced

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

  @id:F-VIEW-LINKS-S005
  Scenario: A relative Markdown link opens in the same viewer
    Given the built-in Markdown viewer shows a document linking to another Markdown document
    When the user activates the Markdown link
    Then the viewer shows the linked document
    And the viewer offers Back

  @id:F-VIEW-LINKS-S006
  Scenario: A pull request link outside Zellij stays in the same viewer
    Given the built-in Markdown viewer shows a document linking to a pull request
    And the user is not inside a Zellij session
    When the user activates the pull request link
    Then the viewer shows the pull request document
    And the viewer process is not replaced

  @id:F-VIEW-LINKS-S007
  Scenario: Back returns to the previous document
    Given the built-in Markdown viewer shows a document linking to another Markdown document
    When the user follows the Markdown link and presses the back key
    Then the viewer shows the previous document
    And the viewer does not offer Back

  @id:F-VIEW-LINKS-S008
  Scenario: Back is offered only while there is a previous document
    Given the built-in Markdown viewer shows a document linking to another Markdown document
    When the user activates the Markdown link
    Then the viewer offers Back
    And the viewer did not offer Back while showing the first document

  @id:F-VIEW-LINKS-S009
  Scenario: A heading link scrolls within the current document
    Given the built-in Markdown viewer shows a document with a heading link
    When the user activates the heading link
    Then the viewer scrolls to that heading
    And the viewer does not offer Back

  @id:F-VIEW-LINKS-S010
  Scenario: A fetched document is reported as loading
    Given the built-in Markdown viewer shows a document linking to a GitHub issue
    When the user activates the issue link
    Then the viewer reports that the document is loading

  @id:F-VIEW-LINKS-S011
  Scenario: A link that cannot be loaded is reported
    Given the built-in Markdown viewer shows a document linking to a missing Markdown document
    When the user activates the Markdown link
    Then the viewer reports that the document could not be loaded
    And the current document stays open
