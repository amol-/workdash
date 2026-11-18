"""Standalone Markdown document viewer."""

import os
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import unquote, urlsplit

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, MarkdownViewer

from . import launcher
from .config import load_config
from .github_client import parse_github_item_url


def run_markdownview(path: str) -> int:
    """Show a Markdown document read from a local path, file URL, or HTTP(S) URL."""
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
    show_markdown(content)
    return 0


def show_markdown(content: str) -> None:
    """Show Markdown content, routing the links it contains."""
    config = load_config()
    _MarkdownApp(content, config.open_command, config.open_needs_pane).run()


class _DocumentViewer(MarkdownViewer):
    async def go(self, location: str | Path) -> None:
        if str(location).startswith("#"):
            await super().go(location)


class _MarkdownApp(App):
    BINDINGS = [
        Binding("ctrl+c", "screen.copy_text", "Copy selected text", priority=True),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, content: str, open_command: str, open_needs_pane: bool) -> None:
        super().__init__()
        self._content = content
        self._open_command = open_command
        self._open_needs_pane = open_needs_pane

    def compose(self) -> ComposeResult:
        yield _DocumentViewer(self._content, show_table_of_contents=False)
        yield Footer()

    def copy_to_clipboard(self, text: str) -> None:
        if sys.platform != "darwin" or os.getenv("ZELLIJ_SESSION_NAME"):
            super().copy_to_clipboard(text)
            return
        try:
            subprocess.run(
                ["pbcopy"], input=text, text=True, check=True, stderr=subprocess.DEVNULL, timeout=5
            )
        except (OSError, subprocess.SubprocessError):
            self.notify("Copy unavailable: could not access the system clipboard", severity="error")

    def open_url(self, url: str, *, new_tab: bool = True) -> None:
        if url.startswith("#"):
            # In-document anchors are handled by the viewer's own go().
            return
        try:
            if parse_github_item_url(url) is not None:
                self._open_item_viewer(url)
            else:
                command = launcher.open_in_browser(
                    url,
                    self._open_command,
                    needs_pane=self._open_needs_pane,
                    zellij_session=os.getenv("ZELLIJ_SESSION_NAME"),
                    direct_nonblocking=True,
                )
                self.notify(f"Opened {url} with {command}", timeout=5, markup=False)
        except (OSError, RuntimeError, ValueError) as error:
            self.notify(
                f"Failed to open {url}: {error}", severity="error", timeout=10, markup=False
            )

    def _open_item_viewer(self, url: str) -> None:
        """Show the linked issue or pull request in a viewer of its own."""
        session = os.getenv("ZELLIJ_SESSION_NAME")
        if session:
            launcher.open_item_document(url, zellij_session=session)
        else:
            error = None
            with self.suspend():
                try:
                    launcher.become_item_document(url)
                except OSError as exc:
                    error = exc
            if error:
                raise error
