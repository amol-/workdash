@feature:F-ANALYSIS-GENERATE
Feature: Generate an analysis

  The system can produce an AI-assisted analysis of a work item, storing
  it locally so the user can re-read it instantly and open it in a
  browser-friendly format.

  Rules:
    - A fresh analysis can be generated with any agent configured for analysis.
    - A fresh issue analysis reuses an existing matching worktree, or runs in a temporary detached worktree based on the target repository's current default branch, even if it has changed since cloning; the temporary worktree is removed even if analysis fails.
    - Issue analysis does not create or link an issue branch; pull request analysis uses the pull request's head repository and branch.
    - Cached analysis is returned without preparing a worktree.
    - The agent can inspect the local source code alongside the supplied issue or pull request discussion.
    - Source citations link to the code repository's own hosting service, including GitHub, GitLab, and Codeberg.
    - When the source-link format is not immediately obvious, the agent cites the file path and line number without inventing a URL.
    - While the analysis runs, the system shows the user that work is in progress.
    - A fresh analysis runs in a Zellij pane of its own, named `analyse_<worktree>`, so the user can watch the analysis pane while it runs.
    - The analysis pane opens in the Zellij tab named after the code repository's basename, or in the tab named by an explicitly requested group.
    - A fresh analysis needs a known Zellij session; opening a cached analysis does not.
    - After a successful fresh analysis, the system caches the result and opens its HTTP analysis URL with the same configured viewer used for work item URLs.
    - Opening a cached analysis also opens that URL, including its fullscreen terminal choice and current session.
    - The analysis URL is passed literally after configured viewer arguments; opening an analysis does not prepare a worktree and does not read the server's filesystem.
    - When an analysis fails or produces no content, the system closes any progress overlay, reports the failure details to the user, and leaves any previous cached analysis untouched.

  @id:F-ANALYSIS-GENERATE-S001
  Scenario: Fresh analysis is cached and opened by URL in the viewer
    Given the user has the analyze dialog open on a work item
    When the user chooses to generate a fresh analysis with a configured analysis agent
    Then the system prepares the work item's worktree
    And the analysis agent starts inside that worktree with access to the code and the work item's discussion
    And the system shows that the analysis is in progress
    And the generated analysis is cached
    And the analysis HTTP URL is opened with the configured viewer

  @id:F-ANALYSIS-GENERATE-S002
  Scenario: Opening a cached analysis opens its URL
    Given the selected work item has a cached analysis
    When the user chooses to open the cached analysis
    Then the cached analysis URL is opened with the configured viewer

  @id:F-ANALYSIS-GENERATE-S003
  Scenario: Failing analysis reports to the user and preserves the cache
    Given the user has the analyze dialog open on a work item with a cached analysis
    And the next fresh analysis will fail
    When the user chooses to generate a fresh analysis
    Then the system reports the failure to the user
    And no dialog or progress overlay remains
    And the previously cached analysis is preserved

  @id:F-ANALYSIS-GENERATE-S005
  Scenario: A fresh analysis runs in its own pane in the code repository's tab
    Given the user has the analyze dialog open on a work item
    And the item's worktree directory is named "owner_repo_52"
    When the user chooses to generate a fresh analysis with a configured analysis agent
    Then the analysis agent runs in a Zellij pane named "analyse_owner_repo_52"
    And the analysis pane opens in the Zellij tab named "repo"

  @id:F-ANALYSIS-GENERATE-S006
  Scenario: A fresh analysis needs a Zellij session
    Given the Workdash server is not attached to a Zellij session
    And the user has the analyze dialog open on a work item
    When the user chooses to generate a fresh analysis with a configured analysis agent
    Then the system reports that analysis requires a Zellij session

  @id:F-ANALYSIS-GENERATE-S007
  Scenario: An issue without a worktree is analyzed without leaving one behind
    Given an issue has no existing worktree or linked branch
    And its repository's default branch contains the current source code
    When the user requests a fresh analysis of the issue
    Then the analysis observes the current default-branch source code
    And the successful analysis leaves no issue worktree or linked branch

  @id:F-ANALYSIS-GENERATE-S004
  Scenario Outline: Analysis source citations follow the code repository's hosting service
    Given the selected work item's code is hosted at `<repository>`
    And the code relevant to the work item is in `src/cards.py` at line 12
    When the user generates a fresh analysis
    Then the analysis cites `src/cards.py` at line 12 using a link on `<hosting_service>`

    Examples:
      | repository                           | hosting_service |
      | https://github.com/owner/repo        | GitHub          |
      | https://gitlab.com/owner/repo        | GitLab          |
      | https://codeberg.org/owner/repo      | Codeberg        |
