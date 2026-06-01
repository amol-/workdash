@feature:F-STARTUP-ZELLIJ-WRAPPER
Feature: Start the interactive dashboard inside Zellij

  The interactive dashboard owns the Zellij boundary at startup. Once the TUI
  starts, terminal-backed work actions can rely on an existing Zellij session
  instead of managing operating system terminals or guessing target sessions.

  Rules:
    - If the user starts the interactive dashboard outside Zellij, the system replaces the current process with Zellij.
    - Before locating Zellij at startup, the system appends the workdash-local bin directory to PATH so a global Zellij wins when present and the local downloaded binary is a fallback.
    - If no Zellij binary can be found after extending PATH, startup exits with a clear message telling the user to run the configuration wizard.
    - The replacement Zellij process starts a new `workdash-<random>` session and runs the dashboard with `--direct`.
    - If the user starts the interactive dashboard inside Zellij, the system starts directly.
    - If the user passes `--direct`, the system starts directly even when Zellij is not detected.
    - Non-interactive command modes do not wrap themselves in Zellij.
    - The replacement Zellij process uses Zellij's documented force-close behavior to quit the session when its terminal closes.
    - The replacement Zellij process disables session serialization and session metadata so closed generated sessions do not become resurrection targets.
    - When the dashboard command exits, its Zellij pane closes instead of leaving an exited command pane behind.
    - Direct mode is only for bypassing the startup wrapper.

  @id:F-STARTUP-ZELLIJ-WRAPPER-S001
  Scenario: Interactive startup outside Zellij replaces itself with a fresh workdash Zellij session
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the system replaces itself with a Zellij process
    And the Zellij process starts a fresh workdash-prefixed session
    And the Zellij process runs the dashboard with `--direct`

  @id:F-STARTUP-ZELLIJ-WRAPPER-S002
  Scenario: Interactive startup inside Zellij starts directly
    Given the system is running inside a Zellij session
    When the user starts the interactive dashboard
    Then the system starts the dashboard directly

  @id:F-STARTUP-ZELLIJ-WRAPPER-S003
  Scenario: Direct mode bypasses Zellij startup
    Given the system is not running inside a Zellij session
    When the user starts the interactive dashboard with `--direct`
    Then the system starts the dashboard directly

  @id:F-STARTUP-ZELLIJ-WRAPPER-S004
  Scenario: List command outside Zellij does not wrap itself
    Given the system is not running inside a Zellij session
    When the user starts the non-interactive list command
    Then the system does not replace itself with Zellij
    And the system prints work items directly

  @id:F-STARTUP-ZELLIJ-WRAPPER-S005
  Scenario: The generated Zellij session uses documented close behavior
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the Zellij process is configured to quit on force close
    And the Zellij process disables session resurrection state
    And the dashboard pane closes when the dashboard exits
    And the dashboard command does not install a manual session cleanup trap

  @id:F-STARTUP-ZELLIJ-WRAPPER-S006
  Scenario: Missing Zellij binary aborts interactive startup
    Given the system is not running inside a Zellij session
    And Zellij is not installed on PATH
    When the user starts the interactive dashboard
    Then the system tells the user to run the configuration wizard
    And the system exits with a non-zero status
