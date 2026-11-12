# Autonomous review implementation plan

## Goal

Add an autonomous pull request review and connect it to autodispatch. Major review findings become resolvable inline threads that dispatch can address. Keep the workflow simple and test it without real GitHub or real agents.

Each phase is one separate devteam execution. Complete and validate each phase before starting the next. Do not commit or push without explicit permission.

## Phase 1: One-shot review command

### Requirements

- Add `workdash review ITEM` using the shared worktree and agent context preparation paths, with an explicit review direction independent of PR classification.
- Accept a pull request or an issue. For an issue, resolve its linked open PR. If no open PR exists, report that there is nothing to review; do not report a completed review.
- Use the selected agent's configured one-shot analysis command, outside a Zellij pane. Preserve existing user configuration. Change the autodetected Pi analysis command to `pi -p`.
- Prepare or update the worktree using existing analysis behavior. Do not add review-specific worktree claims or commit-change checks.
- Instruct the agent to conduct a thorough review, using available tools, skills and subagents; report only major findings whose value outweighs the author's effort. Explain findings simply, with reproduction steps or examples where practical.
- Post findings as inline review comments, not ordinary PR conversation comments. Request changes for major findings or approve when there are none, when GitHub permits. For a PR owned by the authenticated user, submit a comment-only review and do not claim a formal approval or changes-requested verdict.
- Require the agent to write a fresh completion document under the worktree's `.workdash/reviews/` directory. The document must explicitly list the inline review comments posted, or say there were none, and summarize the outcome.
- Treat review as complete only when the process exits successfully and produces the fresh completion document. If the document is missing, retry once with previous-attempt context and instruct the agent to check existing threads before posting again. Report failure if the retry is incomplete or the process fails.
- Preserve existing `code` and `analyze` behavior, apart from the Pi autodetection default. Follow existing CLI, server API, error, agent selection and documentation conventions.

### Plan and acceptance

1. Define externally observable review scenarios in `features/` before code or test changes.
2. Implement issue-to-PR resolution, one-shot agent invocation, dedicated review prompt and completion reporting using existing primitives.
3. Add focused tests with fake GitHub and fake agents for PR and issue targets, no linked PR, self-review prompt, successful completion, missing-document retry and failure. Do not call real GitHub or agents.
4. Update the README and run targeted tests, relevant broader tests and lint.
5. Complete independent devteam code-quality and validation gates.

## Phase 2: Autodispatch and readiness integration

### Requirements

- After an autodispatch task's coding pane disappears, review the PR linked to the dispatched issue, then check readiness again. If no PR exists, skip review and check readiness again.
- Validate that the selected autodispatch agent has both a coding launch command and a one-shot analysis command. Report errors rather than silently skipping review.
- Ready and dispatch consider unresolved inline review threads actionable, regardless of author, bot status or an intervening push. General PR conversation comments and review bodies do not trigger dispatch. Retain formal changes-requested handling.
- Instruct the coding agent to address each inline thread: fix the valid request or reply with a concise explanation, then resolve the thread.
- If the same exact issue is selected 10 consecutive times, fail the autodispatch session before launching the tenth dispatch. Selecting a different issue resets the count.
- Keep existing autodispatch sequencing, stop behavior, errors and worktree handling. Do not add a reviewed-commit registry or review-specific worktree claims.

### Plan and acceptance

1. Update user-facing feature scenarios first.
2. Integrate Phase 1 review with the server-owned autodispatch loop and update the shared readiness classifier and agent instruction.
3. Add focused tests for sequencing, review errors, no PR, agent validation, thread selection and resolution instructions, and consecutive-selection reset and limit. Use fakes only.
4. Update documentation, run targeted and broader checks, and complete the independent devteam review gates.

## Phase 3: Stateful end-to-end test infrastructure

### Requirements

- Exercise actual autodispatch without real GitHub or real agents.
- Use stateful fake GitHub and agent behavior. Introduce reusable `FakeGithub` and `FakeAgent` classes where they make the test clearer; avoid a speculative test framework.
- The fake implementation agent creates a PR linked to the issue. The fake reviewer posts a major inline finding and writes the required completion document. Ready selects the issue again. The fake implementation agent pushes a fix and resolves the thread. The fake reviewer finds no major issues. Ready reports no work and autodispatch finishes.
- Assert the observable state transitions and final outcome, not just prompts or calls. Cover the user-visible workflow without dependence on network, credentials or real agent binaries.

### Plan and acceptance

1. Build the smallest reusable stateful fakes needed by the workflow.
2. Drive the real autodispatch orchestration through the existing public test boundary and assert each transition.
3. Run targeted tests, the full relevant test suite and lint; complete independent devteam quality, bug and test reviews.
