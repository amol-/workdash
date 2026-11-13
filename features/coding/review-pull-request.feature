@feature:F-CODING-REVIEW
Feature: Review a pull request
  A reviewer can request an autonomous review of a pull request or its linked issue.

  Rules:
    - An issue without a linked open pull request, or a closed or merged pull request, has nothing to review.
    - A review reports major findings as inline comments and submits an appropriate review when permitted.
    - A review of one's own pull request is comment-only.
    - The agent is asked to summarize the outcome and list posted inline comments in a fresh review document, but any non-empty document counts as success.
    - Starting a new review removes prior generated review documents in its worktree, except documents belonging to reviews still in progress; other files are preserved.
    - A missing or empty document is retried once, and an agent failure is reported as a failure.
    - A repeated request for a running review returns the same started information without starting another review.
    - A started review reports the intended document path and runs without blocking other control requests.
    - A review runs in a Zellij pane of its own, named `review_<worktree>`, so the user can watch it while it runs.
    - The review pane opens in the Zellij tab named after the code repository's basename, or in the tab named by an explicitly requested group.
    - A review needs a known Zellij session.
    - The code menu offers `(r) auto review`, which asks which configured analysis agent reviews the selected item, shows a progress dialog until the review finishes, and then reports the outcome.

  @id:F-CODING-REVIEW-S001
  Scenario: Start a review of an open pull request
    Given an open pull request is available for review
    When the reviewer requests an autonomous review
    Then the reviewer is told the review started and where its document will be written

  @id:F-CODING-REVIEW-S004
  Scenario: Request the same review again
    Given an open pull request review is in progress
    When the reviewer requests the same review again
    Then the reviewer receives the same started information

  @id:F-CODING-REVIEW-S002
  Scenario: An issue has no open pull request
    Given an issue has no linked open pull request
    When the reviewer requests an autonomous review of the issue
    Then the reviewer is told there is nothing to review

  @id:F-CODING-REVIEW-S005
  Scenario: A review runs in its own pane in the code repository's tab
    Given an open pull request is available for review
    And the pull request's worktree directory is named "owner_repo_77"
    When the reviewer requests an autonomous review
    Then the review agent runs in a Zellij pane named "review_owner_repo_77"
    And the review pane opens in the Zellij tab named "repo"

  @id:F-CODING-REVIEW-S006
  Scenario: A requested group chooses the review tab
    Given an open pull request is available for review
    When the reviewer requests an autonomous review in group "project"
    Then the review pane opens in the Zellij tab named "project"

  @id:F-CODING-REVIEW-S007
  Scenario: A review needs a Zellij session
    Given the Workdash server is not attached to a Zellij session
    And an open pull request is available for review
    When the reviewer requests an autonomous review
    Then the reviewer is told that review requires a Zellij session

  @id:F-CODING-REVIEW-S003
  Scenario: List work while a review is running
    Given an open pull request review is in progress
    When the reviewer lists work items
    Then the list is returned before the review finishes

  @id:F-CODING-REVIEW-S008
  Scenario: Start a review from the code menu
    Given an open pull request is available for review
    When the user chooses auto review from the code menu and picks an analysis agent
    Then the code menu asks which analysis agent reviews the pull request
    And the dashboard shows a progress dialog until the review finishes
    And the dashboard reports the reviewed pull request with the agent that reviewed it
