import json
import subprocess

import pytest

from gh_work_state_fake import (
    AFTER_PUSH,
    BEFORE_PUSH,
    HEAD_SHA,
    OLD_HEAD_SHA,
    PUSHED_AT,
    check_run,
    comment,
    failure,
    install_fake_github,
    review,
    status_context,
    thread,
)
from workdash.github_client import _PULL_REQUESTS_PER_QUERY, GitHubClient
from workdash.models import WorkItemKind

PR = "owner/repo#51"


def _evaluate(*refs: str):
    return GitHubClient().evaluate_pull_requests(
        [(ref.partition("#")[0], int(ref.partition("#")[2])) for ref in refs or (PR,)],
        github_username="TestUser",
        todo_repository="testuser/todos",
    )


@pytest.mark.parametrize(
    ("checks", "rulesets", "protection", "expected"),
    [
        ([], [], "unprotected", ("no_checks", ())),
        (
            [check_run("tests"), status_context("lint", "SUCCESS")],
            [],
            "unprotected",
            ("passing", ()),
        ),
        (
            [check_run("a", "NEUTRAL"), check_run("b", "SKIPPED")],
            [],
            "unprotected",
            ("passing", ()),
        ),
        ([check_run("build")], ["build"], ["build"], ("passing", ())),
        ([check_run("tests")], ["build"], "unprotected", ("unknown", ())),
        ([], [], ["build"], ("unknown", ())),
        ([check_run("build")], [], ["build", "deploy"], ("unknown", ())),
        ([check_run("build")], ["build"], None, ("passing", ())),
        ([], "unreadable", "unprotected", ("unknown", ())),
        ([], [], "unreadable", ("unknown", ())),
        (
            [check_run("a", "FAILURE"), check_run("b", None, status="QUEUED")],
            "unreadable",
            "unprotected",
            ("failing", ("a",)),
        ),
        (
            [check_run("slow", "TIMED_OUT"), status_context("ci/legacy", "ERROR")],
            [],
            "unprotected",
            ("failing", ("slow", "ci/legacy")),
        ),
        ([check_run("tests", None, status="IN_PROGRESS")], [], "unprotected", ("pending", ())),
        ([status_context("deploy", "EXPECTED")], [], "unprotected", ("pending", ())),
        ([check_run("tests", "STALE")], [], "unprotected", ("unknown", ())),
        (
            [check_run("tests"), check_run("tests", "CANCELLED", started=PUSHED_AT)],
            [],
            "unprotected",
            ("passing", ()),
        ),
        (
            [
                check_run("tests", "FAILURE", started=PUSHED_AT),
                check_run("tests", "FAILURE"),
                check_run("lint", "FAILURE"),
            ],
            [],
            "unprotected",
            ("failing", ("tests", "lint")),
        ),
        (
            [
                check_run("tests", None, status="QUEUED", started=None),
                check_run("tests", "FAILURE", started=PUSHED_AT),
            ],
            [],
            "unprotected",
            ("pending", ()),
        ),
        (
            [check_run("build", "FAILURE", workflow=1), check_run("build", workflow=2)],
            [],
            "unprotected",
            ("failing", ("build",)),
        ),
        (
            [
                check_run("lint", "FAILURE", app="circleci", workflow=None),
                check_run("lint", app="buildkite", workflow=None),
            ],
            [],
            "unprotected",
            ("failing", ("lint",)),
        ),
        (
            [
                check_run("build", "FAILURE", started=PUSHED_AT, workflow=2),
                check_run("build", workflow=2),
                check_run("build", workflow=3),
            ],
            ["build"],
            "unprotected",
            ("passing", ()),
        ),
    ],
)
def test_ci_status_of_the_current_head(monkeypatch, checks, rulesets, protection, expected) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, checks={HEAD_SHA: checks} if checks else {})
    github.ruleset_checks[("owner/repo", "main")] = rulesets
    if protection != "unprotected":
        github.protection_checks[("owner/repo", "main")] = protection

    [state] = _evaluate()

    assert (state.ci_status, state.failing_checks) == expected
    assert ("fix_ci" in state.reasons) == (expected[0] == "failing")


