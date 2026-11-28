import asyncio
import io
import json
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.widgets import Markdown, Static
from webob import Request

from workdash import control, launcher, view
from workdash.config import WorkdashConfig, WorkdashConfigValidationError


@pytest.fixture(autouse=True)
def _viewer_config(monkeypatch):
    """Keep the viewer's link handling independent of the developer's own config."""
    monkeypatch.setattr(view, "load_config", lambda: WorkdashConfig(open_command="xdg-open"))


@pytest.mark.parametrize(
    "error",
    [None, subprocess.CalledProcessError(1, "pbcopy"), FileNotFoundError("pbcopy")],
    ids=["copied", "command-failed", "command-missing"],
)
def test_mac_clipboard_outside_zellij(monkeypatch, error):
    app = view._MarkdownApp(view.Document("text", "file:///text.md"), "xdg-open", False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("ZELLIJ_SESSION_NAME", raising=False)
    pbcopy = MagicMock(side_effect=error)
    monkeypatch.setattr(subprocess, "run", pbcopy)
    app.notify = MagicMock()

    app.copy_to_clipboard("Copy this")

    pbcopy.assert_called_once_with(
        ["pbcopy"], input="Copy this", text=True, check=True, stderr=subprocess.DEVNULL, timeout=5
    )
    if error is None:
        app.notify.assert_not_called()
    else:
        assert "Copy unavailable" in app.notify.call_args.args[0]


def test_analysis_viewer_is_required():
    with pytest.raises(WorkdashConfigValidationError) as error:
        WorkdashConfig().require_valid()
    assert "analysis_viewer" in error.value.missing_fields


@pytest.mark.parametrize("format", ["markdown", "html"])
def test_local_viewer_receives_encoded_url_with_correct_content(tmp_path, monkeypatch, format):
    path = tmp_path / "analysis with spaces.md"
    path.write_text("# Heading\n\n**bold**", encoding="utf-8")
    opened = MagicMock()
    opened.return_value.wait.return_value = 0
    monkeypatch.setattr(launcher.subprocess, "Popen", opened)
    launcher.open_markdown(
        str(path),
        "viewer --quiet",
        format=format,
        item_url="https://github.com/owner/repo/issues/42",
    )
    expected = path if format == "markdown" else path.with_suffix(".html")
    opened.assert_called_once_with(
        ["viewer", "--quiet", expected.as_uri()],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    content = expected.read_text(encoding="utf-8")
    assert ("<strong>bold</strong>" if format == "html" else "**bold**") in content


def test_pane_viewer_receives_literal_local_markdown_url_and_focus(tmp_path, monkeypatch):
    path = tmp_path / "analysis with spaces.md"
    path.write_text("# Analysis")
    open_viewer = MagicMock(return_value="terminal_17")
    focus = MagicMock()
    monkeypatch.setattr(launcher.zellij, "open_viewer", open_viewer)
    monkeypatch.setattr(launcher.zellij, "focus_zellij_pane", focus)

    launcher.open_markdown(
        str(path),
        "term-view --quiet",
        format="markdown",
        needs_pane=True,
        zellij_session="workdash-main",
        focus=True,
    )

    open_viewer.assert_called_once_with(
        "workdash-main", ["term-view", "--quiet", path.as_uri()], executable="term-view"
    )
    focus.assert_called_once_with("workdash-main", "terminal_17")


@pytest.mark.parametrize("needs_pane", [False, True], ids=["direct", "pane"])
def test_builtin_analysis_viewer_receives_item_as_target_argument(
    tmp_path, monkeypatch, needs_pane
):
    path = tmp_path / "analysis with spaces.md"
    path.write_text("# Analysis")
    item_url = "https://github.com/owner/repo/pull/42"
    opened = MagicMock(return_value="terminal_17")
    process = MagicMock()
    process.return_value.wait.return_value = 0
    monkeypatch.setattr(launcher.zellij, "open_viewer", opened)
    monkeypatch.setattr(launcher.subprocess, "Popen", process)

    launcher.open_markdown(
        str(path),
        "workdash view",
        format="markdown",
        needs_pane=needs_pane,
        zellij_session="workdash-main",
        item_url=item_url,
    )

    argv = opened.call_args.args[1] if needs_pane else process.call_args.args[0]
    assert argv[-4:] == ["view", path.as_uri(), "--target", item_url]
    assert "env" not in argv


def test_direct_analysis_viewer_keeps_running_without_blocking(tmp_path):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis")
    started = tmp_path / "started"
    release = tmp_path / "release"
    finished = tmp_path / "finished"
    script = (
        "import pathlib, sys, time; "
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[4]); "
        "exec('while not pathlib.Path(sys.argv[2]).exists():\\n time.sleep(0.01)'); "
        "pathlib.Path(sys.argv[3]).write_text('finished')"
    )
    command = shlex.join([sys.executable, "-c", script, str(started), str(release), str(finished)])
    returned = threading.Event()
    thread = threading.Thread(
        target=lambda: (
            launcher.open_markdown(str(path), command, format="markdown"),
            returned.set(),
        ),
        daemon=True,
    )
    thread.start()
    try:
        assert returned.wait(5), "opening a persistent viewer blocked Workdash"
        assert not finished.exists()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if started.exists() and started.read_text() == path.as_uri():
                break
            time.sleep(0.01)
        assert started.read_text() == path.as_uri()
    finally:
        release.touch()
        thread.join(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if finished.exists() and finished.read_text() == "finished":
            break
        time.sleep(0.01)
    assert finished.read_text() == "finished"


def test_direct_analysis_viewer_launch_failure_is_contextual(tmp_path):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis")
    command = str(tmp_path / "missing-viewer")
    with pytest.raises(RuntimeError, match="Failed to open file via .*missing-viewer"):
        launcher.open_markdown(str(path), command, format="markdown")


def test_direct_analysis_viewer_early_failure_is_reported(tmp_path, monkeypatch):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis")
    opened = MagicMock()
    opened.return_value.wait.return_value = 7
    monkeypatch.setattr(launcher.subprocess, "Popen", opened)
    with pytest.raises(RuntimeError, match="Failed to open file via .*exit code 7"):
        launcher.open_markdown(str(path), "viewer", format="markdown")


def test_analysis_api_urls_follow_request_host_and_reject_bad_host():
    session = MagicMock()
    session.analyze.return_value = {"url": "http://127.0.0.1:8765/api/items/a/b%23ISSUE-1/analysis"}
    app = control._make_turbogears_app(session)

    def request(host):
        return Request.blank(
            "/api/v0/analyze",
            method="POST",
            host=host,
            content_type="application/json",
            body=b'{"target":"a/b#1"}',
        ).get_response(app)

    result = json.loads(request("dashboard.local:9999").text)["result"]
    assert result["markdown_url"] == "http://dashboard.local:9999/api/items/a/b%23ISSUE-1/analysis"
    assert result["html_url"] == result["markdown_url"] + "/html"
    result = json.loads(request("localhost.:8765").text)["result"]
    assert result["markdown_url"] == "http://localhost.:8765/api/items/a/b%23ISSUE-1/analysis"
    result = json.loads(request("workdash_server:8765").text)["result"]
    assert result["markdown_url"] == "http://workdash_server:8765/api/items/a/b%23ISSUE-1/analysis"
    assert result["html_url"] == result["markdown_url"] + "/html"
    assert request("evil@host:99").status_int == 400
    assert request("-invalid.example:99").status_int == 400


def test_analysis_endpoints_serve_markdown_and_rendered_html():
    session = MagicMock()
    session.backend.analysis_cache.load_analysis.return_value = "# Example\n\n**real content**"
    app = control._make_turbogears_app(session)
    md = Request.blank("/api/items/owner/repo%23ISSUE-1/analysis").get_response(app)
    assert md.status_int == 200
    assert md.content_type == "text/markdown"
    assert "**real content**" in md.text
    html = Request.blank("/api/items/owner/repo%23ISSUE-1/analysis/html").get_response(app)
    assert html.status_int == 200
    assert html.content_type == "text/html"
    assert "<strong>real content</strong>" in html.text
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html.text


def test_html_analysis_blocks_active_content_locally_and_over_http(tmp_path, monkeypatch):
    markdown = (
        "# Heading\n\n<div>raw HTML</div>\n\n<script>example()</script>"
        '\n\n<img src=x onerror="alert(1)"><meta http-equiv="refresh" content="0;url=/api/v0/info">'
        "\n\n![diagram](http://127.0.0.1:8765/api/v0/info)"
        "\n\n![remote](https://example.com/diagram.png)"
        "\n\n[unsafe](javascript:alert(1))"
        '\n\n<iframe src="http://127.0.0.1:8765/api/v0/info"></iframe>'
        '\n\n<form action="http://127.0.0.1:8765/api/v0/info"><button>Send</button></form>'
        "\n\n```html\n<b>code</b>\n```"
    )
    path = tmp_path / "analysis.md"
    path.write_text(markdown)
    opened = MagicMock()
    opened.return_value.wait.return_value = 0
    monkeypatch.setattr(launcher.subprocess, "Popen", opened)
    launcher.open_markdown(str(path), "viewer", format="html")
    local_html = path.with_suffix(".html").read_text()

    session = MagicMock()
    session.backend.analysis_cache.load_analysis.return_value = markdown
    app = control._make_turbogears_app(session)
    raw = Request.blank("/api/items/owner/repo%23ISSUE-1/analysis").get_response(app)
    assert raw.text == markdown
    response = Request.blank(
        "/api/items/owner/repo%23ISSUE-1/analysis/html", host="127.0.0.1:8765"
    ).get_response(app)
    assert response.status_int == 200

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.events = []

        def handle_starttag(self, tag, attrs):
            self.events.append((tag, dict(attrs)))

        def handle_endtag(self, tag):
            self.events.append((f"/{tag}", {}))

    for html in (local_html, response.text):
        assert "<h1>Heading</h1>" in html
        assert "<div>raw HTML</div>" in html
        assert "<script>" not in html
        assert "onerror=" not in html
        assert 'http-equiv="refresh"' not in html
        assert "javascript:" not in html
        assert "<iframe" not in html
        assert "<form" not in html
        assert '<img alt="diagram" src="http://127.0.0.1:8765/api/v0/info"' in html
        assert '<img alt="remote" src="https://example.com/diagram.png"' in html
        assert "<b>code</b>" not in html

        parser = Tags()
        parser.feed(html)
        events = parser.events
        head = next(i for i, (tag, _) in enumerate(events) if tag == "head")
        end_head = next(i for i, (tag, _) in enumerate(events) if tag == "/head")
        csp = next(
            i
            for i, (tag, attrs) in enumerate(events)
            if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy"
        )
        image = next(i for i, (tag, _) in enumerate(events) if tag == "img")
        assert head < csp < end_head < image
        assert all(tag not in {"style", "link", "script", "img"} for tag, _ in events[:csp])
        directives = {}
        for directive in events[csp][1]["content"].split(";"):
            words = directive.split()
            if words:
                directives.setdefault(words[0].lower(), words[1:])
        assert directives["img-src"] == ["http:", "https:"]
        for name in ("script-src", "connect-src", "frame-src", "object-src"):
            assert directives.get(name, directives.get("default-src")) == ["'none'"]
        assert directives["base-uri"] == ["'none'"]
        assert directives["form-action"] == ["'none'"]
    assert (
        launcher.render_analysis_html("content", "</title><script>alert(1)</script>").count(
            "<script>"
        )
        == 0
    )


def test_view_command_opens_a_local_document(monkeypatch, tmp_path):
    from workdash import workdash

    document = tmp_path / "analysis.md"
    document.write_text("# analysis")
    shown = MagicMock()
    monkeypatch.setattr(view, "show_document", shown)

    assert workdash.main(["view", str(document)]) == 0

    shown.assert_called_once_with(view.Document("# analysis", document.resolve().as_uri()))


@pytest.mark.parametrize("command", ["mdview", "markdownview"])
def test_old_document_commands_are_not_available(capsys, command):
    from workdash import workdash

    with pytest.raises(SystemExit) as error:
        workdash.main([command, "analysis.md"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def _viewer_source(app) -> str:
    return app.query_one(view._DocumentViewer).document.source


def _viewer_status(app) -> str:
    return app.query_one("#status", Static).render().plain


async def _settle(pilot, condition) -> None:
    """Wait for work the viewer started in the background to reach the screen."""
    for _ in range(200):
        await pilot.pause()
        if condition():
            return
    raise AssertionError("the viewer did not finish loading")


def _run_viewer(monkeypatch, exercise) -> None:
    """Patch ``App.run`` so ``exercise(pilot, app)`` runs under ``run_test``."""

    def run(app):
        async def session():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await exercise(pilot, app)

        asyncio.run(session())

    monkeypatch.setattr(App, "run", run)


def _drive_viewer(monkeypatch, app, exercise) -> None:
    """Run ``exercise(pilot)`` against ``app`` with a real Textual pilot."""
    _run_viewer(monkeypatch, lambda pilot, _app: exercise(pilot))
    app.run()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_markdown_viewer_loads_url(scheme, monkeypatch):
    import ssl
    from unittest.mock import patch

    document = io.BytesIO(b"# Remote heading\n\nremote body")
    document.geturl = lambda: f"{scheme}://example.test/analysis"
    rendered = []

    async def capture(pilot, app):
        rendered.append(app.export_screenshot())

    with patch("urllib.request.urlopen", return_value=document) as fetch:
        _run_viewer(monkeypatch, capture)
        assert view.run_view(f"{scheme}://example.test/analysis") == 0
    assert "Remote" in rendered[0] and "heading" in rendered[0] and "body" in rendered[0]
    assert fetch.call_args.kwargs["context"].verify_mode == ssl.CERT_NONE
    assert fetch.call_args.kwargs["timeout"] == view._DOCUMENT_FETCH_TIMEOUT_SECONDS


def test_markdown_viewer_loads_plain_file_path(tmp_path, monkeypatch):
    path = tmp_path / "analysis with spaces.md"
    path.write_text("# Local heading\n\nlocal body", encoding="utf-8")
    rendered = []

    async def capture(pilot, app):
        rendered.append(app.export_screenshot())

    _run_viewer(monkeypatch, capture)
    assert view.run_view(str(path)) == 0
    assert "Local" in rendered[0] and "heading" in rendered[0] and "body" in rendered[0]


def test_analysis_viewer_advertises_copy_and_navigates_to_issue_and_back(tmp_path, monkeypatch):
    from view_fake import ISSUE_URL, fake_gh_view_run, issue_routes

    path = tmp_path / "analysis.md"
    path.write_text(f"# Analysis\n\n[Issue]({ISSUE_URL})\n", encoding="utf-8")
    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run(issue_routes()))

    async def exercise(pilot, app):
        screen = ET.fromstring(app.export_screenshot())
        bottom = "".join(
            element.text or ""
            for element in screen.iter("{http://www.w3.org/2000/svg}text")
            if element.get("clip-path", "").endswith(f"-line-{app.size.height - 1})")
        ).replace("\N{NO-BREAK SPACE}", " ")
        assert "^c Copy selected text" in bottom
        assert "q Quit" in bottom
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: "Crash on startup" in _viewer_source(app))
        assert "Sub issue 13" in _viewer_source(app)
        await pilot.press("b")
        assert _viewer_source(app) == path.read_text(encoding="utf-8")
        assert "b" not in app.screen.active_bindings

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0


def test_markdown_viewer_reports_a_link_it_cannot_load(tmp_path, monkeypatch):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis\n\n[Source](missing-file.md)\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    observed = []

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_status(app).startswith("Failed to read"))
        assert app.is_running
        assert _viewer_source(app).startswith("# Analysis\n")
        observed.append(_viewer_status(app))
        observed.append(_viewer_source(app))
        assert "b" not in app.screen.active_bindings

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0
    assert observed[0].startswith("Failed to read "), observed[0]
    assert "missing-file.md" in observed[0]
    assert "Analysis" in observed[1]


def test_markdown_viewer_fragment_navigates_within_analysis(tmp_path, monkeypatch):
    path = tmp_path / "analysis.md"
    path.write_text(
        "# Analysis\n\n[Jump](#destination)\n\n" + "Paragraph\n\n" * 30 + "# Destination\n"
    )

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: "Destination" in app.export_screenshot())
        assert app.is_running
        assert "b" not in app.screen.active_bindings

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0


