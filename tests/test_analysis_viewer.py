import json
import shlex
import sys
import threading
import time
from html.parser import HTMLParser
from unittest.mock import MagicMock

import pytest
from webob import Request

from workdash import control, launcher
from workdash.config import WorkdashConfig, WorkdashConfigValidationError


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
    launcher.open_markdown(str(path), "viewer", format=format)
    url = opened.call_args.args[0][-1]
    expected = path if format == "markdown" else path.with_suffix(".html")
    assert url == expected.as_uri()
    content = expected.read_text(encoding="utf-8")
    assert ("<strong>bold</strong>" if format == "html" else "**bold**") in content


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
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.read_text() == path.as_uri()
    finally:
        release.touch()
        thread.join(timeout=5)
    deadline = time.monotonic() + 5
    while not finished.exists() and time.monotonic() < deadline:
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


def test_html_analysis_blocks_active_content_locally_and_over_http(tmp_path, monkeypatch):
    markdown = (
        "# Heading\n\n<div>raw HTML</div>\n\n<script>example()</script>"
        '\n\n<img src=x onerror="alert(1)"><meta http-equiv="refresh" content="0;url=/api/v0/info">'
        "\n\n![diagram](http://127.0.0.1:8765/api/v0/info)"
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
        for name in ("script-src", "img-src", "connect-src", "frame-src", "object-src"):
            assert directives.get(name, directives.get("default-src")) == ["'none'"]
        assert directives["base-uri"] == ["'none'"]
        assert directives["form-action"] == ["'none'"]
    assert (
        launcher.render_analysis_html("content", "</title><script>alert(1)</script>").count(
            "<script>"
        )
        == 0
    )


def test_markdownview_command_works_without_dashboard_setup(monkeypatch, tmp_path):
    from workdash import markdownview, workdash

    document = tmp_path / "analysis.md"
    document.write_text("# analysis")
    view = MagicMock(return_value=0)
    monkeypatch.setattr(markdownview, "run_markdownview", view)
    assert workdash.main(["markdownview", str(document)]) == 0
    view.assert_called_once_with(str(document))


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_markdown_viewer_loads_url(scheme, monkeypatch):
    import asyncio
    import io
    import ssl
    from unittest.mock import patch

    from textual.app import App

    from workdash.markdownview import run_markdownview

    document = io.BytesIO(b"# Remote heading\n\nremote body")
    rendered = []

    def run(app):
        async def capture():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                rendered.append(app.export_screenshot())

        asyncio.run(capture())

    with patch("urllib.request.urlopen", return_value=document) as fetch:
        monkeypatch.setattr(App, "run", run)
        assert run_markdownview(f"{scheme}://example.test/analysis") == 0
    assert "Remote" in rendered[0] and "heading" in rendered[0] and "body" in rendered[0]
    assert fetch.call_args.kwargs["context"].verify_mode == ssl.CERT_NONE


def test_markdown_viewer_loads_plain_file_path(tmp_path, monkeypatch):
    import asyncio

    from textual.app import App

    from workdash.markdownview import run_markdownview

    path = tmp_path / "analysis with spaces.md"
    path.write_text("# Local heading\n\nlocal body", encoding="utf-8")
    rendered = []

    def run(app):
        async def capture():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                rendered.append(app.export_screenshot())

        asyncio.run(capture())

    monkeypatch.setattr(App, "run", run)
    assert run_markdownview(str(path)) == 0
    assert "Local" in rendered[0] and "heading" in rendered[0] and "body" in rendered[0]


@pytest.mark.parametrize("target", ["missing-file.md", "existing-file.md"])
def test_markdown_viewer_relative_link_keeps_analysis_open(tmp_path, monkeypatch, target):
    import asyncio

    from textual.app import App
    from textual.widgets import Markdown

    from workdash.markdownview import run_markdownview

    path = tmp_path / "analysis.md"
    path.write_text(f"# Analysis\n\n[Source]({target})\n", encoding="utf-8")
    (tmp_path / "existing-file.md").write_text("# Replacement", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def run(app):
        async def exercise():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.click(Markdown, offset=(2, 4))
                await pilot.pause()
                assert app.is_running
                screenshot = app.export_screenshot()
                assert "Analysis" in screenshot
                assert "Replacement" not in screenshot

        asyncio.run(exercise())

    monkeypatch.setattr(App, "run", run)
    assert run_markdownview(str(path)) == 0


def test_markdown_viewer_fragment_navigates_within_analysis(tmp_path, monkeypatch):
    import asyncio

    from textual.app import App
    from textual.widgets import Markdown

    from workdash.markdownview import run_markdownview

    path = tmp_path / "analysis.md"
    path.write_text(
        "# Analysis\n\n[Jump](#destination)\n\n" + "Paragraph\n\n" * 30 + "# Destination\n"
    )

    def run(app):
        async def exercise():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.click(Markdown, offset=(2, 4))
                await pilot.pause()
                assert app.is_running
                assert "Destination" in app.export_screenshot()

        asyncio.run(exercise())

    monkeypatch.setattr(App, "run", run)
    assert run_markdownview(str(path)) == 0


def test_markdown_viewer_external_link_keeps_analysis_open(tmp_path, monkeypatch):
    import asyncio

    from textual.app import App
    from textual.widgets import Markdown

    from workdash.markdownview import run_markdownview

    path = tmp_path / "analysis.md"
    path.write_text("# Analysis\n\n[Source](https://example.com/source)\n", encoding="utf-8")
    opened = MagicMock()
    monkeypatch.setattr(App, "open_url", opened)

    def run(app):
        async def exercise():
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause()
                await pilot.click(Markdown, offset=(2, 4))
                await pilot.pause()
                assert app.is_running
                assert "Analysis" in app.export_screenshot()

        asyncio.run(exercise())

    monkeypatch.setattr(App, "run", run)
    assert run_markdownview(str(path)) == 0
    opened.assert_called_once_with("https://example.com/source")
