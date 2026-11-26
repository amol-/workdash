@feature:F-CLI-BIRDVIEW
Feature: Share a snapshot of current work
  Share the running server's available work without opening the TUI.

  @id:F-CLI-BIRDVIEW-S001
  Scenario: Share current work from a remote server
    Given a Workdash server with ongoing work
    When the user requests a birdview from that server
    Then the snapshot shows the current work with links, pane IDs and blocked status
