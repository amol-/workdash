import asyncio
import http.client
import io
import json
import os
import subprocess
import urllib.error
import urllib.request
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest
from textual.app import App
from textual.widgets import Markdown, OptionList, Static

from gh_work_state_fake import (
    HEAD_SHA,
    FakeGitHub,
    check_run,
    failure,
    todo_body,
)
from view_fake import (
    ISSUE_URL,
    fake_gh_view_run,
    issue,
    issue_routes,
    pull_request_routes,
    sub_issue,
)
from workdash import github_client, launcher, view
from workdash.config import WorkdashConfig
from workdash.control import WorkdashControlClient, WorkdashControlError, WorkdashSession
from workdash.git import GitHelper


@pytest.fixture(autouse=True)
def _no_workdash_server(monkeypatch):
    def unavailable(*args, **kwargs):
        raise ConnectionRefusedError

    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", unavailable)


def _capture(monkeypatch) -> list[view.Document]:
    documents: list[view.Document] = []
    monkeypatch.setattr(view, "show_document", documents.append)
    return documents


@pytest.mark.parametrize("reference", ["owner/repo#12", "#12", ISSUE_URL])
def test_view_pull_request_document_reports_ci_reviews_and_threads(monkeypatch, reference):
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
    monkeypatch.setattr(GitHelper, "get_repo_root", lambda *args: "/repo")
    monkeypatch.setattr(GitHelper, "remote_url", lambda *args: "git@codeberg.org:owner/repo.git")

    assert view.run_view(reference) == 0

    document = documents[0].content
    assert "**Draft:** yes" in document
    assert "**Mergeable:** MERGEABLE" in document
    assert "**Merge state:** CLEAN" in document
    assert "**Review:** APPROVED" in document
    assert "## Sub-issues (2)\n\n" in document
    assert "└─ └─ [owner/repo#14]" in document
    assert "**Parent status:**" not in document
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
    assert "## CI — failure" in documents[0].content
    assert "- [build](https://ci/build) — ERROR" in documents[0].content


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

    document = documents[0].content
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
    assert f"## Description\n\n{body}\n" in documents[0].content
    assert "## Referenced items" not in documents[0].content
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
                            "sub_issues_summary": {"total": 0},
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
    assert "Later child" in documents[0].content
    assert "Later comment" in documents[0].content


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
    assert "**@one**" in documents[0].content and "**@two**" in documents[0].content
    assert "reply" in documents[0].content
    assert "comments truncated" in documents[0].content.lower()


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
    assert "# Crash on startup" in documents[0].content
    assert "No references." in documents[0].content


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
    assert f"## CI — {state.lower()}" in documents[0].content
    assert "100 of 101 checks passing." in documents[0].content
    assert ("[late](https://ci/late)" in documents[0].content) == (state == "FAILURE")


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
    assert "## CI — pending" in documents[0].content
    assert "0 of 1 checks passing." in documents[0].content
    assert "- [build](https://ci/build) — FAILURE" in documents[0].content


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
            "sub_issues_summary": {"total": 0},
        }
    ]
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))

    assert view.run_view("owner/repo#12") == 0
    assert (
        "| └─ [other/project#13](https://github.com/other/project/issues/13) | open |"
        in documents[0].content
    )
    assert (
        "| └─ └─ [other/project#14](https://github.com/other/project/issues/14) | closed |"
        in documents[0].content
    )


@pytest.mark.parametrize("target", [ISSUE_URL, "owner/repo#12", "12", "#12"])
def test_view_github_target_spellings_name_the_same_item(monkeypatch, target):
    documents = _capture(monkeypatch)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    monkeypatch.setattr(GitHelper, "get_repo_root", lambda *args, **kwargs: "/repo")
    monkeypatch.setattr(GitHelper, "remote_url", lambda *args: "git@github.com:owner/repo.git")

    assert view.run_view(target) == 0
    assert f"**Item:** [owner/repo#12]({ISSUE_URL})" in documents[0].content


def test_issue_document_ends_with_generation_timestamp(monkeypatch):
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))

    document = view.load_document(ISSUE_URL)

    assert document.content.splitlines()[-1].startswith("*Generated from `gh` at ")
    assert document.content.rstrip().endswith("UTC.*")


def test_view_reports_a_failed_github_lookup(monkeypatch, capsys):
    documents = _capture(monkeypatch)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes(), fails=True))

    assert view.run_view(ISSUE_URL) == 1
    assert documents == []
    assert "Failed to fetch GitHub data with gh: no such issue" in capsys.readouterr().err


@pytest.mark.parametrize("path_arg", ["notes.md", "notes/chapter#PR-12"])
def test_view_reads_a_local_file_without_contacting_github(monkeypatch, tmp_path, path_arg):
    from workdash.workdash import main

    documents = _capture(monkeypatch)
    commands = []
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run({}, recorded=commands))
    monkeypatch.chdir(tmp_path)
    path = tmp_path / path_arg
    path.parent.mkdir(exist_ok=True)
    path.write_text("# Local notes\n\nlocal body", encoding="utf-8")

    assert main(["view", path_arg]) == 0

    assert commands == []
    assert documents[0].content == "# Local notes\n\nlocal body"
    assert documents[0].source == path.resolve().as_uri()