def test_markdown_link_keeps_long_running_viewer_alive(monkeypatch):
    processes = []
    popen = subprocess.Popen
    command = shlex.join([sys.executable, "-c", "import time; time.sleep(30)"])

    def start(*args, **kwargs):
        process = popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    app = view._MarkdownApp(view.Document("# Document", "file:///document.md"), command, False)
    statuses = []

    async def follow(pilot):
        app.follow("https://example.com")
        statuses.append(_viewer_status(app))

    try:
        _drive_viewer(monkeypatch, app, follow)
        assert processes[0].poll() is None
        assert statuses == [f"Opened https://example.com with {command}"]
    finally:
        if processes and processes[0].poll() is None:
            processes[0].terminate()
            processes[0].wait(timeout=5)


def test_external_link_honours_the_configured_pane_viewer(monkeypatch):
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "session")
    opened = MagicMock()
    monkeypatch.setattr(launcher.zellij, "open_viewer", opened)
    app = view._MarkdownApp(view.Document("# Document", "file:///document.md"), "viewer", True)
    statuses = []

    async def follow(pilot):
        app.follow("https://example.com")
        statuses.append(_viewer_status(app))

    _drive_viewer(monkeypatch, app, follow)

    opened.assert_called_once_with(
        "session", ["viewer", "https://example.com"], executable="viewer"
    )
    assert statuses == ["Opened https://example.com with viewer"]


