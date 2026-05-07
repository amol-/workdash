@feature:F-SETUP-CONFIGURE
Feature: First-time configuration

  Before the system can triage work, the user must have a configuration file
  that identifies them on GitHub, lists the repositories to track, points at
  a local work directory, and names the coding agent commands to use. The
  system ships an interactive wizard that fills in whatever is missing and
  can install local Zellij and GitHub CLI binaries when global binaries are
  not available.

  Rules:
    - The configuration lives at ~/.config/workdash/config.json.
    - Required configuration fields are the GitHub username, at least one repository selector, the work directory, and the analyze and launch commands for each supported coding agent.
    - The wizard only prompts for fields that are currently empty; previously filled fields are left untouched.
    - When a supported coding agent's command-line tool is detected on PATH, the wizard fills in its analyze and launch commands automatically and tells the user what was detected.
    - When Zellij is detected on PATH, the wizard tells the user what was detected.
    - When Zellij is not detected on PATH, the wizard tells the user it will install a local Zellij binary from the latest release URL and that a global Zellij install can be used instead when it is on PATH.
    - When Zellij is not detected on PATH, the wizard downloads the latest release binary for the current operating system and architecture under the workdash configuration directory and tells the user where it was installed.
    - Re-running the wizard always checks PATH for Zellij first; if no PATH binary is available, it downloads a fresh local binary even when a previous local binary exists.
    - When GitHub CLI is detected on PATH, the wizard tells the user what was detected.
    - When GitHub CLI is not detected on PATH, the wizard tells the user it will install a local GitHub CLI binary from the latest release URL and that a global GitHub CLI install can be used instead when it is on PATH.
    - When GitHub CLI is not detected on PATH, the wizard downloads the latest release binary for the current operating system and architecture under the workdash configuration directory and tells the user where it was installed.
    - Re-running the wizard always checks PATH for GitHub CLI first; if no PATH binary is available, it downloads a fresh local binary even when a previous local binary exists.
    - When a missing field has a default value, submitting an empty response accepts the default.
    - When a missing field has no default value, the wizard keeps prompting until the user provides a value.
    - When the repositories list is empty and a GitHub username is known, the wizard defaults the repositories to "<username>/*" and tells the user what was set.
    - The wizard writes the resulting configuration to the configuration file and reports where it was saved.

  @id:F-SETUP-CONFIGURE-S001
  Scenario: Generate a fresh configuration interactively
    Given the user has no configuration file
    When the user runs the system with "--configure"
    Then the system prompts the user for each missing required field
    And the system writes the collected values to "~/.config/workdash/config.json"
    And the system reports the saved configuration path

  @id:F-SETUP-CONFIGURE-S002
  Scenario: Detected coding agent command-line tools fill in defaults
    Given the user has no configuration file
    And a supported coding agent's command-line tool is on PATH
    When the user runs the system with "--configure"
    Then the system fills in that agent's analyze and launch commands automatically
    And the system tells the user which commands were detected

  @id:F-SETUP-CONFIGURE-S006
  Scenario: Detected Zellij binary is reported automatically
    Given the user has no configuration file
    And Zellij is installed on PATH
    When the user runs the system with "--configure"
    Then the system tells the user which Zellij binary was detected

  @id:F-SETUP-CONFIGURE-S007
  Scenario: Missing Zellij binary can be installed locally
    Given the user has no configuration file
    And Zellij is not installed on PATH
    When the user runs the system with "--configure"
    Then the system installs Zellij under the workdash configuration directory

  @id:F-SETUP-CONFIGURE-S008
  Scenario: Missing GitHub CLI binary can be installed locally
    Given the user has no configuration file
    And GitHub CLI is not installed on PATH
    When the user runs the system with "--configure"
    Then the system installs the GitHub CLI under the workdash configuration directory

  @id:F-SETUP-CONFIGURE-S003
  Scenario: Missing repositories default to the user's own namespace
    Given the user provides a GitHub username during configuration
    And the configuration has no repositories selector
    When the configuration wizard completes
    Then the repositories list contains "<username>/*"
    And the system tells the user what was set

  @id:F-SETUP-CONFIGURE-S004
  Scenario: Re-running the wizard only prompts for empty fields
    Given the user already has a partial configuration
    When the user runs the system with "--configure"
    Then the system only prompts for fields that were empty
    And previously set fields are preserved in the saved configuration

  @id:F-SETUP-CONFIGURE-S005
  Scenario: Empty answers accept defaults but required fields keep prompting
    Given the user has no configuration file
    And the user submits empty answers for defaults and then provides a username
    When the user runs the system with "--configure"
    Then the system writes default values for configurable fields
    And the system prompts again for the GitHub username
