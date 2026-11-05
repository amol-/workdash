"""What a coding agent is told to do for GitHub work, from its current GitHub state."""

from __future__ import annotations

from pathlib import Path

from .models import REVIEW_KINDS, WorkItem, WorkItemKind, WorkItemType
from .work_state import PullRequestWorkState, evaluate_pull_requests, resolve_issue_scope

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_ALSO = {
    "address_review": "Also address these current review requests: {feedback}.",
    "fix_merge_conflicts": (
        "Also resolve the confirmed merge conflicts into {base_ref} (base head {base_sha}) "
        "using the repository's established merge or rebase workflow and verify the "
        "pull request merges cleanly."
    ),
    "fix_ci": "Also investigate and fix these failing checks: {failing_checks}.",
}
# Pull requests needing nothing from the agent: what they wait for, and what the CI status
# of an open one means. CI is never failing there, which would be work to do.
_AWAITING = {
    "awaiting_ci": "still without a final CI result",
    "awaiting_review": "waiting for human review",
    "awaiting_merge": "approved and waiting for a human to merge it",
    "merged": "already merged",
    "closed": "closed",
}
_CI = {
    "passing": "its current head's checks pass",
    "pending": "its current head's checks are still running",
    "no_checks": "GitHub reports no required or observed checks for its current head",
    "unknown": "GitHub reports an unknown or unavailable CI status for its current head, "
    "which is neither passing nor proof that no checks exist",
}


def autostart_briefing(
    item: WorkItem, *, github_username: str, todo_repository: str
) -> tuple[WorkItem, str]:
    """Return the item an autostarted agent works on and the text closing its briefing.

    An issue that an open pull request already closes is worked on through that
    pull request, so its checkout is the one to use. Somebody else's pull request
    stays a review, and needs no GitHub lookup.

    :param WorkItem item: The explicit coding target.
    """

    if item.kind in REVIEW_KINDS:
        return item, _load("autostart_review.txt")

    # The pull requests this work is about: the target itself, or those closing the issue.
    # A targeted todo is never redirected: its checkout is the target repository's.
    candidates: list[tuple[str, int]] = []
    if item.item_type == WorkItemType.PR:
        candidates = [(item.repo, item.number)]
    elif item.todo_target is None:
        for entry in resolve_issue_scope(item.repo, item.number, todo_repository=todo_repository):
            # GitHub names the repository canonically, which may differ in case from the target's.
            if (entry.item.repo.lower(), entry.item.number) == (item.repo.lower(), item.number):
                candidates = list(entry.open_pull_requests)
    states = (
        evaluate_pull_requests(
            candidates, github_username=github_username, todo_repository=todo_repository
        )
        if candidates
        else []
    )
    if item.item_type == WorkItemType.ISSUE:
        # Only the user's own pull request carries on the issue; somebody else's is not this work.
        states = [state for state in states if state.item.kind == WorkItemKind.AUTHORED_PR][:1]
        if states:
            item = states[0].item

    appendix = _load("autostart_implement.txt")
    if not states:
        return item, f"{appendix}\n{render_work_instruction('implement', item)}"
    [state, *_] = states
    instruction = render_work_instruction(state.action, state)
    # Nothing to implement or repair, so the briefing carries no order to start working.
    return item, instruction if state.action in _AWAITING else f"{appendix}\n{instruction}"


def render_work_instruction(action: str, work: WorkItem | PullRequestWorkState) -> str:
    """Render the instruction for a work action, independent of how the agent got it.

    :param str action: ``implement`` for an issue ``WorkItem``, otherwise the action
        of a pull request's work state, which also lists its other reasons as
        ``Also`` clauses.
    :param work: The issue to implement, or the pull request state to act on.
    """

    if action != ("implement" if isinstance(work, WorkItem) else work.action):
        raise ValueError(f"Action {action!r} does not match the work it is rendered for.")
    if action == "implement":
        return _load("work_implement.txt").format(url=work.url).strip()
    fields = {
        "url": work.item.url,
        "head_sha": work.head_sha,
        "base_ref": work.base_ref,
        "base_sha": work.base_head_sha or "unknown",
        "ci_status": work.ci_status,
        "failing_checks": ", ".join(work.failing_checks),
        "feedback": ", ".join(
            f"{feedback.kind.replace('_', ' ')} by {feedback.author} ({feedback.url})"
            for feedback in work.feedback
        ),
    }
    if action in _AWAITING:
        fields["status"] = _AWAITING[action]
        if work.state == "OPEN":
            fields["status"] += f" ({_CI[work.ci_status]})"
    template = _load("work_awaiting.txt" if action in _AWAITING else f"work_{action}.txt")
    also = [] if action in _AWAITING else [_ALSO[reason] for reason in work.reasons[1:]]
    return " ".join([template.strip(), *also]).format(**fields)


def _load(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")
