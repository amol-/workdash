import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import workdash.agent_execution as agent_execution
import workdash.control as control
import workdash.github_client as github_client
from workdash.config import AgentConfig, WorkdashConfig
from workdash.models import WorkItem, WorkItemKind, WorkItemType


@pytest.fixture
def setup_review(monkeypatch, tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    now = datetime.now(UTC)
    issue = WorkItem(
        WorkItemKind.TRACKED_ISSUE,
        WorkItemType.ISSUE,
        "owner/repo",
        1,
        "issue",
        now,
        now,
        "https://github.com/owner/repo/issues/1",
    )
    pr = WorkItem(
        WorkItemKind.TRACKED_PR,
        WorkItemType.PR,
        "owner/repo",
        2,
        "pr",
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
    monkeypatch.setattr(control, "ensure_worktree", lambda *args, **kwargs: str(tmp_path))
    backend.github_client.fetch_launch_context.return_value = {
        "state": "OPEN",
        "author": {"login": "me"},
    }
    backend.github_client.linked_open_pull_request.return_value = ("owner/repo", 2)
    return session, tmp_path


def test_issue_resolves_pr_and_retries_with_existing_threads(setup_review, monkeypatch):
    session, tmp_path = setup_review
    prompts = []

    def run(*, item, prompt, command_tokens, cwd):
        assert item.number == 2 and command_tokens == ["agent", "-p"] and cwd == str(tmp_path)
        prompts.append(prompt)
        if len(prompts) == 2:
            Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
                "Outcome: comment-only review\nInline comments: none"
            )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    result = session.review(target="owner/repo#1")
    assert result["status"] == "reviewed" and "comments: none" in result["content"]
    assert "authenticated GitHub identity" in prompts[0]
    assert "gh api user" in prompts[0]
    assert "Check existing review threads" in prompts[1]
    assert "inline review comments" in prompts[0]


def test_issue_finds_open_pr_after_first_page(setup_review, monkeypatch):
    session, _ = setup_review
    queries = []

    def graphql(command, **kwargs):
        query = command[-1].removeprefix("query=")
        queries.append(query)
        if len(queries) == 1:
            connection = {
                "nodes": [
                    {"number": n, "state": "CLOSED", "repository": {"nameWithOwner": "owner/repo"}}
                    for n in range(100)
                ],
                "pageInfo": {"hasNextPage": True, "endCursor": "next-page"},
            }
        else:
            assert 'after: "next-page"' in query
            connection = {
                "nodes": [
                    {"number": 2, "state": "OPEN", "repository": {"nameWithOwner": "owner/repo"}}
                ],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        data = {"repository": {"issue": {"closedByPullRequestsReferences": connection}}}
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"data": data}))

    def run(*, item, prompt, command_tokens, cwd):
        assert item.number == 2
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(github_client, "_run_gh_command_with_retry", graphql)
    session.backend.github_client.linked_open_pull_request.side_effect = (
        github_client.GitHubClient().linked_open_pull_request
    )
    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    assert session.review(target="owner/repo#1")["item_id"] == "owner/repo#PR-2"
    assert len(queries) == 2


def test_review_runs_with_dirty_worktree_and_live_coding_pane(setup_review, monkeypatch):
    session, worktree = setup_review
    (worktree / "uncommitted.txt").write_text("local work")
    session.zellij_session = "workdash-main"
    panes = [{"id": 5, "title": f"code_{worktree.name}", "pane_cwd": str(worktree)}]
    assert control._agent_panes_in_worktree(panes, str(worktree)) == panes
    pane_lookup = MagicMock(return_value=panes)
    monkeypatch.setattr(session, "_live_panes", pane_lookup)
    claim = MagicMock()
    monkeypatch.setattr(session, "_claimed_launch", claim)

    def run(*, item, prompt, command_tokens, cwd):
        assert (Path(cwd) / "uncommitted.txt").read_text() == "local work"
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    result = session.review(target="owner/repo#PR-2")
    assert result["status"] == "reviewed"
    assert result["content"] == "Outcome: approved\nInline comments: none"
    pane_lookup.assert_not_called()
    claim.assert_not_called()