def test_view_reads_a_file_url(monkeypatch, tmp_path):
    documents = _capture(monkeypatch)
    path = tmp_path / "notes with spaces.md"
    path.write_text("# Local notes\n\nlocal body\n", encoding="utf-8")

    assert view.run_view(path.as_uri()) == 0

    assert documents[0].content == "# Local notes\n\nlocal body\n"
    assert documents[0].source == path.resolve().as_uri()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("a missing file.md", "Failed to read a missing file.md"),
        ("file://elsewhere/missing.md", "Unsupported file URL"),
        (
            "ftp://example.test/doc.md",
            "Unsupported target ftp://example.test/doc.md: expected a local path, a file URL, "
            "an HTTP(S) URL, or a GitHub issue or pull request.",
        ),
    ],
)
def test_view_reports_a_target_it_cannot_read(monkeypatch, capsys, tmp_path, target, message):
    monkeypatch.chdir(tmp_path)
    documents = _capture(monkeypatch)

    assert view.run_view(target) == 1

    assert documents == []
    assert message in capsys.readouterr().err


def test_view_reports_a_document_it_cannot_decode(monkeypatch, capsys, tmp_path):
    documents = _capture(monkeypatch)
    path = tmp_path / "latin.md"
    path.write_bytes("# Caf\xe9".encode("latin-1"))

    assert view.run_view(str(path)) == 1

    assert documents == []
    assert f"Failed to read {path}" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (urllib.error.URLError("no route to host"), "<urlopen error no route to host>"),
        (http.client.IncompleteRead(b"# partial"), "IncompleteRead(9 bytes read)"),
    ],
)
def test_view_reports_a_remote_document_it_cannot_fetch(monkeypatch, capsys, error, reason):
    documents = _capture(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", MagicMock(side_effect=error))

    assert view.run_view("https://docs.example/guide.md") == 1

    assert documents == []
    assert f"Failed to read https://docs.example/guide.md: {reason}" in capsys.readouterr().err


def _run_viewer(monkeypatch, exercise) -> None:
    """Patch ``App.run`` so ``exercise(pilot, app)`` runs under ``run_test``."""

    def run(app):
        async def session():
            async with app.run_test(size=(80, 24), notifications=True) as pilot:
                await pilot.pause()
                await exercise(pilot, app)

        asyncio.run(session())

    monkeypatch.setattr(App, "run", run)


def test_viewer_opens_a_github_item_link_without_launching_anything(monkeypatch):
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    launched = []
    monkeypatch.setattr(launcher, "open_in_browser", lambda *args, **kwargs: launched.append(args))
    app = view._MarkdownApp(view.Document("# Analysis", "file:///analysis.md"), "xdg-open", False)
    shown = []

    async def exercise(pilot, app):
        app.follow(ISSUE_URL)
        for _ in range(200):
            await pilot.pause()
            if (
                app.query_one(view._DocumentViewer).document.source.startswith("# Crash")
                and "unavailable" in app.query_one("#status", Static).render().plain
            ):
                break
        shown.append(app.query_one(view._DocumentViewer).document.source)
        shown.append(app.query_one("#status", Static).render().plain)
        shown.append("b" in app.screen.active_bindings)

    _run_viewer(monkeypatch, exercise)
    app.run()

    assert shown[0].startswith("# Crash on startup\n")
    assert "No Workdash server" in shown[1]
    assert shown[2] is True
    assert launched == []


@pytest.mark.parametrize("session", [None, "workdash-current"], ids=["standalone", "zellij"])
def test_viewer_follows_a_pull_request_without_launching_or_replacing_a_process(
    monkeypatch, session
):
    if session is not None:
        monkeypatch.setenv("ZELLIJ_SESSION_NAME", session)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(pull_request_routes()))
    pane = MagicMock()
    replace = MagicMock()
    monkeypatch.setattr(launcher.zellij, "open_viewer", pane)
    monkeypatch.setattr(os, "execvp", replace)
    opener = MagicMock()
    monkeypatch.setattr(launcher, "open_in_browser", opener)
    app = view._MarkdownApp(view.Document("# Analysis", "file:///analysis.md"), "xdg-open", False)

    async def exercise(pilot, app):
        app.follow("https://github.com/owner/repo/pull/12")
        async with asyncio.timeout(5):
            while app.query_one("#status", Static).render().plain.startswith("Loading "):
                await asyncio.sleep(0.03)
        assert app.is_running
        content = app.query_one(view._DocumentViewer).document.source
        assert content.startswith("# Crash on startup\n")
        assert "**Draft:** yes" in content

    _run_viewer(monkeypatch, exercise)
    app.run()

    pane.assert_not_called()
    replace.assert_not_called()
    opener.assert_not_called()


def test_project_cannot_confirm_parent_readiness_when_a_closed_childs_tree_is_unavailable(
    monkeypatch,
):
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12/sub_issues"] = [
        sub_issue(13, children=1, state="closed")
    ]
    project = FakeGitHub(subprocess.run)
    project.add_issue("owner/repo#12")
    project.add_issue("owner/repo#13", state="CLOSED", parent="owner/repo#12")
    gh_run = fake_gh_view_run(routes, project=project)

    def run(command, **kwargs):
        if "repos/owner/repo/issues/13/sub_issues?per_page=100" in command:
            raise failure("HTTP 403")
        return gh_run(command, **kwargs)

    monkeypatch.setattr(view.subprocess, "run", run)

    document = view.load_document("owner/repo#12").content

    assert document.startswith("# Crash on startup\n")
    assert "## Description" in document and "## Comments (1)" in document
    assert "[owner/repo#13](https://github.com/owner/repo/issues/13) | closed |" in document
    assert "Some sub-issues are unavailable" in document
    assert "**Parent status:** Readiness unavailable" in document
    assert "Ready on GitHub" not in document