@pytest.mark.parametrize("launch_fails", [False, True], ids=["nonzero-exit", "launch-error"])
def test_markdown_link_reports_command_failure_and_keeps_document(monkeypatch, launch_fails):
    process = MagicMock()
    process.wait.return_value = 7
    launch = MagicMock(return_value=process)
    if launch_fails:
        launch.side_effect = OSError("cannot open display")
    monkeypatch.setattr(launcher.subprocess, "Popen", launch)
    document = view.Document(
        "# Analysis\n\n[Source](https://example.com/source)", "file:///document.md"
    )
    app = view._MarkdownApp(document, "xdg-open", False)

    async def follow(pilot):
        await pilot.click(Markdown, offset=(2, 4))
        reason = "cannot open display" if launch_fails else "exit code 7"
        assert _viewer_status(app) == (
            "Failed to open https://example.com/source: Failed to open URL via xdg-open: " + reason
        )
        assert _viewer_source(app) == document.content
        assert app.is_running

    _drive_viewer(monkeypatch, app, follow)
    assert launch.call_args.args == (["xdg-open", "https://example.com/source"],)


def test_markdown_viewer_resolves_relative_links_against_the_document_url(monkeypatch):
    app = view._MarkdownApp(
        view.Document("# Remote\n\n[Next](other.md)", "https://docs.example/guide/start.md"),
        "xdg-open",
        False,
    )
    loaded = []

    def load_document(target):
        loaded.append(target)
        return view.Document("# Next", target)

    monkeypatch.setattr(view, "load_document", load_document)
    sources = []

    async def follow(pilot):
        app.follow("other.md")
        await _settle(pilot, lambda: _viewer_source(app) == "# Next")
        sources.append(_viewer_source(app))

    _drive_viewer(monkeypatch, app, follow)

    assert loaded == ["https://docs.example/guide/other.md"]
    assert sources == ["# Next"]


