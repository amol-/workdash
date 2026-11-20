@feature:F-SITE-SOCIAL-PREVIEW
Feature: Share the workdash site

  @id:F-SITE-SOCIAL-PREVIEW-S001
  Scenario: A shared site link has a rich preview
    When a social platform reads the workdash site
    Then it can display the workdash title, description, and dashboard image in a large preview
