#!/usr/bin/env python3
"""Regenerate the website images, then check the pages."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from capture.drive import capture
from capture.render import render
from capture.shots import SHOTS
from capture.world import build

PAGES = ("index", "work", "review", "agentic", "control")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("shot", nargs="?", choices=SHOTS)
    parser.add_argument("--check", action="store_true", help="check the pages")
    parser.add_argument("--setup", action="store_true", help="download the renderer and its font")
    parser.add_argument("--list", action="store_true", help="list available captures")
    parser.add_argument(
        "--work",
        type=Path,
        default=Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        / "workdash-site-capture",
    )
    parser.add_argument("--keep-work", action="store_true")
    args = parser.parse_args()
    site = Path(__file__).resolve().parent
    renderer = args.work / "agg-1.5.0"
    font = args.work / "fonts/NotoEmoji.ttf"
    if args.setup:
        for path, url, sha256 in (
            (
                renderer,
                "https://github.com/asciinema/agg/releases/download/v1.5.0/agg-x86_64-unknown-linux-gnu",
                "59673469d625df6f55c72159c84ffbb7af1348ac379d0e79db43f4b80fb986a3",
            ),
            (
                font,
                "https://raw.githubusercontent.com/google/fonts/"
                "b979dba422e445492b0eb9951ac52ee0b4d648c3/ofl/notoemoji/NotoEmoji%5Bwght%5D.ttf",
                "de6c18832938afc99caf132b39d6a30a19bac7f2e812e28db2535b4608d27551",
            ),
        ):
            with urllib.request.urlopen(url) as download:
                data = download.read()
            if hashlib.sha256(data).hexdigest() != sha256:
                raise RuntimeError(f"{path.name} checksum mismatch")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"Installed {path}")
        renderer.chmod(0o755)
        return 0
    if args.list:
        for name, (page, size, _, _) in SHOTS.items():
            print(f"{name:20} {page:15} {size[0]}x{size[1]}")
        return 0
    if args.check:
        return report(site)
    if not renderer.is_file() or not font.is_file():
        parser.error("run --setup first to install the renderer and its font")
    work = Path(tempfile.mkdtemp(dir=args.work))
    real_config = config_snapshot()
    try:
        names = ["dashboard" if args.shot == "og-card" else args.shot] if args.shot else SHOTS
        for name in names:
            if name == "og-card":
                continue
            page, size, steps, animated = SHOTS[name]
            # Every shot gets its own world, so one capture never depends on another.
            env = build(site.parent, work / name)
            cast = work / f"{name}.cast"
            capture(env, size, steps, cast)
            gh_errors = work / name / "home/gh-errors.log"
            if gh_errors.exists():
                raise RuntimeError(f"Unsupported gh calls:\n{gh_errors.read_text()}")
            validate_capture(name, cast)
            output = site / "img" / f"{name}.gif"
            card = site / "img/og-card.png" if name == "dashboard" else None
            render(work / name / "venv/bin/python", renderer, cast, output, animated, card)
            print(f"{name}: {output}")
            if card:
                print(f"og-card: {card}")
    finally:
        if args.keep_work:
            print(f"Scratch: {work}")
        else:
            shutil.rmtree(work)
        if sys.exception() is None and config_snapshot() != real_config:
            raise RuntimeError("Real workdash configuration changed during capture")
    return report(site)


def validate_capture(name, cast):
    """Refuse images whose terminal output does not demonstrate the site's claims."""
    raw = "".join(
        event[2]
        for line in cast.read_text().splitlines()[1:]
        if (event := json.loads(line))[1] == "o"
    )
    text = re.sub(
        r"\x1b(?:\][^\x1b\x07]*(?:\x07|\x1b\\)|P[^\x1b]*\x1b\\|\[[0-?]*[ -/]*[@-~]|[()][A-Z0-9]|[=>])",
        "",
        raw,
    )
    markers = {
        "dashboard": (
            "PR 51",
            "RV 52",
            "TD 7",
            "IS 44",
            "IS 42",
            "Coordinate workspace rollout",
            "✓✓PR 51",
        ),
        "dashboard-progress": ("demo/project#42", "developing (implement)"),
        "analyze-dialog": (
            "(a) Open analysis",
            "(1) Analyze with claude",
            "(2) Analyze with codex",
        ),
        "analysis-viewer": ("Shared workspace analysis", "Improve the handoff notes"),
        "viewer": ("Make it easier to pick up work together.", "Comments (0)"),
        "agent-session": (
            "code_demo_project_42",
            "Claude Code",
            "staged transcript",
            "Ready to work together.",
        ),
        "branchdiff": ("README.md (+2, -0)", "Coordinate work and share progress."),
        "code-menu": ("Code menu:", "(1) claude", "(2) codex", "(d) branchdiff", "(r) auto review"),
        "loop": ("[automerge]", "developing (implement)", "reviewing", "(closed)"),
    }
    if any("\ue000" <= char <= "\uf8ff" or char == "\ufffd" for char in text):
        raise RuntimeError(f"{name}: capture contains unsupported terminal glyphs")
    missing = [marker for marker in markers[name] if marker not in text]
    if name == "dashboard" and "\x1b[38;5;80m\x1b[1mCoordinate workspace rollout" not in raw:
        missing.append("native sub-issue title highlight")
    if missing:
        raise RuntimeError(f"{name}: capture missing expected content: {', '.join(missing)}")


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []
        self.metadata = {}
        self.navigation = set()
        self.in_nav = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if tag == "nav":
            self.in_nav = True
        if tag == "a" and self.in_nav and attrs.get("href"):
            self.navigation.add(attrs["href"])
        for name in ("href", "src", "data-full"):
            if attrs.get(name):
                self.links.append(attrs[name])
        if tag == "meta" and "content" in attrs:
            self.metadata[attrs.get("property", attrs.get("name"))] = attrs["content"]

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_nav = False