def test_project_keeps_document_and_pr_link_when_pr_details_lookup_fails(monkeypatch):
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(github_username="testuser"))
    routes = issue_routes()
    routes["api repos/owner/repo/issues/14/sub_issues"] = []
    routes["api repos/owner/repo/issues/13/sub_issues"] = [sub_issue(14, children=0)]
    project = FakeGitHub(subprocess.run)
    project.add_issue("owner/repo#12")
    project.add_issue("owner/repo#13", parent="owner/repo#12")
    project.add_issue("owner/repo#14", parent="owner/repo#13")
    project.add_pull_request("owner/repo#20", closes=("owner/repo#14",))
    project.failures["ReadyPullRequests"] = failure("HTTP 403")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))

    document = view.load_document("owner/repo#12").content

    row = next(line for line in document.splitlines() if line.startswith("| └─ └─"))
    assert "[PR owner/repo#20](https://github.com/owner/repo/pull/20)" in row
    assert "Review and checks unavailable" in row
    assert "Readiness unavailable" in row
    assert "Linked PRs incomplete" not in row
    assert "Ready on GitHub" not in row
    assert document.startswith("# Crash on startup\n")
    assert "## Description" in document and "## Comments (1)" in document


@pytest.fixture
def two_child_project():
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12/sub_issues"] = [
        sub_issue(number, children=0) for number in (13, 14)
    ]
    project = FakeGitHub(subprocess.run)
    project.add_issue("owner/repo#12")
    for number in (13, 14):
        project.add_issue(f"owner/repo#{number}", parent="owner/repo#12")
    return routes, project


@pytest.mark.parametrize(
    "viewer_session, targeted_todo",
    [("workdash-test", False), ("workdash-test", True), ("workdash-other", False)],
)
def test_project_reports_launch_work_in_the_viewers_session(
    monkeypatch, tmp_path, two_child_project, viewer_session, targeted_todo
):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", viewer_session)
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1")
    routes, project = two_child_project
    repo = "owner/todos" if targeted_todo else "owner/repo"
    if targeted_todo:
        project.add_issue(f"{repo}#13", parent="owner/repo#12", body=todo_body("owner/repo"))
        routes["api repos/owner/repo/issues/12/sub_issues"][0]["html_url"] = (
            f"https://github.com/{repo}/issues/13"
        )
    client = github_client.GitHubClient()
    config = WorkdashConfig(workdir=str(tmp_path), github_username="testuser")
    monkeypatch.setattr(view, "load_config", lambda: config)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))
    worktrees = {n: tmp_path / f"owner_repo_{n}" for n in (13, 14)}
    for worktree in worktrees.values():
        worktree.mkdir()
    session = WorkdashSession(
        config=config,
        backend=SimpleNamespace(github_client=client),
        work_items=[
            client.resolve_issue_scope(item_repo, n, todo_repository="owner/todos")[0].item
            for item_repo, n in ((repo, 13), ("owner/repo", 14))
        ],
        suggestion_markers={},
        zellij_session="workdash-test",
    )
    session.git = MagicMock()
    session.git.worktree_proves_item.side_effect = lambda path, item: path == worktrees[item.number]
    monkeypatch.setattr(
        launcher.zellij,
        "load_zellij_panes",
        lambda _: [
            {"id": 1, "title": "code_owner_repo_13", "pane_cwd": str(worktrees[14])},
            {"id": 2, "title": "code_unknown", "pane_cwd": str(tmp_path / "unknown")},
            {"id": 3, "title": "terminal_owner_repo_14", "pane_cwd": str(worktrees[14])},
            {"id": 4, "title": "code_owner_repo_14", "exited": True},
        ],
    )
    info = session.info()
    assert info["panes"][0]["item"] == (
        "owner/repo#ISSUE-WT13" if targeted_todo else "owner/repo#ISSUE-13"
    )
    assert info["panes"][0]["cwd"] == str(worktrees[14])
    monkeypatch.setattr(WorkdashControlClient, "request", lambda *args, **kwargs: info)

    document = view.load_document("owner/repo#12").content
    active = next(row for row in document.splitlines() if f"[{repo}#13]" in row)
    other = next(row for row in document.splitlines() if "[owner/repo#14]" in row)
    if viewer_session == "workdash-test":
        assert "Workdash coding pane" in active and "Ready on GitHub" not in active
    else:
        assert "Workdash coding pane" not in active and "Ready on GitHub" in active
    assert "Workdash coding pane" not in other and "Ready on GitHub" in other
    assert "Couldn’t check whether Workdash is working on" in document


def test_project_does_not_attribute_remote_panes_from_the_same_named_session(
    monkeypatch, two_child_project
):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    monkeypatch.setenv("WORKDASH_HOST", "192.168.5.6:8765")
    routes, project = two_child_project
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))
    open_http = MagicMock(side_effect=urllib.error.URLError(ConnectionRefusedError()))
    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", open_http)

    document = view.load_document("owner/repo#12").content

    open_http.assert_not_called()
    row = next(line for line in document.splitlines() if "[owner/repo#13]" in line)
    assert "Ready on GitHub" in row and "Workdash coding pane" not in row
    assert "Couldn’t check whether Workdash is working on" in document


