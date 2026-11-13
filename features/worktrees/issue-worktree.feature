@feature:F-WORKTREES-ISSUE
Feature: Issue worktree

  When the user works on an issue, the system preserves existing work and
  creates a GitHub-linked branch when no issue branch already exists.

  Rules:
    - A new issue worktree is checked out on a branch named "issue-<number>".
    - A matching existing worktree is reused without changing its branch or attempting to link it.
    - An existing local issue branch is reused even if origin is unavailable, without resetting it or changing its upstream; updating it is best effort and fast-forward only.
    - Otherwise an issue branch that still exists on origin is reused and tracked, including on retry after local setup fails.
    - Deleting an issue branch on origin does not prevent later fetches or work on other items.
    - If neither branch exists, the system creates a GitHub-linked issue branch and tracks it locally; if linking fails, it reuses an issue branch now on origin when available, otherwise it creates a local-only branch from the best available default branch.
    - Existing branches and fallback local branches are not necessarily linked to the issue.
    - Coding an issue with a linked open pull request uses the pull request's head repository and branch instead.
    - Todos never create GitHub-linked branches in the todo repository; untargeted todos keep ordinary issue branch names and targeted todos retain their target repository and branch behavior.

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
