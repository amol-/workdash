@feature:F-BRANCHDIFF-VIEW
Feature: View branch diff in standalone TUI command

  Users need to review code changes in branches and pull requests without leaving
  the terminal. The `workdash branchdiff` command provides a standalone side-by-side
  diff viewer that works in any git repository directory.

  Rules:
    - The `workdash branchdiff` command works in any git repository directory.
    - By default, it compares the current branch against its upstream branch.
    - An optional argument specifies a different target branch for comparison.
    - The viewer shows files in a list with side-by-side diff view (old vs new).
    - Navigation uses vim-style keybindings (j/k for file list, Enter to view, q to quit).
    - Added lines are highlighted in green, removed lines in red.
    - The command shows both committed and uncommitted changes.
    - Workdash TUI's 'd' keybinding spawns `workdash branchdiff` in a new zellij pane.
    - Workdash ensures the worktree exists before spawning the command.
    - If spawning fails, workdash reports the error to the user.

  @id:F-BRANCHDIFF-S001
  Scenario: Run branchdiff command in git repository
    Given the current directory is a git repository
    And the repository has changes compared to its upstream branch
    When the user runs "workdash branchdiff"
    Then the diff viewer opens
    And the file list shows all changed files
    And the diff view shows the first file's changes in side-by-side view

  @id:F-BRANCHDIFF-S002
  Scenario: Branchdiff with target branch specification
    Given the current directory is a git repository
    When the user runs "workdash branchdiff main"
    Then the diff viewer opens comparing current branch against main
    And the side-by-side diff is displayed

  @id:F-BRANCHDIFF-S003
  Scenario: Branchdiff shows both committed and uncommitted changes
    Given the current directory is a git repository
    And there are both committed and uncommitted changes
    When the user runs "workdash branchdiff"
    Then the diff viewer shows all changes including uncommitted ones

  @id:F-BRANCHDIFF-S004
  Scenario: Navigate between files in diff viewer
    Given the diff viewer is open with multiple changed files
    And the first file's diff is displayed
    When the user presses "j"
    Then the cursor moves down to the next file in the list
    And the diff pane updates to show that file's changes
    When the user presses "k"
    Then the cursor moves up to the previous file in the list
    And the diff pane updates to show that file's changes

  @id:F-BRANCHDIFF-S005
  Scenario: Workdash TUI spawns branchdiff in new pane
    Given the TUI has a pull request work item selected
    And the worktree for that item exists
    When the user presses "d"
    Then a new zellij pane opens
    And the pane runs "workdash branchdiff" in the worktree directory
    And the diff viewer displays the PR changes
    And the TUI reports that the diff viewer was opened

  @id:F-BRANCHDIFF-S006
  Scenario: Workdash TUI handles worktree preparation failure
    Given the TUI has a work item selected
    And the next worktree preparation will fail
    When the user presses "d"
    Then the system reports the worktree error details to the user
    And no diff viewer pane is opened
    And no progress overlay remains

  @id:F-BRANCHDIFF-S007
  Scenario: Branchdiff command handles non-git directory
    Given the current directory is not a git repository
    When the user runs "workdash branchdiff"
    Then the command reports an error
    And exits with non-zero status
