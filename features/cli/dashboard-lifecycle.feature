@feature:F-CLI-DASHBOARD-LIFECYCLE
Feature: Start and reopen a persistent dashboard

  @id:F-STARTUP-ZELLIJ-WRAPPER-S008
  @id:F-STARTUP-ZELLIJ-WRAPPER-S017
  @id:F-STARTUP-ZELLIJ-WRAPPER-S011
  Scenario: Open a dashboard and restart it without losing shell work
    Given the user is outside Zellij and has not yet opened a Workdash session
    When the user launches `workdash` from the terminal
    Then the dashboard responds to `/` and its sibling shell accepts work
    When the user runs `workdash --refresh` in the sibling shell
    Then Workdash reports that the new options are ignored and leaves the original dashboard running
    When the user presses `q` in the dashboard and runs `workdash --debug` from another terminal
    Then the reopened dashboard is usable and the sibling shell still holds the user's work

  @id:F-STARTUP-ZELLIJ-WRAPPER-S015
  Scenario: Start a second dashboard on a free address
    Given the dashboard in session `other` is running at the selected address
    When the user runs `workdash --session project` from another terminal
    Then Workdash identifies the session and address already in use
    When the user sets `WORKDASH_HOST` to a free address and reruns `workdash --session project`
    Then `workdash info` finds `project` at the new address and `other` at the original address
