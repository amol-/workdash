"""Shared fake GitHub answering the work state lookups at the ``gh`` boundary.

The unit tests and the BDD steps describe issues, pull requests, and branch
rules as plain records; the fake answers each GraphQL operation (dispatched by
its operation name and the repository or number it names) and each REST path
with payloads shaped like GitHub's.
"""

from __future__ import annotations

import itertools
import json
import re
import subprocess
import urllib.parse

import pytest

import workdash.github_client as github_client_module

BEFORE_PUSH = "2026-02-20T00:00:00Z"
PUSHED_AT = "2026-03-01T00:00:00Z"
AFTER_PUSH = "2026-03-02T00:00:00Z"
HEAD_SHA = "c0ffee2"
OLD_HEAD_SHA = "c0ffee1"
FORBIDDEN_STDERR = "gh: Resource not accessible by integration (HTTP 403)"
_urls = itertools.count(1)


def check_run(
    name: str,
    conclusion: str | None = "SUCCESS",
    status: str = "COMPLETED",
    *,
    started: str | None = AFTER_PUSH,
    workflow: int | None = 1,
    app: str = "github-actions",
) -> dict:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started,
        "checkSuite": {
            "app": {"slug": app},
            "workflowRun": None if workflow is None else {"workflow": {"databaseId": workflow}},
        },
    }


def status_context(name: str, state: str) -> dict:
    return {"__typename": "StatusContext", "context": name, "state": state}


def actor(login: str) -> dict:
    return {"__typename": "Bot" if login.endswith("[bot]") else "User", "login": login}


def review(
    login: str,
    state: str,
    *,
    created: str,
    drafted: str | None = None,
    commit: str = HEAD_SHA,
    body: str = "",
) -> dict:
    """A review submitted at ``created``, after being drafted since ``drafted``."""

    return {
        "state": state,
        "body": body,
        "url": f"https://github.com/review/{next(_urls)}",
        "createdAt": drafted or created,
        "submittedAt": created,
        "isMinimized": False,
        "commit": {"oid": commit},
        "author": actor(login),
    }


def comment(login: str, *, created: str, drafted: str | None = None) -> dict:
    """A comment published at ``created``; a review comment may be drafted earlier."""

    return {
        "url": f"https://github.com/comment/{next(_urls)}",
        "createdAt": drafted or created,
        "publishedAt": created,
        "isMinimized": False,
        "author": actor(login),
    }


def todo_body(target: str | None) -> str:
    metadata = {"workdash_todo_version": 1} | ({"target": target} if target else {})
    return f"```json\n{json.dumps(metadata)}\n```\n"


def thread(*comments: dict, resolved: bool = False, outdated: bool = False) -> dict:
    return {"isResolved": resolved, "isOutdated": outdated, "comments": {"nodes": list(comments)}}


def failure(stderr: str, stdout: str = "") -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["gh"], output=stdout, stderr=stderr)