@pytest.mark.parametrize("local_reply", [False, True], ids=["proxy-only", "direct-reply"])
def test_project_only_attributes_direct_panes_to_the_local_session(
    monkeypatch, two_child_project, local_reply
):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-test")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8765")
    monkeypatch.setenv("http_proxy", "http://proxy.example:8888")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(urllib.request, "urlopen", urllib.request.build_opener().open)
    routes, project = two_child_project
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))

    response = MagicMock(code=200)
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(
        {
            "ok": True,
            "result": {
                "session": "workdash-test",
                "panes": [{"kind": "agent", "item_url": "https://github.com/owner/repo/issues/13"}],
            },
        }
    ).encode()

    def open_http(_handler, _connection, request):
        if request.has_proxy() or local_reply:
            response.geturl.return_value = request.full_url
            return response
        raise urllib.error.URLError(ConnectionRefusedError())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", open_http)
    document = view.load_document("owner/repo#12").content

    target = next(line for line in document.splitlines() if "[owner/repo#13]" in line)
    other = next(line for line in document.splitlines() if "[owner/repo#14]" in line)
    assert ("Workdash coding pane" in target) is local_reply
    assert ("Ready on GitHub" in target) is not local_reply
    assert "Ready on GitHub" in other and "Workdash coding pane" not in other
    assert ("Couldn’t check whether Workdash is working on" in document) is not local_reply


@pytest.mark.parametrize("pane_pr, ready", [(20, False), (21, True)])
def test_project_only_counts_the_selected_repair_pane_against_readiness(
    monkeypatch, two_child_project, pane_pr, ready
):
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(github_username="testuser"))
    routes, project = two_child_project
    project.add_pull_request(
        "owner/repo#20",
        closes=("owner/repo#13",),
        checks={HEAD_SHA: [check_run("unit tests", "FAILURE")]},
    )
    project.add_pull_request(
        "owner/repo#21", closes=("owner/repo#13",), review_decision="REVIEW_REQUIRED"
    )
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))
    monkeypatch.setattr(
        WorkdashControlClient,
        "request",
        lambda *args, **kwargs: {
            "panes": [
                {"kind": "agent", "item_url": f"https://github.com/owner/repo/pull/{pane_pr}"}
            ]
        },
    )

    document = view.load_document("owner/repo#12").content

    row = next(line for line in document.splitlines() if "[owner/repo#13]" in line)
    assert ("Ready on GitHub" in row) is ready
    pull = row.split(f"[PR owner/repo#{pane_pr}]", 1)[1].split(" · ", 1)[0]
    assert "Workdash coding pane mapped here" in pull


def test_project_reports_shared_repair_underway_through_linked_issue(
    monkeypatch, two_child_project
):
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(github_username="testuser"))
    routes, project = two_child_project
    project.add_pull_request(
        "owner/repo#20",
        closes=("owner/repo#13", "owner/repo#14"),
        checks={HEAD_SHA: [check_run("unit tests", "FAILURE")]},
    )
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))

    def other_issue_row():
        document = view.load_document("owner/repo#12").content
        return next(line for line in document.splitlines() if "[owner/repo#14]" in line)

    assert "Ready on GitHub" in other_issue_row()
    monkeypatch.setattr(
        WorkdashControlClient,
        "request",
        lambda *args, **kwargs: {
            "panes": [{"kind": "agent", "item_url": "https://github.com/owner/repo/issues/13"}]
        },
    )

    row = other_issue_row()
    assert "Ready on GitHub" not in row
    assert "Workdash coding pane holds repair through" in row
    assert "[owner/repo#13](https://github.com/owner/repo/issues/13)" in row
    assert "Checks failing" in row


def test_project_keeps_explicit_review_decision_and_request(monkeypatch, two_child_project):
    routes, project = two_child_project
    project.add_pull_request(
        "owner/repo#20",
        closes=("owner/repo#13",),
        review_decision="APPROVED",
        review_requests=["human"],
        checks={HEAD_SHA: [check_run("unit tests")]},
    )
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))

    document = view.load_document("owner/repo#12").content

    row = next(line for line in document.splitlines() if "[owner/repo#13]" in line)
    assert "Review approved; Review requested; Reported checks passing" in row
    assert "Ready on GitHub" not in row


@pytest.mark.parametrize(
    "state, label", [("CLOSED", "Closed without merging"), ("MERGED", "Merged")]
)
def test_project_shows_ready_issue_with_coding_pane_on_its_inactive_pr(
    monkeypatch, two_child_project, state, label
):
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    routes, project = two_child_project
    project.add_pull_request("owner/repo#20", closes=("owner/repo#13",), state=state)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes, project=project))
    monkeypatch.setattr(
        WorkdashControlClient,
        "request",
        lambda *args, **kwargs: {
            "panes": [
                {
                    "kind": "agent",
                    "item": "owner/repo#PR-20",
                    "item_url": "https://github.com/owner/repo/pull/20",
                }
            ]
        },
    )

    document = view.load_document("owner/repo#12").content

    row = next(line for line in document.splitlines() if "[owner/repo#13]" in line)
    issue, pull = row.split("[PR owner/repo#20]", 1)
    assert "Ready on GitHub" in issue and "Workdash coding pane" not in issue
    assert f"{label}; Workdash coding pane" in pull


