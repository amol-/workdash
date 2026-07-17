@feature:F-WORKTREES-ISSUE
Feature: Issue worktree

  When the user works on an issue, the system gives them a fresh branch
  derived from the repository's default branch so implementation work
  starts from a clean baseline.

  Rules:
    - An issue's worktree is checked out on a branch named "issue-<number>".
    - The branch is created from the repository's current default branch on the origin remote.
    - The system fetches the origin remote before creating the branch so the branch is based on the latest default branch state.
    - A newly created worktree and its repository clone use the latest default branch commit from origin.

  @id:F-WORKTREES-ISSUE-S001
  Scenario: Issue worktree starts from the repository's default branch
    Given the user needs a worktree for an issue
    When the system prepares the worktree
    Then the worktree is checked out on a branch named "issue-<number>"
    And that branch was created from the repository's current default branch
