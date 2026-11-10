from datetime import UTC, datetime

import pytest

from gh_work_state_fake import install_fake_github
from workdash.models import WorkItem, WorkItemKind, WorkItemType
from workdash.work_instruction import autostart_briefing, render_work_instruction
from workdash.work_state import PullRequestWorkState, ReviewFeedback

NOW = datetime(2026, 3, 1, tzinfo=UTC)


def _item(item_type: WorkItemType, number: int) -> WorkItem:
    return WorkItem(
        kind=WorkItemKind.AUTHORED_PR
        if item_type == WorkItemType.PR
        else WorkItemKind.ASSIGNED_ISSUE,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title="Work",
        created_at=NOW,
        updated_at=NOW,
        url=f"https://github.com/owner/repo/{'pull' if item_type == WorkItemType.PR else 'issues'}/{number}",
    )


def _state(
    *,
    state: str = "OPEN",
    mergeability: str = "mergeable",
    ci_status: str = "passing",
    failing_checks: tuple[str, ...] = (),
    feedback: tuple[ReviewFeedback, ...] = (),
    review_decision: str | None = None,
) -> PullRequestWorkState:
    return PullRequestWorkState(
        item=_item(WorkItemType.PR, 51),
        state=state,
        author="testuser",
        linked_issues=(),
        head_sha="c0ffee2",
        head_writable=True,
        base_ref="main",
        base_head_sha="base1",
        mergeability=mergeability,
        review_decision=review_decision,
        ci_status=ci_status,
        failing_checks=failing_checks,
        feedback=feedback,
    )


REVIEW = ReviewFeedback("review_thread", "reviewer", "https://github.com/review/1", NOW)


def test_implement_is_a_self_contained_kickoff_without_the_autostart_policy() -> None:
    text = render_work_instruction("implement", _item(WorkItemType.ISSUE, 7))

    assert "Continue the assigned issue https://github.com/owner/repo/issues/7." in text
    assert "Fixes reference" in text
    assert "no checks are required or observed" in text
    assert "coordination workflows" not in text


@pytest.mark.parametrize(
    ("state", "phrases"),
    [
        (
            _state(ci_status="failing", failing_checks=("tests", "lint {x}")),
            [
                "pull request https://github.com/owner/repo/pull/51 at head c0ffee2",
                "failing checks: tests, lint {x}.",
            ],
        ),
        (
            _state(mergeability="conflicting"),
            ["cannot merge cleanly into main (base head base1) from its head c0ffee2"],
        ),
        (
            _state(feedback=(REVIEW,)),
            ["actionable review feedback: review thread by reviewer (https://github.com/review/1)"],
        ),
    ],
)
def test_pull_request_actions_carry_the_state_data(state, phrases) -> None:
    text = render_work_instruction(state.action, state)

    for phrase in phrases:
        assert phrase in text
    assert "Also" not in text


def test_several_reasons_are_all_included_with_the_most_urgent_first() -> None:
    state = _state(
        mergeability="conflicting",
        ci_status="failing",
        failing_checks=("tests",),
        feedback=(REVIEW,),
    )

    text = render_work_instruction(state.action, state)

    assert state.action == "address_review"
    assert (
        text.index("has actionable review feedback")
        < text.index("Also resolve the confirmed merge conflicts into main (base head base1)")
        < text.index("Also investigate and fix these failing checks: tests.")
    )


def test_conflicts_with_failing_checks_put_the_conflicts_first() -> None:
    state = _state(mergeability="conflicting", ci_status="failing", failing_checks=("tests",))

    text = render_work_instruction(state.action, state)

    assert text.index("cannot merge cleanly") < text.index("Also investigate and fix")


@pytest.mark.parametrize(
    ("state", "status"),
    [
        (
            _state(ci_status="pending"),
            "still without a final CI result (its current head's checks are still running)",
        ),
        (
            _state(ci_status="unknown"),
            "still without a final CI result (GitHub reports an unknown or unavailable CI status",
        ),
        (
            _state(),
            "waiting for human review (its current head's checks pass)",
        ),
        (
            _state(ci_status="no_checks"),
            "waiting for human review (GitHub reports no required or observed checks",
        ),
        (
            _state(review_decision="APPROVED"),
            "approved and waiting for a human to merge it (its current head's checks pass)",
        ),
        (_state(state="MERGED", feedback=(REVIEW,)), "already merged. It is not"),
        (_state(state="CLOSED", ci_status="failing"), "closed. It is not"),
    ],
)
def test_a_pull_request_waiting_for_others_is_described_without_inventing_work(
    state, status
) -> None:
    text = render_work_instruction(state.action, state)

    assert f"is {status}" in text
    assert "It is not waiting for work from you" in text
    assert "create another pull request" in text
    # An absent or unavailable CI result is never described as passing.
    if state.ci_status in {"no_checks", "unknown"}:
        assert "checks pass" not in text


