@feature:F-TUI-STARTUP
Feature: Open the dashboard in Zellij

  @id:F-STARTUP-ZELLIJ-WRAPPER-S002
  Scenario: Open a populated dashboard in the current pane
    Given the system is running inside a Zellij session
    And the user has a tracked issue titled "Fix the issue"
    When the user runs `workdash`
    Then the dashboard opens in the current pane and shows "Fix the issue"
