@feature:F-WORKTREES-LAYOUT
Feature: Work directory layout

  The system keeps every repository and every work item in a predictable
  place so the user can hop between items without losing track of
  in-progress work.

  Rules:
    - Repositories and worktrees live under the configured work directory.
    - Each tracked repository has one main clone rooted at "<workdir>/<owner>_<repo>".
    - Each work item has its own worktree named "<owner>_<repo>_<number>", placed as a sibling of the main clone.
    - Worktrees for pull requests from a fork are named after the fork's owner and repository, not the upstream repository.
    - Preparing a worktree never disturbs other worktrees.

  @id:F-WORKTREES-LAYOUT-S001
  Scenario: First use of a repository clones it into the work directory
    Given the repository has never been used on this machine
    When the user triggers an action that needs the worktree
    Then the system clones the repository into "<workdir>/<owner>_<repo>"
    And a worktree for the work item is created alongside it

  @id:F-WORKTREES-LAYOUT-S002
  Scenario: Fork pull request worktrees use the fork's repository name
    Given a pull request comes from a fork of the upstream repository
    When the user triggers an action that needs the worktree
    Then the worktree directory is named after the fork's owner and repository

  @id:F-WORKTREES-LAYOUT-S003
  Scenario: Preparing a worktree leaves other worktrees alone
    Given several work items already have prepared worktrees
    When the user triggers an action that prepares a new worktree
    Then the new worktree is created
    And the existing worktrees remain untouched