def test_ci_status_is_unknown_when_github_lists_an_older_commit_than_the_head(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(
        PR, latest_commit=OLD_HEAD_SHA, checks={OLD_HEAD_SHA: [check_run("tests", "FAILURE")]}
    )

    [state] = _evaluate()

    assert (state.ci_status, state.ready) == ("unknown", False)


def test_ci_status_reads_every_page_of_checks_of_the_current_head(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.page_size = 2
    checks = [check_run("a"), check_run("b"), check_run("c"), check_run("d", "FAILURE")]
    github.add_pull_request(PR, checks={HEAD_SHA: checks})

    [state] = _evaluate()

    assert (state.ci_status, state.failing_checks, state.ready) == ("failing", ("d",), True)


def test_required_checks_are_read_once_per_base_branch(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request("owner/repo#51")
    github.add_pull_request("owner/repo#52")
    github.add_pull_request("owner/repo#53", base_ref="release/1.x")

    states = _evaluate("owner/repo#51", "owner/repo#52", "owner/repo#53")

    assert [state.ci_status for state in states] == ["passing"] * 3
    assert [path for path in github.operations if "/rules/" in path] == [
        "repos/owner/repo/rules/branches/main?per_page=100",
        "repos/owner/repo/rules/branches/release%2F1.x?per_page=100",
    ]


@pytest.mark.parametrize(
    ("answers", "mergeability", "lookups"),
    [
        (["UNKNOWN", "CONFLICTING"], "conflicting", 2),
        (["UNKNOWN", "UNKNOWN"], "unknown", 2),
        (["MERGEABLE"], "mergeable", 1),
    ],
)
def test_mergeability_is_asked_again_once_while_github_computes_it(
    monkeypatch, answers, mergeability, lookups
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, mergeable=answers)

    [state] = _evaluate()

    assert state.mergeability == mergeability
    assert ("fix_merge_conflicts" in state.reasons) == (mergeability == "conflicting")
    assert github.pull_requests[PR]["lookups"] == lookups


def test_unknown_mergeability_does_not_hide_failed_ci(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(
        PR, mergeable=["UNKNOWN", "UNKNOWN"], checks={HEAD_SHA: [check_run("tests", "FAILURE")]}
    )

    [state] = _evaluate()

    assert (state.ready, state.reasons, state.mergeability) == (True, ("fix_ci",), "unknown")


def test_pull_requests_beyond_one_query_are_evaluated_like_the_first(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    refs = [f"owner/repo#{number}" for number in range(1, _PULL_REQUESTS_PER_QUERY + 2)]
    for ref in refs[:-1]:
        github.add_pull_request(ref)
    github.add_pull_request(
        refs[-1],
        mergeable=["UNKNOWN", "CONFLICTING"],
        checks={HEAD_SHA: [check_run("tests", "FAILURE")]},
    )

    *first, last = _evaluate(*refs)

    assert [state.action for state in first] == ["awaiting_review"] * len(first)
    assert (last.item.number, last.reasons) == (len(refs), ("fix_merge_conflicts", "fix_ci"))
    assert github.operations.count("ReadyPullRequests") == 3


@pytest.mark.parametrize(
    ("fields", "kinds"),
    [
        ({"comments": [comment("reviewer", created=AFTER_PUSH)]}, []),
        (
            {
                "author": "other",
                "reviews": [review("other", "CHANGES_REQUESTED", created=AFTER_PUSH)],
            },
            ["changes_requested"],
        ),
        (
            {
                "reviews": [
                    review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH),
                    review("reviewer", "APPROVED", created="2026-03-03T00:00:00Z"),
                ]
            },
            [],
        ),
        ({"reviews": [review("reviewer", "COMMENTED", created=AFTER_PUSH, body="Feedback")]}, []),
        (
            {"reviews": [review("reviewer", "CHANGES_REQUESTED", created=BEFORE_PUSH)]},
            ["changes_requested"],
        ),
        (
            {
                "reviews": [
                    review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH, commit=OLD_HEAD_SHA)
                ]
            },
            ["changes_requested"],
        ),
        (
            {
                "reviews": [review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH)],
                "review_requests": ["REVIEWER"],
            },
            [],
        ),
        (
            {
                "reviews": [review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH)],
                "review_requests": ["another"],
            },
            ["changes_requested"],
        ),
        (
            {
                "reviews": [review("reviewer", "CHANGES_REQUESTED", created=AFTER_PUSH)],
                "review_requests": ["reviewer"],
                "threads": [thread(comment("reviewer", created=AFTER_PUSH))],
            },
            ["review_thread"],
        ),
        ({"threads": [thread(comment("reviewer", created=AFTER_PUSH), resolved=True)]}, []),
        (
            {
                "threads": [
                    thread(
                        comment("reviewer", created=BEFORE_PUSH),
                        comment("testuser", created=AFTER_PUSH),
                    )
                ]
            },
            ["review_thread"],
        ),
        (
            {"threads": [thread(comment("renovate[bot]", created=BEFORE_PUSH), outdated=True)]},
            ["review_thread"],
        ),
    ],
)
def test_actionable_feedback(monkeypatch, fields, kinds) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, **fields)
    [state] = _evaluate()
    assert [feedback.kind for feedback in state.feedback] == kinds
    assert ("address_review" in state.reasons) == bool(kinds)


