"""Standalone Markdown document viewer."""

import ssl
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

from textual.app import App, ComposeResult
from textual.widgets import MarkdownViewer


def run_markdownview(path: str) -> int:
    url = urlsplit(path)
    if url.scheme in ("http", "https"):
        with urllib.request.urlopen(path, context=ssl._create_unverified_context()) as response:
            content = response.read().decode("utf-8")
    elif url.scheme == "file":
        if url.netloc not in ("", "localhost"):
            raise ValueError("File URL must refer to the local host")
        content = Path(unquote(url.path)).read_text(encoding="utf-8")
    else:
        content = Path(path).read_text(encoding="utf-8")

    class AnalysisViewer(MarkdownViewer):
        async def go(self, location: str | Path) -> None:
            if str(location).startswith("#"):
                await super().go(location)

    class MarkdownApp(App):
        BINDINGS = [("q", "quit", "Quit")]

        def compose(self) -> ComposeResult:
            yield AnalysisViewer(content, show_table_of_contents=False)

    MarkdownApp().run()
    return 0
