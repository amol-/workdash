@feature:F-WORKTREES-PR
Feature: Pull request worktree

  When the user works on a pull request, the system gives them a
  worktree already checked out on the pull request's branch so the
  changes on the branch are immediately visible.

  Rules:
    - A pull request's worktree is checked out on the pull request's head branch when the system creates that worktree.
    - When the system creates the worktree for a pull request that originates in a fork, it clones the fork's repository rather than the upstream one.
    - A fork clone the system creates has an `upstream` remote pointing at the pull request's base repository.
    - The system fetches the remote before creating the worktree so the branch reflects the latest state of the pull request.
    - A coding session or analysis running in the worktree has access to a stable diff target that represents only this pull request's changes.
    - An authored pull request that closes an issue in the same repository is the implementation of that issue, so it uses the worktree directory named after the linked issue instead of opening a second checkout for the same work.
    - A worktree that already exists for the linked issue is reused whatever branch it holds, and whatever repository the pull request's own branch lives in: it was opened for this work, so its remote configuration decides where commits go. This takes precedence over the fork rules above, and two open pull requests closing one issue therefore share that single checkout.
    - A REVIEW or CHECK pull request keeps its own pull-request-numbered worktree directory, because the user reviews the author's branch rather than continuing their own work.
    - A pull-request-numbered worktree directory that already exists keeps resolving to its pull request, so checkouts opened before this naming rule keep working.

  @id:F-WORKTREES-PR-S001
  Scenario: Pull request worktree is checked out on the PR branch
    Given the user needs a worktree for a pull request
    When the system prepares the worktree
    Then the worktree is checked out on the pull request's head branch

  @id:F-WORKTREES-PR-S003
  Scenario: An authored pull request's worktree is named after the issue it closes
    Given the user needs a worktree for an authored pull request that closes an issue
    When the system prepares the worktree
    Then the worktree directory is named after the issue the pull request closes
    And the worktree is checked out on the pull request's head branch

  @id:F-WORKTREES-PR-S004
  Scenario: An authored pull request reuses the worktree already opened from its issue
    Given the user already has a worktree opened from an issue
    And the user authored a pull request that closes that issue
    When the system prepares the worktree
    Then the same worktree is returned to the user

  @id:F-WORKTREES-PR-S002
  Scenario: Fork pull request worktree uses the fork as its remote
    Given the pull request originates in a fork of the upstream repository
    When the system prepares the worktree
    Then the worktree is backed by a clone of the fork's repository
    And the fork worktree has an upstream remote for the pull request's base repository
    And the worktree is checked out on the pull request's head branch
