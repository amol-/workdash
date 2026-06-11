@feature:F-TUI-BRANCHDIFF-SPAWN
Feature: TUI spawns branchdiff viewer

  The workdash TUI provides a keybinding to spawn a branchdiff viewer
  in a new terminal pane for reviewing pull request changes.

  Rules:
    - Pressing 'd' in the TUI spawns `workdash branchdiff` in a new zellij pane.
    - The command runs in the worktree directory of the selected work item.
    - Workdash ensures the worktree exists before spawning the command.
    - For pull request work items, Workdash asks GitHub for the PR base branch at launch time and passes the Workdash-managed base remote target to branchdiff.
    - Same-repo pull requests compare against `origin/<base branch>`; fork pull requests compare against `upstream/<base branch>`.
    - For non-PR work items, Workdash launches branchdiff without a target branch.
    - If spawning fails, workdash reports the error to the user.

  @id:F-TUI-BRANCHDIFF-SPAWN-S001
  Scenario: Workdash TUI spawns branchdiff for a pull request against its GitHub base branch
    Given the TUI has a pull request work item selected
    And GitHub reports the PR base branch "release/2026"
    And the worktree for that item exists
    When the user presses "d"
    Then Workdash has prepared the worktree for the selected item
    And Workdash asks GitHub for that PR's base branch
    And a new zellij pane opens
    And the pane runs "workdash branchdiff origin/release/2026" in the worktree directory
    And the diff viewer displays the PR changes
    And the TUI reports that the diff viewer was opened

  @id:F-TUI-BRANCHDIFF-SPAWN-S003
  Scenario: Workdash TUI spawns branchdiff for a non-PR work item without a target branch
    Given the TUI has a work item selected
    And the worktree for that item exists
    When the user presses "d"
    Then Workdash has prepared the worktree for the selected item
    And GitHub is not queried for a PR base branch
    And a new zellij pane opens
    And the pane runs "workdash branchdiff" in the worktree directory

  @id:F-TUI-BRANCHDIFF-SPAWN-S002
  Scenario: Workdash TUI handles worktree preparation failure
    Given the TUI has a work item selected
    And the next worktree preparation will fail
    When the user presses "d"
    Then the system reports the worktree error details to the user
    And no diff viewer pane is opened
    And no progress overlay remains
