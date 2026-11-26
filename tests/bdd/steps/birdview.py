"""Share a work snapshot from a remote server."""

from dataclasses import replace
from io import BytesIO
from urllib.parse import urlsplit

from pytest_bdd import given, then, when
from webob import Request

from workdash import launcher
from workdash.autodispatch import _AutodispatchSession
from workdash.control import _make_turbogears_app, _source_and_header_guarded_wsgi_app
from workdash.models import WorkItemKind, WorkItemType
from workdash.workdash import main

from .common import ensure_api_session, make_work_item


@given("a Workdash server with ongoing work")
def server(scenario_state, work_items, tmp_path, monkeypatch):
    issue_url = "https://github.com/owner/repo/issues/1"
    work_items.extend(
        [
            replace(make_work_item(title="Build feature", url=issue_url), sub_issue_total=3),
            make_work_item(
                item_type=WorkItemType.PR,
                kind=WorkItemKind.AUTHORED_PR,
                number=2,
                title="Feature pull request",
                url="https://github.com/owner/repo/pull/2",
                review_decision="APPROVED",
                has_unresolved_comments=True,
            ),
        ]
    )
    session = ensure_api_session(scenario_state, work_items, tmp_path)
    dispatch = _AutodispatchSession(session, issue_url, None, "claude")
    dispatch.current_item = "owner/repo#ISSUE-1"
    dispatch.pane = (session.zellij_session, "terminal_3")
    dispatch.stage = "developing"
    dispatch.iteration = 2
    session._autodispatch[dispatch.id] = dispatch
    monkeypatch.setattr(session.autoclose, "blocked", lambda *_: True)
    monkeypatch.setattr(
        launcher.zellij,
        "list_workdash_panes",
        lambda _: [
            {"id": 3, "title": "code_example", "exited": False},
            {"id": 4, "title": "code_other", "exited": False},
        ],
    )
    app = _source_and_header_guarded_wsgi_app(_make_turbogears_app(session))

    def urlopen(request, **kwargs):
        destination = urlsplit(request.full_url)
        assert destination.hostname == "dashboard.lan" and destination.port == 9001
        inbound = Request.blank(request.full_url, method=request.method, body=request.data)
        inbound.headers.update(request.header_items())
        inbound.environ["REMOTE_ADDR"] = "127.0.0.1"
        response = inbound.get_response(app)
        assert response.status_int == 200, response.text
        return BytesIO(response.body)

    monkeypatch.setattr("workdash.control.urllib.request.urlopen", urlopen)


@when("the user requests a birdview from that server")
def run(scenario_state, capsys):
    assert main(["birdview", "--host", "dashboard.lan:9001"]) == 0
    scenario_state["birdview"] = capsys.readouterr().out


@then("the snapshot shows the current work with links, pane IDs and blocked status")
def check(scenario_state):
    text = scenario_state["birdview"]
    autodispatch = text.split("*Autodispatch*")[1].split("*Running agents*")[0]
    agents = text.split("*Running agents*")[1].split("*Open pull requests*")[0]
    prs = text.split("*Open pull requests*")[1].split("*Recent items*")[0]
    recent = text.split("*Recent items*")[1]
    assert "](https://github.com/owner/repo/issues/1)" in autodispatch
    assert "developing" in autodispatch and "2" in autodispatch
    assert "blocked" in autodispatch
    assert "3 sub\\-issues" in autodispatch
    assert "terminal\\_3" in autodispatch and "terminal\\_3" not in agents
    assert "terminal\\_4" in agents
    assert "](https://github.com/owner/repo/pull/2)" in prs
    assert "Feature pull request" in prs and "APPROVED" in prs
    assert "unresolved comments" in prs
    assert "Build feature" in recent and "Feature pull request" in recent