def test_issue_resolves_pr_not_on_dashboard(setup_review, monkeypatch):
    session, _ = setup_review
    issue, pr = session.work_items
    session.work_items = [issue]
    session.backend.github_client.fetch_item.return_value = pr

    def run(*, item, prompt, command_tokens, cwd):
        assert item is pr
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    result = session.review(target="owner/repo#1")
    assert result["item_id"] == "owner/repo#PR-2"
    assert result["content"] == "Outcome: approved\nInline comments: none"
    session.backend.github_client.fetch_item.assert_called_once_with(
        "owner/repo", 2, github_username="me", todo_repository=session.config.todo_repository
    )
    assert session.work_items == [issue]


def test_missing_document_fails_after_one_retry(setup_review, monkeypatch):
    session, tmp_path = setup_review
    (tmp_path / ".workdash/reviews").mkdir(parents=True)
    (tmp_path / ".workdash/reviews/old.md").write_text("user notes")
    (tmp_path / ".workdash/reviews/review-0123456789abcdef0123456789abcdef.md").write_text(
        "old review"
    )
    (tmp_path / ".workdash/reviews/review-notes.md").write_text("user review notes")
    run = MagicMock(return_value="")
    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    with pytest.raises(RuntimeError, match="missing or empty after retry"):
        session.review(target="owner/repo#PR-2")
    assert run.call_count == 2
    assert not (tmp_path / ".workdash/reviews/review-0123456789abcdef0123456789abcdef.md").exists()
    assert (tmp_path / ".workdash/reviews/review-notes.md").read_text() == "user review notes"
    assert (tmp_path / ".workdash/reviews/old.md").read_text() == "user notes"


@pytest.mark.parametrize("content", ["reviewed", "Outcome: approved", " "])
def test_any_nonempty_document_succeeds(setup_review, monkeypatch, content):
    session, _ = setup_review
    attempts = []

    def run(*, item, prompt, command_tokens, cwd):
        attempts.append(prompt)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(content)
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    assert session.review(target="owner/repo#PR-2")["content"] == content
    assert len(attempts) == 1


@pytest.mark.parametrize("eventually_succeeds", [False, True])
def test_empty_document_retries(setup_review, monkeypatch, eventually_succeeds):
    session, _ = setup_review
    attempts = []

    def run(*, item, prompt, command_tokens, cwd):
        attempts.append(prompt)
        content = "reviewed" if eventually_succeeds and len(attempts) == 2 else ""
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(content)
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    if eventually_succeeds:
        assert session.review(target="owner/repo#PR-2")["content"] == "reviewed"
    else:
        with pytest.raises(RuntimeError, match="missing or empty"):
            session.review(target="owner/repo#PR-2")
    assert len(attempts) == 2
    assert "Check existing review threads" in attempts[1]


def test_agent_failure_is_not_retried(setup_review, monkeypatch):
    session, _ = setup_review
    context_files = []

    def run(*, item, prompt, command_tokens, cwd):
        context_files.append(Path(re.search(r"GitHub context file: (.+)", prompt).group(1)))
        assert context_files[0].is_file()
        raise RuntimeError("agent failed")

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    with pytest.raises(RuntimeError, match="agent failed"):
        session.review(target="owner/repo#PR-2")
    assert len(context_files) == 1 and not context_files[0].exists()

    def succeed(*, item, prompt, command_tokens, cwd):
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=succeed))
    assert session.review(target="owner/repo#PR-2")["status"] == "reviewed"


def test_other_authors_get_formal_verdict_instructions(setup_review, monkeypatch):
    session, _ = setup_review
    session.backend.github_client.fetch_launch_context.return_value = {
        "state": "OPEN",
        "author": {"login": "other"},
    }
    prompts = []

    def run(*, item, prompt, command_tokens, cwd):
        prompts.append(prompt)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    session.review(target="owner/repo#PR-2")
    assert "changes-requested review" in prompts[0] and "approved review" in prompts[0]


def test_cli_routes_review_to_server(monkeypatch, capsys):
    import workdash.workdash as cli

    request = MagicMock(
        return_value={"status": "nothing_to_review", "item_id": "owner/repo#ISSUE-1"}
    )
    monkeypatch.setattr(cli.WorkdashControlClient, "request", request)
    assert cli.main(["review", "owner/repo#1", "--agent", "agent", "--host", "localhost"]) == 0
    request.assert_called_once_with("review", {"target": "owner/repo#1", "agent": "agent"})
    assert "No open pull request" in capsys.readouterr().out


