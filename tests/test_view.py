import json
import subprocess
from unittest.mock import MagicMock

import pytest

from view_fake import fake_gh_view_run, issue, issue_routes
from workdash import view


def _capture(monkeypatch) -> list[str]:
    documents: list[str] = []
    monkeypatch.setattr(view, "show_markdown", documents.append)
    return documents


def test_view_pull_request_document_reports_ci_reviews_and_threads(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(pull_request={"url": "pulls/12"})
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": True,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "unit tests",
                "conclusion": "FAILURE",
                "detailsUrl": "https://ci/unit",
            },
            {
                "__typename": "CheckRun",
                "name": "lint",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://ci/lint",
            },
            {
                "__typename": "StatusContext",
                "context": "docs",
                "state": "PENDING",
                "targetUrl": "https://ci/docs",
            },
        ],
        "reviews": [{"author": {"login": "reviewer"}, "state": "APPROVED", "body": "Looks good"}],
    }
    hunk = "\n".join(f" line {number}" for number in range(20))
    routes["api graphql"] = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "isResolved": True,
                                "isOutdated": False,
                                "path": "src/app.py",
                                "line": 42,
                                "comments": {
                                    "nodes": [
                                        {
                                            "author": {"login": "reviewer"},
                                            "body": "Rename this",
                                            "diffHunk": hunk,
                                        }
                                    ]
                                },
                            },
                            {
                                "isResolved": False,
                                "isOutdated": True,
                                "path": "src/cli.py",
                                "line": None,
                                "comments": {"nodes": []},
                            },
                        ]
                    }
                }
            }
        }
    }
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))

    assert view.run_view("owner/repo#12") == 0

    document = documents[0]
    assert "**Draft:** yes" in document
    assert "**Mergeable:** MERGEABLE" in document
    assert "**Merge state:** CLEAN" in document
    assert "**Review:** APPROVED" in document
    assert "## CI — failure" in document
    assert "1 of 3 checks passing." in document
    assert "- [unit tests](https://ci/unit) — FAILURE" in document
    assert "lint" not in document and "docs" not in document
    assert "## Reviews (1)" in document
    assert "### @reviewer — APPROVED" in document
    assert "Looks good" in document
    assert "## Review threads (2)" in document
    assert "### src/app.py:42 — resolved" in document
    assert "### src/cli.py:? — outdated" in document
    assert (
        "```diff\n" + "\n".join(f" line {number}" for number in range(10, 20)) + "\n```" in document
    )
    assert "## Comments (1)" in document


def test_view_reports_error_status_as_failing_ci(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(pull_request={})
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "statusCheckRollup": [
            {"context": "build", "state": "ERROR", "targetUrl": "https://ci/build"}
        ],
    }
    routes["api graphql"] = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
    }
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))

    assert view.run_view("owner/repo#12") == 0
    assert "## CI — failure" in documents[0]
    assert "- [build](https://ci/build) — ERROR" in documents[0]


@pytest.mark.parametrize("check_count", [20, 21])
def test_view_truncates_long_check_and_thread_lists(monkeypatch, check_count):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(pull_request={"url": "pulls/12"})
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "statusCheckRollup": [
            {"__typename": "CheckRun", "name": f"check {number}", "conclusion": "FAILURE"}
            for number in range(check_count)
        ],
        "reviews": [],
    }
    routes["api graphql"] = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {"isResolved": False, "isOutdated": False, "path": "a.py", "line": 1}
                            for _ in range(51)
                        ]
                    }
                }
            }
        }
    }
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))

    assert view.run_view("owner/repo#12") == 0

    document = documents[0]
    assert "**Review:** none" in document
    assert "## Reviews (0)" in document
    assert "*No reviews.*" in document
    assert "## CI — failure" in document
    assert ("*Truncated to the first 20 failing checks.*" in document) == (check_count > 20)
    assert "- [check 19]" in document and "- [check 20]" not in document
    assert "## Review threads (50)" in document
    assert "*Truncated to the first 50 review threads.*" in document


def test_view_preserves_description_without_guessing_references(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    body = (
        "# Original heading\n\n- [ ] Fix #12\nUse #123456 for the button\n"
        "See #34 and [details](https://example.com/#456).\n\n"
        "`#123`\n\n```\n#789\n```\n"
    )
    routes["api repos/owner/repo/issues/12"] = issue(body=body)
    commands = []
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, recorded=commands))

    assert view.run_view("owner/repo#12") == 0
    assert f"## Description\n\n{body}\n" in documents[0]
    assert "## Referenced items" not in documents[0]
    assert ["gh", "api", "repos/owner/repo/issues/34"] not in commands


