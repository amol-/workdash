@feature:F-VIEW-ITEM
Feature: View an issue or pull request as a document

  The user can read a GitHub issue or pull request as one Markdown
  document in the terminal, without leaving the current session.

  Rules:
    - `workdash view TARGET` shows the target as one Markdown document.
    - TARGET is a GitHub issue or pull request URL, `owner/repo#NUMBER`, or a bare `NUMBER` or `#NUMBER` resolved against the current directory's origin remote.
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
    - An unrecognised target or a failed lookup reports an error instead of showing a document.
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
  Scenario Outline: Every accepted target names the same item
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
  Scenario: An unrecognised target is reported
    When the user views `not a target` with `workdash view`
    Then the system reports that the target is not recognised

  @id:F-VIEW-ITEM-S005
  Scenario: A failed lookup is reported
    Given a GitHub issue with sub-issues and comments
    And the next GitHub lookup will fail
    When the user views it with `workdash view`
    Then the system reports that the item could not be fetched
