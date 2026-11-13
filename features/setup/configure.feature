@feature:F-SETUP-CONFIGURE
Feature: First-time configuration

  Before the system can triage work, the user must have a configuration file
  that identifies them on GitHub, lists the repositories to track, points
  at a local work directory, names the repository where todos are
  captured, and configures at least one agent. An agent is a named
  command-line tool the user runs to analyze work items, to code on them, or
  both. The system ships an interactive wizard that fills in whatever is
  missing, detects installed agents, and can install local Zellij and GitHub
  CLI binaries when global binaries are not available.

  Rules:
    - The configuration lives at ~/.config/workdash/config.json.
    - Required configuration fields are the GitHub username, open command, at least one repository selector, the work directory, the todo repository, and at least one agent.
    - Every required field must be present for the system to start; the wizard backfills missing required fields, using a default when one exists.
    - When the open command is missing, malformed, or its executable is unavailable, the wizard prompts until a valid executable command is supplied, allowing arguments and explicit executable paths.
    - The wizard suggests only an available platform opener: `xdg-open` on Linux or `open` on macOS, explicitly explaining that Enter accepts it. Without a suggestion, empty input is rejected.
    - For a missing or invalid viewer terminal choice, the wizard asks "Does the viewer require its own terminal window? [y/N]", defaulting to No and preserving explicit boolean choices.
    - The wizard explains that advanced viewer commands can be edited in config.json.
    - When the todo repository is empty or not in "owner/repo" form, the wizard prompts for it with the default "<username>/todos"; the repository does not have to exist yet.
    - Present agent command fields must be valid command lines; each valid command enables that agent action.
    - The wizard preserves previously filled valid fields while repairing missing or invalid viewer settings.
    - An agent has a unique name and an analyze command, a launch command, or both. An agent with an analyze command is an analysis agent; an agent with a launch command is a coding agent.
    - The names `cached` and `vscode` are reserved and cannot be used for an agent.
    - The wizard asks about agents only when no agent is configured; when any agent is already configured, it does not ask about agents and leaves them untouched.
    - When no agent is configured, the wizard detects Claude, ChatGPT Codex, and pi on PATH, in that order, tells the user which ones it detected, and adds each detected agent as `claude`, `codex`, or `pi` with its default analyze and launch commands.
    - When none of them is detected, the wizard says so and names the agents it looks for.
    - The wizard then asks for agents in two sections, "Agents for analyze" and "Agents for code". Each section lists the agents that have that kind of command and asks for another command until the user answers with an empty line.
    - Before asking for a command, the wizard tells the user that the task prompt is appended to the command as its last argument.
    - Each section says what kind of command it asks for, with an example: a command that runs once and prints the analysis for "Agents for analyze", and a command that starts an interactive coding session for "Agents for code".
    - For every command the user adds, the wizard asks for the agent's name. The name cannot be empty, cannot be reserved, and cannot already have that kind of command in the same section; a name that exists only in the other section gives that agent both commands.
    - The wizard asks again when an entered command is not a valid command line or its program cannot be found by the user's shell, so typos are caught. The user's shell is the one that runs agents, so executables on PATH, aliases, and shell functions are all accepted.
    - When no agent is detected and the user adds none, the wizard tells the user that at least one agent is required and asks for agents again; it never saves a configuration without an agent.
    - Agents are saved in the order they were detected or added.
    - The wizard explains that agents can be added or edited later in config.json.
    - The TUI dialogs number their entries with single keys, so the wizard accepts at most 9 analysis agents and at most 9 coding agents. Each section states its limit, counts the detected agents toward it, and stops asking for more commands once it is full.
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
    Then the system prompts the user for each missing globally required field
    And the system writes the collected values to "~/.config/workdash/config.json"
    And the system reports the saved configuration path

  @id:F-SETUP-CONFIGURE-S002
  Scenario: Detected agent command-line tools are added with their default commands
    Given the user has no configuration file
    And a supported agent's command-line tool is on PATH
    When the user runs the system with "--configure"
    Then the system tells the user which agents were detected
    And the system adds each detected agent with its default analyze and launch commands

  @id:F-SETUP-CONFIGURE-S012
  Scenario: Agents are asked in two sections that list the detected agents
    Given the user has no configuration file
    And the command-line tools of Claude and pi are on PATH
    When the user runs the system with "--configure"
    Then the system shows an "Agents for analyze" section listing `claude` and `pi`
    And the system shows an "Agents for code" section listing `claude` and `pi`
    And each section states how many agents it accepts
    And each section asks for another command until the user answers with an empty line

  @id:F-SETUP-CONFIGURE-S013
  Scenario: A custom agent is added with a command and a name
    Given the user has no configuration file
    And no supported agent's command-line tool is on PATH
    When the user runs the system with "--configure"
    And the user adds the analyze command `aider --yes` named `aider`
    And the user adds no more agents
    Then the saved configuration has an agent named `aider` with the analyze command `aider --yes`
    And that agent has no launch command

  @id:F-SETUP-CONFIGURE-S014
  Scenario: The wizard explains that the prompt is appended to the command
    Given the user has no configuration file
    When the user runs the system with "--configure"
    Then before asking for an agent command the system tells the user that the task prompt is appended as the command's last argument
    And each section tells the user what kind of command it asks for, with an example

  @id:F-SETUP-CONFIGURE-S015
  Scenario: The wizard insists on at least one agent
    Given the user has no configuration file
    And no supported agent's command-line tool is on PATH
    When the user runs the system with "--configure"
    And the user adds no agents
    Then the system tells the user that no supported agent was detected
    And the system tells the user that at least one agent is required
    And the system asks for agents again
    And no configuration is saved until an agent has been added

  @id:F-SETUP-CONFIGURE-S016
  Scenario: Configured agents are not asked about again
    Given the user already has a configuration with agents
    When the user runs the system with "--configure"
    Then the system does not ask about agents
    And the saved configuration keeps the same agents in the same order

  @id:F-SETUP-CONFIGURE-S017
  Scenario: A command whose program cannot be found is rejected
    Given the user has no configuration file
    And no supported agent's command-line tool is on PATH
    When the user runs the system with "--configure"
    And the user enters the analyze command `aidre --yes`, which no program matches
    Then the system tells the user that `aidre` was not found
    And the system asks for the analyze command again

  @id:F-SETUP-CONFIGURE-S018
  Scenario: The wizard stops asking when a section is full
    Given the user has no configuration file
    And no supported agent's command-line tool is on PATH
    When the user runs the system with "--configure"
    And the user adds 9 analyze commands
    Then the system tells the user that the analyze section is full
    And the system does not ask for another analyze command

  @id:F-SETUP-CONFIGURE-S019
  Scenario: The wizard explains that agents can be edited later
    Given the user has no configuration file
    When the user runs the system with "--configure"
    Then the system tells the user that agents can be added or edited later in "~/.config/workdash/config.json"

  @id:F-SETUP-CONFIGURE-S010
  Scenario: Missing open command accepts the platform default
    Given the user has no configuration file
    And the platform opener is available
    When the user runs the system with "--configure"
    Then submitting an empty open command stores the platform default

  @id:F-SETUP-CONFIGURE-S011
  Scenario: Repair an unavailable viewer without changing other settings
    Given the user has a configured viewer whose executable is unavailable
    When the user runs the system with "--configure"
    Then the wizard saves a working custom viewer and its terminal choice
    And previously set fields are preserved in the saved configuration

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

  @id:F-SETUP-CONFIGURE-S009
  Scenario: Missing todo repository is prompted with the user's todos default
    Given the user provides a GitHub username during configuration
    And the configuration has no todo repository
    When the configuration wizard completes
    Then the system prompts for the todo repository with the default "<username>/todos"
    And submitting an empty response stores "<username>/todos"

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

  @id:F-SETUP-CONFIGURE-S020
  Scenario: Accept the default analysis viewer
    Given the user has no configuration file
    And the user accepts the default analysis viewer
    When the user runs the system with "--configure"
    Then the saved analysis viewer uses Markdown in a terminal pane

  @id:F-SETUP-CONFIGURE-S021
  Scenario: Choose a custom HTML analysis viewer
    Given the user has no configuration file
    And the user chooses a custom HTML analysis viewer requiring a terminal pane
    When the user runs the system with "--configure"
    Then the saved analysis viewer uses that command and HTML in a terminal pane
