@feature:F-TUI-WORKTREES
Feature: Start and resume work on the dashboard

  Open a coding agent or terminal from the code menu without managing checkouts by hand.

  @id:F-CODING-LAUNCH-S001
  Scenario: Start coding an issue without cloning the repository first
    Given an issue is selected for a repository I have not cloned
    When I press "c" and choose "1" from the code menu
    Then the chosen coding agent opens with the repository's current default-branch code
    And the agent is preloaded with the issue's GitHub context
    And the TUI reports that the session was launched for that issue

  @id:F-WORKTREES-PR-S001
  Scenario: Inspect a pull request's changes in a terminal
    Given a pull request by another author is selected
    When I press "c" and choose "t" from the code menu
    Then the terminal opens on the pull request's branch with its proposed changes

  @id:F-WORKTREES-PR-S004
  Scenario: Continue issue work from the pull request that closes it
    Given I started work from an issue and left local changes in its checkout
    And my pull request from that branch now replaces the issue on the dashboard
    When I press "c" and choose "t" from the code menu
    Then the terminal opens in the same checkout with my local changes

  @id:F-WORKTREES-REUSE-S001
  Scenario: Pick up where I left off with the latest remote changes
    Given I closed the terminal for an issue after making local edits
    And a new commit is available on its branch that does not conflict with my edits
    When I press "c" and choose "t" from the code menu
    Then the terminal opens in the same checkout with my local changes
    And the latest remote changes are visible in the checkout
