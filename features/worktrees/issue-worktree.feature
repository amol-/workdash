@feature:F-WORKTREES-ISSUE
Feature: Issue worktree

  When the user works on an issue, the system preserves existing work and
  creates a GitHub-linked branch when no issue branch already exists.

  Rules:
    - A new issue worktree is checked out on a branch named "issue-<number>".
    - Existing worktrees and local issue branches are reused without losing work or changing their branch or upstream. Workdash does not attempt to link an existing worktree's branch; updates are best effort and fast-forward only, even when origin is unavailable.
    - An existing issue branch on origin is reused and tracked locally, including after an interrupted setup.
    - New issue branches are linked on GitHub and tracked locally when possible. If linking fails, work continues on an available remote branch or a local branch from the best available default branch.
    - Reused and local-only branches are not necessarily linked to the issue.
    - Removing a remote issue branch does not stop work on other items.
    - Coding an issue with a linked open pull request uses the pull request's head repository and branch instead.
    - Todos do not create linked branches in the todo repository; untargeted todos use ordinary issue branch names, and targeted todos retain their target repository and branch behavior.

  @id:F-WORKTREES-ISSUE-S001
  Scenario: New issue worktree starts from the repository's default branch
    Given the user needs a worktree for an issue
    When the system prepares the worktree
    Then the worktree is checked out on a branch named "issue-<number>"
    And that branch was created from the repository's current default branch

  @id:F-WORKTREES-ISSUE-S002
  Scenario: A new issue branch is linked on GitHub
    Given the user needs a worktree for an issue
    And GitHub can create a linked issue branch
    When the system prepares the worktree
    Then the issue branch is linked on GitHub and tracked locally

  @id:F-WORKTREES-ISSUE-S003
  Scenario: Work continues locally when GitHub cannot link the issue branch
    Given the user needs a worktree for an issue
    And GitHub cannot create a linked issue branch
    When the system prepares the worktree
    Then the issue branch is local and starts from the default branch
