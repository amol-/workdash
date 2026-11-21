import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import unquote, urlsplit

import pytest
from pytest_bdd import parsers, then, when


@when(
    parsers.parse("a social platform reads the {page} page of the workdash site"),
    target_fixture="site_metadata",
)
def read_site_metadata(page):
    class MetadataParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.metadata = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "meta" and "content" in attrs:
                self.metadata[attrs.get("property", attrs.get("name"))] = attrs["content"]

    parser = MetadataParser()
    parser.feed((Path(__file__).resolve().parents[3] / f"site/{page}.html").read_text())
    return page, parser.metadata


@then("it can display the workdash title, description, and dashboard image in a large preview")
def check_social_preview(site_metadata):
    page, metadata = site_metadata
    base = "https://alessandro.molina.fyi/workdash/"
    image = base + "img/og-card.png"
    title = {
        "index": "workdash — your work and your agents, together",
        "work": "Your work — workdash",
        "review": "Review pull requests together — workdash",
        "agentic": "Supervise your dispatch loop — workdash",
        "control": "Automation through the CLI — workdash",
    }[page]
    description = {
        "index": "Coordinate your work and your agents' work in a development environment you own. Share tasks, context, and terminal panes. Keep your choice of tools.",
        "work": "See what is waiting, read documents, analyze work items, and code alongside your chosen agent in the same worktree.",
        "review": "Find your review requests, understand the change, and inspect an agent's findings before you decide.",
        "agentic": "Delegate ready issues to your agent in a worktree. Supervise implementation, review and optional merging from the dashboard, and join the pane when you need.",
        "control": "Use the workdash CLI to let agents and scripts inspect your work, read live panes, and send input while you keep visibility.",
    }[page]
    assert (
        metadata.items()
        >= {
            "og:title": title,
            "og:description": description,
            "og:type": "website",
            "og:url": base + (f"{page}.html" if page != "index" else ""),
            "og:image": image,
            "og:image:width": "967",
            "og:image:height": "520",
            "og:image:alt": "workdash dashboard showing GitHub issues and pull requests",
            "twitter:card": "summary_large_image",
            "twitter:title": title,
            "twitter:description": description,
            "twitter:image": image,
        }.items()
    )


def test_site_staging(tmp_path):
    source = Path(__file__).resolve().parents[3] / "site"
    shutil.copytree(source, tmp_path / "site")
    (tmp_path / "site" / "notes.md").write_text("not for publication")
    (tmp_path / "site" / "capture.png").write_bytes(b"not for publication")
    (tmp_path / "site" / "capture").mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=tmp_path / "site" / "capture", delete=False
    ) as f:
        f.write("not for publication")
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/pages.yml").read_text()
    stage = re.search(
        r"(?ms)^      - name: Stage site\n        run: \|\n(.*?)(?=^      - )", workflow
    )
    upload = re.search(
        r"(?ms)^      - name: Upload site artifact\n.*?^          path: (\S+)", workflow
    )
    assert stage and upload
    subprocess.run(textwrap.dedent(stage.group(1)), shell=True, cwd=tmp_path, check=True)
    published_site = tmp_path / upload.group(1)
    assert {p.name for p in published_site.glob("*.html")} >= {
        "index.html",
        "work.html",
        "review.html",
        "agentic.html",
        "control.html",
    }
    for page in source.glob("*.html"):
        assert (published_site / page.name).read_bytes() == page.read_bytes()
    assert (published_site / "style.css").read_bytes() == (source / "style.css").read_bytes()
    for image in (source / "img").rglob("*"):
        if image.is_file():
            assert (published_site / image.relative_to(source)).read_bytes() == image.read_bytes()
    assert not (published_site / "notes.md").exists()
    assert not (published_site / "capture.png").exists()
    assert not (published_site / "capture").exists()
    assert "uv tool install workdash" in (published_site / "index.html").read_text()
    assert "workdash list --json" in (published_site / "control.html").read_text()


@when("a maintainer checks the site", target_fixture="site_check")
def run_site_check():
    root = Path(__file__).resolve().parents[3]
    images = root / "site/img"
    before = {p: p.read_bytes() for p in images.iterdir() if p.is_file()}
    result = subprocess.run(
        [sys.executable, "site/generate.py", "--check"], cwd=root, capture_output=True, text=True
    )
    after = {p: p.read_bytes() for p in images.iterdir() if p.is_file()}
    return result, before, after


@then("the site check succeeds without generating images")
def check_site_check(site_check):
    result, before, after = site_check
    assert result.returncode == 0, result.stdout + result.stderr
    assert before == after