@pytest.mark.parametrize(
    ("fields", "writable"),
    [
        ({}, True),
        ({"head_permission": "READ"}, False),
        ({"head_permission": "READ", "maintainer_can_modify": True}, True),
        (
            {"head_permission": "READ", "maintainer_can_modify": True, "base_permission": "READ"},
            False,
        ),
        ({"head_permission": None}, False),
        ({"head_ref": None}, False),
        ({"head_ref": {"refUpdateRule": {"viewerCanPush": False}}}, False),
        ({"head_ref": {"refUpdateRule": {"viewerCanPush": True}}}, True),
    ],
)
def test_only_a_pull_request_the_server_can_push_to_is_ready(monkeypatch, fields, writable) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, checks={HEAD_SHA: [check_run("tests", "FAILURE")]}, **fields)

    [state] = _evaluate()

    assert (state.head_writable, state.ready) == (writable, writable)


@pytest.mark.parametrize(
    ("fields", "required", "action"),
    [
        ({}, [], "awaiting_review"),
        ({"review_decision": "APPROVED"}, [], "awaiting_merge"),
        ({"checks": {HEAD_SHA: [check_run("tests", None, status="QUEUED")]}}, [], "awaiting_ci"),
        ({}, ["build"], "awaiting_ci"),
        ({"state": "MERGED"}, [], "merged"),
        ({"state": "CLOSED", "checks": {HEAD_SHA: [check_run("t", "FAILURE")]}}, [], "closed"),
    ],
)
def test_a_pull_request_without_work_reports_what_it_waits_for(
    monkeypatch, fields, required, action
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, **fields)
    github.ruleset_checks[("owner/repo", "main")] = required

    [state] = _evaluate()

    assert (state.action, state.ready) == (action, False)


