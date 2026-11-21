@feature:F-STARTUP-ZELLIJ-WRAPPER
Feature: Persistent dashboard Zellij session

  Rules:
    - Outside Zellij, workdash creates or attaches to the `workdash` session; `--session NAME` selects another session. An exited selected session is recovered with its existing tabs and panes before ensuring the dashboard is running. Tabs added after recovery are also preserved for a later recovery, even if the user's Zellij configuration disables session persistence.
    - A new session has a `dashboard` tab with tab and status bars, a dashboard pane named `workdash tui` above a shell pane.
    - If a dashboard is already running for the session, workdash reports that the new options are ignored and attaches after one second, even when that dashboard serves a different address than the one this start would use. When possible, it brings the dashboard into focus, even if its tab was renamed or shares a name with another tab. Focus applies to the client that most recently sent input, which may not be the returning client. If Zellij rejects focus because the dashboard pane no longer exists, clients remain on their current tabs.
    - A dashboard that exited without cleanup is not reported as running, so the next start replaces it.
    - If the dashboard is not running, workdash starts it with the new options and current `WORKDASH_HOST` setting in the `dashboard` tab, creating that tab if needed. An unset `WORKDASH_HOST` selects the default address.
    - If the address this start would use already serves a dashboard for another session, workdash reports that session and the address instead of starting a dashboard that cannot serve.
    - If the dashboard at the address refuses the client version, workdash reports the version mismatch and starts nothing.
    - If the address this start would use answers without proving which session it serves, workdash reports that it cannot tell whether a dashboard is running and starts nothing.
    - Quitting the dashboard leaves the session running and tells the user that Ctrl+Q quits the whole session and all its panes. A dashboard restarted in that tab leaves the pane it replaced as a shell, so both panes are named `workdash tui`.
    - Inside Zellij, the dashboard starts in the current pane and uses the current session regardless of `--session`; when that session already has a dashboard, workdash reports that the new options are ignored and starts nothing.
    - Server-backed client commands never wrap.
    - Startup preflight runs before starting a new dashboard; attaching to a running dashboard does not require it. Missing Zellij reports configuration guidance.

  @id:F-STARTUP-ZELLIJ-WRAPPER-S013
  Scenario: Recover an exited selected session
    Given the selected Zellij session has exited with existing work panes
    When the user starts the dashboard in the named session with configured listeners
    Then the existing session is recovered with its work panes and a dashboard

  @id:F-STARTUP-ZELLIJ-WRAPPER-S001
  Scenario: Start a dashboard outside Zellij
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the system replaces itself with a Zellij process

  @id:F-STARTUP-ZELLIJ-WRAPPER-S002
  Scenario: Interactive startup inside Zellij starts directly
    Given the system is running inside a Zellij session
    When the user starts the interactive dashboard
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
    When the user starts the dashboard in the named session with configured listeners
    Then the system attaches to the named session
    And the system reports that the new options are ignored
    And the system does not start a dashboard

  @id:F-STARTUP-ZELLIJ-WRAPPER-S019
  Scenario: Return to a running dashboard in another tab
    Given a running dashboard is in another tab of the named session
    When the user returns to that session with Workdash and sends the most recent input
    Then the returning client's input reaches the dashboard pane

  @id:F-STARTUP-ZELLIJ-WRAPPER-S011
  Scenario: Restart a dashboard in an existing session
    Given the named session `project` has a stopped dashboard
    When the user starts the dashboard in the named session with configured listeners
    Then the system starts a dashboard in that session
    And the new pane is named `workdash tui` and the replaced pane is not renamed
    And the restarted dashboard accepts configured clients and rejects other clients

  @id:F-STARTUP-ZELLIJ-WRAPPER-S014
  Scenario: Attach to a dashboard running on another address in the named session
    Given the named session `project` already has a running dashboard on another address
    When the user starts the dashboard in the named session with configured listeners
    Then the system attaches to the named session
    And the system reports that the new options are ignored
    And the system does not start a dashboard

  @id:F-STARTUP-ZELLIJ-WRAPPER-S015
  Scenario: Refuse to start a dashboard on an address that serves another session
    Given the named session `project` has no dashboard
    And its address serves session `other`
    When the user starts the dashboard in the named session with configured listeners
    Then the system reports the other session and the address it serves
    And the system does not start a dashboard
    And the system exits with a non-zero status

  @id:F-STARTUP-ZELLIJ-WRAPPER-S016
  Scenario: Refuse to start when the dashboard address cannot be identified
    Given the named session `project` has no dashboard
    And its address answers without proving which session it serves
    When the user starts the dashboard in the named session with configured listeners
    Then the system reports that it cannot tell whether a dashboard is running
    And the system does not start a dashboard
    And the system exits with a non-zero status

  @id:F-STARTUP-ZELLIJ-WRAPPER-S018
  Scenario: Refuse to start against a dashboard with a different version
    Given the named session `project` has no dashboard
    And its address serves a dashboard with a different Workdash version
    When the user starts the dashboard in the named session with configured listeners
    Then the system reports the version mismatch and restart guidance
    And the system does not start a dashboard
    And the system exits with a non-zero status

  @id:F-STARTUP-ZELLIJ-WRAPPER-S017
  Scenario: Starting the dashboard from a pane of a session that already has one
    Given the system is running inside a Zellij session whose dashboard is already running
    When the user starts the interactive dashboard
    Then the system reports that the new options are ignored
    And the system does not start a dashboard
    And the system exits successfully

  @id:F-STARTUP-ZELLIJ-WRAPPER-S012
  Scenario: Closing the terminal leaves the new session running
    Given the system is not running inside a Zellij session
    And Zellij is installed on PATH
    When the user starts the interactive dashboard
    Then the Zellij session detaches when the terminal closes

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
