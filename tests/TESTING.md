# Testing

## Choose the test before writing it

**BDD is the executable user guide.** Ask: “Would we teach this journey in a
Workdash user guide?” Being user-visible is not enough. A scenario shows a
realistic way to accomplish a goal, recover from a problem, or experience an
automatic outcome that is part of the main workflow. The journey does not need
an explicit user action at every step.

- **TUI journeys** go in `features/tui/`. Use the keys and actions a person
  would actually use.
- **CLI journeys** go in `features/cli/`. Use the command a person would run and
  verify the outcome through its human-facing output. Test `--json` fields and
  formatting in plain tests, not in scenarios.
- **Internal HTTP API** coverage belongs in plain tests. A real user journey
  should not become an API scenario just because the TUI or CLI uses HTTP.
- **Everything else** goes in plain tests under `tests/`: corner cases,
  internal rules, failure handling that requires no user action, and layout
  regressions. Use the smallest test that proves the behavior; use a TUI or
  end-to-end test only when a lower-level test cannot prove it.

Classify by what the test proves, not by the tool it uses. A plain test can
press real keys; a BDD step can use a helper to set up state without exposing
that helper in the scenario.

## Workdash examples

| BDD journey | Plain test |
| --- | --- |
| Press `r` to refresh the dashboard. | A failed refresh shows an error, dismisses the overlay, and keeps the old list. |
| Sign in with `gh auth login` and relaunch Workdash when prompted. | Different ways the authentication check can fail. |
| Start new work from an issue; continue existing work from an authored PR. These are distinct journeys even though both start with `d`. | Which linked issue wins when one PR closes several issues. |
| Focus a blocked agent's pane, type guidance, and see autodispatch resume. Automatic resumption is part of the journey. Stopping a blocked session is another journey. | Verify that `workdash write` sends input. Don't use it in the recovery scenario just because it is convenient for the test. |
| Find and open a work item. | Verify every row stays visible above the action bar while scrolling. |

## Write and maintain scenarios

- Name the user's goal. Use `Given` for relevant starting state, `When` for
  the action or event that moves the journey forward, and `Then` for an
  observable outcome. Leave out internal calls, data structures, and incidental
  setup. For recovery, show what the user does to get unstuck.
- Use the interface that teaches the action. How the user first noticed a
  blocked session is usually irrelevant to the recovery scenario. Give the TUI
  and CLI their own scenarios when both teach a real way to use Workdash.
- Keep feature descriptions brief. Don't put behavioral requirements in a
  prose `Rules:` list. Prove them in executable scenarios or plain tests.
- Check existing coverage before adding a test. Test each behavior once. Two
  tests can exercise the same feature if they prove different things; don't
  repeat a shared assertion just because there are two interfaces. When moving
  a corner case out of BDD, preserve its coverage in a plain test.
- Start with a `.feature` scenario when adding or changing a user journey.
  Don't create or update a scenario solely to document an internal change,
  a corner case, or a formatting regression.

## Textual UI tests

Don't add `await pilot.pause()` immediately after `await pilot.press(...)` just to let the UI settle. Textual already waits for idle while processing each key. If the test depends on a separate asynchronous operation, wait for the specific condition the test needs instead.
