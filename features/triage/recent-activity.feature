@feature:F-TRIAGE-RECENT
Feature: Recent activity highlighting

  The TUI visually emphasises work items that changed recently so the user
  can spot fresh activity at a glance.

  Rules:
    - In the TUI, a work item whose last update is within the last 24 hours is rendered in bold across all of its columns.
    - Work items outside that window are rendered in the normal weight.
    - This highlighting only affects the TUI; list command output is not styled.

  @id:F-TRIAGE-RECENT-S001
  Scenario: Recently updated items are bolded in the TUI
    Given a work item was last updated within the last 24 hours
    When the user opens the dashboard
    Then that work item is rendered in bold

  @id:F-TRIAGE-RECENT-S002
  Scenario: Older items are rendered normally
    Given a work item was last updated more than 24 hours ago
    When the user opens the dashboard
    Then that work item is rendered in the normal weight
