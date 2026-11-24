@feature:F-SITE-SOCIAL-PREVIEW
Feature: Share the workdash site

  Rules:
    - Dashboard examples show authored and review-requested pull requests, todos, assigned issues, and a parent issue with native sub-issues together.
    - Analysis examples offer both cached analysis and a choice of Claude and Codex. Visitors see how to read the result in the terminal or as HTML.
    - The dispatch demo explains each stage on screen and plays without requiring a click. Visitors who prefer reduced motion see a still.
    - Pull request analysis covers the description, comments, and reviews.
    - Terminal examples display readable tabs and action labels without missing-glyph squares.
    - The dispatch demo holds each stage long enough to read: choosing an agent, implementing, reviewing, and completing work.
    - The dispatch diagram shows rework returning to implementation and completed work returning to the ready-work queue.
    - The Work and Review pages introduce their workflows before showing commands and examples.

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

  @id:F-SITE-BROWSE-S004
  Scenario: Reach the workdash repository from the navigation
    When a visitor browses the workdash site
    Then every page offers navigation to the workdash topics and installation
    And every page links to the workdash repository after the installation link

  @id:F-SITE-BROWSE-S003
  Scenario: Get started from the homepage
    When a visitor browses the workdash site
    Then the homepage shows the install command and starting workdash before the feature tour
    And the homepage explains that setup provides missing GitHub CLI and Zellij binaries
    And visitors using Workdash 0.5.0 see the manual setup command below the quickstart box

  @id:F-SITE-BROWSE-S002
  Scenario: Check the site before publishing
    When a maintainer checks the site
    Then the site check succeeds without generating images