def test_cli_reports_review_document_path(monkeypatch, capsys):
    import workdash.workdash as cli

    monkeypatch.setattr(
        cli.WorkdashControlClient,
        "request",
        MagicMock(
            return_value={
                "status": "started",
                "item_id": "owner/repo#PR-2",
                "agent": "agent",
                "document_path": "/server/worktree/.workdash/reviews/review-123.md",
            }
        ),
    )
    assert cli.main(["review", "owner/repo#PR-2"]) == 0
    assert "/server/worktree/.workdash/reviews/review-123.md" in capsys.readouterr().out


def test_one_shot_command_gets_prompt_as_final_shell_argument(setup_review):
    session, worktree = setup_review
    script = worktree / "agent with spaces"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, re, sys\n"
        "assert sys.argv[1] == '-p' and len(sys.argv) == 3\n"
        "prompt = sys.argv[-1]\n"
        "with pathlib.Path('agent-cwd').open('a') as f: f.write(str(pathlib.Path.cwd()) + '\\n')\n"
        "path = pathlib.Path(re.search(r'completion document at (\\S+)', prompt).group(1))\n"
        "path.write_text('Outcome: comment-only review\\nInline comments: none')\n"
    )
    script.chmod(0o755)
    session.backend.resolve_analyze_command_tokens.return_value = [str(script), "-p"]
    result = session.review(target="owner/repo#PR-2")
    assert result["status"] == "reviewed"
    assert (worktree / "agent-cwd").read_text().splitlines() == [str(worktree)]


def test_issue_without_pr_needs_no_agent(setup_review, monkeypatch):
    session, _ = setup_review
    session.config.agents.clear()
    session.backend.github_client.linked_open_pull_request.return_value = None
    assert session.review(target="owner/repo#1")["status"] == "nothing_to_review"


def test_open_pr_still_requires_agent(setup_review):
    session, _ = setup_review
    session.config.agents.clear()
    with pytest.raises(control.WorkdashControlError, match="No analysis agents"):
        session.review(target="owner/repo#PR-2")