def test_pull_request_identity_matches_the_dashboard_item(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#42")
    github.add_issue("other/lib#7")
    github.add_pull_request(PR, closes=("other/lib#7", "owner/repo#42"))
    github.add_pull_request("owner/repo#52", author="someone-else")

    mine, theirs = _evaluate(PR, "owner/repo#52")

    assert (mine.item.kind, mine.item.closing_issue_numbers) == (WorkItemKind.AUTHORED_PR, (42,))
    assert [(issue.repo, issue.number) for issue in mine.linked_issues] == [
        ("other/lib", 7),
        ("owner/repo", 42),
    ]
    assert (mine.head_sha, mine.base_ref, mine.base_head_sha) == (HEAD_SHA, "main", "base1")
    assert theirs.item.kind == WorkItemKind.TRACKED_PR


def test_repository_scope_reads_every_page(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.page_size = 2
    github.owners["team"] = ["team/a", "team/b", "team/c"]
    for repo in github.owners["team"]:
        for number in (1, 2, 3):
            github.add_issue(f"{repo}#{number}")
    for number in (10, 11, 12):
        github.add_pull_request(
            f"team/b#{number}", checks={HEAD_SHA: [check_run("tests", "FAILURE")]}
        )

    ready = GitHubClient().find_ready_work(
        repositories=["team/*", "team/a"],
        github_username="testuser",
        todo_repository="testuser/todos",
    )

    assert [(work.item.repo, work.item.number) for work in ready] == [
        *((repo, number) for repo in ("team/a", "team/b", "team/c") for number in (1, 2, 3)),
    ]


def test_review_feedback_changes_readiness_for_the_same_pull_request(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    pr = github.add_pull_request(PR, closes=("owner/repo#1",))
    client = GitHubClient()

    def ready():
        return client.find_ready_work(
            repositories=["owner/repo"],
            github_username="testuser",
            todo_repository="testuser/todos",
        )

    assert ready() == []
    pr["threads"].append(thread(comment("reviewer", created=AFTER_PUSH)))
    [work] = ready()
    assert work.item.number == 1
    assert "address_review" in work.reasons
    pr["threads"][0]["isResolved"] = True
    assert ready() == []


def test_actionable_issues_are_ordered_by_pull_request_age(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1", created="2026-01-01T00:00:00Z")
    github.add_issue("owner/repo#2", created="2026-01-02T00:00:00Z")
    github.add_pull_request(
        "owner/repo#51",
        closes=("owner/repo#1",),
        created="2026-02-02T00:00:00Z",
        mergeable="CONFLICTING",
    )
    github.add_pull_request(
        "owner/repo#52",
        closes=("owner/repo#2",),
        created="2026-02-01T00:00:00Z",
        mergeable="CONFLICTING",
    )

    ready = GitHubClient().find_ready_work(
        repositories=["owner/repo"], github_username="testuser", todo_repository="testuser/todos"
    )

    assert [work.item.number for work in ready] == [2, 1]


def test_items_created_together_are_ordered_by_number(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#2")
    github.add_issue("owner/repo#1")

    ready = GitHubClient().find_ready_work(
        repositories=["owner/repo"], github_username="testuser", todo_repository="testuser/todos"
    )

    assert [work.item.number for work in ready] == [1, 2]


def test_owner_repositories_without_open_work_are_never_queried(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.owners["team"] = ["team/a", "team/empty"]
    github.add_issue("team/a#1")
    github.add_issue("team/empty#1", state="CLOSED")

    ready = GitHubClient().find_ready_work(
        repositories=["team/*"], github_username="testuser", todo_repository="testuser/todos"
    )

    assert [(work.item.repo, work.item.number) for work in ready] == [("team/a", 1)]
    assert github.operations == [
        "ReadyOwnerRepositories",
        "ReadyRepositoryIssues",
        "ReadyRepositoryPullRequests",
    ]


def test_only_a_missing_todo_repository_counts_as_empty(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.failures["ReadyRepositoryIssues"] = failure(
        "gh: Could not resolve to a Repository with the name 'testuser/todos'."
    )

    def ready(repo: str):
        return GitHubClient().find_ready_work(
            repositories=[repo], github_username="testuser", todo_repository="testuser/todos"
        )

    assert ready("testuser/todos") == []
    with pytest.raises(RuntimeError, match="Could not resolve to a Repository"):
        ready("owner/missing")


def test_issue_scope_follows_sub_issues_across_repositories(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    failed_ci = {HEAD_SHA: [check_run("tests", "FAILURE")]}
    github.add_issue("owner/repo#1", state="CLOSED")
    github.add_issue("owner/repo#2", parent="owner/repo#1", state="CLOSED")
    github.add_issue("other/lib#3", parent="owner/repo#2")
    github.add_issue("other/lib#4", parent="other/lib#3")
    github.add_issue("owner/repo#5", parent="owner/repo#1")
    github.add_issue("owner/repo#6", parent="owner/repo#1", state="CLOSED")
    # Outside the tree, and preferred over #4 if the scope did not come first.
    github.add_issue("other/lib#1")
    github.add_pull_request("other/lib#9", closes=("other/lib#1", "other/lib#4"), checks=failed_ci)
    github.add_pull_request("owner/repo#10", closes=("owner/repo#6",), checks=failed_ci)

    ready = GitHubClient().find_ready_work(
        issue=("owner/repo", 1), github_username="testuser", todo_repository="testuser/todos"
    )

    assert [(work.item.repo, work.item.number, work.action) for work in ready] == [
        ("other/lib", 4, "fix_ci"),
        ("owner/repo", 5, "implement"),
    ]


def test_a_ready_pull_request_is_listed_once_for_eligible_issues_in_scope(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#100")
    github.add_issue("other/lib#1", parent="owner/repo#100")
    github.add_issue("owner/repo#42", parent="owner/repo#100")
    github.add_pull_request(
        PR, closes=("other/lib#1", "owner/repo#42"), checks={HEAD_SHA: [check_run("t", "FAILURE")]}
    )

    works = GitHubClient().find_ready_work(
        repositories=["owner/repo", "other/lib"],
        github_username="testuser",
        todo_repository="testuser/todos",
    )

    assert [
        (work.item.repo, work.item.number, work.pull_request.item.number) for work in works
    ] == [("other/lib", 1, 51)]
    repository = GitHubClient().find_ready_work(
        repositories=["owner/repo"], github_username="testuser", todo_repository="testuser/todos"
    )
    assert [(work.item.repo, work.item.number) for work in repository] == [("other/lib", 1)]

    scoped = GitHubClient().find_ready_work(
        issue=("owner/repo", 100), github_username="testuser", todo_repository="testuser/todos"
    )
    assert [(work.item.repo, work.item.number) for work in scoped] == [("other/lib", 1)]
    leaf = GitHubClient().find_ready_work(
        issue=("owner/repo", 42), github_username="testuser", todo_repository="testuser/todos"
    )
    assert [(work.item.repo, work.item.number) for work in leaf] == [("owner/repo", 42)]


def test_repository_scope_fetches_nested_subtrees_once(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#3", parent="other/lib#2")
    github.add_issue("other/lib#2", parent="owner/repo#1", state="CLOSED")
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#4", parent="owner/repo#3")

    ready = GitHubClient().find_ready_work(
        repositories=["owner/repo"], github_username="testuser", todo_repository="testuser/todos"
    )

    assert [work.item.number for work in ready] == [4]
    assert github.operations.count("ReadyIssueTree") == 3


def test_issue_scope_of_a_parent_with_only_closed_sub_issues_is_ready(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2", parent="owner/repo#1", state="CLOSED")

    ready = GitHubClient().find_ready_work(
        issue=("owner/repo", 1), github_username="testuser", todo_repository="testuser/todos"
    )

    assert [work.item.number for work in ready] == [1]


def test_a_partial_graphql_answer_fails_the_lookup(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_pull_request(PR, closes=("owner/repo#1",))
    message = "Resource not accessible by integration"
    github.failures["ReadyPullRequests"] = failure(
        f"GraphQL: {message} (p0.pullRequest)",
        json.dumps({"data": {"p0": None}, "errors": [{"message": message, "path": ["p0"]}]}),
    )

    with pytest.raises(RuntimeError, match=message):
        GitHubClient().find_ready_work(
            repositories=["owner/repo"],
            github_username="testuser",
            todo_repository="testuser/todos",
        )


def test_a_malformed_github_answer_fails_the_lookup(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, checks={HEAD_SHA: [{"__typename": "CheckRun", "name": "tests"}]})

    with pytest.raises(RuntimeError, match=f"Invalid gh payload for {PR}"):
        _evaluate()


def test_an_answer_github_cut_off_at_its_timeout_is_retried(monkeypatch) -> None:
    github = install_fake_github(monkeypatch)
    github.add_pull_request(PR, checks={HEAD_SHA: [check_run("tests", "FAILURE")]})
    # GitHub sends the headers, then drops the body when the query runs out of time.
    github.failures["ReadyPullRequests"] = failure("unexpected end of JSON input")
    answered = github.run

    def cut_off_once(command, *args, **kwargs):
        try:
            return answered(command, *args, **kwargs)
        finally:
            github.failures.clear()

    monkeypatch.setattr(subprocess, "run", cut_off_once)
    [state] = _evaluate()
    assert (state.ci_status, state.ready) == ("failing", True)

    github.failures["ReadyPullRequests"] = failure("unexpected end of JSON input")
    monkeypatch.setattr(subprocess, "run", answered)
    with pytest.raises(RuntimeError, match=f"Failed to look up {PR} via gh: unexpected end"):
        _evaluate()


def test_ready_returns_issues_with_actionable_authored_prs_and_completed_parents(
    monkeypatch,
) -> None:
    github = install_fake_github(monkeypatch)
    github.add_issue("owner/repo#1")
    github.add_issue("owner/repo#2", parent="owner/repo#1", state="CLOSED")
    github.add_issue("other/lib#3", parent="owner/repo#2")
    github.add_issue("owner/repo#4")
    github.add_issue("owner/repo#5")
    github.add_issue("owner/repo#6")
    github.add_pull_request(
        PR, closes=("owner/repo#4",), checks={HEAD_SHA: [check_run("t", "FAILURE")]}
    )
    github.add_pull_request(
        "owner/repo#52",
        closes=("owner/repo#5",),
        author="someone-else",
        checks={HEAD_SHA: [check_run("t", "FAILURE")]},
    )
    github.add_pull_request("owner/repo#53", checks={HEAD_SHA: [check_run("t", "FAILURE")]})

    def ready():
        return GitHubClient().find_ready_work(
            repositories=["owner/repo"],
            github_username="testuser",
            todo_repository="testuser/todos",
        )

    result = ready()
    assert [(w.item.number, w.action) for w in result] == [(4, "fix_ci"), (6, "implement")]
    assert result[0].pull_request.item.number == 51
    github.issues["other/lib#3"]["state"] = "CLOSED"
    assert [(w.item.number, w.action) for w in ready()] == [
        (4, "fix_ci"),
        (1, "implement"),
        (6, "implement"),
    ]


def test_client_owns_work_state_github_access(monkeypatch):
    from gh_work_state_fake import FakeGitHub
    from workdash.github_client import GitHubClient

    fake = FakeGitHub(subprocess.run)
    fake.page_size = 1
    fake.add_issue("owner/repo#42")
    fake.add_pull_request(
        PR,
        closes=("owner/repo#42",),
        mergeable="CONFLICTING",
        checks={HEAD_SHA: [check_run("build"), check_run("test", "FAILURE")]},
    )
    calls = []

    class Client(GitHubClient):
        def _graphql(self, query, subject):
            calls.append(subject)
            return json.loads(fake.run(["gh", "api", "graphql", "-f", f"query={query}"]).stdout)[
                "data"
            ]

        def _required_checks(self, repo, branch):
            calls.append((repo, branch))
            return frozenset()

    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected gh"))
    client = Client()
    [work] = client.find_ready_work(
        issue=("owner/repo", 42), github_username="testuser", todo_repository="testuser/todos"
    )
    assert work.reasons == ("fix_merge_conflicts", "fix_ci")
    assert work.pull_request.failing_checks == ("test",)
    assert f"owner/repo@{HEAD_SHA}" in calls
    assert (
        client.work_pull_request(
            work.item, github_username="testuser", todo_repository="testuser/todos"
        ).item.number
        == 51
    )
    assert client.pull_request_states([("owner/repo", 51)]) == {("owner/repo", 51): "OPEN"}
    assert ("owner/repo", "main") in calls
