"""Shared fake for the ``gh`` boundary that the item document reads.

``workdash view`` reads one issue or pull request, its sub-issues and its
comments, so the unit tests and the BDD steps
replace ``subprocess.run`` with the same dispatcher and only vary the answers.
"""

from __future__ import annotations

import json
import subprocess

from gh_work_state_fake import FakeGitHub

ISSUE_URL = "https://github.com/owner/repo/issues/12"


def issue(**overrides) -> dict:
    """The issue read by default, with a description that references two other items."""

    html_url = ISSUE_URL.replace("/issues/", "/pull/") if "pull_request" in overrides else ISSUE_URL
    return {
        "number": 12,
        "title": "Crash on startup",
        "body": "# Crash on startup\n\n## Steps\n\nReproduce with #34 and #35.\n",
        "state": "open",
        "user": {"login": "octocat"},
        "labels": [{"name": "bug"}],
        "assignees": [{"login": "hubot"}],
        "milestone": {"title": "v1"},
        "comments": 1,
        "html_url": html_url,
        "updated_at": "2026-02-25T10:00:00Z",
        **overrides,
    }


def sub_issue(number: int, *, children: int, state: str = "open") -> dict:
    return {
        "number": number,
        "title": f"Sub issue {number}",
        "state": state,
        "html_url": f"https://github.com/owner/repo/issues/{number}",
        "sub_issues_summary": {"total": children},
    }


def issue_routes() -> dict[str, object]:
    """The answers for one issue, its nested sub-issues and its comments."""

    return {
        "api repos/owner/repo/issues/12/sub_issues": [sub_issue(13, children=1)],
        "api repos/owner/repo/issues/13/sub_issues": [sub_issue(14, children=0, state="closed")],
        "api repos/owner/repo/issues/14/sub_issues": [],
        "api repos/owner/repo/issues/12/comments": [
            {
                "user": {"login": "octocat"},
                "body": "Fixed in #34",
                "created_at": "2026-02-26T09:00:00Z",
            }
        ],
        "api repos/owner/repo/issues/12": issue(),
    }


def fake_gh_view_run(
    routes: dict[str, object],
    *,
    recorded: list[list[str]] | None = None,
    fails: bool = False,
    rollup_state: str | None = None,
    project: FakeGitHub | None = None,
):
    """Return a ``subprocess.run`` replacement answering ``gh`` by command shape.

    :param dict[str, object] routes: Payload keyed by the command prefix that reads it.
    :param list[list[str]] | None recorded: Every issued command is appended here.
    :param bool fails: Fail every lookup, as gh does when the item cannot be read.
    """

    commands = [] if recorded is None else recorded
    prefixes = sorted(routes, key=len, reverse=True)

    def fake_run(command, **_kwargs):
        commands.append(list(command))
        if "--slurp" in command:
            raise subprocess.CalledProcessError(1, command, stderr="unknown flag: --slurp")
        if fails:
            raise subprocess.CalledProcessError(1, command, stderr="no such issue")
        key = " ".join(command[1:])
        if key.startswith(
            ("api graphql -f query=query ProjectIssue", "api graphql -f query=query IssueBlockers")
        ):
            if project is not None:
                return project.run(command, **_kwargs)
            raise subprocess.CalledProcessError(1, command, stderr="HTTP 403")
        if project is not None and (
            key.startswith("api graphql -f query=query Ready") or "/branches/" in key
        ):
            return project.run(command, **_kwargs)
        if key.startswith("api graphql") and "statusCheckRollup" in key:
            pull = routes["pr view 12 --repo owner/repo"]
            checks = pull.get("statusCheckRollup") or []
            states = [
                check.get("conclusion") or check.get("state") or "PENDING" for check in checks
            ]
            state = rollup_state or (
                "FAILURE" if any(s in {"ERROR", "FAILURE"} for s in states) else "SUCCESS"
            )
            rollup = (
                {"state": state, "contexts": {"nodes": checks, "pageInfo": {"hasNextPage": False}}}
                if checks
                else None
            )
            payload = {"data": {"repository": {"object": {"statusCheckRollup": rollup}}}}
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        for prefix in prefixes:
            if key.startswith(prefix):
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(
                        {"headRefOid": "abc", **routes[prefix]}
                        if prefix.startswith("pr view ")
                        else routes[prefix]
                    ),
                    stderr="",
                )
        raise AssertionError(f"unexpected gh command: {command}")

    return fake_run


def pull_request_routes() -> dict[str, object]:
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12"] = issue(
        pull_request={"url": "https://api.github.com/repos/owner/repo/pulls/12"}
    )
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
        ],
        "reviews": [{"author": {"login": "reviewer"}, "state": "APPROVED", "body": "Looks good"}],
    }
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
                                            "diffHunk": "@@ -1,2 +1,2 @@\n-old\n+new",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                }
            }
        }
    }
    return routes