def test_issue_with_only_closed_pr_reports_nothing(setup_review, monkeypatch):
    session, _ = setup_review
    data = {
        "repository": {
            "issue": {
                "closedByPullRequestsReferences": {
                    "nodes": [
                        {
                            "number": 2,
                            "state": "CLOSED",
                            "repository": {"nameWithOwner": "owner/repo"},
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }
    }
    monkeypatch.setattr(
        github_client,
        "_run_gh_command_with_retry",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=json.dumps({"data": data})
        ),
    )
    session.backend.github_client.linked_open_pull_request.side_effect = (
        github_client.GitHubClient().linked_open_pull_request
    )
    assert session.review(target="owner/repo#1")["status"] == "nothing_to_review"


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_closed_pr_needs_no_worktree(setup_review, monkeypatch, state):
    session, _ = setup_review
    session.config.agents.clear()
    session.backend.github_client.fetch_launch_context.return_value = {"state": state}
    worktree = MagicMock(side_effect=RuntimeError("head unavailable"))
    monkeypatch.setattr(control, "ensure_worktree", worktree)
    result = session.review(target="owner/repo#PR-2")
    assert result["status"] == "nothing_to_review"
    worktree.assert_not_called()


def test_review_prompt_instructs_agent_how_to_submit(setup_review, monkeypatch):
    session, _ = setup_review
    prompts = []

    def run(*, item, prompt, command_tokens, cwd):
        prompts.append(prompt)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    session.review(target="owner/repo#PR-2")
    prompt = prompts[0]
    assert (
        "Post findings as inline review comments on the pull request, not conversation comments"
        in prompt
    )
    assert "authenticated GitHub identity used to submit the review" in prompt
    assert "compare it case-insensitively with the pull request author" in prompt
    assert (
        "If they match, submit a comment-only review; do not claim approval or request changes"
        in prompt
    )
    assert (
        "submit a changes-requested review for major findings, or an approved review if there are none"
        in prompt
    )
    assert "If the identity cannot be determined, do not submit a formal verdict" in prompt
    assert (
        "summarizing the outcome and explicitly listing every inline review comment posted"
        in prompt
    )
    assert "or stating that none were posted" in prompt
    assert "Use separate lines" not in prompt


def test_review_documents_are_not_staged_in_linked_worktree(setup_review, monkeypatch, tmp_path):
    session, repo = setup_review
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    worktree = tmp_path.parent / (tmp_path.name + "-worktree")
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-qb", "review", str(worktree)], check=True
    )
    monkeypatch.setattr(control, "ensure_worktree", lambda *args, **kwargs: str(worktree))
    (worktree / "real-change.txt").write_text("change")
    contents = [
        "Outcome: changes requested\nInline comments: fix the race",
        "Outcome: approved\nInline comments: none",
    ]
    paths = []

    def run(*, item, prompt, command_tokens, cwd):
        path = Path(re.search(r"completion document at (\S+)", prompt).group(1))
        paths.append(path)
        path.write_text(contents[len(paths) - 1])
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    results = [session.review(target="owner/repo#PR-2") for _ in range(2)]
    assert [result["content"] for result in results] == contents
    assert [result["document_path"] for result in results] == [str(path) for path in paths]
    assert paths[0] != paths[1] and not paths[0].exists() and paths[1].is_file()
    (worktree / ".workdash/reviews/notes.md").write_text("user notes")
    subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
    staged = subprocess.check_output(
        ["git", "-C", str(worktree), "diff", "--cached", "--name-only"], text=True
    ).splitlines()
    assert staged == [".workdash/reviews/notes.md", "real-change.txt"]


def test_selected_agent_runs_in_prepared_pr_worktree(setup_review, monkeypatch, tmp_path):
    from workdash.repo_worktree import ensure_worktree

    session, source = setup_review
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=t@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "initial",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "branch", "feature"], check=True)
    origin = tmp_path / "owner" / "repo.git"
    origin.parent.mkdir()
    subprocess.run(["git", "clone", "-q", "--bare", str(source), str(origin)], check=True)
    workdir = tmp_path / "work"
    workdir.mkdir()
    clone = workdir / "owner_repo"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "remote",
            "set-url",
            "origin",
            "https://github.com/owner/repo.git",
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "config",
            f"url.{origin}.insteadOf",
            "https://github.com/owner/repo.git",
        ],
        check=True,
    )
    script = tmp_path / "agent"
    script.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, re, subprocess, sys\n"
        "prompt = sys.argv[-1]\n"
        "assert 'https://github.com/owner/repo/pull/2' in prompt\n"
        "branch = subprocess.check_output(['git', 'branch', '--show-current'], text=True).strip()\n"
        "pathlib.Path('agent-ran').write_text(str(pathlib.Path.cwd()) + '\\n' + branch)\n"
        "path = pathlib.Path(re.search(r'completion document at (\\S+)', prompt).group(1))\n"
        "path.write_text('Outcome: changes requested\\nInline comments: fix the race')\n"
    )
    script.chmod(0o755)
    session.config = WorkdashConfig(
        github_username="me",
        workdir=str(workdir),
        agents={
            "default": AgentConfig(analyze="false"),
            "selected": AgentConfig(analyze=str(script)),
        },
    )
    session.backend.resolve_analyze_command_tokens.side_effect = (
        session.config.analyze_agent_command_tokens
    )
    monkeypatch.setattr(control, "ensure_worktree", ensure_worktree)
    session.backend.github_client.fetch_worktree_head.return_value = ("feature", "owner/repo")

    result = session.review(target="owner/repo#PR-2", agent="selected")
    cwd = Path(result["document_path"]).parent.parent.parent
    assert (cwd / "agent-ran").read_text().splitlines() == [str(cwd), "feature"]
    assert cwd != workdir / "owner_repo"
    assert result["agent"] == "selected"
    assert result["content"] == "Outcome: changes requested\nInline comments: fix the race"


def test_real_agent_failure_after_writing_document_is_not_retried(setup_review, tmp_path):
    session, _ = setup_review
    script = tmp_path / "agent.py"
    script.write_text(
        "import pathlib, re, sys\n"
        "path = pathlib.Path(re.search(r'completion document at (\\S+)', sys.argv[-1]).group(1))\n"
        "path.write_text('Outcome: failed')\n"
        "with pathlib.Path('attempts').open('a') as f: f.write('attempt\\n')\n"
        "sys.exit(7)\n"
    )
    session.backend.resolve_analyze_command_tokens.return_value = [sys.executable, str(script)]
    with pytest.raises(RuntimeError, match="Failed to generate analysis"):
        session.review(target="owner/repo#PR-2")
    assert (tmp_path / "attempts").read_text().splitlines() == ["attempt"]
    assert [path.read_text() for path in (tmp_path / ".workdash/reviews").glob("review-*.md")] == [
        "Outcome: failed"
    ]