def test_project_supplemental_reads_stop_at_a_deadline(monkeypatch):
    routes = issue_routes()
    routes["api repos/owner/repo/issues/12/sub_issues"] = [
        sub_issue(number, children=0) for number in range(13, 23)
    ]
    run = fake_gh_view_run(routes)
    issue_batches = []
    clock = 0.0

    def slow_lookup(command, **kwargs):
        nonlocal clock
        if command[-1].startswith("query=query ProjectIssue"):
            issue_batches.append(command[-1])
            timeout = kwargs["timeout"]
            clock += timeout
            raise subprocess.TimeoutExpired(command, timeout)
        return run(command, **kwargs)

    monkeypatch.setattr(github_client.time, "monotonic", lambda: clock)
    monkeypatch.setattr(view.subprocess, "run", slow_lookup)

    document = view.load_document("owner/repo#12").content

    assert len(issue_batches) == 1 and "issue(number: 22)" not in issue_batches[0]
    assert "## Sub-issues (10)" in document
    for number in (13, 22):
        row = next(line for line in document.splitlines() if f"[owner/repo#{number}]" in line)
        assert "Linked PRs unavailable" in row and "Readiness unavailable" in row
    assert "Ready on GitHub" not in document


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        (["owner/repo#ISSUE-3", "--target", "owner/repo#ISSUE-12"], "conflicts with --target"),
        (["#3", "--target", "owner/repo#ISSUE-12"], "conflicts with --target"),
        (["report.md", "--target", "owner/repo#ISSUE-404"], "no such issue"),
        (["report.md", "--target", "owner/repo#PR-12"], "is an issue; use owner/repo#ISSUE-12"),
    ],
)
def test_view_rejects_conflicting_or_unresolvable_targets(monkeypatch, capsys, args, reason):
    from workdash.workdash import main

    documents = _capture(monkeypatch)
    gh = MagicMock(side_effect=fake_gh_view_run(issue_routes(), fails="404" in args[-1]))
    monkeypatch.setattr(view.subprocess, "run", gh)
    request = MagicMock()
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    assert main(["view", *args]) == 1
    error = capsys.readouterr().err
    assert reason in error and "--target" in error
    assert not documents
    request.assert_not_called()
    if "conflicts" in reason:
        gh.assert_not_called()


@pytest.mark.parametrize(
    ("target", "origin", "reason"),
    [
        ("owner/repo#12", None, "is a pull request; use owner/repo#PR-12"),
        ("#12", "git@github.com:owner/repo.git", "is a pull request; use owner/repo#PR-12"),
        ("#12", "https://codeberg.org/owner/repo.git", "origin remote is not a GitHub repository"),
        (
            "#12",
            "https://github.com:8443/owner/repo.git",
            "origin remote is not a GitHub repository",
        ),
        (
            "#12",
            "ssh://git@ssh.github.com:443/owner/repo.git",
            "origin remote is not a GitHub repository",
        ),
    ],
)
def test_explicit_issue_shorthand_rejects_prs_and_non_github_origins(
    tmp_path, monkeypatch, capsys, target, origin, reason
):
    from workdash.workdash import main

    path = tmp_path / "report.md"
    path.write_text("# Report", encoding="utf-8")
    documents = _capture(monkeypatch)
    github_origin = "origin remote" not in reason
    gh = MagicMock(
        side_effect=fake_gh_view_run(pull_request_routes() if github_origin else issue_routes())
    )
    monkeypatch.setattr(view.subprocess, "run", gh)
    monkeypatch.setattr(GitHelper, "get_repo_root", lambda *args: "/repo")
    monkeypatch.setattr(GitHelper, "remote_url", lambda *args: origin)

    assert main(["view", str(path), "--target", target]) == 1
    assert reason in capsys.readouterr().err
    assert not documents
    if not github_origin:
        gh.assert_not_called()


@pytest.mark.parametrize(
    ("target", "origin", "lookup"),
    [
        ("owner/repo#ISSUE-12", None, True),
        ("owner/repo#12", None, True),
        ("#12", "git@github.com:owner/repo.git", True),
        ("#12", "owner/repo", True),
        # A URL already names the item, so it is not looked up again.
        (ISSUE_URL, None, False),
    ],
)
def test_target_identifies_item_without_rendering_it(tmp_path, monkeypatch, target, origin, lookup):
    from workdash.workdash import main

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "owner/repo#ISSUE-12"
    path.parent.mkdir()
    path.write_text("# Report\n\n[Details](details.md)", encoding="utf-8")
    calls = []
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes(), recorded=calls))
    monkeypatch.setattr(GitHelper, "get_repo_root", lambda *args: "/repo")
    monkeypatch.setattr(GitHelper, "remote_url", lambda *args: origin)
    request = MagicMock()
    monkeypatch.setattr(WorkdashControlClient, "request", request)
    documents = _capture(monkeypatch)

    assert main(["view", "owner/repo#ISSUE-12", "--target", target]) == 0
    [document] = documents
    assert document.item.canonical_url == ISSUE_URL
    assert document.source == path.as_uri() and document.content == path.read_text()
    assert calls == ([["gh", "api", "repos/owner/repo/issues/12"]] if lookup else [])
    request.assert_not_called()