def test_the_merge_conflict_instruction_integrates_safely_and_never_merges() -> None:
    state = _state(mergeability="conflicting")

    text = " ".join(render_work_instruction(state.action, state).split())

    assert "Do not create another pull request, merge the pull request into the base" in text
    assert "force-push any branch other than this pull request's branch" in text
    assert "Follow repository restrictions and prefer --force-with-lease" in text
    assert "check the CI of the NEW head SHA" in text
    assert "conflict-free against the current base" in text


@pytest.mark.parametrize(
    "work",
    [
        _item(WorkItemType.ISSUE, 7),
        _state(ci_status="failing", failing_checks=("tests",)),
        _state(mergeability="conflicting"),
        _state(feedback=(REVIEW,)),
    ],
)
def test_the_instruction_does_not_assume_when_the_session_started(work) -> None:
    action = "implement" if isinstance(work, WorkItem) else work.action

    text = render_work_instruction(action, work).lower()

    for phrase in ("idle", "just started", "since you started"):
        assert phrase not in text


def test_an_action_that_disagrees_with_the_work_is_refused() -> None:
    with pytest.raises(ValueError, match="does not match"):
        render_work_instruction("fix_ci", _state(mergeability="conflicting"))
    with pytest.raises(ValueError, match="does not match"):
        render_work_instruction("fix_ci", _item(WorkItemType.ISSUE, 7))


def test_a_briefing_does_not_redirect_an_issue_to_a_merged_pull_request(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#42")
    # GitHub reports a merged head as UNKNOWN mergeable, which is not worth asking about again.
    github.add_pull_request(
        "owner/repo#51", closes=("owner/repo#42",), state="MERGED", mergeable="UNKNOWN"
    )

    item, text = autostart_briefing(
        _item(WorkItemType.ISSUE, 42),
        github_username="testuser",
        todo_repository="testuser/todos",
    )

    assert (item.item_type, item.number) == (WorkItemType.ISSUE, 42)
    assert "Continue the assigned issue https://github.com/owner/repo/issues/42." in text
    assert "--add-assignee" not in text
    assert github.pull_requests["owner/repo#51"]["lookups"] == 1


def test_implementation_instruction_proves_a_parent_goal_already_met_by_subissues():
    text = render_work_instruction("implement", _item(WorkItemType.ISSUE, 7))
    assert "verify" in text.lower()
    assert "sub-issues" in text
    assert "test" in text.lower()


@pytest.mark.parametrize("pr_state", ["OPEN", "MERGED"])
def test_issue_pull_request_lookup_ignores_descendant_failures(monkeypatch, pr_state):
    from gh_work_state_fake import failure
    from workdash.work_state import work_pull_request

    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#7")
    github.add_issue("owner/repo#8", parent="owner/repo#7")
    github.add_issue("owner/repo#9", parent="owner/repo#8")
    github.add_pull_request("owner/repo#51", closes=("owner/repo#7",), state=pr_state)
    original = github._ReadyIssueTree

    def tree(query):
        if "issue(number: 8)" in query:
            raise failure("descendant unavailable")
        return original(query)

    monkeypatch.setattr(github, "_ReadyIssueTree", tree)
    state = work_pull_request(
        _item(WorkItemType.ISSUE, 7), github_username="testuser", todo_repository="testuser/todos"
    )
    assert state is not None and state.item.number == 51


def test_issue_pull_request_lookup_reports_target_failure(monkeypatch):
    from gh_work_state_fake import failure
    from workdash.work_state import work_pull_request

    github = install_fake_github(monkeypatch)
    github.failures["ReadyIssueTree"] = failure("target unavailable")
    with pytest.raises(RuntimeError, match="owner/repo#7.*target unavailable"):
        work_pull_request(
            _item(WorkItemType.ISSUE, 7),
            github_username="testuser",
            todo_repository="testuser/todos",
        )