def test_large_context_and_retry_output_reach_real_agent(setup_review, monkeypatch, tmp_path):
    import sys

    session, _ = setup_review
    body = "context-marker-" + "x" * 200_000
    session.backend.github_client.fetch_launch_context.return_value = {
        "state": "OPEN",
        "body": body,
    }
    script = tmp_path / "agent.py"
    script.write_text(
        """import re, sys
from pathlib import Path
prompt = sys.argv[-1]
context = Path(re.search(r"GitHub context file: (.+)", prompt).group(1))
assert "context-marker-" + "x" * 200_000 in context.read_text()
assert "untrusted" in prompt.lower()
paths = Path("agent-paths")
if not paths.exists():
    paths.write_text(str(context))
    print("output-marker-" + "y" * 200_000)
else:
    previous = Path(re.search(r"Previous agent output file: (.+)", prompt).group(1))
    assert "output-marker-" + "y" * 200_000 in previous.read_text()
    paths.write_text(paths.read_text() + "\\n" + str(previous))
    assert "Check existing review threads" in prompt
    document = Path(re.search(r"completion document at (\\S+)", prompt).group(1))
    document.write_text("Outcome: comment-only review\\nInline comments: none")
"""
    )
    session.backend.resolve_analyze_command_tokens.return_value = [sys.executable, str(script)]
    result = session.review(target="owner/repo#PR-2")
    assert result["content"] == "Outcome: comment-only review\nInline comments: none"
    assert all(
        not Path(path).exists() for path in (tmp_path / "agent-paths").read_text().splitlines()
    )


def test_concurrent_reviews_reuse_same_pr_but_allow_other_pr(setup_review, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    from webob import Request

    session, _ = setup_review
    pr = session.work_items[1]

    session.work_items.append(replace(pr, number=3, url="https://github.com/owner/repo/pull/3"))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def run(*, item, prompt, command_tokens, cwd):
        calls.append(item.number)
        if item.number == 2:
            started.set()
            assert release.wait(5)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.review, target="owner/repo#1")
        try:
            assert started.wait(5)
            request = Request.blank(
                "/api/v0/review",
                method="POST",
                content_type="application/json",
                body=b'{"target":"owner/repo#PR-2"}',
            )
            response = request.get_response(control._make_turbogears_app(session))
            assert response.status_int == 200
            assert response.json["result"]["status"] == "started"
            assert (
                pool.submit(session.review, target="owner/repo#PR-3").result(5)["status"]
                == "reviewed"
            )
        finally:
            release.set()
        assert first.result(5)["status"] == "reviewed"
    assert calls == [2, 3]
    assert session.review(target="owner/repo#PR-2")["status"] == "reviewed"


def test_new_review_preserves_active_review_document(setup_review, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace

    session, _ = setup_review
    session.work_items.append(replace(session.work_items[1], number=3))
    running, release = threading.Event(), threading.Event()
    paths = {}

    def run(*, item, prompt, command_tokens, cwd):
        path = Path(re.search(r"completion document at (\S+)", prompt).group(1))
        paths[item.number] = path
        path.write_text(f"review {item.number}")
        if item.number == 2:
            running.set()
            assert release.wait(5)
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.review, target="owner/repo#PR-2")
        try:
            assert running.wait(5)
            assert session.review(target="owner/repo#PR-3")["content"] == "review 3"
            assert paths[2].read_text() == "review 2"
        finally:
            release.set()
        assert first.result(5)["content"] == "review 2"


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("background", [False, True])
def test_synchronous_review_joins_running_review(setup_review, monkeypatch, failure, background):
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    session, _ = setup_review
    running, release = threading.Event(), threading.Event()
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        running.set()
        assert release.wait(5)
        if failure:
            raise RuntimeError("review failed")
        return {
            "status": "reviewed",
            "item_id": "owner/repo#PR-2",
            "document_path": str(kwargs["document"]),
            "content": "done",
        }

    monkeypatch.setattr(control, "review_pull_request", run)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session.review, target="owner/repo#PR-2", background=background)
        try:
            assert running.wait(5)
            started = session.review(target="owner/repo#PR-2", background=True)
            assert started == session.review(target="owner/repo#PR-2", background=True)
            if background:
                assert first.result(5) == started
            joined = pool.submit(session.review, target="owner/repo#PR-2")
            with pytest.raises(TimeoutError):
                joined.result(0.1)
        finally:
            release.set()
        if failure:
            with pytest.raises(RuntimeError, match="review failed"):
                joined.result(5)
            if not background:
                with pytest.raises(RuntimeError, match="review failed"):
                    first.result(5)
        else:
            result = joined.result(5)
            assert result == {**started, "status": "reviewed", "content": "done"}
            if not background:
                assert first.result(5) == result
    assert calls == [1]