@pytest.mark.parametrize(
    "recorded_target", ["https://codeberg.org/owner/repo.git", "owner/other", None]
)
def test_explicit_targeted_todo_uses_configured_repository(
    tmp_path, monkeypatch, capsys, recorded_target
):
    path = tmp_path / "report.md"
    path.write_text("# Report")
    docs = _capture(monkeypatch)
    url = "https://github.com/owner/todos/issues/12"
    routes = {
        "api repos/owner/todos/issues/12": issue(html_url=url, body=todo_body(recorded_target))
    }
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(todo_repository="owner/todos"))
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))

    result = view.run_view(str(path), item_target="owner/repo#ISSUE-WT12")
    if recorded_target == "https://codeberg.org/owner/repo.git":
        assert result == 0
        assert docs[0].item.canonical_url == url and docs[0].content == "# Report"
    else:
        assert result == 1
        assert "--target" in capsys.readouterr().err
        assert not docs


@pytest.mark.parametrize("offline", [False, True], ids=["no-item", "offline"])
def test_disabled_launch_actions_explain_why(tmp_path, monkeypatch, offline):
    from workdash.workdash import main

    path = tmp_path / "report.md"
    path.write_text("# Report", encoding="utf-8")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    request = MagicMock(
        side_effect=WorkdashControlError("server_unreachable", "No Workdash server")
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    async def exercise(pilot, app):
        status = app.query_one("#status", Static)
        expected = "No Workdash server" if offline else "no GitHub item"
        await _wait_viewer(pilot, lambda: expected in status.render().plain)
        assert all(not app.screen.active_bindings[key].enabled for key in "cd")
        await pilot.press("c", "d")
        assert expected in status.render().plain
        assert app.query_one(Markdown).source == "# Report"
        await pilot.press("q")

    _run_viewer(monkeypatch, exercise)
    assert main(["view", str(path), *(["--target", ISSUE_URL] if offline else [])]) == 0
    if offline:
        assert (
            request.call_args_list
            == [call("show_config", timeout=view._LAUNCH_PROBE_TIMEOUT_SECONDS)]
            * view._LAUNCH_PROBE_ATTEMPTS
        )
    else:
        request.assert_not_called()


@pytest.mark.parametrize("status_line", ["200 OK", "503 Unavailable"])
def test_interrupted_config_reply_leaves_document_open(tmp_path, monkeypatch, status_line):
    path = tmp_path / "report.md"
    path.write_text("# Report")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8765")
    monkeypatch.setattr(view, "load_config", WorkdashConfig)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))

    def open_response(request, **_kwargs):
        assert request.selector == "/api/v0/show_config"
        wire = f'HTTP/1.1 {status_line}\r\nContent-Length: 100\r\n\r\n{{"ok":'.encode()
        response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *_: io.BytesIO(wire)))
        response.begin()
        if response.status != 200:
            raise urllib.error.HTTPError(
                request.full_url, response.status, response.reason, {}, response
            )
        return response

    monkeypatch.setattr(urllib.request, "urlopen", open_response)

    async def exercise(pilot, app):
        status = app.query_one("#status", Static)
        await _wait_viewer(pilot, lambda: "unavailable" in status.render().plain)
        assert not any(app.screen.active_bindings[key].enabled for key in "cd")
        assert app.query_one(Markdown).source == "# Report"
        await pilot.press("q")

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path), item_target=ISSUE_URL) == 0