def check_pages(site):
    """Return the problems of the published pages and how many pages were read."""
    pages = {}
    for name in PAGES:
        path = site / f"{name}.html"
        page = Page()
        page.feed(path.read_text())
        pages[path] = page
    errors = []
    for path, page in pages.items():
        for link in {
            "work.html",
            "review.html",
            "agentic.html",
            "control.html",
            "index.html#install",
        } - page.navigation:
            errors.append(f"{path.name}: missing navigation link {link}")
        for key in (
            "og:title",
            "og:description",
            "og:image",
            "og:image:width",
            "og:image:height",
            "og:image:alt",
            "og:url",
            "og:type",
            "twitter:card",
            "twitter:title",
            "twitter:description",
            "twitter:image",
        ):
            if not page.metadata.get(key):
                errors.append(f"{path.name}: missing {key}")
        image = page.metadata.get("og:image")
        if image:
            image_path = unquote(urlsplit(image).path)
            if image_path.startswith("/workdash/"):
                image_path = image_path.removeprefix("/workdash/")
            if not (site / image_path).is_file():
                errors.append(f"{path.name}: missing social image {image}")
        for link in page.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_file() or (
                url.fragment
                and (target not in pages or unquote(url.fragment) not in pages[target].ids)
            ):
                errors.append(f"{path.name}: invalid link {link}")
    return errors, len(pages)


def report(site):
    errors, count = check_pages(site)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Checked {count} pages: links, images and social metadata OK")
    return 0


def config_snapshot():
    """Hash the real configuration, ignoring the log the user's own dashboard appends."""
    config = Path.home() / ".config/workdash"
    if not config.exists():
        return None
    return {
        str(path.relative_to(config)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in config.rglob("*")
        if path.is_file() and path.name != "workdash.log"
    }


if __name__ == "__main__":
    raise SystemExit(main())
