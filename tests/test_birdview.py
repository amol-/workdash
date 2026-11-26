"""Birdview reports available work without performing fresh lookups."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from bdd.steps.common import make_valid_config, make_work_item
from workdash.control import WorkdashSession
from workdash.models import WorkItemKind, WorkItemType


def test_snapshot_reports_loaded_work_without_fetching(monkeypatch, tmp_path):
    issue = replace(make_work_item(url="https://github.com/owner/repo/issues/1"), sub_issue_total=3)
    pr = replace(
        issue,
        number=2,
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        url="https://github.com/owner/repo/pull/2",
        review_decision="APPROVED",
        has_unresolved_comments=True,
    )
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr(
        session,
        "autodispatch_sessions",
        lambda: [
            {
                "status": "running",
                "scope": issue.url,
                "current_item": "owner/repo#ISSUE-1",
                "pane_id": "terminal_3",
                "stage": "developing",
                "agent": "claude",
                "iteration": 1,
            }
        ],
    )
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.list_workdash_panes",
        lambda _: [
            {"id": 3, "title": "code_one", "exited": False},
            {"id": 4, "title": "code_two", "exited": False},
        ],
    )
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "workdash.launcher.zellij.dump_zellij_pane",
        MagicMock(side_effect=AssertionError("pane read")),
    )
    result = session.birdview(top=5, format="json")
    assert result["autodispatch"]["items"][0]["sub_issue_total"] == 3
    assert result["autodispatch"]["items"][0]["agent"] == "claude"
    assert [p["pane_id"] for p in result["running_agents"]["items"]] == ["terminal_4"]
    assert result["open_pull_requests"]["items"][0]["has_unresolved_comments"] is True
    assert len(result["recent_items"]["items"]) == 2
    assert session.backend.github_client.mock_calls == []
    text = session.birdview(top=5, format="markdown")["text"]
    assert "https://github.com/owner/repo/pull/2" in text
    assert "APPROVED" in text and "unresolved comments" in text
    assert "3 sub-issues" in text
    assert "claude" in text
    assert "claude" in session.birdview(top=5, format="telegram")["text"]


@pytest.mark.parametrize(
    ("decision", "comments", "expected"),
    [
        (None, False, "review requested"),
        ("APPROVED", False, "APPROVED"),
        (None, True, "unresolved comments"),
    ],
)
def test_pending_review_request_in_snapshot(monkeypatch, tmp_path, decision, comments, expected):
    pr = replace(
        make_work_item(url="https://github.com/owner/repo/pull/1"),
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        review_requested=True,
        review_decision=decision,
        has_unresolved_comments=comments,
    )
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=[pr],
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr("workdash.control.launcher.zellij.list_workdash_panes", lambda _: [])
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *args, **kwargs: None)
    assert (
        session.birdview(top=5, format="json")["open_pull_requests"]["items"][0]["review_requested"]
        is True
    )
    for format in ("markdown", "telegram"):
        prs = (
            session.birdview(top=5, format=format)["text"]
            .split("Open pull requests")[1]
            .split("Recent items")[0]
        )
        assert expected in prs
        assert "review unknown" not in prs


def test_pull_requests_match_visible_dashboard(monkeypatch, tmp_path):
    pr = replace(make_work_item(), item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR)
    excluded = replace(pr, repo="other/repo", number=2)
    config = replace(
        make_valid_config(), workdir=str(tmp_path), excluded_repositories=["other/repo"]
    )
    session = WorkdashSession(
        config=config,
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=[pr, excluded],
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr("workdash.control.launcher.zellij.list_workdash_panes", lambda _: [])
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *args, **kwargs: None)
    result = session.birdview(top=5, format="json")
    assert [item["id"] for item in result["open_pull_requests"]["items"]] == ["owner/repo#PR-1"]
    assert session.backend.github_client.mock_calls == []


def test_telegram_omits_whole_entries(monkeypatch, tmp_path):
    items = [
        replace(
            make_work_item(url=f"https://github.com/owner/repo/issues/{n}"),
            number=n,
            title="long title " * 80,
        )
        for n in range(1, 20)
    ]
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=items,
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr("workdash.control.launcher.zellij.list_workdash_panes", lambda _: [])
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *args, **kwargs: None)
    text = session.birdview(top=20, format="telegram")["text"]
    assert len(text) < 4096
    assert "… and " in text
    assert text.count("](https://") == text.count(")")


@pytest.mark.parametrize("format", ["markdown", "telegram"])
@pytest.mark.parametrize("scope", ["owner/repo#99", "owner/repo#ISSUE-99"])
def test_autodispatch_links_unloaded_references(monkeypatch, tmp_path, format, scope):
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=[],
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr(
        session,
        "autodispatch_sessions",
        lambda: [
            {"status": "running", "scope": scope, "current_item": "owner/repo#ISSUE-100"},
            {"status": "running", "scope": "all work"},
        ],
    )
    monkeypatch.setattr("workdash.control.launcher.zellij.list_workdash_panes", lambda _: [])
    text = session.birdview(top=5, format=format)["text"]
    label = scope.replace("#", r"\#").replace("-", r"\-") if format == "telegram" else scope
    current = "owner/repo#ISSUE-100"
    if format == "telegram":
        current = current.replace("#", r"\#").replace("-", r"\-")
    assert f"[{label}](https://github.com/owner/repo/issues/99)" in text
    assert f"[{current}](https://github.com/owner/repo/issues/100)" in text
    assert "all work" in text
    assert session.backend.github_client.mock_calls == []


@pytest.mark.parametrize("format", ["markdown", "telegram", "json"])
def test_shared_checkout_agent_shows_known_items_and_blocked_status(monkeypatch, tmp_path, format):
    issue = make_work_item(url="https://github.com/owner/repo/issues/1")
    pr = replace(
        issue,
        number=2,
        item_type=WorkItemType.PR,
        kind=WorkItemKind.AUTHORED_PR,
        url="https://github.com/owner/repo/pull/2",
    )
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr(
        session,
        "autodispatch_sessions",
        lambda: [{"status": "blocked", "scope": issue.url, "pane_id": "terminal_3"}],
    )
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path", lambda *args, **kwargs: tmp_path / "shared"
    )
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.list_workdash_panes",
        lambda _: [{"id": 4, "title": "code_shared", "exited": False}],
    )
    normal_pane = session.info()["panes"][0]
    assert normal_pane["item"] == "unknown"
    assert "items" not in normal_pane
    result = session.birdview(top=5, format=format)
    if format == "json":
        assert result["autodispatch"]["items"][0]["status"] == "blocked"
        assert result["running_agents"]["items"][0]["items"] == [
            {"id": "owner/repo#ISSUE-1", "url": issue.url},
            {"id": "owner/repo#PR-2", "url": pr.url},
        ]
    else:
        autodispatch, agents = result["text"].split("Running agents")
        assert "blocked" in autodispatch
        agents = agents.split("Open pull requests")[0]
        assert "unknown" not in agents
        assert f"]({issue.url})" in agents and f"]({pr.url})" in agents
    assert session.backend.github_client.mock_calls == []


def test_same_pane_title_in_different_checkouts_stays_unknown(monkeypatch, tmp_path):
    issue = make_work_item(url="https://github.com/owner/repo/issues/1")
    pr = replace(issue, number=2, item_type=WorkItemType.PR, kind=WorkItemKind.AUTHORED_PR)
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=[issue, pr],
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr(session, "autodispatch_sessions", lambda: [])
    monkeypatch.setattr(
        "workdash.control.existing_worktree_path",
        lambda _, item, **kwargs: tmp_path / str(item.number) / "shared",
    )
    monkeypatch.setattr(
        "workdash.control.launcher.zellij.list_workdash_panes",
        lambda _: [{"id": 4, "title": "code_shared", "exited": False}],
    )
    pane = session.birdview(top=5, format="json")["running_agents"]["items"][0]
    assert pane["item"] == "unknown"
    assert "items" not in pane
    assert session.info()["panes"][0]["item"] == "unknown"


def test_cli_top_and_format_parity(monkeypatch, tmp_path, capsys):
    import json
    from io import BytesIO

    from webob import Request

    from workdash.control import _make_turbogears_app, _source_and_header_guarded_wsgi_app
    from workdash.workdash import main

    items = [
        replace(
            make_work_item(url=f"https://github.com/owner/repo/pull/{n}"),
            number=n,
            title=f"Pull request {n}",
            item_type=WorkItemType.PR,
            kind=WorkItemKind.AUTHORED_PR,
        )
        for n in range(1, 4)
    ]
    session = WorkdashSession(
        config=replace(make_valid_config(), workdir=str(tmp_path)),
        backend=SimpleNamespace(github_client=MagicMock()),
        work_items=items,
        suggestion_markers={},
        zellij_session="test",
    )
    monkeypatch.setattr("workdash.control.launcher.zellij.list_workdash_panes", lambda _: [])
    monkeypatch.setattr("workdash.control.existing_worktree_path", lambda *args, **kwargs: None)
    app = _source_and_header_guarded_wsgi_app(_make_turbogears_app(session))

    def urlopen(request, **kwargs):
        inbound = Request.blank(request.full_url, method=request.method, body=request.data)
        inbound.headers.update(request.header_items())
        inbound.environ["REMOTE_ADDR"] = "127.0.0.1"
        response = inbound.get_response(app)
        assert response.status_int == 200, response.text
        return BytesIO(response.body)

    monkeypatch.setattr("workdash.control.urllib.request.urlopen", urlopen)
    assert main(["birdview", "--top", "2", "--format", "json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    for name in ("open_pull_requests", "recent_items"):
        assert len(snapshot[name]["items"]) == 2
        assert snapshot[name]["omitted"] == 1

    for format in ("markdown", "telegram"):
        assert main(["birdview", "--top", "2", "--format", format]) == 0
        text = capsys.readouterr().out
        prs = text.split("Open pull requests")[1].split("Recent items")[0]
        recent = text.split("Recent items")[1]
        for name, section in (("open_pull_requests", prs), ("recent_items", recent)):
            for item in snapshot[name]["items"]:
                assert f"]({item['url']})" in section
                assert item["title"] in section
            assert "… and 1 more" in section
            omitted_urls = {item.url for item in items} - {
                item["url"] for item in snapshot[name]["items"]
            }
            assert all(url not in section for url in omitted_urls)