def test_view_pr_code_menu_cancels_or_reports_existing_pane(tmp_path, monkeypatch):
    from workdash.workdash import main

    path = tmp_path / "report.md"
    path.write_text("# Report", encoding="utf-8")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(pull_request_routes()))
    request = MagicMock(
        side_effect=[
            {"agents": {"code": ["studio", "editorial"], "analyze": ["analyst"]}},
            {"status": "already_running", "pane_id": "terminal_31"},
        ]
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    async def exercise(pilot, app):
        await _wait_viewer(pilot, lambda: app.screen.active_bindings["c"].enabled)
        assert "d" not in app.screen.active_bindings
        await pilot.press("d", "c")
        assert _menu_agents(app) == ["studio", "editorial"]
        await pilot.press("escape")
        request.assert_called_once_with("show_config", timeout=2)
        await pilot.press("c", "down", "enter")
        await _wait_viewer(
            pilot,
            lambda: (
                app.query_one("#status", Static).render().plain
                == "Code already_running: pane terminal_31"
            ),
        )

    _run_viewer(monkeypatch, exercise)
    assert main(["view", str(path), "--target", "owner/repo#PR-12"]) == 0
    assert request.call_args_list[1:] == [
        call("code", {"target": "https://github.com/owner/repo/pull/12", "agent": "editorial"})
    ]


@pytest.mark.parametrize(
    "final",
    [
        {"status": "completed", "outcome": "Blocked by owner/repo#13."},
        {"status": "failed", "error": "GitHub blocker lookup failed."},
    ],
)
def test_view_dispatch_waits_for_real_first_check(monkeypatch, final):
    from workdash.workdash import main

    routes = issue_routes()
    routes["api repos/owner/repo/issues/12/sub_issues"] = []
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(routes))
    request = MagicMock(
        side_effect=[
            {
                "agents": {
                    "code": ["coder", "studio", "editorial"],
                    "analyze": ["analyst", "editorial", "studio"],
                }
            },
            {
                "id": "dispatch-1",
                "status": "running",
                "issue_scope": True,
                "initial_check_complete": False,
            },
            {"session": {"id": "dispatch-1", "initial_check_complete": True, **final}},
        ]
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    async def exercise(pilot, app):
        await _wait_viewer(pilot, lambda: app.screen.active_bindings["d"].enabled)
        assert app.screen.active_bindings["c"].enabled
        await pilot.press("d")
        assert _menu_agents(app) == ["studio", "editorial"]
        await pilot.press("enter")
        status = app.query_one("#status", Static)
        await _wait_viewer(
            pilot, lambda: status.render().plain.startswith("Autodispatch dispatch-1:")
        )
        assert final["status"] in status.render().plain
        assert (final.get("outcome") or final["error"]) in status.render().plain

    _run_viewer(monkeypatch, exercise)
    assert main(["view", "owner/repo#ISSUE-12"]) == 0
    assert request.call_args_list[1:] == [
        call("autodispatch", {"scope": ISSUE_URL, "agent": "studio"}),
        call("autodispatch_status", {"id": "dispatch-1"}),
    ]


def test_followed_document_cannot_launch_original_item_but_back_restores_it(tmp_path, monkeypatch):
    from workdash.workdash import main

    path = tmp_path / "report.md"
    path.write_text("# Report\n\n[Details](details.md)", encoding="utf-8")
    path.with_name("details.md").write_text("# Details", encoding="utf-8")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    request = MagicMock(
        side_effect=[
            {"agents": {"code": ["studio"], "analyze": []}},
            {"agents": {"code": ["studio"], "analyze": []}},
            {"status": "launched", "pane_id": "terminal_18"},
        ]
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    async def exercise(pilot, app):
        await _wait_viewer(pilot, lambda: app.screen.active_bindings["c"].enabled)
        await pilot.click(Markdown, offset=(2, 4))
        status = app.query_one("#status", Static)
        await _wait_viewer(pilot, lambda: "no GitHub item" in status.render().plain)
        assert app.query_one(Markdown).source == "# Details"
        await pilot.press("c", "d")
        request.assert_called_once_with("show_config", timeout=2)
        await pilot.press("b")
        await _wait_viewer(pilot, lambda: app.screen.active_bindings["c"].enabled)
        assert app.query_one(Markdown).source == path.read_text()
        await pilot.press("c", "enter")
        await _wait_viewer(pilot, lambda: "terminal_18" in status.render().plain)

    _run_viewer(monkeypatch, exercise)
    assert main(["view", str(path), "--target", ISSUE_URL]) == 0
    assert request.call_args_list[2:] == [call("code", {"target": ISSUE_URL, "agent": "studio"})]


@pytest.mark.parametrize("replaced", [False, True], ids=["wrong-session", "replaced-server"])
def test_viewer_refuses_launch_into_another_zellij_session(tmp_path, monkeypatch, replaced):
    path = tmp_path / "report.md"
    path.write_text("# Report")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "viewer-session")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8765")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    server_session = "viewer-session" if replaced else "other-session"

    def respond(endpoint, payload=None, **kwargs):
        if endpoint == "info":
            return {"session": server_session}
        assert endpoint == "show_config", "viewer must not send launch requests to another session"
        return {"agents": {"code": ["studio"], "analyze": ["studio"]}}

    monkeypatch.setattr(WorkdashControlClient, "request", MagicMock(side_effect=respond))

    async def exercise(pilot, app):
        nonlocal server_session
        status = app.query_one("#status", Static)
        if replaced:
            await _wait_viewer(pilot, lambda: app.screen.active_bindings["c"].enabled)
            await pilot.press("c")
            server_session = "other-session"
            await pilot.press("enter")
        await _wait_viewer(pilot, lambda: "other-session" in status.render().plain)
        assert all(not app.screen.active_bindings[key].enabled for key in "cd")
        await pilot.press("c", "d")
        assert app.query_one(Markdown).source == "# Report"

    _run_viewer(monkeypatch, exercise)
    monkeypatch.setattr(view, "load_config", WorkdashConfig)
    assert view.run_view(str(path), item_target=ISSUE_URL) == 0


@pytest.mark.parametrize("local", [True, False], ids=["local-lan", "remote-lan"])
def test_viewer_launch_requires_local_address_in_zellij(tmp_path, monkeypatch, local):
    path = tmp_path / "report.md"
    path.write_text("# Report")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "viewer-session")
    monkeypatch.setenv("WORKDASH_HOST", "192.0.2.5:8765")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    bind = MagicMock(side_effect=None if local else OSError("Cannot assign requested address"))
    probe = MagicMock()
    probe.__enter__.return_value.bind = bind
    monkeypatch.setattr(
        view,
        "socket",
        SimpleNamespace(AF_INET=2, socket=MagicMock(return_value=probe)),
        raising=False,
    )
    request = MagicMock(
        side_effect=[
            {"session": "viewer-session"},
            {"agents": {"code": ["studio"], "analyze": ["studio"]}},
            {"session": "viewer-session"},
            {"status": "launched", "pane_id": "terminal_31"},
        ]
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    async def exercise(pilot, app):
        status = app.query_one("#status", Static)
        if local:
            await _wait_viewer(pilot, lambda: app.screen.active_bindings["c"].enabled)
            await pilot.press("c", "enter")
            await _wait_viewer(pilot, lambda: "terminal_31" in status.render().plain)
            assert request.call_args_list[-1] == call(
                "code", {"target": ISSUE_URL, "agent": "studio"}
            )
        else:
            await _wait_viewer(pilot, lambda: "requires a local" in status.render().plain)
            assert not app.screen.active_bindings["c"].enabled
            request.assert_not_called()

    _run_viewer(monkeypatch, exercise)
    monkeypatch.setattr(view, "load_config", WorkdashConfig)
    assert view.run_view(str(path), item_target=ISSUE_URL) == 0
    bind.assert_called_once_with(("192.0.2.5", 0))


def test_busy_viewer_server_recovers_without_navigation(tmp_path, monkeypatch):
    path = tmp_path / "report.md"
    path.write_text("# Report")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    request = MagicMock(
        side_effect=[
            WorkdashControlError("server_unresponsive", "The server did not answer."),
            WorkdashControlError("server_unresponsive", "The server did not answer."),
            {"agents": {"code": ["studio"], "analyze": ["studio"]}},
        ]
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)

    async def exercise(pilot, app):
        await _wait_viewer(
            pilot, lambda: all(app.screen.active_bindings[key].enabled for key in "cd")
        )
        await pilot.press("d")
        assert _menu_agents(app) == ["studio"]

    _run_viewer(monkeypatch, exercise)
    monkeypatch.setattr(view, "load_config", WorkdashConfig)
    assert view.run_view(str(path), item_target=ISSUE_URL) == 0


@pytest.mark.parametrize("timeout_at", ["info", "code"], ids=["preflight", "launch-reply"])
def test_launch_timeout_recovers_controls_without_relaunch(tmp_path, monkeypatch, timeout_at):
    path = tmp_path / "report.md"
    path.write_text("# Report")
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "viewer-session")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8765")
    monkeypatch.setattr(view, "load_config", WorkdashConfig)
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    session = {"session": "viewer-session"}
    config = {"agents": {"code": ["studio"], "analyze": ["studio"]}}
    timeout = WorkdashControlError("server_unresponsive", "The server did not answer.")
    request = MagicMock(
        side_effect=[
            session,
            config,
            *([] if timeout_at == "info" else [session]),
            timeout,
            session,
            config,
            session,
            {"status": "launched", "pane_id": "terminal_31"},
        ]
    )
    monkeypatch.setattr(WorkdashControlClient, "request", request)
    launch = call("code", {"target": ISSUE_URL, "agent": "studio"})

    async def exercise(pilot, app):
        status = app.query_one("#status", Static)
        await _wait_viewer(pilot, lambda: app.screen.active_bindings["c"].enabled)
        await pilot.press("c", "enter")
        await _wait_viewer(pilot, lambda: "code failed" in status.render().plain)
        await _wait_viewer(
            pilot, lambda: all(app.screen.active_bindings[key].enabled for key in "cd")
        )
        assert request.call_args_list.count(launch) == (timeout_at == "code")
        await pilot.press("c", "enter")
        await _wait_viewer(pilot, lambda: "terminal_31" in status.render().plain)
        assert request.call_args_list.count(launch) == 1 + (timeout_at == "code")

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path), item_target=ISSUE_URL) == 0