def test_review_api_returns_while_agent_is_blocked(setup_review, monkeypatch):
    import json
    import threading

    from webob import Request

    session, _ = setup_review
    started, release, completed = threading.Event(), threading.Event(), threading.Event()

    def run(*, item, prompt, command_tokens, cwd):
        started.set()
        assert release.wait(5)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        completed.set()
        return ""

    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    request = Request.blank(
        "/api/v0/review",
        method="POST",
        content_type="application/json",
        body=json.dumps({"target": "owner/repo#PR-2"}).encode(),
    )
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            request.get_response(control._make_turbogears_app(session)).json
        )
    )
    thread.start()
    try:
        assert started.wait(3)
        thread.join(1)
        assert not thread.is_alive(), "review request waited for the agent"
        assert result[0]["result"]["status"] == "started"
        document = Path(result[0]["result"]["document_path"])
    finally:
        release.set()
        thread.join(5)
    assert completed.wait(5)
    assert document.read_text() == "Outcome: approved\nInline comments: none"


def test_background_review_failure_is_logged(setup_review, monkeypatch, caplog):
    import threading

    session, _ = setup_review
    finished = threading.Event()

    def fail(*args, **kwargs):
        raise RuntimeError("agent failed")

    original = control.logger.error

    def log_failure(*args, **kwargs):
        original(*args, **kwargs)
        finished.set()

    monkeypatch.setattr(control, "review_pull_request", fail)
    monkeypatch.setattr(control.logger, "error", log_failure)
    result = session.review(target="owner/repo#PR-2", background=True)
    assert result["status"] == "started"
    assert finished.wait(3)
    assert "Review failed for owner/repo#2" in caplog.text
    assert "RuntimeError: agent failed" in caplog.text


def test_real_server_cli_returns_while_agent_is_blocked(setup_review, monkeypatch, capsys):
    import socket
    import threading
    from concurrent.futures import ThreadPoolExecutor

    import workdash.workdash as cli

    session, worktree = setup_review
    running, release, finished = (threading.Event() for _ in range(3))
    calls = []
    log_info = control.logger.info

    def completed(*args, **kwargs):
        log_info(*args, **kwargs)
        finished.set()

    def run(*, item, prompt, command_tokens, cwd):
        calls.append(item.number)
        running.set()
        assert release.wait(10)
        Path(re.search(r"completion document at (\S+)", prompt).group(1)).write_text(
            "Outcome: approved\nInline comments: none"
        )
        return ""

    monkeypatch.setattr(control.logger, "info", completed)
    monkeypatch.setattr(agent_execution, "executor", MagicMock(analyze=run))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("WORKDASH_HOST", f"127.0.0.1:{port}")
    server = control.WorkdashControlServer(session)
    server.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(cli.main, ["review", "owner/repo#PR-2", "--json"])
            assert running.wait(3)
            assert first.result(timeout=2) == 0
            import json

            started = json.loads(capsys.readouterr().out)
            assert started["item_id"] == "owner/repo#PR-2"
            assert started["agent"] == "agent"
            document = Path(started["document_path"])
            assert document.parent == worktree / ".workdash/reviews"
            client = control.WorkdashControlClient()
            assert pool.submit(client.request, "list", {}).result(timeout=2)["items"]
            repeat = pool.submit(client.request, "review", {"target": "owner/repo#1"}).result(
                timeout=2
            )
            assert repeat == started
            assert calls == [2]
            release.set()
            assert finished.wait(3)
        finished.clear()
        new_review = client.request("review", {"target": "owner/repo#PR-2"})
        assert new_review["status"] == "started"
        assert new_review["document_path"] != started["document_path"]
        assert finished.wait(3)
    finally:
        release.set()
        server.stop()
    assert calls == [2, 2]
    assert not document.exists()
