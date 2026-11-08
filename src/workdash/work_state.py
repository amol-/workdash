"""GitHub work state: which issues and pull requests need work now.

One evaluator classifies pull requests (review feedback, merge conflicts, CI on
the current head) and issues (leaf, blockers, implementing pull requests) from
GitHub metadata only, so every command deciding what to work on next agrees on
the answer. Live panes are not GitHub state, so callers filter them out.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from .github_client import _run_gh_command_with_retry, parse_github_datetime
from .models import WorkItem, WorkItemKind, WorkItemType, display_repo
from .todo import is_missing_repository_error, todo_target_from_body

# GitHub computes mergeability in the background after it is first asked for,
# so an UNKNOWN answer is asked once more after this delay.
_MERGEABILITY_RETRY_DELAY_SECONDS = 3.0
# Keeps one aliased pull request query well within GitHub's 10 second query timeout:
# a pull request with a hundred checks takes about half a second.
_PULL_REQUESTS_PER_QUERY = 10
_PUSH_PERMISSIONS = {"WRITE", "MAINTAIN", "ADMIN"}
_FAILED_RESULTS = {
    "FAILURE",
    "ERROR",
    "TIMED_OUT",
    "CANCELLED",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}
_PENDING_RESULTS = {"PENDING", "EXPECTED"}
_SUCCESSFUL_RESULTS = {"SUCCESS", "NEUTRAL", "SKIPPED"}
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"
# Nobody closes an issue with more than a handful of pull requests.
_ISSUE_FRAGMENT = (
    "fragment ReadyIssue on Issue { number title url createdAt updatedAt state body "
    "repository { nameWithOwner } subIssuesSummary { total } "
    "issueDependenciesSummary { blockedBy } "
    "closedByPullRequestsReferences(first: 10) "
    "{ nodes { number state repository { nameWithOwner } } } }"
)
_ACTOR = "author { __typename login }"
_CHECK_CONTEXTS = (
    f"{_PAGE_INFO} nodes {{ __typename "
    "... on CheckRun { name status conclusion startedAt "
    "checkSuite { app { slug } workflowRun { workflow { databaseId } } } } "
    "... on StatusContext { context state } }"
)
# Per pull request feedback lists stop at GitHub's page size of 100, keeping the
# newest entries: only feedback after the latest push can be actionable.
_PULL_REQUEST_FRAGMENT = (
    f"fragment ReadyPullRequest on PullRequest {{ number title url createdAt updatedAt state "
    f"{_ACTOR} repository {{ nameWithOwner }} headRefOid mergedAt maintainerCanModify "
    "headRef { refUpdateRule { viewerCanPush } } "
    "headRepository { viewerPermission } baseRepository { viewerPermission } "
    "baseRefName baseRef { target { oid } } mergeable reviewDecision "
    "closingIssuesReferences(first: 10) "
    "{ nodes { number title url createdAt updatedAt body repository { nameWithOwner } } } "
    "commits(last: 1) { nodes { commit { oid committedDate statusCheckRollup { "
    f"contexts(first: 100) {{ {_CHECK_CONTEXTS} }} }} }} }} }} "
    "timelineItems(last: 1, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) "
    "{ nodes { ... on HeadRefForcePushedEvent { createdAt afterCommit { oid } } } } "
    "latestOpinionatedReviews(first: 100) { nodes { state url submittedAt isMinimized "
    f"commit {{ oid }} {_ACTOR} }} }} "
    f"reviews(last: 100) {{ nodes {{ state body url submittedAt isMinimized {_ACTOR} }} }} "
    # A thread is judged by its latest human comment, which bots may have replied to.
    "reviewThreads(last: 100) { nodes { isResolved isOutdated "
    f"comments(last: 10) {{ nodes {{ url publishedAt isMinimized {_ACTOR} }} }} }} }} "
    f"comments(last: 100) {{ nodes {{ url createdAt isMinimized {_ACTOR} }} }} }}"
)


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
    blocked: bool
    open_pull_requests: tuple[tuple[str, int], ...]
    merged_pull_requests: tuple[tuple[str, int], ...]

    @property
    def ready(self) -> bool:
        return (
            self.is_open
            and not self.has_sub_issues
            and not self.blocked
            and not self.open_pull_requests
        )


@dataclass(frozen=True, slots=True)
class ReadyWork:
    """An issue or pull request ready to be worked on, with the issue it implements."""

    item: WorkItem
    issue: WorkItem | None
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

        return (self.item, *(self.pull_request.linked_issues if self.pull_request else ()))


def find_ready_work(
    *,
    repositories: Sequence[str] = (),
    issue: tuple[str, int] | None = None,
    github_username: str,
    todo_repository: str,
) -> list[ReadyWork]:
    """Return the ready pull requests and then the ready issues of a scope.

    A repository scope covers every open issue and pull request of the
    repositories, plus the open pull requests closing their issues; an issue
    scope covers the issues of :func:`resolve_issue_scope` and the open pull
    requests closing them. Each group is ordered oldest first.

    :param Sequence repositories: ``owner/repo`` or ``owner/*`` selectors.
    :param tuple issue: ``(repo, number)`` of the scope issue, instead of repositories.
    """

    pull_requests: list[tuple[str, int]] = []
    if issue is not None:
        issues = resolve_issue_scope(*issue, todo_repository=todo_repository)
    else:
        names: dict[str, str] = {}
        for selector in repositories:
            owner, _, name = selector.partition("/")
            for repo in _owner_repositories(owner) if name == "*" else [selector]:
                names.setdefault(repo.lower(), repo)
        issues = []
        for repo in names.values():
            try:
                issues += _repository_issues(repo, todo_repository)
                pull_requests += _open_pull_requests(repo)
            except RuntimeError as error:
                # The todo repository does not exist until the first todo is captured.
                if repo.lower() != todo_repository.lower() or not is_missing_repository_error(
                    str(error)
                ):
                    raise
    # A closed issue needs no more work, so the pull requests closing it are not its work.
    pull_requests += [
        pr for scope_issue in issues if scope_issue.is_open for pr in scope_issue.open_pull_requests
    ]
    unique_pull_requests = {
        (repo.lower(), number): (repo, number) for repo, number in pull_requests
    }
    scope = {(entry.item.repo.lower(), entry.item.number) for entry in issues}

    ready = []
    for state in evaluate_pull_requests(
        list(unique_pull_requests.values()),
        github_username=github_username,
        todo_repository=todo_repository,
    ):
        if state.ready:
            # Prefer an issue of the scope, then the pull request's own repository.
            linked = min(
                state.linked_issues,
                key=lambda linked: (
                    (linked.repo.lower(), linked.number) not in scope,
                    linked.repo != state.item.repo,
                    linked.number,
                ),
                default=None,
            )
            ready.append(ReadyWork(item=state.item, issue=linked, pull_request=state))
    ready += [ReadyWork(item=entry.item, issue=entry.item) for entry in issues if entry.ready]
    return sorted(
        ready,
        key=lambda work: (
            work.pull_request is None,
            work.item.created_at,
            display_repo(work.item),
            work.item.item_type.value,
            work.item.number,
        ),
    )


def resolve_issue_scope(repo: str, number: int, *, todo_repository: str) -> list[IssueWorkState]:
    """Return the issues an issue scope covers.

    That is every native sub-issue descendant, recursively and across
    repositories, closed ones included, or the issue itself when it never had
    sub-issues. An issue with sub-issues never covers itself.
    """

    def tree(repo: str, number: int) -> dict:
        owner, _, name = repo.partition("/")
        # GitHub allows at most 100 sub-issues per issue.
        data = _graphql(
            f"query ReadyIssueTree {{ repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ issue(number: {number}) {{ ...ReadyIssue "
            f"subIssues(first: 100) {{ nodes {{ ...ReadyIssue }} }} }} }} }} {_ISSUE_FRAGMENT}",
            f"{repo}#{number}",
        )
        return data["repository"]["issue"]

    with _github_payload(f"{repo}#{number}"):
        root = tree(repo, number)
        if root["subIssuesSummary"]["total"] == 0:
            return [_issue_state(root, todo_repository)]
        issues: list[IssueWorkState] = []
        parents = [root]
        while parents:
            for child in parents.pop()["subIssues"]["nodes"]:
                state = _issue_state(child, todo_repository)
                issues.append(state)
                if state.has_sub_issues:
                    parents.append(tree(state.item.repo, state.item.number))
        return issues


def evaluate_pull_requests(
    pull_requests: Sequence[tuple[str, int]], *, github_username: str, todo_repository: str
) -> list[PullRequestWorkState]:
    """Return the work state of each ``(repo, number)`` pull request, in the same order.

    :param str github_username: The user whose pull requests are their own work.
    :param str todo_repository: Repository whose issues are todos, so a linked
        issue's target is read from its body.
    """

    nodes = _fetch_pull_requests(pull_requests)
    # GitHub computes mergeability lazily, but reports UNKNOWN for a head it no longer
    # recomputes, so asking a merged or closed one again only costs a call.
    recomputing = [
        index
        for index, node in enumerate(nodes)
        if node["state"] == "OPEN" and node.get("mergeable") == "UNKNOWN"
    ]
    if recomputing:
        time.sleep(_MERGEABILITY_RETRY_DELAY_SECONDS)
        refreshed = _fetch_pull_requests([pull_requests[index] for index in recomputing])
        for index, node in zip(recomputing, refreshed, strict=True):
            nodes[index] = node
    required_checks: dict[tuple[str, str], frozenset[str] | None] = {}
    return [
        _pull_request_state(
            node, required_checks, github_username, todo_repository, subject=f"{repo}#{number}"
        )
        for (repo, number), node in zip(pull_requests, nodes, strict=True)
    ]


def work_pull_request(
    item: WorkItem, *, github_username: str, todo_repository: str
) -> PullRequestWorkState | None:
    """Return the pull request whose state carries an item's work, ``None`` when it has none.

    That is the item's own pull request, or the user's closing pull request of the
    issue: the open one when there is one, otherwise one that merged before it was
    looked for. A targeted todo has none: its checkout is the target repository's.
    """

    candidates: list[tuple[str, int]] = []
    if item.item_type == WorkItemType.PR:
        candidates = [(item.repo, item.number)]
    elif item.todo_target is None:
        for entry in resolve_issue_scope(item.repo, item.number, todo_repository=todo_repository):
            # GitHub names the repository canonically, which may differ in case from the target's.
            if (entry.item.repo.lower(), entry.item.number) == (item.repo.lower(), item.number):
                candidates = [*entry.open_pull_requests, *entry.merged_pull_requests]
    if not candidates:
        return None
    states = evaluate_pull_requests(
        candidates, github_username=github_username, todo_repository=todo_repository
    )
    if item.item_type == WorkItemType.PR:
        return states[0]
    # Only the user's own pull request carries on the issue; somebody else's is not this work.
    # The issue's open pull requests are listed before its merged ones, so the open one is first.
    authored = [state for state in states if state.item.kind is WorkItemKind.AUTHORED_PR]
    return authored[0] if authored else None


def pull_request_states(pull_requests: Sequence[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """Return each pull request's state: ``OPEN``, ``MERGED``, or ``CLOSED``.

    Repeated checks of pull requests that are already tracked only need their state,
    so this reads one metadata query instead of a whole work state evaluation.
    """

    states: dict[tuple[str, int], str] = {}
    for start in range(0, len(pull_requests), _PULL_REQUESTS_PER_QUERY):
        batch = pull_requests[start : start + _PULL_REQUESTS_PER_QUERY]
        subject = ", ".join(f"{repo}#{number}" for repo, number in batch)
        data = _graphql(
            f"query PullRequestStates {{ {_pull_request_aliases(batch, 'state')} }}", subject
        )
        with _github_payload(subject):
            for index, pull_request in enumerate(batch):
                states[pull_request] = data[f"p{index}"]["pullRequest"]["state"]
    return states


def _owner_repositories(owner: str) -> list[str]:
    nodes = _all_nodes(
        f"{owner}/*",
        lambda after: (
            f"query ReadyOwnerRepositories {{ repositoryOwner(login: {json.dumps(owner)}) "
            f"{{ repositories(first: 100, after: {after}, isArchived: false, "
            f"ownerAffiliations: OWNER) {{ {_PAGE_INFO} nodes {{ nameWithOwner "
            "issues(states: OPEN) { totalCount } pullRequests(states: OPEN) { totalCount } "
            "} } } }"
        ),
        ("repositoryOwner", "repositories"),
    )
    # Most repositories of an owner have no open work; skipping them saves two lookups each.
    with _github_payload(f"{owner}/*"):
        return [
            node["nameWithOwner"]
            for node in nodes
            if node["issues"]["totalCount"] or node["pullRequests"]["totalCount"]
        ]


def _repository_issues(repo: str, todo_repository: str) -> list[IssueWorkState]:
    owner, _, name = repo.partition("/")
    nodes = _all_nodes(
        repo,
        lambda after: (
            f"query ReadyRepositoryIssues {{ repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ issues(states: OPEN, first: 100, after: {after}) "
            f"{{ {_PAGE_INFO} nodes {{ ...ReadyIssue }} }} }} }} {_ISSUE_FRAGMENT}"
        ),
        ("repository", "issues"),
    )
    with _github_payload(repo):
        return [_issue_state(node, todo_repository) for node in nodes]


def _open_pull_requests(repo: str) -> list[tuple[str, int]]:
    owner, _, name = repo.partition("/")
    nodes = _all_nodes(
        repo,
        lambda after: (
            f"query ReadyRepositoryPullRequests {{ repository(owner: {json.dumps(owner)}, "
            f"name: {json.dumps(name)}) {{ pullRequests(states: OPEN, first: 100, after: {after}) "
            f"{{ {_PAGE_INFO} nodes {{ number }} }} }} }}"
        ),
        ("repository", "pullRequests"),
    )
    return [(repo, node["number"]) for node in nodes]


def _fetch_pull_requests(pull_requests: Sequence[tuple[str, int]]) -> list[dict]:
    nodes = []
    for start in range(0, len(pull_requests), _PULL_REQUESTS_PER_QUERY):
        batch = pull_requests[start : start + _PULL_REQUESTS_PER_QUERY]
        subject = ", ".join(f"{repo}#{number}" for repo, number in batch)
        data = _graphql(
            f"query ReadyPullRequests {{ {_pull_request_aliases(batch, '...ReadyPullRequest')} }} "
            f"{_PULL_REQUEST_FRAGMENT}",
            subject,
        )
        with _github_payload(subject):
            nodes += [data[f"p{index}"]["pullRequest"] for index in range(len(batch))]
    return nodes


def _pull_request_aliases(batch: Sequence[tuple[str, int]], fields: str) -> str:
    """Name each pull request of a batch by one alias, selecting ``fields`` of it."""

    return " ".join(
        f"p{index}: repository(owner: {json.dumps(repo.partition('/')[0])}, "
        f"name: {json.dumps(repo.partition('/')[2])}) "
        f"{{ pullRequest(number: {number}) {{ {fields} }} }}"
        for index, (repo, number) in enumerate(batch)
    )


def _issue_state(node: dict, todo_repository: str) -> IssueWorkState:
    return IssueWorkState(
        item=_issue_item(node, todo_repository),
        is_open=node["state"] == "OPEN",
        has_sub_issues=node["subIssuesSummary"]["total"] > 0,
        blocked=node["issueDependenciesSummary"]["blockedBy"] > 0,
        open_pull_requests=tuple(
            (pr["repository"]["nameWithOwner"], pr["number"])
            for pr in node["closedByPullRequestsReferences"]["nodes"]
            if pr["state"] == "OPEN"
        ),
        merged_pull_requests=tuple(
            (pr["repository"]["nameWithOwner"], pr["number"])
            for pr in node["closedByPullRequestsReferences"]["nodes"]
            if pr["state"] == "MERGED"
        ),
    )


def _issue_item(node: dict, todo_repository: str) -> WorkItem:
    repo = node["repository"]["nameWithOwner"]
    return WorkItem(
        kind=WorkItemKind.TRACKED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo=repo,
        number=node["number"],
        title=node["title"],
        created_at=parse_github_datetime(node["createdAt"]),
        updated_at=parse_github_datetime(node["updatedAt"]),
        url=node["url"],
        todo_target=todo_target_from_body(node["body"])
        if repo.lower() == todo_repository.lower()
        else None,
    )


def _pull_request_state(
    node: dict,
    required_checks: dict[tuple[str, str], frozenset[str] | None],
    github_username: str,
    todo_repository: str,
    *,
    subject: str,
) -> PullRequestWorkState:
    with _github_payload(subject):
        repo = node["repository"]["nameWithOwner"]
        author = (node["author"] or {}).get("login")
        linked_issues = tuple(
            _issue_item(issue, todo_repository)
            for issue in node["closingIssuesReferences"]["nodes"]
        )
        head_sha = node["headRefOid"]
        head_repository = node["headRepository"]
        commit = node["commits"]["nodes"][0]["commit"]
        base_key = (repo, node["baseRefName"])
        if base_key not in required_checks:
            required_checks[base_key] = _required_checks(*base_key)
        ci_status, failing_checks = _ci_status(repo, commit, head_sha, required_checks[base_key])
        pushes = [
            event["createdAt"]
            for event in node["timelineItems"]["nodes"]
            if (event.get("afterCommit") or {}).get("oid") == head_sha
        ]
        return PullRequestWorkState(
            item=WorkItem(
                kind=WorkItemKind.AUTHORED_PR
                if author and author.lower() == github_username.strip().lower()
                else WorkItemKind.TRACKED_PR,
                item_type=WorkItemType.PR,
                repo=repo,
                number=node["number"],
                title=node["title"],
                created_at=parse_github_datetime(node["createdAt"]),
                updated_at=parse_github_datetime(node["updatedAt"]),
                url=node["url"],
                # Only an issue of the pull request's own repository can name its worktree.
                closing_issue_numbers=tuple(
                    sorted(issue.number for issue in linked_issues if issue.repo == repo)
                ),
            ),
            state=node["state"],
            author=author,
            linked_issues=linked_issues,
            head_sha=head_sha,
            head_writable=head_repository is not None
            and node["headRef"] is not None
            # A protected head branch can refuse pushes even from a writer.
            and (node["headRef"]["refUpdateRule"] or {}).get("viewerCanPush", True)
            and (
                head_repository["viewerPermission"] in _PUSH_PERMISSIONS
                or (
                    node["maintainerCanModify"]
                    and node["baseRepository"]["viewerPermission"] in _PUSH_PERMISSIONS
                )
            ),
            base_ref=node["baseRefName"],
            base_head_sha=node["baseRef"]["target"]["oid"] if node["baseRef"] else None,
            mergeability={"MERGEABLE": "mergeable", "CONFLICTING": "conflicting"}.get(
                node["mergeable"], "unknown"
            ),
            review_decision=node["reviewDecision"],
            ci_status=ci_status,
            failing_checks=failing_checks,
            feedback=_actionable_feedback(
                node,
                head_sha,
                author,
                github_username,
                parse_github_datetime(pushes[-1] if pushes else commit["committedDate"]),
            ),
            merged_at=parse_github_datetime(node["mergedAt"]) if node["mergedAt"] else None,
        )


def _ci_status(
    repo: str, commit: dict, head_sha: str, required: frozenset[str] | None
) -> tuple[str, tuple[str, ...]]:
    """Classify the latest run of each check of the current head.

    Incomplete metadata fails closed. Checks beyond the first page are read by
    the head commit, so a push between pages cannot mix two heads.

    :param frozenset required: Checks the base branch requires, ``None`` when unreadable.
    """

    if commit["oid"] != head_sha:
        return "unknown", ()
    rollup = commit["statusCheckRollup"]
    contexts = rollup["contexts"]["nodes"] if rollup else []
    if rollup and rollup["contexts"]["pageInfo"]["hasNextPage"]:
        owner, _, name = repo.partition("/")
        contexts = _all_nodes(
            f"{repo}@{head_sha}",
            lambda after: (
                f"query ReadyCommitChecks {{ repository(owner: {json.dumps(owner)}, "
                f"name: {json.dumps(name)}) {{ object(oid: {json.dumps(head_sha)}) "
                "{ ... on Commit { statusCheckRollup { "
                f"contexts(first: 100, after: {after}) {{ {_CHECK_CONTEXTS} }} }} }} }} }} }}"
            ),
            ("repository", "object", "statusCheckRollup", "contexts"),
        )
    # Re-runs keep their earlier attempts on the head; a run not started yet is the latest.
    # Workflows and apps may share a check name, so each keeps its own latest attempt.
    results: dict[tuple[str | None, int | None, str], str] = {}
    for context in sorted(
        contexts, key=lambda context: (context.get("startedAt") is None, context.get("startedAt"))
    ):
        if context["__typename"] == "CheckRun":
            suite = context["checkSuite"]
            run = suite["workflowRun"]
            key = (
                suite["app"] and suite["app"]["slug"],
                run and run["workflow"]["databaseId"],
                context["name"],
            )
            results[key] = context["conclusion"] if context["status"] == "COMPLETED" else "PENDING"
        else:
            results[(None, None, context["context"])] = context["state"]
    failing = dict.fromkeys(key[-1] for key, result in results.items() if result in _FAILED_RESULTS)
    if failing:
        return "failing", tuple(failing)
    if any(result in _PENDING_RESULTS for result in results.values()):
        return "pending", ()
    if required is None:
        return "unknown", ()
    if not results and not required:
        return "no_checks", ()
    if required - {key[-1] for key in results} or any(
        result not in _SUCCESSFUL_RESULTS for result in results.values()
    ):
        return "unknown", ()
    return "passing", ()


def _required_checks(repo: str, branch: str) -> frozenset[str] | None:
    """Return the checks rulesets and branch protection require, ``None`` when unreadable."""

    def rest(path: str) -> object:
        completed = _run_gh_command_with_retry(
            ["gh", "api", path],
            not_found_message="Failed to run gh required checks lookup: "
            "gh CLI is not installed or not on PATH.",
            failure_message_prefix=f"Failed to read the required checks of {repo}:{branch} via gh",
        )
        return json.loads(completed.stdout)

    quoted_branch = urllib.parse.quote(branch, safe="")
    try:
        # A branch has far fewer than 100 rules.
        rules = rest(f"repos/{repo}/rules/branches/{quoted_branch}?per_page=100")
        required = {
            check["context"]
            for rule in rules
            if rule["type"] == "required_status_checks"
            for check in rule["parameters"]["required_status_checks"]
        }
        # The branch summary shows branch protection to every reader, unlike the
        # admin-only protection endpoints.
        branch_summary = rest(f"repos/{repo}/branches/{quoted_branch}")
        if branch_summary["protected"]:
            protection = branch_summary["protection"]["required_status_checks"]
            required |= set(protection["contexts"])
            required |= {check["context"] for check in protection["checks"]}
        return frozenset(required)
    except (RuntimeError, ValueError, KeyError, TypeError):
        # Checks that may be required are never mistaken for none being required.
        return None


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


def _all_nodes(
    subject: str, query: Callable[[str], str], connection_path: Sequence[str]
) -> list[dict]:
    """Collect every node of a paginated GraphQL connection.

    :param Callable query: Builds the query for an ``after`` cursor literal.
    """

    nodes: list[dict] = []
    after = "null"
    while True:
        connection = _graphql(query(after), subject)
        with _github_payload(subject):
            for key in connection_path:
                connection = connection[key]
            nodes += connection["nodes"]
            if not connection["pageInfo"]["hasNextPage"]:
                return nodes
            after = json.dumps(connection["pageInfo"]["endCursor"])


def _graphql(query: str, subject: str) -> dict:
    # gh exits non-zero on any GraphQL error, so a partial answer fails the lookup.
    completed = _run_gh_command_with_retry(
        ["gh", "api", "graphql", "-f", f"query={query}"],
        not_found_message="Failed to run gh work state lookup: "
        "gh CLI is not installed or not on PATH.",
        failure_message_prefix=f"Failed to look up {subject} via gh",
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to parse gh JSON for {subject}: {error.msg}") from error
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid gh payload for {subject}: missing data.")
    return data


@contextmanager
def _github_payload(subject: str) -> Iterator[None]:
    """Report a GitHub answer lacking an expected field as a failed lookup."""

    try:
        yield
    except (KeyError, TypeError, AttributeError, IndexError) as error:
        raise RuntimeError(
            f"Invalid gh payload for {subject}: missing or invalid {error}."
        ) from error
