@feature:F-CODING-REVIEW
Feature: Review a pull request
  A reviewer can request an autonomous review of a pull request or its linked issue.

  Rules:
    - An issue without a linked open pull request, or a closed or merged pull request, has nothing to review.
    - A review reports major findings as inline comments and submits an appropriate review when permitted.
    - A review of one's own pull request is comment-only.
    - A review document summarizes the outcome and any posted inline comments.
    - A repeated request for a running review returns the same started information without starting another review.
    - A started review reports the intended document path and runs without blocking other control requests.

  @id:F-CODING-REVIEW-S001
  Scenario: Start a review of an open pull request
    Given an open pull request is available for review
    When the reviewer requests an autonomous review
    Then the reviewer is told the review started and where its document will be written

  @id:F-CODING-REVIEW-S004
  Scenario: Request the same review again
    Given an open pull request review is in progress
    When the reviewer requests the same review again
    Then the reviewer receives the same started information

  @id:F-CODING-REVIEW-S002
  Scenario: An issue has no open pull request
    Given an issue has no linked open pull request
    When the reviewer requests an autonomous review of the issue
    Then the reviewer is told there is nothing to review

  @id:F-CODING-REVIEW-S003
  Scenario: List work while a review is running
    Given an open pull request review is in progress
    When the reviewer lists work items
    Then the list is returned before the review finishes
