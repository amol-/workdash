@feature:F-CLI-RETURN-TO-DASHBOARD
Feature: Return to a running dashboard

  @id:F-STARTUP-ZELLIJ-WRAPPER-S010
  @id:F-STARTUP-ZELLIJ-WRAPPER-S019
  @id:F-STARTUP-ZELLIJ-WRAPPER-S025
  Scenario: Return to a named dashboard from another tab
    Given the dashboard is running in a named session and the active tab is a work tab
    And the user has moved the configuration file aside
    When the user runs `workdash --session NAME --refresh` from another terminal and sends input there
    Then Workdash reports that the new options are ignored
    When the user presses `/` in the returning terminal
    Then the filter opens in the original dashboard without starting a second dashboard
    And Workdash does not prompt for setup or recreate the configuration file
