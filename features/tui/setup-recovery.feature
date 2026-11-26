@feature:F-SETUP-RECOVERY
Feature: Get to the dashboard after repairing setup

  @id:F-SETUP-CONFIGURE-S022
  Scenario: Restore the missing analysis viewer and open the dashboard
    Given the saved configuration is missing the analysis viewer
    When the user runs `workdash` to open the dashboard
    Then Workdash reports the missing `analysis_viewer` setting
    And Workdash directs them to `workdash --configure`
    When the user runs `workdash --configure` and answers the wizard:
      | prompt          | answer |
      | Analysis viewer | Enter  |
    Then the wizard offers and saves `workdash view` as the replacement
    When the user runs `workdash` to open the dashboard again
    Then the dashboard shows the tracked issue "Fix the issue"

  @id:F-SETUP-RECOVERY-S001
  Scenario: Install missing Zellij and open the dashboard
    Given the user has a complete Workdash configuration but no installed Zellij
    When the user runs `workdash` to open the dashboard
    Then Workdash directs them to `workdash --configure`
    When the user completes `workdash --configure`
    Then the wizard reports a usable local installation of Zellij
    When the user runs `workdash` to open the dashboard again
    Then the dashboard shows the tracked issue "Fix the issue"
