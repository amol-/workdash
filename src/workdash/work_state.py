"""GitHub work readiness and pull request classification."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import WorkItem, display_repo


@dataclass(frozen=True, slots=True)
class ReviewFeedback:
    """A review, review thread, or comment a pull request still has to address."""

    kind: str
    author: str
    url: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PullRequestWorkState:
    """What a pull request's current head needs, according to GitHub."""

    item: WorkItem
    state: str
    author: str | None
    linked_issues: tuple[WorkItem, ...]
    head_sha: str
    head_writable: bool
    base_ref: str
    base_head_sha: str | None
    mergeability: str
    review_decision: str | None
    ci_status: str
    failing_checks: tuple[str, ...]
    feedback: tuple[ReviewFeedback, ...]
    merged_at: datetime | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        """The work the current head needs, most urgent first."""

        return tuple(
            reason
            for reason, applies in (
                ("address_review", bool(self.feedback)),
                ("fix_merge_conflicts", self.mergeability == "conflicting"),
                ("fix_ci", self.ci_status == "failing"),
            )
            if applies
        )

    @property
    def action(self) -> str:
        """The next step: the most urgent reason, or what the pull request waits for."""

        if self.state != "OPEN":
            return self.state.lower()
        if self.reasons:
            return self.reasons[0]
        if self.ci_status in {"pending", "unknown"}:
            return "awaiting_ci"
        return "awaiting_merge" if self.review_decision == "APPROVED" else "awaiting_review"

    @property
    def ready(self) -> bool:
        return self.state == "OPEN" and self.head_writable and bool(self.reasons)


@dataclass(frozen=True, slots=True)
class IssueWorkState:
    """Whether an issue can be implemented now, according to GitHub."""

    item: WorkItem
    is_open: bool
    has_sub_issues: bool
    has_open_descendants: bool
    blocked: bool
    open_pull_requests: tuple[tuple[str, int], ...]
    merged_pull_requests: tuple[tuple[str, int], ...]

    @property
    def ready(self) -> bool:
        return (
            self.is_open
            and not self.has_open_descendants
            and not self.blocked
            and not self.open_pull_requests
        )


@dataclass(frozen=True, slots=True)
class ReadyWork:
    """An issue ready to be worked on, with its actionable pull request if any."""

    item: WorkItem
    pull_request: PullRequestWorkState | None = None

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.pull_request.reasons if self.pull_request else ("implement",)

    @property
    def action(self) -> str:
        return self.reasons[0]

    @property
    def claims(self) -> tuple[WorkItem, ...]:
        """The work items whose worktrees hold this work when an agent runs there."""

        if self.pull_request:
            return (self.item, self.pull_request.item, *self.pull_request.linked_issues)
        return (self.item,)


def select_ready_work(
    issues: list[IssueWorkState],
    states: dict[tuple[str, int], PullRequestWorkState],
    github_username: str,
    include_shared_pr_issues: bool,
) -> list[ReadyWork]:
    """Select and order actionable work from the resolved GitHub state."""

    ready = []
    for entry in issues:
        if not entry.is_open or entry.has_open_descendants or entry.blocked:
            continue
        linked = [states[(repo.lower(), number)] for repo, number in entry.open_pull_requests]
        if not linked:
            ready.append(ReadyWork(item=entry.item))
        elif all(
            state.author and state.author.lower() == github_username.lower() for state in linked
        ):
            actionable = [state for state in linked if state.ready]
            if actionable:
                state = min(
                    actionable,
                    key=lambda state: (
                        state.item.created_at,
                        state.item.repo.lower(),
                        state.item.number,
                    ),
                )
                ready.append(ReadyWork(item=entry.item, pull_request=state))
    ready = sorted(
        ready,
        key=lambda work: (
            work.pull_request is None,
            (work.pull_request.item if work.pull_request else work.item).created_at,
            display_repo(work.pull_request.item if work.pull_request else work.item).lower(),
            (work.pull_request.item if work.pull_request else work.item).number,
            work.item.repo.lower(),
            work.item.number,
        ),
    )
    if include_shared_pr_issues:
        return ready
    seen_pull_requests = set()
    unique = []
    for work in ready:
        if work.pull_request:
            key = (work.pull_request.item.repo.lower(), work.pull_request.item.number)
            if key in seen_pull_requests:
                continue
            seen_pull_requests.add(key)
        unique.append(work)
    return unique


