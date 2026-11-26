@feature:F-TUI-REVIEW
Feature: Review a pull request from the dashboard

  @id:F-CODING-REVIEW-S008
  Scenario: Start a review from the code menu
    Given an open pull request is available for review
    When the user presses `c`, `r`, then `2` to choose a review agent
    Then the code menu offers a choice of analysis agent
    And the dashboard shows a progress dialog until the review finishes
    And the dashboard reports the reviewed pull request with the agent that reviewed it