def test_viewer_reports_launch_result_after_following_a_link(tmp_path, monkeypatch):
    path = tmp_path / "report.md"
    path.write_text("# Report\n\n[Details](details.md)")
    path.with_name("details.md").write_text("# Details")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))
    release = Event()

    def respond(endpoint, payload=None, **kwargs):
        if endpoint == "show_config":
            return {"agents": {"code": ["studio"], "analyze": ["studio"]}}
        assert endpoint == "autodispatch"
        assert payload == {"scope": ISSUE_URL, "agent": "studio"}
        assert release.wait(4)
        return {"id": "dispatch-1", "status": "running"}

    monkeypatch.setattr(WorkdashControlClient, "request", MagicMock(side_effect=respond))

    async def exercise(pilot, app):
        try:
            await _wait_viewer(pilot, lambda: app.screen.active_bindings["d"].enabled)
            await pilot.press("d", "enter")
            await pilot.click(Markdown, offset=(2, 4))
            await _wait_viewer(pilot, lambda: app.query_one(Markdown).source == "# Details")
        finally:
            release.set()
        await _wait_viewer(
            pilot,
            lambda: any(
                "Autodispatch dispatch-1: running" in toast.render().plain
                for toast in app.query("Toast")
            ),
        )
        assert "no GitHub item" in app.query_one("#status", Static).render().plain

    _run_viewer(monkeypatch, exercise)
    monkeypatch.setattr(view, "load_config", WorkdashConfig)
    assert view.run_view(str(path), item_target=ISSUE_URL) == 0


async def _wait_viewer(pilot, condition):
    async with asyncio.timeout(4):
        while not condition():
            await pilot.pause(0.01)


def _menu_agents(app):
    options = app.screen.query_one(OptionList)
    return [str(options.get_option_at_index(index).prompt) for index in range(options.option_count)]
