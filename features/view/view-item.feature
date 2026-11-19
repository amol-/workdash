@feature:F-VIEW-ITEM
Feature: View a document

  The user can read a GitHub issue or pull request, a local Markdown file,
  or a remote document in the terminal, without leaving the current session.

  Rules:
    - `workdash view TARGET` shows the target as one document in the built-in viewer.
    - TARGET is a local path, a `file://` URL, an HTTP or HTTPS URL, a GitHub issue or pull request URL, `owner/repo#NUMBER`, or a bare `NUMBER` or `#NUMBER` resolved against the current directory's origin remote.
    - GitHub lookups happen only for GitHub targets; a local file never contacts GitHub.
    - Whether the target is an issue or a pull request comes from GitHub, not from the shape of the URL.
    - The document starts with the title, a metadata line, and a labels, assignees, milestone and comments line.
    - The description preserves the original Markdown body; bare issue references are not converted to links.
    - The view command can use the GitHub CLI installed locally by Workdash.
    - Sub-issues are listed as a table at every nesting level, each level marked with a `└─ ` prefix.
    - Every comment is a section naming its author and date, and an item without comments says so.
    - A pull request document also reports draft, mergeable, merge state and review decision, plus CI, reviews and review threads.
    - CI reports GitHub's overall state, counts all checks and links failing checks, including GitHub error statuses, even when there are more than 100 checks; passing checks are only counted.
    - Each review thread shows its location, comments and the surrounding diff, and marks resolved and outdated threads. A truncated comment list says so.
    - Review threads are capped at 50 and checks at 20, and a truncated list says so.
    - The document ends with a line saying it was generated from `gh` with a UTC timestamp.
    - A document that cannot be read, a failed lookup, or a target with an unsupported URL scheme reports an error instead of showing a document.
    - In a Zellij viewer pane, a lookup error remains visible until dismissed; a successfully closed viewer pane closes automatically.

  @id:F-VIEW-ITEM-S001
  Scenario: View an issue
    Given a GitHub issue with sub-issues and comments
    When the user views it with `workdash view`
    Then the document shows the issue metadata, description, sub-issues and comments
    And the document ends with the generation timestamp

  @id:F-VIEW-ITEM-S002
  Scenario: View a pull request
    Given a GitHub pull request with checks, reviews and review threads
    When the user views it with `workdash view`
    Then the document shows the pull request metadata, description, CI, reviews, review threads and comments

  @id:F-VIEW-ITEM-S003
  Scenario Outline: Every accepted GitHub target names the same item
    Given the current directory's origin remote is `owner/repo`
    When the user views `<target>` with `workdash view`
    Then the document is for `owner/repo#12`

    Examples:
      | target                                  |
      | https://github.com/owner/repo/issues/12 |
      | owner/repo#12                           |
      | 12                                      |
      | #12                                     |

  @id:F-VIEW-ITEM-S004
  Scenario: A local document that cannot be read is reported
    When the user views `a missing file.md` with `workdash view`
    Then the system reports that the document could not be read

  @id:F-VIEW-ITEM-S005
  Scenario: A failed lookup is reported
    Given a GitHub issue with sub-issues and comments
    And the next GitHub lookup will fail
    When the user views it with `workdash view`
    Then the system reports that the item could not be fetched

  @id:F-VIEW-ITEM-S006
  Scenario: A local file path opens the file
    Given a local Markdown document
    When the user views it with `workdash view`
    Then the document shows the local content

  @id:F-VIEW-ITEM-S007
  Scenario: A file URL opens the local file
    Given a local Markdown document
    When the user views its file URL with `workdash view`
    Then the document shows the local content

  @id:F-VIEW-ITEM-S008
  Scenario: A remote document is read from its URL
    Given a document served at an HTTP URL
    When the user views that URL with `workdash view`
    Then the document shows the remote content

  @id:F-VIEW-ITEM-S009
  Scenario: A local target never contacts GitHub
    Given a local Markdown document
    When the user views it with `workdash view`
    Then no GitHub lookup happens

  @id:F-VIEW-ITEM-S010
  Scenario: A target with an unsupported URL scheme is reported
    When the user views `ftp://example.test/doc.md` with `workdash view`
    Then the system reports that the target is not supported
