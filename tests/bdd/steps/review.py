"""Autonomous review scenarios at external boundaries."""

import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pytest_bdd import given, then, when

import workdash.control as control
import workdash.review as review
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType


@pytest.fixture
def review_state(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    now = datetime.now(UTC)
    issue = WorkItem(
        WorkItemKind.TRACKED_ISSUE,
        WorkItemType.ISSUE,
        "owner/repo",
        1,
        "Issue",
        now,
        now,
        "https://github.com/owner/repo/issues/1",
    )
    pr = WorkItem(
        WorkItemKind.TRACKED_PR,
        WorkItemType.PR,
        "owner/repo",
        2,
        "PR",
        now,
        now,
        "https://github.com/owner/repo/pull/2",
    )
    backend = MagicMock()
    backend.resolve_analyze_command_tokens.return_value = ["agent", "-p"]
    session = control.WorkdashSession(
        config=WorkdashConfig(
            github_username="me", agents={"agent": AgentConfig(analyze="agent -p")}
        ),
        backend=backend,
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session=None,
    )
    monkeypatch.setattr(control, "ensure_worktree", lambda *args: str(tmp_path))
    monkeypatch.setattr(
        control.GithubHelper,
        "fetch_launch_context",
        lambda *args: {"state": "OPEN", "author": {"login": "someone"}},
    )

    def run(self, *, item, prompt, command_tokens, cwd):
        path = re.search(r"completion document at (\S+)", prompt).group(1)
        Path(path).write_text("Outcome: approved\nInline comments: none")
        return ""

    original_run = review.Analyzer._run_analysis_command
    monkeypatch.setattr(review.Analyzer, "_run_analysis_command", run)
    return {"session": session, "original_run": original_run, "tmp_path": tmp_path}


@given("an open pull request is available for review")
def pr_available(review_state):
    pass


@when("the reviewer requests an autonomous review")
def review_pr(review_state):
    review_state["result"] = review_state["session"].review(
        target="owner/repo#PR-2", background=True
    )


@then("the reviewer is told the review started and where its document will be written")
def review_started(review_state):
    assert review_state["result"]["status"] == "started"
    assert (
        Path(review_state["result"]["document_path"]).parent
        == review_state["tmp_path"] / ".workdash" / "reviews"
    )


@given("an issue has no linked open pull request")
def no_pr(review_state, monkeypatch):
    monkeypatch.setattr(control, "linked_open_pull_request", lambda item: None)


@when("the reviewer requests an autonomous review of the issue")
def review_issue(review_state):
    review_state["result"] = review_state["session"].review(target="owner/repo#1")


@then("the reviewer is told there is nothing to review")
def nothing(review_state):
    assert review_state["result"]["status"] == "nothing_to_review"


@given("an open pull request review is in progress")
def running_review(review_state, monkeypatch):
    import socket
    import sys
    import threading
    import time

    tmp_path = review_state["tmp_path"]
    started, release, posted = (tmp_path / name for name in ("started", "release", "posted"))
    script = (
        "import pathlib, re, sys, time\n"
        "started, release, posted = map(pathlib.Path, sys.argv[1:4])\n"
        "started.touch()\n"
        "while not release.exists(): time.sleep(0.01)\n"
        "posted.touch()\n"
        "path = re.search(r'completion document at (\\S+)', sys.argv[4]).group(1)\n"
        "pathlib.Path(path).write_text('Outcome: approved\\nInline comments: none')\n"
    )
    review_state["session"].backend.resolve_analyze_command_tokens.return_value = [
        sys.executable,
        "-c",
        script,
        str(started),
        str(release),
        str(posted),
    ]
    monkeypatch.setattr(review.Analyzer, "_run_analysis_command", review_state["original_run"])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    server = control.WorkdashControlServer(review_state["session"])
    server.start()
    review_state["server"] = server
    result = []

    def request_review():
        try:
            result.append(
                control.WorkdashControlClient().request("review", {"target": "owner/repo#PR-2"})
            )
        except control.WorkdashControlError as error:
            result.append(error)

    thread = threading.Thread(target=request_review)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.exists()
        thread.join(2)
        assert not thread.is_alive()
        review_state.update(release=release, posted=posted, result=result)
        yield
    finally:
        release.touch()
        thread.join(10)
        server.stop()
    assert not thread.is_alive()
    assert result[0]["status"] == "started"


@when("the reviewer lists work items")
def list_during_review(review_state):
    import json
    from urllib.request import Request, urlopen

    request = Request(
        f"http://{control.server_address()[0]}:{control.server_address()[1]}/api/v0/list",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=2) as response:
        review_state["list"] = json.load(response)["result"]


@then("the list is returned before the review finishes")
def list_returned(review_state):
    assert {item["id"] for item in review_state["list"]["items"]} == {
        "owner/repo#ISSUE-1",
        "owner/repo#CHECK-2",
    }


@when("the reviewer requests the same review again")
def repeat_review(review_state):
    review_state["repeat"] = control.WorkdashControlClient().request(
        "review", {"target": "owner/repo#PR-2"}
    )


@then("the reviewer receives the same started information")
def same_review(review_state):
    assert review_state["repeat"] == review_state["result"][0]
