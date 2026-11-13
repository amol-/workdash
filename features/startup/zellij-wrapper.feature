@feature:F-STARTUP-ZELLIJ-WRAPPER
Feature: Persistent dashboard Zellij session

  Rules:
    - Outside Zellij, workdash creates or attaches to the `workdash` session; `--session NAME` selects another session.
    - A new session has a `dashboard` tab with tab and status bars, a dashboard pane named `workdash tui` above a shell pane.
    - If the dashboard is already running, workdash reports that the new options are ignored and attaches after one second.
    - If the dashboard is not running, workdash starts it with the new options in the `dashboard` tab, creating that tab if needed, and preserves any stale dashboard pane under another name.
    - Quitting the dashboard leaves the session running and tells the user that Ctrl+Q quits the whole session and all its panes.
    - Inside Zellij, the dashboard starts in the current pane and uses the current session regardless of `--session`; stale dashboard panes are preserved under another name, and a running dashboard is not renamed.
    - Direct mode bypasses the wrapper; server-backed client commands never wrap.
    - Startup preflight runs before starting a new dashboard; attaching to a running dashboard does not require it. Missing Zellij reports configuration guidance.

  @id:F-STARTUP-ZELLIJ-WRAPPER-S001
  Scenario: Start a dashboard outside Zellij
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the system replaces itself with a Zellij process
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
  Scenario: Server-backed client commands do not wrap themselves
    Given the system is not running inside a Zellij session
    When the user starts a server-backed client command
    Then the system does not replace itself with Zellij
    And the command connects to the local Workdash server

  @id:F-STARTUP-ZELLIJ-WRAPPER-S006
  Scenario: Missing Zellij aborts interactive startup
    Given the system is not running inside a Zellij session
    And Zellij is not installed on PATH
    When the user starts the interactive dashboard
    Then the system tells the user to run the configuration wizard
    And the system exits with a non-zero status

  @id:F-STARTUP-ZELLIJ-WRAPPER-S010
  Scenario: Attach to a running dashboard in a named session
    Given the named session `project` already has a running dashboard
    When the user starts the dashboard in the named session with server mode
    Then the system attaches to the named session
    And the system reports that the new options are ignored

  @id:F-STARTUP-ZELLIJ-WRAPPER-S011
  Scenario: Restart a dashboard in an existing session
    Given the named session `project` has a stopped dashboard
    When the user starts the dashboard in the named session with server mode
    Then the system starts a dashboard with server mode in that session
    And the previous dashboard pane is preserved

  @id:F-STARTUP-ZELLIJ-WRAPPER-S012
  Scenario: Closing the terminal leaves the new session running
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the Zellij session detaches when the terminal closes

  @id:F-STARTUP-ZELLIJ-WRAPPER-S007
  Scenario: Server mode starts a server-backed dashboard
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard with `--server`
    Then the Zellij process runs the dashboard with `--direct --server`

  @id:F-STARTUP-ZELLIJ-WRAPPER-S008
  Scenario: A new session starts with a dashboard and shell
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the first tab is named `dashboard`
    And the dashboard is above a shell pane

  @id:F-STARTUP-ZELLIJ-WRAPPER-S009
  Scenario: A new session shows the tab bar and status bar
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the Zellij session shows the tab bar at the top
    And the Zellij session shows the status bar at the bottom
