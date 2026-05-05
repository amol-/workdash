@feature:F-WORKTREES-PR
Feature: Pull request worktree

  When the user works on a pull request, the system gives them a
  worktree already checked out on the pull request's branch so the
  changes on the branch are immediately visible.

  Rules:
    - A pull request's worktree is checked out on the pull request's head branch.
    - For a pull request that originates in a fork, the worktree is cloned from the fork's repository rather than the upstream one.
    - The system fetches the remote before creating the worktree so the branch reflects the latest state of the pull request.
    - A coding session or analysis running in the worktree has access to a stable diff target that represents only this pull request's changes.

  @id:F-WORKTREES-PR-S001
  Scenario: Pull request worktree is checked out on the PR branch
    Given the user needs a worktree for a pull request
    When the system prepares the worktree
    Then the worktree is checked out on the pull request's head branch

  @id:F-WORKTREES-PR-S002
  Scenario: Fork pull request worktree uses the fork as its remote
    Given the pull request originates in a fork of the upstream repository
    When the system prepares the worktree
    Then the worktree is backed by a clone of the fork's repository
    And the worktree is checked out on the pull request's head branch
