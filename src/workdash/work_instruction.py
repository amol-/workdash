"""What a coding agent is told to do for GitHub work, from its current GitHub state."""

from __future__ import annotations

from pathlib import Path

from .models import REVIEW_KINDS, WorkItem, WorkItemType
from .work_state import PullRequestWorkState, work_pull_request

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

    state = work_pull_request(
        item, github_username=github_username, todo_repository=todo_repository
    )
    if item.item_type == WorkItemType.ISSUE:
        # Only an open pull request carries the issue's work on; a merged one has finished it.
        state = state if state is not None and state.state == "OPEN" else None
        if state is not None:
            item = state.item

    if state is None:
        return item, render_autonomous_work_instruction("implement", item, github_username)
    # Nothing to implement or repair, so the briefing carries no order to start working.
    if state.action in _AWAITING:
        return item, render_work_instruction(state.action, state)
    return item, render_autonomous_work_instruction(state.action, state, github_username)


def render_autonomous_work_instruction(
    action: str, work: WorkItem | PullRequestWorkState, github_username: str
) -> str:
    """Render actionable work with its autonomous publishing rules."""

    appendix = _load("autostart_implement.txt").format(github_username=github_username)
    return f"{appendix}\n{render_work_instruction(action, work)}"


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
