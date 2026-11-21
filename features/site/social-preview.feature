@feature:F-SITE-SOCIAL-PREVIEW
Feature: Share the workdash site

  Rules:
    - Dashboard examples show authored and review-requested pull requests, todos, assigned issues, and a parent issue with native sub-issues together.
    - Analysis examples offer both the cached analysis and a configured analysis agent.
    - Ready-work guidance distinguishes skipping active work when picking ready issues from starting an exact issue session, which can interrupt its current agent.
    - Pull request analysis covers the description, comments, and reviews.

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