def test_github_markdown_page_opens_with_the_configured_command(monkeypatch):
    app = view._MarkdownApp(view.Document("# Document", "file:///document.md"), "xdg-open", False)
    opened = []
    monkeypatch.setattr(
        launcher, "open_in_browser", lambda url, *args, **kwargs: opened.append(url) or "xdg-open"
    )
    statuses = []

    async def follow(pilot):
        app.follow("https://github.com/owner/repo/blob/main/README.md")
        statuses.append(_viewer_status(app))

    _drive_viewer(monkeypatch, app, follow)

    assert opened == ["https://github.com/owner/repo/blob/main/README.md"]
    assert statuses == ["Opened https://github.com/owner/repo/blob/main/README.md with xdg-open"]


def test_view_uses_the_configured_open_command_for_external_links(monkeypatch, tmp_path):
    monkeypatch.setattr(
        view,
        "load_config",
        lambda: WorkdashConfig(open_command="term-view --quiet", open_needs_pane=True),
    )
    monkeypatch.setenv("ZELLIJ_SESSION_NAME", "workdash-current")
    opened = []
    monkeypatch.setattr(
        launcher,
        "open_in_browser",
        lambda url, command, **kwargs: opened.append((url, command, kwargs)) or command,
    )
    document = tmp_path / "notes.md"
    document.write_text("# Notes\n\n[Web](https://example.com/source)\n", encoding="utf-8")
    statuses = []

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_status(app) != "")
        statuses.append(_viewer_status(app))

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(document)) == 0

    assert opened == [
        (
            "https://example.com/source",
            "term-view --quiet",
            {
                "needs_pane": True,
                "zellij_session": "workdash-current",
                "direct_nonblocking": True,
            },
        )
    ]
    assert statuses == ["Opened https://example.com/source with term-view --quiet"]


