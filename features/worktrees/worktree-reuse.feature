@feature:F-WORKTREES-REUSE
Feature: Reuse existing worktrees

  Once a worktree exists for a work item, the system returns to it
  instead of creating a new one, so in-progress changes are preserved
  across triage sessions.

  Rules:
    - When a worktree for a work item already exists, the system reuses it rather than creating a new one.
    - A directory is reused only when repository-local git metadata proves it belongs to the work item; inherited global git configuration is ignored.
    - Before handing the worktree back to the user, the system makes a best-effort fast-forward pull so the worktree reflects the latest remote state.
    - Local work that cannot be fast-forwarded is left untouched; the system never discards uncommitted or divergent local work on the user's behalf.

  @id:F-WORKTREES-REUSE-S001
  Scenario: Existing worktree is reused and fast-forwarded
    Given the work item already has a prepared worktree
    And the remote has new commits that can be fast-forwarded
    When the user triggers an action that needs the worktree
    Then the same worktree is returned to the user
    And the worktree is updated to the latest remote state

  @id:F-WORKTREES-REUSE-S002
  Scenario: Divergent local work is preserved
    Given the work item already has a prepared worktree
    And local work in the worktree diverges from the remote
    When the user triggers an action that needs the worktree
    Then the same worktree is returned to the user
    And the local work is not discarded
