@feature:F-TERMINAL-PANE-TITLE
Feature: Name Zellij panes for work actions

  When a work action opens a Zellij pane, the user needs the pane title to
  identify both what kind of work is happening there and which worktree it is
  using.

  Rules:
    - New Zellij panes for terminal-backed work actions are named "<action>_<worktree>".
    - The "code" action name is used for coding sessions that open in Zellij.
    - The "terminal" action name is used for plain terminal panes.
    - The worktree part of the name is the worktree directory name, not the full path. An authored pull request that closes an issue in the same repository shares that issue's worktree, so its pane title carries the linked issue's number.

  @id:F-TERMINAL-PANE-TITLE-S001
  Scenario Outline: Terminal-backed work action names its Zellij pane
    Given the system is running inside a Zellij session
    And the selected work item uses the worktree directory "amol-_repoze.who_52"
    When the user launches the "<action>" terminal-backed work action
    Then the new Zellij pane is named "<pane_name>"

    Examples:
      | action   | pane_name                       |
      | code     | code_amol-_repoze.who_52        |
      | terminal | terminal_amol-_repoze.who_52    |