def test_markdown_viewer_reports_a_malformed_link(tmp_path, monkeypatch):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis\n\n[Bad](http://[::1/docs/x.md)\n", encoding="utf-8")
    observed = []

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_status(app) != "")
        observed.append(_viewer_status(app))
        observed.append(_viewer_source(app))

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0

    assert observed[0].startswith("Failed to load "), observed[0]
    assert observed[0].endswith(": Invalid IPv6 URL"), observed[0]
    assert observed[1].startswith("# Analysis\n")


def test_markdown_viewer_reports_a_link_it_cannot_decode(tmp_path, monkeypatch):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis\n\n[Bad](latin.md)\n", encoding="utf-8")
    (tmp_path / "latin.md").write_bytes("# Caf\xe9".encode("latin-1"))
    monkeypatch.chdir(tmp_path)
    observed = []

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_status(app) != "")
        observed.append(_viewer_status(app))
        observed.append(_viewer_source(app))

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0

    assert observed[0].startswith("Failed to read "), observed[0]
    assert "latin.md" in observed[0] and "utf-8" in observed[0]
    assert observed[1].startswith("# Analysis\n")


def test_markdown_viewer_reports_a_remote_link_it_cannot_fetch(tmp_path, monkeypatch):
    path = tmp_path / "analysis.md"
    path.write_text("# Analysis\n\n[Missing](https://docs.example/missing.md)\n", encoding="utf-8")
    monkeypatch.setattr(
        urllib.request, "urlopen", MagicMock(side_effect=urllib.error.URLError("not found"))
    )
    observed = []

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_status(app) != "")
        observed.append(_viewer_status(app))
        observed.append(_viewer_source(app))

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0

    assert observed[0] == (
        "Failed to read https://docs.example/missing.md: <urlopen error not found>"
    )
    assert observed[1].startswith("# Analysis\n")