class FakeGitHub:
    """GitHub state the work state lookups read, plus every operation they ran."""

    def __init__(self, previous_run) -> None:
        self._previous_run = previous_run
        self.issues: dict[str, dict] = {}
        self.pull_requests: dict[str, dict] = {}
        self.owners: dict[str, list[str]] = {}
        # (repo, branch) -> required check names, or "unreadable".
        self.ruleset_checks: dict[tuple[str, str], list[str] | str] = {}
        # (repo, branch) -> classic required check names, "unreadable" when the branch
        # summary omits them, or None for a branch protected by rulesets only;
        # branches missing here are unprotected.
        self.protection_checks: dict[tuple[str, str], list[str] | str | None] = {}
        # Operation name -> failure gh raises for it.
        self.failures: dict[str, subprocess.CalledProcessError] = {}
        self.page_size = 100
        self.operations: list[str] = []

    def add_issue(
        self,
        ref: str,
        *,
        state: str = "OPEN",
        parent: str | None = None,
        blocked_by: tuple[str, ...] = (),
        body: str = "",
        created: str = "2026-01-01T00:00:00Z",
    ) -> dict:
        self.issues[ref] = {
            "state": state,
            "parent": parent,
            "blocked_by": blocked_by,
            "body": body,
            "created": created,
        }
        return self.issues[ref]

    def add_pull_request(self, ref: str, *, closes: tuple[str, ...] = (), **fields) -> dict:
        """Add an open, mergeable pull request by ``testuser`` with passing CI on its head."""

        self.pull_requests[ref] = {
            "state": "OPEN",
            "author": "testuser",
            "created": "2026-02-01T00:00:00Z",
            # When the pull request last changed, when it is not its creation.
            "updated": None,
            # When it merged, which defaults to its creation so it reads as long merged.
            "merged": None,
            "closes": closes,
            "head": HEAD_SHA,
            # The latest commit GitHub lists, when it lags behind the head ref.
            "latest_commit": None,
            "pushed_at": PUSHED_AT,
            "force_pushed_at": None,
            "head_permission": "WRITE",
            # None once the head branch is deleted.
            "head_ref": {"refUpdateRule": None},
            "maintainer_can_modify": False,
            "base_permission": "WRITE",
            "base_ref": "main",
            "base_sha": "base1",
            # One answer, or the successive answers of repeated lookups.
            "mergeable": "MERGEABLE",
            "review_decision": None,
            # Head SHA -> its checks, paged by page_size; a head missing here reports none.
            "checks": {HEAD_SHA: [check_run("tests")]},
            "reviews": [],
            "review_requests": [],
            "threads": [],
            "comments": [],
            "lookups": 0,
            **fields,
        }
        return self.pull_requests[ref]

    def run(self, command, *args, **kwargs):
        if command[:2] != ["gh", "api"]:
            return self._previous_run(command, *args, **kwargs)
        if command[2] != "graphql":
            return self._rest(command, command[2])
        query = command[-1].removeprefix("query=")
        operation = re.match(r"query (\w+)", query)
        name = operation[1] if operation else "ItemLookup"
        self.operations.append(name)
        if name in self.failures:
            raise self.failures[name]
        if name == "ItemLookup":
            return self._item_lookup(command, query)
        data = getattr(self, f"_{name}")(query)
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"data": data}), stderr="")

    # -- GraphQL operations --------------------------------------------------

    def _ReadyOwnerRepositories(self, query: str) -> dict:
        login = re.search(r'repositoryOwner\(login: "([^"]+)"\)', query)[1]
        nodes = [
            {
                "nameWithOwner": repo,
                "issues": {"totalCount": self._open_count(self.issues, repo)},
                "pullRequests": {"totalCount": self._open_count(self.pull_requests, repo)},
            }
            for repo in self.owners[login]
        ]
        return {"repositoryOwner": {"repositories": self._page(query, nodes)}}

    def _ReadyRepositoryIssues(self, query: str) -> dict:
        repo = _queried_repository(query)
        nodes = [
            self._issue_node(ref)
            for ref, issue in self.issues.items()
            if _split(ref)[0] == repo and issue["state"] == "OPEN"
        ]
        return {"repository": {"issues": self._page(query, nodes)}}

    def _ReadyRepositoryPullRequests(self, query: str) -> dict:
        repo = _queried_repository(query)
        nodes = [
            {"number": _split(ref)[1]}
            for ref, pull_request in self.pull_requests.items()
            if _split(ref)[0] == repo and pull_request["state"] == "OPEN"
        ]
        return {"repository": {"pullRequests": self._page(query, nodes)}}

    def _ReadyIssueTree(self, query: str) -> dict:
        ref = f"{_queried_repository(query)}#{re.search(r'issue\(number: (\d+)\)', query)[1]}"
        children = [child for child, issue in self.issues.items() if issue["parent"] == ref]
        node = self._issue_node(ref) | {
            "subIssues": {"nodes": [self._issue_node(child) for child in children]}
        }
        return {"repository": {"issue": node}}

    def _ReadyPullRequests(self, query: str) -> dict:
        return {
            alias: {"pullRequest": self._pull_request_node(f"{owner}/{name}#{number}")}
            for alias, owner, name, number in _queried_pull_requests(query)
        }

    def _ReadyCommitChecks(self, query: str) -> dict:
        repo = _queried_repository(query)
        oid = re.search(r'object\(oid: "([^"]+)"\)', query)[1]
        checks = next(
            pr["checks"][oid]
            for ref, pr in self.pull_requests.items()
            if _split(ref)[0] == repo and oid in pr["checks"]
        )
        contexts = self._page(query, checks)
        return {"repository": {"object": {"statusCheckRollup": {"contexts": contexts}}}}

    def _PullRequestStates(self, query: str) -> dict:
        return {
            alias: {
                "pullRequest": {"state": self.pull_requests[f"{owner}/{name}#{number}"]["state"]}
            }
            for alias, owner, name, number in _queried_pull_requests(query)
        }

    def _item_lookup(self, command: list[str], query: str):
        """Answer the single issue or pull request lookup explicit targets resolve with."""

        match = re.search(r"issueOrPullRequest\(number: (\d+)\)", query)
        repo, number = _queried_repository(query), int(match[1])
        ref = f"{repo}#{number}"
        if ref in self.issues:
            node = {"__typename": "Issue", **_identity(self._issue_node(ref))}
            node["body"] = self.issues[ref]["body"]
            node["subIssuesSummary"] = self._issue_node(ref)["subIssuesSummary"]
        elif ref in self.pull_requests:
            node = {"__typename": "PullRequest", **_identity(self._pull_request_node(ref))}
            node["closingIssuesReferences"] = {
                "nodes": [
                    {"number": _split(issue)[1], "repository": {"nameWithOwner": _split(issue)[0]}}
                    for issue in self.pull_requests[ref]["closes"]
                ]
            }
        else:
            message = f"Could not resolve to an issue or pull request with the number of {number}."
            payload = {
                "data": {"repository": {"nameWithOwner": repo, "issueOrPullRequest": None}},
                "errors": [{"type": "NOT_FOUND", "message": message}],
            }
            raise failure(f"GraphQL: {message}", json.dumps(payload))
        payload = {"data": {"repository": {"nameWithOwner": repo, "issueOrPullRequest": node}}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    # -- REST paths ----------------------------------------------------------

    def _rest(self, command: list[str], path: str):
        self.operations.append(path)
        rules = re.fullmatch(r"repos/([^/]+/[^/]+)/rules/branches/([^?]+)\?per_page=100", path)
        branch = re.fullmatch(r"repos/([^/]+/[^/]+)/branches/([^/]+)", path)
        if rules:
            checks = self.ruleset_checks.get(_branch_key(rules), [])
            if checks == "unreadable":
                raise failure(FORBIDDEN_STDERR)
            answer: object = [
                {
                    "type": "required_status_checks",
                    "parameters": {"required_status_checks": [{"context": c} for c in checks]},
                }
            ][: 1 if checks else 0]
        elif branch:
            answer = self._branch(*_branch_key(branch))
        else:
            raise AssertionError(f"Unexpected GitHub REST call: {command}")
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(answer), stderr="")

    # -- Payload builders ----------------------------------------------------

    def _branch(self, repo: str, name: str) -> dict:
        """The branch summary any reader gets, including the classic required checks."""

        protected = (repo, name) in self.protection_checks
        checks = self.protection_checks.get((repo, name)) or []
        protection: dict = {"enabled": protected}
        if checks != "unreadable":
            # GitHub names required checks in both lists; split them so both are read.
            protection["required_status_checks"] = {
                "enforcement_level": "non_admins" if checks else "off",
                "contexts": checks[:1],
                "checks": [{"context": check, "app_id": None} for check in checks[1:]],
            }
        return {
            "name": name,
            "commit": {"sha": "base1"},
            "protected": protected,
            "protection": protection,
            "protection_url": f"https://api.github.com/repos/{repo}/branches/{name}/protection",
        }

    @staticmethod
    def _open_count(items: dict[str, dict], repo: str) -> int:
        return sum(
            _split(ref)[0] == repo and item["state"] == "OPEN" for ref, item in items.items()
        )

    def _page(self, query: str, nodes: list[dict]) -> dict:
        cursor = re.search(r'after: (?:null|"(\d+)")', query)[1]
        start = int(cursor or 0)
        end = start + self.page_size
        return {
            "pageInfo": {"hasNextPage": end < len(nodes), "endCursor": str(end)},
            "nodes": nodes[start:end],
        }

    def _issue_node(self, ref: str) -> dict:
        issue = self.issues[ref]
        repo, number = _split(ref)
        return {
            "number": number,
            "title": f"Issue {ref}",
            "url": f"https://github.com/{repo}/issues/{number}",
            "createdAt": issue["created"],
            "updatedAt": issue["created"],
            "state": issue["state"],
            "body": issue["body"],
            "repository": {"nameWithOwner": repo},
            "subIssuesSummary": {
                "total": sum(child["parent"] == ref for child in self.issues.values())
            },
            "issueDependenciesSummary": {
                "blockedBy": sum(self.issues[b]["state"] == "OPEN" for b in issue["blocked_by"])
            },
            "closedByPullRequestsReferences": {
                "nodes": [
                    {
                        "number": _split(pr_ref)[1],
                        "state": pull_request["state"],
                        "repository": {"nameWithOwner": _split(pr_ref)[0]},
                    }
                    for pr_ref, pull_request in self.pull_requests.items()
                    if ref in pull_request["closes"]
                ]
            },
        }

    def _pull_request_node(self, ref: str) -> dict:
        pr = self.pull_requests[ref]
        repo, number = _split(ref)
        pr["lookups"] += 1
        answers = pr["mergeable"] if isinstance(pr["mergeable"], list) else [pr["mergeable"]]
        merged_at = pr["merged"]
        if merged_at is None and pr["state"] == "MERGED":
            merged_at = pr["created"]
        latest_commit = pr["latest_commit"] or pr["head"]
        checks = pr["checks"].get(latest_commit)
        latest_opinions = {
            entry["author"]["login"]: entry
            for entry in pr["reviews"]
            if entry["state"] in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
        }
        return {
            "number": number,
            "title": f"Pull request {ref}",
            "url": f"https://github.com/{repo}/pull/{number}",
            "createdAt": pr["created"],
            "updatedAt": pr["updated"] or pr["created"],
            "mergedAt": merged_at,
            "state": pr["state"],
            "author": actor(pr["author"]),
            "repository": {"nameWithOwner": repo},
            "headRefOid": pr["head"],
            "maintainerCanModify": pr["maintainer_can_modify"],
            "headRef": pr["head_ref"],
            "headRepository": {"viewerPermission": pr["head_permission"]}
            if pr["head_permission"]
            else None,
            "baseRepository": {"viewerPermission": pr["base_permission"]},
            "baseRefName": pr["base_ref"],
            "baseRef": {"target": {"oid": pr["base_sha"]}},
            "mergeable": answers[min(pr["lookups"], len(answers)) - 1],
            "reviewDecision": pr["review_decision"],
            "closingIssuesReferences": {
                "nodes": [
                    _identity(self._issue_node(issue)) | {"body": self.issues[issue]["body"]}
                    for issue in pr["closes"]
                ]
            },
            "commits": {
                "nodes": [
                    {
                        "commit": {
                            "oid": latest_commit,
                            "committedDate": pr["pushed_at"],
                            "statusCheckRollup": None
                            if checks is None
                            else {"contexts": self._page("after: null", checks)},
                        }
                    }
                ]
            },
            "timelineItems": {
                "nodes": [{"createdAt": pr["force_pushed_at"], "afterCommit": {"oid": pr["head"]}}]
                if pr["force_pushed_at"]
                else []
            },
            "reviewRequests": {
                "nodes": [{"requestedReviewer": actor(login)} for login in pr["review_requests"]]
            },
            "latestOpinionatedReviews": {"nodes": list(latest_opinions.values())},
            "reviews": {"nodes": pr["reviews"]},
            "reviewThreads": {"nodes": pr["threads"]},
            "comments": {"nodes": pr["comments"]},
        }