def test_view_loads_all_pages_of_subissues_and_comments(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    run = fake_gh_view_run(routes)

    def gh(command, **kwargs):
        result = run(command, **kwargs)
        if "--paginate" in command:
            pages = [json.loads(result.stdout)]
            if "issues/12/sub_issues" in command[2]:
                pages.append(
                    [
                        {
                            "number": 99,
                            "title": "Later child",
                            "state": "open",
                            "html_url": "https://github.com/owner/repo/issues/99",
                        }
                    ]
                )
            if "issues/12/comments" in command[2]:
                pages.append(
                    [
                        {
                            "user": {"login": "later"},
                            "body": "Later comment",
                            "created_at": "2026-01-01",
                        }
                    ]
                )
            result.stdout = "\n".join(json.dumps(page) for page in pages)
        return result

    monkeypatch.setattr(view.subprocess, "run", gh)
    assert view.run_view("owner/repo#12") == 0
    assert "Later child" in documents[0]
    assert "Later comment" in documents[0]


def test_view_shows_thread_replies_and_comment_truncation(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(pull_request={})
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
    }
    routes["api graphql"] = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "path": "a.py",
                                "line": 1,
                                "comments": {
                                    "nodes": [
                                        {"author": {"login": "one"}, "body": "first"},
                                        {"author": {"login": "two"}, "body": "reply"},
                                    ],
                                    "pageInfo": {"hasNextPage": True},
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))
    assert view.run_view("owner/repo#12") == 0
    assert "**@one**" in documents[0] and "**@two**" in documents[0]
    assert "reply" in documents[0]
    assert "comments truncated" in documents[0].lower()


def test_view_command_finds_workdash_local_gh(monkeypatch, tmp_path):
    import shlex

    from workdash import launcher, workdash

    documents = _capture(monkeypatch)
    local_bin = tmp_path / "bin"
    local_bin.mkdir()
    gh = local_bin / "gh"
    payload = json.dumps(issue(body="No references."))
    gh.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"  'api repos/owner/repo/issues/12') printf %s {shlex.quote(payload)} ;;\n"
        "  *) printf '[]' ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    monkeypatch.setattr(launcher, "_WORKDASH_LOCAL_BIN", local_bin)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    assert workdash.main(["view", "owner/repo#12"]) == 0
    assert "# Crash on startup" in documents[0]
    assert "No references." in documents[0]


def test_view_command_runs_without_dashboard_setup(monkeypatch):
    from workdash import workdash

    run = MagicMock(return_value=0)
    monkeypatch.setattr(view, "run_view", run)

    assert workdash.main(["view", "owner/repo#12"]) == 0
    run.assert_called_once_with("owner/repo#12")


@pytest.mark.parametrize("state", ["FAILURE", "PENDING"])
def test_view_counts_checks_beyond_first_page(monkeypatch, state):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(pull_request={})
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "headRefOid": "abc",
        "statusCheckRollup": [{"conclusion": "SUCCESS"}] * 100,
    }
    routes["api graphql"] = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
    }
    run = fake_gh_view_run(routes)

    def gh(command, **kwargs):
        if command[:3] == ["gh", "api", "graphql"] and "statusCheckRollup" in command[-1]:
            second = "cursor-1" in command[-1]
            checks = (
                [{"name": "late", "conclusion": state, "detailsUrl": "https://ci/late"}]
                if second
                else [{"conclusion": "SUCCESS"}] * 100
            )
            payload = {
                "data": {
                    "repository": {
                        "object": {
                            "statusCheckRollup": {
                                "state": state,
                                "contexts": {
                                    "nodes": checks,
                                    "pageInfo": {
                                        "hasNextPage": not second,
                                        "endCursor": "cursor-1" if not second else None,
                                    },
                                },
                            }
                        }
                    }
                }
            }
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        return run(command, **kwargs)

    monkeypatch.setattr(view.subprocess, "run", gh)
    assert view.run_view("owner/repo#12") == 0
    assert f"## CI — {state.lower()}" in documents[0]
    assert "100 of 101 checks passing." in documents[0]
    assert ("[late](https://ci/late)" in documents[0]) == (state == "FAILURE")


def test_view_uses_github_rollup_independently_of_check_states(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(pull_request={})
    routes["pr view 12 --repo owner/repo"] = {
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
        "statusCheckRollup": [
            {"name": "build", "conclusion": "FAILURE", "detailsUrl": "https://ci/build"}
        ],
    }
    routes["api graphql"] = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}
    }
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, rollup_state="PENDING"))

    assert view.run_view("owner/repo#12") == 0
    assert "## CI — pending" in documents[0]
    assert "0 of 1 checks passing." in documents[0]
    assert "- [build](https://ci/build) — FAILURE" in documents[0]


def test_view_nested_subissues_across_repositories(monkeypatch):
    documents = _capture(monkeypatch)
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12/sub_issues"] = [
        {
            "number": 13,
            "title": "Cross-repository child",
            "state": "open",
            "html_url": "https://github.com/other/project/issues/13",
            "sub_issues_summary": {"total": 1},
        }
    ]
    routes["api repos/other/project/issues/13/sub_issues"] = [
        {
            "number": 14,
            "title": "Nested child",
            "state": "closed",
            "html_url": "https://github.com/other/project/issues/14",
        }
    ]
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))

    assert view.run_view("owner/repo#12") == 0
    assert (
        "| └─ [other/project#13](https://github.com/other/project/issues/13) | open |"
        in documents[0]
    )
    assert (
        "| └─ └─ [other/project#14](https://github.com/other/project/issues/14) | closed |"
        in documents[0]
    )