def test_back_walks_the_whole_history(tmp_path, monkeypatch):
    first = tmp_path / "analysis.md"
    first.write_text("# First\n\n[Next](second.md)\n", encoding="utf-8")
    (tmp_path / "second.md").write_text("# Second\n\n[Next](third.md)\n", encoding="utf-8")
    (tmp_path / "third.md").write_text("# Third\n", encoding="utf-8")
    shown = []

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_source(app).startswith("# Second"))
        await pilot.click(Markdown, offset=(2, 4))
        await _settle(pilot, lambda: _viewer_source(app).startswith("# Third"))
        shown.append((_viewer_source(app), "b" in app.screen.active_bindings))
        for _ in range(2):
            await pilot.press("b")
            shown.append((_viewer_source(app), "b" in app.screen.active_bindings))

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(first)) == 0

    assert shown == [
        ("# Third\n", True),
        ("# Second\n\n[Next](third.md)\n", True),
        ("# First\n\n[Next](second.md)\n", False),
    ]


def test_back_during_loading_keeps_the_previous_document(tmp_path, monkeypatch):
    first = tmp_path / "analysis.md"
    first.write_text("# First\n\n[Next](second.md)\n", encoding="utf-8")
    (tmp_path / "second.md").write_text("# Second\n\n[Next](third.md)\n", encoding="utf-8")
    (tmp_path / "third.md").write_text("# Third\n", encoding="utf-8")
    real_load_document = view.load_document
    started = threading.Event()
    finished = threading.Event()
    release = threading.Event()

    def slow_load_document(target):
        started.set()
        assert release.wait(timeout=10)
        finished.set()
        return real_load_document(target)

    observed = {}

    async def exercise(pilot, app):
        app.follow("second.md")
        await _settle(pilot, lambda: _viewer_source(app).startswith("# Second"))
        monkeypatch.setattr(view, "load_document", slow_load_document)
        app.follow("third.md")
        while not started.is_set():
            await pilot.pause()
        assert _viewer_status(app).startswith("Loading ")
        await app.action_back()
        assert _viewer_source(app).startswith("# First\n")
        release.set()
        while not finished.is_set():
            await pilot.pause()
        await pilot.pause()
        observed["source"] = _viewer_source(app)
        observed["back"] = "b" in app.screen.active_bindings

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(first)) == 0

    assert observed["source"].startswith("# First\n")
    assert observed["back"] is False


