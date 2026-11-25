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
    - When the configuration file is missing, starting the system runs the wizard unless it can attach to a running dashboard in the selected Zellij session. If setup succeeds, the system tells the user to run it again instead of starting the dashboard.
    - Automatic setup runs only when the configuration file is confirmed missing. If required settings are missing when starting a new dashboard, the system tells the user to run the wizard.
    - A configuration symlink with a missing target does not start automatic setup. Explicit `--configure` can create the target without replacing the symlink.
    - Automatic setup leaves a configuration saved by another run untouched and reports that it did not save the user's answers. Explicit `--configure` can still update an existing configuration.
    - If automatic setup cannot finish saving the configuration, it reports the failure without leaving an incomplete configuration. If the filesystem does not support hard links, the error explains that limitation and directs the user to run `workdash --configure`. If publication is denied, the wizard gives the same recovery command without guessing why. For other failures, the user can retry by starting the system again.
    - If the configuration was saved but a temporary file could not be removed, the system warns about that file and still reports success.
    - Ctrl+C cancels the wizard with a short message and exit status 130, whether it started automatically or with `--configure`.
    - Required configuration fields are the GitHub username, open command, at least one repository selector, the work directory, the todo repository, and at least one agent.
    - Every required field must be present for the system to start; the wizard backfills missing required fields, using a default when one exists.
    - The analysis viewer is required; the wizard defaults to `workdash view` for Markdown in a terminal pane.
    - A configuration naming the removed `workdash mdview` or `workdash markdownview` viewer is repaired to the default `workdash view`, and the wizard rejects that removed command if it is typed again.
    - When the open command is missing, malformed, or its executable is unavailable, the wizard prompts until a valid executable command is supplied, allowing arguments and explicit executable paths.
    - The wizard suggests only an available platform opener: `xdg-open` on Linux or `open` on macOS, explicitly explaining that Enter accepts it. Without a suggestion, empty input is rejected.
    - For a missing or invalid viewer terminal choice, the wizard asks "Does the viewer require its own terminal window? [y/N]", defaulting to No and preserving explicit boolean choices.
    - The wizard explains that advanced viewer commands can be edited in config.json.
    - When the todo repository is empty or not in "owner/repo" form, the wizard prompts for it. It offers "<username>/todos" only if that is a valid repository selector and names a specific repository. Otherwise it requires an explicit "owner/repo". It explains the required format and asks again if the answer is invalid; the repository does not have to exist yet.
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
    - When automatic setup finds no system installation of GitHub CLI or Zellij, it installs a fresh private copy of the missing dependency. It replaces any leftover copy in one step, preserving the permissions of an existing regular file while ensuring it is executable. If the private path is a symlink, automatic setup replaces the link and leaves its target unchanged.
    - If automatic setup needs to install a dependency and either the Workdash configuration directory or its local `bin` directory is a symlink, it reports the linked directory, leaves its files unchanged, and exits without saving configuration or reporting completion. Explicit `workdash --configure` can install through an intentional link. When both tools are on PATH, automatic setup does not write local dependencies and can proceed with either directory symlinked.
    - Re-running the wizard always checks PATH for GitHub CLI first; if no PATH binary is available, it downloads a fresh local binary even when a previous local binary exists.
    - A stalled dependency download times out. If a dependency download fails, including when it ends early, the wizard reports the download URL and reason without a traceback and exits with status 1 without saving the configuration, reporting completion, or starting the dashboard.
    - During automatic setup, a failed installation of a local Zellij or GitHub CLI binary leaves any previously installed copy usable, including one installed by another setup run.
    - Explicit `--configure` updates an installed binary in place. Upgrading a symlinked executable preserves the link, and upgrading an executable preserves its existing permissions while ensuring it is executable.
    - When a missing field has a default value, submitting an empty response accepts the default.
    - When a missing field has no default value, the wizard keeps prompting until the user provides a value.
    - A newly entered GitHub username starts with an ASCII letter or digit and may contain more ASCII letters, digits, or hyphens, including consecutive or trailing hyphens in legacy logins. A managed username may use single hyphens between letters and digits before the underscore and its 3–8 character alphanumeric enterprise shortcode. The whole username may be at most 39 characters. The wizard keeps the username's case and asks again if the answer is invalid.
    - When the repositories list is empty, the wizard defaults it to "<username>/*" with the owner in lowercase if that is a valid repository selector. Otherwise it asks for one valid "owner/repo" or "owner/*" selector and asks again if the answer is empty or invalid. Existing selectors are preserved.
    - If `listens` is not a list, the wizard reports how to correct it and leaves the configuration file unchanged.
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
    Given the user already has a complete configuration with agents
    When the user runs the system with "--configure"
    Then the system saves the configuration without prompting
    And the saved configuration keeps the same agents in the same order
    And after reporting the saved configuration path, the wizard explains how to hide repositories

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

  @id:F-SETUP-CONFIGURE-S022
  Scenario: Repair an existing configuration missing its analysis viewer
    Given the user has an existing configuration missing the analysis viewer
    When the user starts Workdash and then runs the configuration wizard
    Then startup directs the user to run --configure
    And the wizard asks for an analysis viewer and saves the default
    And the other configuration settings are preserved

  @id:F-SETUP-CONFIGURE-S023
  Scenario: Repair a configuration naming the removed Markdown viewer
    Given the user has a configuration naming the removed Markdown viewer
    When the user runs the system with "--configure"
    Then the wizard asks for the analysis viewer with the single view command default
    And the saved analysis viewer uses Markdown in a terminal pane

  @id:F-SETUP-CONFIGURE-S024
  Scenario: A removed viewer typed again is refused
    Given the user has a configuration naming the removed Markdown viewer
    And the user answers the analysis viewer prompt with the removed command
    When the user runs the system with "--configure"
    Then the wizard asks for the analysis viewer again
    And the saved analysis viewer uses Markdown in a terminal pane

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

  @id:F-SETUP-CONFIGURE-S025
  Scenario: Starting the system without a configuration file runs the wizard
    Given the user has no configuration file
    When the user starts the system
    Then the system prompts the user for each missing globally required field
    And the system writes the collected values to "~/.config/workdash/config.json"
    And the system tells the user to run the system again
    And the system stops without starting the dashboard

  @id:F-SETUP-CONFIGURE-S035
  Scenario: A stalled dependency download stops automatic setup
    Given the user has no configuration file
    And Zellij is not installed on PATH
    And the Zellij download stalls
    When the user starts the system
    Then the system reports that the Zellij download timed out
    And the system exits with a non-zero status
    And no configuration file is saved
    And the system does not tell the user that setup completed or to run it again
    And the system stops without starting the dashboard

  @id:F-SETUP-CONFIGURE-S036
  Scenario: An incomplete dependency download stops automatic setup
    Given the user has no configuration file
    And Zellij is not installed on PATH
    And the Zellij download ends before all bytes arrive
    When the user starts the system
    Then the system reports the incomplete Zellij download without a traceback and exits with status 1
    And no configuration file is saved
    And the system does not tell the user that setup completed or to run it again
    And the system stops without starting the dashboard

  @id:F-SETUP-CONFIGURE-S031
  Scenario: Automatic setup accepts a mixed-case GitHub username
    Given the user has no configuration file
    And the user enters "Octocat" as their GitHub username
    When the user starts the system
    Then the saved configuration keeps "Octocat" as the GitHub username
    And the wizard saves and reports "octocat/*" as the default repository selector

  @id:F-SETUP-CONFIGURE-S032
  Scenario: Automatic setup rejects an invalid GitHub username
    Given the user has no configuration file
    And the user answers the GitHub username prompt with "@octocat" and then "octocat"
    When the user starts the system
    Then the wizard explains the GitHub username format
    And the system prompts again for the GitHub username
    And the saved configuration keeps "octocat" as the GitHub username
    And the wizard saves and reports "octocat/*" as the default repository selector

  @id:F-SETUP-CONFIGURE-S033
  Scenario Outline: Setup accepts a managed GitHub username and asks for repositories separately
    Given the user has no configuration file
    And the user enters "<username>" as their GitHub username
    And the user first leaves the repository selector blank, then enters "mona-cat_octo/*" and "<selector>"
    And the user first leaves the todo repository blank, then enters "octo-org/*" and "octo-org/tasks"
    When <invocation>
    Then the saved configuration keeps "<username>" as the GitHub username
    And the wizard asks for the repository selector again until it can save "<selector>"
    And the wizard asks for the todo repository again without suggesting a default
    And the saved configuration uses "octo-org/tasks" as the todo repository

    Examples:
      | invocation                                  | username      | selector          |
      | the user starts the system                  | mona-cat_octo | octo-org/*        |
      | the user runs the system with "--configure" | Mona-Cat_Octo | octo-org/projects |

  @id:F-SETUP-CONFIGURE-S034
  Scenario Outline: Setup accepts a legacy GitHub username and offers repository defaults
    Given the user has no configuration file
    And the user enters "<username>" as their GitHub username
    When <invocation>
    Then the saved configuration keeps "<username>" as the GitHub username
    And the wizard saves and reports "<selector>" as the default repository selector
    And the saved configuration uses "<username>/todos" as the todo repository

    Examples:
      | invocation                                  | username  | selector    |
      | the user starts the system                  | Amol-     | amol-/*     |
      | the user runs the system with "--configure" | john--doe | john--doe/* |

  @id:F-SETUP-CONFIGURE-S026
  Scenario: An existing configuration file is never reconfigured automatically
    Given the user has a configuration file missing a required field
    When the user starts the system
    Then the system tells the user to run the configuration wizard
    And the system does not run the configuration wizard

  @id:F-SETUP-CONFIGURE-S029
  Scenario: Another run saves the configuration during automatic setup
    Given the user has no configuration file
    And another run saves a configuration while the user answers the wizard
    When the user starts the system
    Then the other run's configuration is unchanged
    And the system reports that setup did not save the user's answers
    And the system exits with a non-zero status
    And the system does not tell the user that setup completed or to run it again

  @id:F-SETUP-CONFIGURE-S030
  Scenario: An invalid todo repository is rejected before saving
    Given the user has no configuration file
    And the user answers the todo repository prompt with "todos" and then "octocat/tasks"
    When the user runs the system with "--configure"
    Then the wizard says the todo repository must be in "owner/repo" form and asks again
    And the saved configuration uses "octocat/tasks" as the todo repository

  @id:F-SETUP-CONFIGURE-S027
  Scenario: Cancel automatic setup
    Given the user has no configuration file
    And the user presses Ctrl+C at the first wizard prompt
    When the user starts the system
    Then the system reports that setup was cancelled and exits with status 130
    And the system does not tell the user that setup completed or to run it again

  @id:F-SETUP-CONFIGURE-S028
  Scenario: Cancel explicitly requested setup
    Given the user has no configuration file
    And the user presses Ctrl+C at the first wizard prompt
    When the user runs the system with "--configure"
    Then the system reports that setup was cancelled and exits with status 130
