@feature:F-TUI-AUTODISPATCH
Feature: Control autodispatch from the dashboard

  @id:F-AUTODISPATCH-S013
  Scenario: Start autodispatch from an issue in the dashboard
    Given a ready issue selected in the dashboard and another item to triage
    And the default coding agent cannot analyze but another agent can code and analyze
    When the user presses d, chooses agent 1, and uses / to find the other item once work begins
    Then an autodispatch session launches that issue with the compatible agent
    And the dashboard has announced the start and shows the other item while autodispatch continues

  @id:F-AUTODISPATCH-S020 @id:F-CODING-AUTOCLOSE-S019
  Scenario: Guide a blocked agent from the dashboard
    Given a blocked autodispatch session visible in dashboard progress
    When the user presses Shift+Tab and f, then types guidance directly into the focused agent pane
    Then the dashboard shows that the session has resumed

  @id:F-AUTODISPATCH-S021
  Scenario: Stop a blocked session from the dashboard
    Given a blocked autodispatch session visible in dashboard progress
    When the user presses Shift+Tab and s to stop the blocked session
    Then the session disappears from progress
    And its agent pane closes and its worktree is preserved

  @id:F-AUTODISPATCH-S028
  Scenario: Cancel choosing an autodispatch agent
    Given a ready issue selected in the dashboard
    When the user presses d and then Escape to cancel agent selection
    Then the work table is available again and no autodispatch session starts

  @id:F-AUTODISPATCH-S005
  Scenario: Start autodispatch from an authored pull request linked to an issue in another repository
    Given an authored pull request selected in the dashboard linked only to an issue in another repository
    When the user presses d and chooses an autodispatch agent
    Then an autodispatch session launches that linked issue

  @id:F-AUTODISPATCH-S007
  Scenario: Start a session for a linked issue that is not ready
    Given an authored pull request linked to an issue that is not ready
    When the user presses d and chooses an autodispatch agent
    Then an autodispatch session starts for the linked issue
    And the dashboard reports that no work is available to dispatch

  @id:F-AUTODISPATCH-S027
  Scenario: Report when an issue has no ready work because of a dependency
    Given an issue blocked by an open dependency selected in the dashboard
    When the user presses d and chooses an autodispatch agent
    Then an autodispatch session starts for the selected issue
    And the dashboard reports that the issue is blocked by its open dependency

  @id:F-AUTODISPATCH-S032
  Scenario: Inspect an active review from dashboard progress
    Given an autodispatch session is reviewing after its coding pane closes
    When the user presses Shift+Tab and f on the session in progress
    Then the active review pane is focused

  @id:F-AUTODISPATCH-S030
  Scenario: Clear a failed session from dashboard progress
    Given a failed autodispatch session is visible in progress
    When the user presses Shift+Tab and s on the failed session
    Then the failed session disappears from progress

  @id:F-AUTODISPATCH-S034
  Scenario: Change automerge for sessions in progress and in the work table
    Given the dashboard has different sessions selected in the work table and progress
    When the user presses Shift+Tab, Down and m for the session in progress, then Tab and m for the work item
    Then only the selected progress session changes and the dashboard identifies it
    And the dashboard identifies the work item's session after returning to the table

  @id:F-AUTODISPATCH-S022
  Scenario: Leave a pull request open by turning automerge back off
    Given an autodispatch session for the selected dashboard work
    When the user presses m to turn automerge on and then m to turn it back off
    Then the dashboard reports and displays both changes
    When its completed pull request has been reviewed successfully
    Then the pull request is left open
