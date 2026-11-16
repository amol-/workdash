import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock
from urllib.request import Request, urlopen

import pytest

from workdash import control, launcher


@pytest.mark.browser
def test_analysis_images_and_control_security_in_browser(tmp_path, monkeypatch):
    browser = (
        os.environ.get("CHROME_BIN") or shutil.which("chromium") or shutil.which("google-chrome")
    )
    node = shutil.which("node")
    if not browser or not node:
        pytest.fail("Chromium and Node.js with built-in WebSocket are required")
    if subprocess.run(
        [node, "-e", "process.exit(typeof WebSocket === 'function' ? 0 : 1)"], check=False
    ).returncode:
        pytest.fail("Node.js with built-in WebSocket is required")

    image_requests = []

    class Images(BaseHTTPRequestHandler):
        def do_GET(self):
            image_requests.append(self.path)
            image = b'<svg xmlns="http://www.w3.org/2000/svg" width="7" height="7"></svg>'
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.end_headers()
            self.wfile.write(image)

        def log_message(self, *args):
            pass

    images = ThreadingHTTPServer(("127.0.0.1", 0), Images)
    thread = threading.Thread(target=images.serve_forever, daemon=True)
    thread.start()
    session = MagicMock()
    session.show_config.return_value = {"ok": True}
    monkeypatch.setattr(control, "server_address", lambda: ("127.0.0.1", 0))
    requests = []
    source_filter = control._source_and_header_guarded_wsgi_app

    def observe(app, *args):
        filtered = source_filter(app, *args)

        def observed(environ, start_response):
            def record(status, headers, *args):
                requests.append((environ["REQUEST_METHOD"], environ["PATH_INFO"], status))
                return start_response(status, headers, *args)

            return filtered(environ, record)

        return observed

    monkeypatch.setattr(control, "_source_and_header_guarded_wsgi_app", observe)
    server = control.WorkdashControlServer(session)
    try:
        server.start()
        base = f"http://127.0.0.1:{server._httpds[0].server_port}"
        action = f"{base}/api/v0/show-config"
        markdown = (
            f"![image](http://127.0.0.1:{images.server_port}/image.svg)\n\n![attack]({action})"
        )
        session.backend.analysis_cache.load_analysis.return_value = markdown
        local = tmp_path / "analysis.html"
        local.write_text(launcher.render_analysis_html(markdown))
        for index, url in enumerate(
            (local.as_uri(), f"{base}/api/items/owner/repo%23ISSUE-1/analysis/html")
        ):
            result = subprocess.run(
                [
                    node,
                    str(Path(__file__).with_name("browser_analysis.mjs")),
                    browser,
                    str(tmp_path / f"profile-{index}"),
                    url,
                    action,
                    f"http://127.0.0.1:{images.server_port}/attacker",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            )
            assert json.loads(result.stdout) == {"images": [7, 0], "attack": "blocked"}
        assert image_requests.count("/image.svg") == 2
        assert [entry for entry in requests if entry[1] == "/api/v0/show-config"] == [
            ("GET", "/api/v0/show-config", "403 Forbidden"),
            ("OPTIONS", "/api/v0/show-config", "403 Forbidden"),
        ] * 2
        session.show_config.assert_not_called()
        request = Request(
            action, data=b"{}", headers={"X-Workdash": "1", "Content-Type": "application/json"}
        )
        with urlopen(request) as response:
            assert response.status == 200
        session.show_config.assert_called_once()
    finally:
        server.stop()
        images.shutdown()
        images.server_close()
        thread.join()