def install_fake_github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    """Answer ``gh api`` calls from a fresh fake GitHub; other commands run as before."""

    github = FakeGitHub(subprocess.run)
    monkeypatch.setattr(subprocess, "run", github.run)
    monkeypatch.setattr(github_client_module, "_TRANSIENT_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(github_client_module, "_MERGEABILITY_RETRY_DELAY_SECONDS", 0)
    return github


def _split(ref: str) -> tuple[str, int]:
    repo, _, number = ref.partition("#")
    return repo, int(number)


def _queried_pull_requests(query: str) -> list[tuple[str, str, str, str]]:
    """Each aliased pull request a query names, as ``(alias, owner, name, number)``."""

    return re.findall(
        r'(p\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\) '
        r"\{ pullRequest\(number: (\d+)\)",
        query,
    )


def _queried_repository(query: str) -> str:
    match = re.search(r'repository\(owner: "([^"]+)", name: "([^"]+)"\)', query)
    return f"{match[1]}/{match[2]}"


def _branch_key(match: re.Match[str]) -> tuple[str, str]:
    return match[1], urllib.parse.unquote(match[2])


def _identity(node: dict) -> dict:
    keys = ("number", "title", "url", "createdAt", "updatedAt", "author", "repository")
    return {key: node[key] for key in keys if key in node}