def parse_github_datetime(timestamp: str) -> datetime:
    """Parse a GitHub ISO-8601 timestamp into a timezone-aware datetime."""

    try:
        parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as error:
        raise RuntimeError(f"Invalid GitHub timestamp {timestamp!r}: {error}") from error
    if parsed_timestamp.tzinfo is None:
        raise RuntimeError(f"Invalid GitHub timestamp {timestamp!r}: timezone offset is required.")
    return parsed_timestamp


def _actionable_feedback(
    node: dict, head_sha: str, author: str | None, github_username: str, pushed_at: datetime
) -> tuple[ReviewFeedback, ...]:
    """Collect the human feedback the current head has not addressed yet.

    That is a changes-requested verdict on the current head, and review bodies,
    unresolved current threads, and comments posted after the head was pushed,
    from anyone but bots, the pull request author, and the server's GitHub
    identity. Minimized entries, review bodies and comments their author
    approved the current head after, comments the author or the server identity
    replied to later, and threads whose latest human comment is theirs are left out.

    :param str github_username: The server's GitHub identity, answering feedback
        like the pull request author.
    """

    feedback: dict[str, ReviewFeedback] = {}
    own = {login.lower() for login in (author, github_username.strip()) if login}

    def is_bot(entry: dict) -> bool:
        actor = entry["author"] or {}
        return actor.get("__typename") == "Bot" or (actor.get("login") or "").endswith("[bot]")

    def add(kind: str, entry: dict, posted_key: str, *, after: datetime | None = pushed_at) -> None:
        login = (entry["author"] or {}).get("login") or ""
        if (
            not login
            or not entry[posted_key]  # A draft nobody else can see yet.
            or entry["isMinimized"]
            or is_bot(entry)
            or login.lower() in own
        ):
            return
        created_at = parse_github_datetime(entry[posted_key])
        if after is None or created_at > after:
            feedback.setdefault(entry["url"], ReviewFeedback(kind, login, entry["url"], created_at))

    approved_at: dict[str, datetime] = {}
    for review in node["latestOpinionatedReviews"]["nodes"]:
        if (review["commit"] or {}).get("oid") != head_sha:
            continue
        if review["state"] == "CHANGES_REQUESTED":
            add("changes_requested", review, "submittedAt", after=None)
        elif review["state"] == "APPROVED" and review["author"]:
            approved_at[review["author"]["login"].lower()] = parse_github_datetime(
                review["submittedAt"]
            )

    def approval_after_push(entry: dict) -> datetime:
        # Approving the current head settles the reviewer's earlier review bodies and comments.
        login = ((entry["author"] or {}).get("login") or "").lower()
        return max(pushed_at, approved_at.get(login, pushed_at))

    for review in node["reviews"]["nodes"]:
        if review["state"] in {"COMMENTED", "CHANGES_REQUESTED"} and review["body"].strip():
            add("review", review, "submittedAt", after=approval_after_push(review))
    for thread in node["reviewThreads"]["nodes"]:
        latest = next(
            (
                comment
                for comment in reversed(thread["comments"]["nodes"])
                if comment["publishedAt"] and not is_bot(comment)
            ),
            None,
        )
        if not thread["isResolved"] and not thread["isOutdated"] and latest:
            add("review_thread", latest, "publishedAt")
    # The latest own reply answers the conversation comments before it.
    replied_at = max(
        (
            parse_github_datetime(comment["createdAt"])
            for comment in node["comments"]["nodes"]
            if ((comment["author"] or {}).get("login") or "").lower() in own
        ),
        default=pushed_at,
    )
    for comment in node["comments"]["nodes"]:
        add("comment", comment, "createdAt", after=max(replied_at, approval_after_push(comment)))
    return tuple(feedback.values())