@pytest.mark.parametrize("href", ["#", ""])
def test_markdown_viewer_ignores_an_empty_anchor_link(tmp_path, monkeypatch, href):
    path = tmp_path / "analysis.md"
    path.write_text(f"# Analysis\n\n[Top]({href})\n", encoding="utf-8")
    observed = {}

    async def exercise(pilot, app):
        await pilot.click(Markdown, offset=(2, 4))
        await pilot.pause()
        observed["running"] = app.is_running
        observed["source"] = _viewer_source(app)
        observed["back"] = "b" in app.screen.active_bindings

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(path)) == 0

    assert observed == {
        "running": True,
        "source": f"# Analysis\n\n[Top]({href})\n",
        "back": False,
    }


def test_markdown_viewer_resolves_relative_links_against_the_redirected_url(monkeypatch):
    response = io.BytesIO(b"# Guide\n\n[Next](other.md)")
    response.geturl = lambda: "https://docs.example/new/guide.md"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: response)

    document = view.load_document("https://docs.example/old/start.md")
    assert document.source == "https://docs.example/new/guide.md"

    loaded = []

    def load_document(target):
        loaded.append(target)
        return view.Document("# Next", target)

    monkeypatch.setattr(view, "load_document", load_document)
    app = view._MarkdownApp(document, "xdg-open", False)

    async def follow(pilot):
        app.follow("other.md")
        await _settle(pilot, lambda: _viewer_source(app) == "# Next")

    _drive_viewer(monkeypatch, app, follow)

    assert loaded == ["https://docs.example/new/other.md"]


def test_markdown_viewer_reports_a_github_link_that_cannot_be_fetched(monkeypatch):
    from view_fake import ISSUE_URL, fake_gh_view_run

    monkeypatch.setattr(view.subprocess, "run", fake_gh_view_run({}, fails=True))
    app = view._MarkdownApp(
        view.Document(f"# Analysis\n\n[Issue]({ISSUE_URL})\n", "file:///analysis.md"),
        "xdg-open",
        False,
    )
    observed = {}

    async def follow(pilot):
        app.follow(ISSUE_URL)
        await _settle(pilot, lambda: _viewer_status(app) != "")
        observed["status"] = _viewer_status(app)
        observed["source"] = _viewer_source(app)

    _drive_viewer(monkeypatch, app, follow)

    assert observed["status"] == (
        f"Failed to load {ISSUE_URL}: Failed to fetch GitHub data with gh: no such issue"
    )
    assert observed["source"].startswith("# Analysis\n")


def test_back_during_a_failed_load_restores_the_previous_status(tmp_path, monkeypatch):
    first = tmp_path / "analysis.md"
    first.write_text("# First\n\n[Next](second.md)\n", encoding="utf-8")
    (tmp_path / "second.md").write_text("# Second\n\n[Broken](missing.md)\n", encoding="utf-8")
    release = threading.Event()
    finished = threading.Event()

    def failing_load_document(target):
        assert release.wait(timeout=10)
        finished.set()
        raise RuntimeError(f"Failed to read {target}: broken")

    observed = {}

    async def exercise(pilot, app):
        app.follow("second.md")
        await _settle(pilot, lambda: _viewer_source(app).startswith("# Second"))
        monkeypatch.setattr(view, "load_document", failing_load_document)
        app.follow("missing.md")
        while not _viewer_status(app).startswith("Loading "):
            await pilot.pause()
        await app.action_back()
        release.set()
        while not finished.is_set():
            await pilot.pause()
        await pilot.pause()
        observed["status"] = _viewer_status(app)
        observed["source"] = _viewer_source(app)

    _run_viewer(monkeypatch, exercise)
    assert view.run_view(str(first)) == 0

    assert observed["source"] == "# First\n\n[Next](second.md)\n"
    assert "no GitHub item" in observed["status"] and "broken" not in observed["status"]
