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
    - The command shows committed changes, modified working-tree files, and untracked files.

  @id:F-BRANCHDIFF-S001
  Scenario: Run branchdiff command in git repository
    Given the current directory is a git repository
    And the repository has changes compared to its upstream branch
    When the user runs "workdash branchdiff"
    Then the diff viewer displays changes for the first file

  @id:F-BRANCHDIFF-S002
  Scenario: Branchdiff with target branch specification
    Given the current directory is a git repository
    And the repository has changes compared to its upstream branch
    When the user runs "workdash branchdiff main"
    Then the diff viewer displays a meaningful side-by-side diff

  @id:F-BRANCHDIFF-S003
  Scenario: Branchdiff shows committed, modified, and untracked changes
    Given the current directory is a git repository
    And there are committed changes, modified working-tree files, and untracked files
    When the user runs "workdash branchdiff"
    Then the file list shows all changed files

  @id:F-BRANCHDIFF-S004
  Scenario: Navigate between files in diff viewer
    Given the diff viewer is open with multiple changed files
    When the user navigates to the next file
    Then the diff viewer displays that file's changes

  @id:F-BRANCHDIFF-S007
  Scenario: Branchdiff command handles non-git directory
    Given the current directory is not a git repository
    When the user runs "workdash branchdiff"
    Then the command reports an error
    And exits with non-zero status

  @id:F-BRANCHDIFF-S008
  Scenario: Branchdiff with no changes
    Given the current directory is a git repository
    And the repository has no changes compared to upstream
    When the user runs "workdash branchdiff"
    Then the command reports no changes found
    And exits with zero status
