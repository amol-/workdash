@feature:F-API-SERVER-LOG
Feature: Server diagnostic log

  A server-backed Workdash session writes a diagnostic log so the user can
  see why a background action failed after the fact.

  Rules:
    - Each successful server start replaces `~/.config/workdash/workdash.log`; a start that cannot bind leaves the existing log untouched.
    - A server start that cannot write the log fails and names the log path instead of running without diagnostics.
    - The log records the server-side failure, the code location that raised it, and known diagnostics such as the exit code and HTTP status of a failed external command.
    - The log never contains raw external command output, request bodies, or other secrets.

  @id:F-API-SERVER-LOG-S001
  Scenario: A failed background action is recorded in the server log
    Given an existing server log from a previous server run
    When a server-backed Workdash session starts and a background dispatch fails
    Then the server log records the failure and where it happened
    And the server log holds only the current run
    And the server log omits the failure's raw details
