# Testing

## Textual UI tests

Don't add `await pilot.pause()` immediately after `await pilot.press(...)` just to let the UI settle. Textual already waits for idle while processing each key. If the test depends on a separate asynchronous operation, wait for the specific condition the test needs instead.
