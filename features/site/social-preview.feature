@feature:F-SITE-SOCIAL-PREVIEW
Feature: Share the workdash site

  @id:F-SITE-SOCIAL-PREVIEW-S001
  Scenario Outline: A shared site link has a rich preview
    When a social platform reads the <page> page of the workdash site
    Then it can display the workdash title, description, and dashboard image in a large preview

    Examples:
      | page     |
      | index    |
      | work     |
      | review   |
      | agentic  |
      | control  |

  @id:F-SITE-BROWSE-S001
  Scenario: Navigate between site pages
    When a visitor browses the workdash site
    Then every page offers navigation to the workdash topics and installation
    And local links and images on the pages resolve

  @id:F-SITE-BROWSE-S002
  Scenario: Check the site before publishing
    When a maintainer checks the site
    Then the site check succeeds without generating images
