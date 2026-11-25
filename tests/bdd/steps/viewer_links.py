"""Drive links using the mouse and keyboard in the built-in viewer."""

import asyncio
import os
import shlex
import threading
from contextlib import AsyncExitStack
from html import unescape

from pytest_bdd import given, parsers, then, when
from textual.app import App
from textual.widgets import Markdown, Static

from view_fake import ISSUE_URL, fake_gh_view_run, issue_routes, pull_request_routes
from workdash import view
from workdash.config import WorkdashConfig
from workdash.workdash import main


@given("the viewer is showing a link to a GitHub issue")
def _issue_link(scenario_state):
    scenario_state.update(link=ISSUE_URL, text="Crash on startup", routes=issue_routes())


@given("the viewer is showing a link to a pull request")
def _pull_request_link(scenario_state):
    scenario_state.update(
        link="https://github.com/owner/repo/pull/12", text="MERGEABLE", routes=pull_request_routes()
    )


@given("the viewer is showing a link to a web page")
def _web_link(scenario_state, tmp_path, monkeypatch):
    record = tmp_path / "opened-url"
    browser = tmp_path / "test-browser"
    browser.write_text(f'#!/bin/sh\nprintf "%s" "$1" > {shlex.quote(str(record))}\n')
    browser.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    url = "https://example.com/source"
    scenario_state.update(link=url, text=f"Opened {url} with test-browser", record=record)


@given(parsers.parse('the viewer is showing a relative link to "{filename}"'))
def _relative_link(scenario_state, tmp_path, filename):
    (tmp_path / filename).write_text("# Replacement\n\nlinked body\n")
    scenario_state.update(link=filename, text="linked body")


@given("the viewer is showing a link to a heading in the current document")
def _heading_link(scenario_state):
    scenario_state.update(
        link="#destination", text="Destination", rest="\nParagraph\n" * 30 + "\n# Destination\n"
    )


@given("the viewer is showing a link to the Part section of another Markdown document")
def _section_link(scenario_state, tmp_path):
    (tmp_path / "guide.md").write_text(
        "# Guide\n\n" + "Paragraph\n\n" * 40 + "# Part\n\nsection body\n"
    )
    scenario_state.update(link="guide.md#part", text="section body")


@given("the viewer is showing a link to a slow Markdown document")
def _slow_link(scenario_state):
    scenario_state["link"] = "slow.md"


@when("I click the link in the viewer")
@when("I click the link and press q while the viewer says it is loading")
def _click_link(scenario_state, tmp_path, monkeypatch, request):
    path = tmp_path / "analysis.md"
    link = scenario_state["link"]
    path.write_text(f"# Analysis\n\n[Link]({link})\n" + scenario_state.get("rest", ""))
    if "routes" in scenario_state:
        monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(scenario_state["routes"]))
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(open_command="test-browser"))
    release = threading.Event()
    load = view.load_document
    wait_for_load = link in (ISSUE_URL, "slow.md")

    def delayed_load(target):
        assert release.wait(10), f"Timed out holding {target} open"
        return load(target)

    # Keep the viewer running across steps so Back runs after the linked page is checked.
    runner = asyncio.Runner()
    viewer = AsyncExitStack()
    request.addfinalizer(runner.close)
    request.addfinalizer(lambda: runner.run(viewer.aclose()))

    def run(app):
        if wait_for_load:
            monkeypatch.setattr(view, "load_document", delayed_load)

        async def click():
            pilot = await viewer.enter_async_context(app.run_test(size=(100, 24)))
            scenario_state.update(pilot=pilot, runner=runner, before=_screen(app))
            await pilot.click(Markdown, offset=(2, 4))
            scenario_state["loading"] = app.query_one("#status", Static).render().plain
            if link == "slow.md":
                assert scenario_state["loading"].startswith("Loading ")
                await asyncio.wait_for(pilot.press("q"), timeout=2)
                scenario_state["returned"] = not app.is_running and not release.is_set()
                return
            release.set()
            async with asyncio.timeout(5):
                while scenario_state["text"] not in _screen(app):
                    await asyncio.sleep(0.03)
            assert app.is_running
            scenario_state.update(screen=_screen(app), document=app._document.content)

        runner.run(click())

    monkeypatch.setattr(App, "run", run)
    try:
        assert main(["view", str(path)]) == 0
    finally:
        release.set()


def _screen(app):
    return unescape(app.export_screenshot()).replace("\xa0", " ")


@then("the viewer says it is loading the issue")
def _loading_issue(scenario_state):
    assert scenario_state["loading"] == f"Loading {ISSUE_URL}..."


@then(parsers.parse('I can read the issue "{title}" in the same viewer'))
def _read_issue(scenario_state, title):
    assert title in scenario_state["screen"]
    assert "Description" in scenario_state["screen"]
    assert scenario_state["document"].startswith(f"# {title}\n")


@then("I can read the draft pull request in the same viewer")
def _read_pull_request(scenario_state):
    assert "Crash on startup" in scenario_state["screen"]
    assert "Draft:" in scenario_state["screen"]
    assert scenario_state["document"].startswith("# Crash on startup\n")
    assert "**Draft:** yes" in scenario_state["document"]


@then("my browser opens the linked web page and the viewer names the opener")
def _web_page_opens(scenario_state):
    assert scenario_state["record"].read_text() == scenario_state["link"]
    assert scenario_state["text"] in scenario_state["screen"]


@then(parsers.parse('I can read "{text}" instead of the original document, and Back is offered'))
def _read_linked_document(scenario_state, text):
    screen = scenario_state["screen"]
    assert text in screen and "Back" in screen
    assert "Analysis" not in screen
    assert scenario_state["document"].startswith("# Replacement\n")
    assert "Back" not in scenario_state["before"]


@when("I press b to return")
def _press_back(scenario_state):
    pilot = scenario_state["pilot"]
    scenario_state["runner"].run(pilot.press("b"))
    scenario_state["after_back"] = _screen(pilot.app)


@then(
    "I can read the original document instead of the linked document, and Back is no longer offered"
)
def _back_at_original(scenario_state):
    screen = scenario_state["after_back"]
    assert "Analysis" in screen and "Back" not in screen
    assert "Replacement" not in screen and scenario_state["text"] not in screen


@then(parsers.parse('the heading "{heading}" is on screen'))
def _heading_visible(scenario_state, heading):
    assert heading not in scenario_state["before"]
    assert heading in scenario_state["screen"]


@then(parsers.parse('I can read "{text}" at the Part heading in the linked document'))
def _linked_section_visible(scenario_state, text):
    assert scenario_state["document"].startswith("# Guide\n")
    assert "Part" in scenario_state["screen"] and text in scenario_state["screen"]


@then("I return from the viewer without waiting for the document")
def _quit_while_loading(scenario_state):
    assert scenario_state["returned"]
