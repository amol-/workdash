@feature:F-SETUP-CONFIGURE
Feature: Configure Workdash from the command line
  Use the setup wizard to get started or repair an existing configuration.

  @id:F-SETUP-CONFIGURE-S001
  Scenario: Generate a configuration with the suggested defaults
    Given the user has no Workdash configuration
    When the user runs `workdash --configure` and answers the wizard:
      | prompt             | answer     |
      | Open command       | Enter      |
      | Analysis viewer    | Enter      |
      | GitHub username    | Enter      |
      | GitHub username    | octocat    |
      | Work directory     | ~/projects |
      | Todo repository    | Enter      |
    Then the wizard asks again for the required GitHub username
    And the wizard saves the configuration to `~/.config/workdash/config.json`
    And the saved username is `octocat` and work directory is `~/projects`
    And the suggested todo destination `octocat/todos` is saved

  @id:F-SETUP-CONFIGURE-S002
  Scenario: Use an installed agent without entering its commands
    Given the user has no Workdash configuration
    And Claude is installed
    When the user runs `workdash --configure` and answers the wizard:
      | prompt             | answer |
      | Agents for analyze | Enter  |
      | Agents for code    | Enter  |
    Then the wizard reports detecting Claude and saves a configuration with Claude

  @id:F-SETUP-CONFIGURE-S015
  Scenario: Continue setup after skipping agent selection
    Given the user has no Workdash configuration
    And no supported agent is installed
    When the user runs `workdash --configure` and answers the wizard:
      | prompt             | answer      |
      | Agents for analyze | Enter       |
      | Agents for code    | Enter       |
      | Agents for analyze | aider --yes |
      | Agent name         | aider       |
      | Agents for analyze | Enter       |
      | Agents for code    | Enter       |
    Then the wizard explains that an agent is required
    And the wizard saves `aider` as an analysis agent without a launch command

  @id:F-SETUP-CONFIGURE-S011
  Scenario: Replace an unavailable URL opener
    Given the saved URL opener is no longer installed
    When the user runs `workdash --configure` and answers the wizard:
      | prompt                  | answer            |
      | Open command            | term-view --quiet |
      | Does the viewer require | y                 |
    Then the wizard saves the replacement URL opener for use in a terminal
    And the saved repositories are unchanged

  @id:F-SETUP-CONFIGURE-S007
  Scenario: Install a local copy of Zellij during setup
    Given the user has no Workdash configuration
    And Zellij is not on PATH for setup
    When the user completes `workdash --configure`
    Then the wizard reports a usable local installation of Zellij

  @id:F-SETUP-CONFIGURE-S008
  Scenario: Install a local copy of GitHub CLI during setup
    Given the user has no Workdash configuration
    And GitHub CLI is not on PATH for setup
    When the user completes `workdash --configure`
    Then the wizard reports a usable local installation of GitHub CLI

  @id:F-SETUP-CONFIGURE-S004
  Scenario: Complete a partial configuration without re-entering saved settings
    Given the user has saved their settings except for the analysis viewer, work directory, and todo destination
    When the user runs `workdash --configure` and answers the wizard:
      | prompt          | answer |
      | Analysis viewer | Enter  |
      | Work directory  | ~/src  |
      | Todo repository | Enter  |
    Then the wizard asks only for the missing settings
    And the wizard saves `~/src` as the work directory without changing the saved repositories

  @id:F-SETUP-CONFIGURE-S016
  Scenario: Learn how to hide repositories without re-entering settings
    Given the user already has a complete configuration with agents
    When the user completes `workdash --configure`
    Then the wizard saves the configuration without prompting
    And the wizard explains how to hide repositories from the dashboard

  @id:F-SETUP-CONFIGURE-S023
  Scenario: Replace the retired Markdown viewer after upgrading
    Given the configuration still names `workdash mdview` as the analysis viewer
    When the user runs `workdash --configure` and answers the wizard:
      | prompt          | answer |
      | Analysis viewer | Enter  |
    Then the wizard offers and saves `workdash view` as the replacement
    And all other settings are unchanged

  @id:F-SETUP-CONFIGURE-S021
  Scenario: Choose a custom terminal viewer for HTML analyses
    Given the user has no Workdash configuration
    When the user runs `workdash --configure` and answers the wizard:
      | prompt                           | answer            |
      | Analysis viewer                  | term-view --quiet |
      | Analysis format                  | html              |
      | Does the analysis viewer require | y                 |
    Then the wizard saves the custom HTML analysis viewer for use in a terminal

  @id:F-SETUP-CONFIGURE-S025
  Scenario: Set up Workdash on the first launch
    Given the user has no Workdash configuration
    When the user completes the wizard started by `workdash`
    Then the wizard saves the configuration to `~/.config/workdash/config.json`
    And the saved username is `octocat` and work directory is `~/projects`
    And Workdash exits and asks the user to run `workdash` again to open the dashboard

  @id:F-SETUP-CONFIGURE-S033
  Scenario: Choose repositories with a managed GitHub login
    Given the user has no Workdash configuration
    When the user runs `workdash` and answers the wizard:
      | prompt              | answer         |
      | GitHub username     | mona-cat_octo  |
      | Repository selector | octo-org/*     |
      | Todo repository     | octo-org/tasks |
    Then the wizard saves `octo-org/*` to track and `octo-org/tasks` for todos

  @id:F-SETUP-CONFIGURE-S030
  Scenario: Correct a todo destination that is missing its owner
    Given the user has no Workdash configuration
    When the user runs `workdash --configure` and answers the wizard:
      | prompt          | answer        |
      | Todo repository | todos         |
      | Todo repository | octocat/tasks |
    Then the wizard asks for a todo repository in `owner/repo` form
    And the wizard saves `octocat/tasks` as the todo destination

  @id:F-SETUP-CONFIGURE-S027
  Scenario: Cancel the first-run wizard
    Given the user has no Workdash configuration
    When the user runs `workdash` and presses Ctrl+C at the first wizard prompt
    Then Workdash reports that configuration was cancelled without saving it