def test_capture_github_stub_rejects_unknown_calls(tmp_path):
    gh = Path(__file__).resolve().parents[3] / "site/capture/gh"
    env = {**os.environ, "HOME": str(tmp_path)}
    response = subprocess.run(
        [sys.executable, str(gh), "api", "graphql", "-f", "query={ tracked: search }"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    items = json.loads(response.stdout)["data"]["tracked"]["nodes"]
    assert items[0]["title"] == "Improve the shared workspace"
    for args in (["api", "unsupported"], ["search", "prs", "--author", "someone-else"]):
        unknown = subprocess.run(
            [sys.executable, str(gh), *args], env=env, capture_output=True, text=True
        )
        assert unknown.returncode != 0
        assert " ".join(args) in unknown.stderr


@pytest.mark.parametrize("image", ["dashboard.gif", "og-card.png"])
def test_site_check_reports_broken_images(tmp_path, image):
    source = Path(__file__).resolve().parents[3] / "site"
    shutil.copytree(source, tmp_path / "site")
    (tmp_path / "site/img" / image).unlink()
    result = subprocess.run(
        [sys.executable, "site/generate.py", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert f"img/{image}" in result.stdout


@pytest.mark.parametrize("capture_fails", [False, True])
def test_capture_preserves_failure_when_configuration_changes(tmp_path, monkeypatch, capture_fails):
    source = Path(__file__).resolve().parents[3] / "site"
    monkeypatch.syspath_prepend(str(source))
    generate = importlib.import_module("generate")
    (tmp_path / "agg-1.5.0").touch()
    monkeypatch.setattr(sys, "argv", ["generate.py", "dashboard", "--work", str(tmp_path)])
    monkeypatch.setattr(
        generate, "config_snapshot", MagicMock(side_effect=[{}, {"changed": "hash"}])
    )
    monkeypatch.setattr(generate, "build", MagicMock(return_value={}))
    monkeypatch.setattr(
        generate,
        "capture",
        MagicMock(side_effect=ValueError("capture failed") if capture_fails else None),
    )
    monkeypatch.setattr(generate, "render", MagicMock())
    error = ValueError if capture_fails else RuntimeError
    message = "capture failed" if capture_fails else "Real workdash configuration changed"
    with pytest.raises(error, match=message):
        generate.main()
    assert not list(tmp_path.glob("tmp*"))


def test_site_check_reports_missing_navigation_and_metadata(tmp_path):
    source = Path(__file__).resolve().parents[3] / "site"
    shutil.copytree(source, tmp_path / "site")
    page = tmp_path / "site/index.html"
    page.write_text(
        page.read_text()
        .replace('href="review.html"', 'href="work.html"')
        .replace('property="og:type"', 'property="not-og-type"')
    )
    result = subprocess.run(
        [sys.executable, "site/generate.py", "--check"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "missing navigation link review.html" in result.stdout
    assert "missing og:type" in result.stdout


@when("a visitor browses the workdash site", target_fixture="published_site")
def browse_site():
    return Path(__file__).resolve().parents[3] / "site"


@then(
    "every page offers navigation to the workdash topics and installation",
    target_fixture="published_pages",
)
def check_navigation(published_site):
    class PageParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()
            self.links = []
            self.navigation = []
            self.in_nav = False

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.add(attrs["id"])
            if tag == "nav":
                self.in_nav = True
            for attr in ("href", "src", "data-full"):
                if attrs.get(attr):
                    self.links.append(attrs[attr])
            if self.in_nav and tag == "a":
                self.navigation.append(attrs.get("href"))

        def handle_endtag(self, tag):
            if tag == "nav":
                self.in_nav = False

    pages = {}
    for path in published_site.glob("*.html"):
        parser = PageParser()
        parser.feed(path.read_text())
        pages[path] = parser
    assert {path.name for path in pages} >= {
        "index.html",
        "work.html",
        "review.html",
        "agentic.html",
        "control.html",
    }
    for page in pages.values():
        assert set(page.navigation) >= {
            "work.html",
            "review.html",
            "agentic.html",
            "control.html",
            "index.html#install",
        }
    return pages


@then("local links and images on the pages resolve")
def check_local_links(published_pages):
    for page, parsed in published_pages.items():
        for link in parsed.links:
            url = urlsplit(link)
            if url.scheme or url.netloc:
                continue
            target = (page.parent / unquote(url.path)).resolve() if url.path else page
            assert target.is_file(), f"{page.name}: {link}"
            if url.fragment:
                assert (
                    target in published_pages
                    and unquote(url.fragment) in published_pages[target].ids
                ), f"{page.name}: {link}"
