"""Show a document in the built-in Markdown viewer.

`workdash view` reads a GitHub issue or pull request with `gh`, a local file, or a
remote document, and shows it with no server dependency. The viewer follows the
document's own links and keeps the documents it visited in a history.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, MarkdownViewer, Static

from . import launcher
from .config import load_config
from .git import GitHelper
from .github_client import parse_github_item_url

_ITEM_REFERENCE_RE = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
_BARE_NUMBER_RE = re.compile(r"^#?(?P<number>\d+)$")
_PR_JSON_FIELDS = "isDraft,mergeable,mergeStateStatus,reviewDecision,headRefOid,reviews"
_FAILING_CHECKS = frozenset(
    {"ERROR", "FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
)
_MAX_REVIEW_THREADS = 50
_MAX_CHECKS = 20
_DIFF_HUNK_LINES = 10
_DOCUMENT_SUFFIXES = (".md", ".markdown")
_DOCUMENT_FETCH_TIMEOUT_SECONDS = 10
# A Markdown file on GitHub is a repository page, not a document to fetch.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})


@dataclass(frozen=True)
class Document:
    """A document's text and the address its relative links resolve against."""

    content: str
    source: str


def run_view(target: str) -> int:
    """Show the document named by a command-line target."""
    try:
        document = load_document(target)
    except (OSError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    show_document(document)
    return 0


def load_document(target: str) -> Document:
    """Read the document that a target or an in-document link points at."""
    try:
        item = _github_item(target)
        url = urlsplit(target)
    except ValueError as error:
        raise RuntimeError(f"Unsupported target {target}: {error}") from error
    if item is not None:
        repo, number = item
        try:
            issue = _gh_json(["gh", "api", f"repos/{repo}/issues/{number}"])
            pull = (
                _gh_json(
                    ["gh", "pr", "view", str(number), "--repo", repo, "--json", _PR_JSON_FIELDS]
                )
                if "pull_request" in issue
                else None
            )
            return Document(_document(repo, number, issue, pull), issue["html_url"])
        except RuntimeError as error:
            # A link's failure names the item it could not load.
            raise RuntimeError(f"Failed to load {target}: {error}") from error
    if url.scheme in ("http", "https"):
        try:
            with urllib.request.urlopen(
                target,
                context=ssl._create_unverified_context(),
                timeout=_DOCUMENT_FETCH_TIMEOUT_SECONDS,
            ) as response:
                return Document(response.read().decode("utf-8"), response.geturl())
        except (OSError, ValueError, http.client.HTTPException) as error:
            raise RuntimeError(f"Failed to read {target}: {error}") from error
    if url.scheme == "file":
        if url.netloc not in ("", "localhost"):
            raise RuntimeError(f"Unsupported file URL {target}: it does not name the local host.")
        path = Path(unquote(url.path))
    elif url.scheme:
        raise RuntimeError(
            f"Unsupported target {target}: expected a local path, a file URL, an HTTP(S) URL, "
            "or a GitHub issue or pull request."
        )
    else:
        path = Path(target)
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        raise RuntimeError(f"Failed to read {target}: {error}") from error
    return Document(content, path.resolve().as_uri())


def show_document(document: Document) -> None:
    """Show a document, following the links it contains."""
    config = load_config()
    _MarkdownApp(document, config.open_command, config.open_needs_pane).run()


class _DocumentViewer(MarkdownViewer):
    """Send every link to the app, which owns the document history."""

    async def go(self, location: str | Path) -> None:
        href = str(location)
        if href in ("", "#"):
            # An empty link names the document already on screen.
            return
        if href.startswith("#"):
            # An in-document anchor scrolls the current document.
            await super().go(href)
            return
        await self.app.follow(href)


class _MarkdownApp(App):
    BINDINGS = [
        Binding("ctrl+c", "screen.copy_text", "Copy selected text", priority=True),
        Binding("b", "back", "Back"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, document: Document, open_command: str, open_needs_pane: bool) -> None:
        super().__init__()
        self._document = document
        self._open_command = open_command
        self._open_needs_pane = open_needs_pane
        self._history: list[Document] = []

    def compose(self) -> ComposeResult:
        yield _DocumentViewer(
            self._document.content, show_table_of_contents=False, open_links=False
        )
        yield Static("", id="status", markup=False)
        yield Footer()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        return action != "back" or bool(self._history)

    async def follow(self, href: str) -> None:
        """Open a link found in the current document."""
        try:
            target = urljoin(self._document.source, href)
            url = urlsplit(target)
        except ValueError as error:
            self._set_status(f"Failed to load {href}: {error}")
            return
        if parse_github_item_url(target) is not None or (
            url.path.lower().endswith(_DOCUMENT_SUFFIXES) and url.hostname not in _GITHUB_HOSTS
        ):
            # A section of the linked document is scrolled to after that document loads.
            anchor = url.fragment
            target = urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
            await self._load(target, anchor)
            return
        try:
            command = launcher.open_in_browser(
                target,
                self._open_command,
                needs_pane=self._open_needs_pane,
                zellij_session=os.getenv("ZELLIJ_SESSION_NAME"),
                direct_nonblocking=True,
            )
        except (OSError, RuntimeError, ValueError) as error:
            self._set_status(f"Failed to open {target}: {error}")
        else:
            self._set_status(f"Opened {target} with {command}")

    async def action_back(self) -> None:
        """Return to the previous document."""
        if self._history:
            await self._show(self._history.pop())

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

    async def _load(self, target: str, anchor: str = "") -> None:
        previous = self._document
        self._set_status(f"Loading {target}...")
        try:
            document = await _read_document(target)
        except (OSError, RuntimeError) as error:
            if self._document is previous:
                # The loader's message already names the target and the reason.
                self._set_status(str(error))
            return
        if self._document is not previous:
            # The reader went back while this document was loading.
            return
        self._history.append(previous)
        await self._show(document, anchor)

    async def _show(self, document: Document, anchor: str = "") -> None:
        self._document = document
        viewer = self.query_one(_DocumentViewer)
        await viewer.document.update(document.content)
        if not (anchor and viewer.document.goto_anchor(anchor)):
            viewer.scroll_home(animate=False)
        self._set_status("")
        self.refresh_bindings()

    def _set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)


async def _read_document(target: str) -> Document:
    """Read a document in a daemon thread, so quitting never waits for a slow read."""
    loop = asyncio.get_running_loop()
    result: asyncio.Future[Document] = loop.create_future()

    def settle(document: Document | None, error: Exception | None) -> None:
        if result.done():
            return
        if error is not None:
            result.set_exception(error)
        elif document is not None:
            result.set_result(document)

    def read() -> None:
        try:
            outcome: tuple[Document | None, Exception | None] = (load_document(target), None)
        except Exception as error:  # noqa: BLE001 - re-raised where the read was awaited
            outcome = (None, error)
        # The loop is closed when the viewer quits before the read finishes.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(settle, *outcome)

    # Not the loop's executor: that one is joined on shutdown, which would stall quitting.
    threading.Thread(target=read, daemon=True).start()
    return await result


def _github_item(target: str) -> tuple[str, int] | None:
    """Return the ``owner/repo`` and number named by a GitHub target, or None."""
    parsed = parse_github_item_url(target)
    if parsed is not None:
        return parsed.repo, parsed.number
    reference = _ITEM_REFERENCE_RE.match(target.strip())
    if reference is not None:
        return reference["repo"], int(reference["number"])
    number = _BARE_NUMBER_RE.match(target.strip())
    if number is not None:
        return _origin_repo(), int(number["number"])
    return None


def _origin_repo() -> str:
    """Return the ``owner/repo`` the current directory's ``origin`` remote points at."""
    git_helper = GitHelper()
    try:
        remote_url = git_helper.remote_url(git_helper.get_repo_root(), "origin")
    except RuntimeError as error:
        raise RuntimeError(f"Failed to resolve the current repository: {error}") from error
    repo = git_helper.repo_from_remote_url(remote_url) if remote_url else ""
    if not repo:
        raise RuntimeError("Could not determine owner/repo from the origin remote.")
    return repo


def _gh_json(command: list[str]) -> Any:
    """Run a gh command and return its parsed JSON output."""
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True)
    except FileNotFoundError as error:
        raise RuntimeError(
            "Failed to fetch GitHub data: gh is not installed or on PATH."
        ) from error
    except subprocess.CalledProcessError as error:
        details = (error.stderr or "").strip() or f"exit code {error.returncode}"
        raise RuntimeError(f"Failed to fetch GitHub data with gh: {details}") from error
    try:
        if "--paginate" in command:
            pages = []
            remaining = completed.stdout.strip()
            while remaining:
                page, end = json.JSONDecoder().raw_decode(remaining)
                pages.extend(page)
                remaining = remaining[end:].lstrip()
            return pages
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Failed to parse gh output: {error.msg}") from error


def _document(repo: str, number: int, issue: dict, pull: dict | None) -> str:
    """Render the whole document, adding the pull request sections when ``pull`` is set."""
    lines = [
        f"# {issue['title']}",
        "",
        _metadata_line(repo, number, issue, pull),
        "",
        _labels_line(issue),
        "",
        "## Description",
        "",
        issue.get("body") or "*No description.*",
        "",
        *_sub_issues_section(repo, number),
    ]
    if pull is not None:
        lines += _checks_section(repo, pull["headRefOid"]) + _reviews_section(pull)
        lines += _review_threads_section(repo, number)
    lines += _comments_section(repo, number)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines += ["---", "", f"*Generated from `gh` at {timestamp}.*", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _metadata_line(repo: str, number: int, issue: dict, pull: dict | None) -> str:
    item = f"{repo}#{number}"
    fields = [
        f"**State:** {issue['state']}",
        f"**Item:** {_link(item, issue['html_url'])}",
        f"**Author:** {_login(issue.get('user'))}",
        f"**Updated:** {_date(issue['updated_at'])}",
    ]
    if pull is not None:
        fields += [
            f"**Draft:** {'yes' if pull['isDraft'] else 'no'}",
            f"**Mergeable:** {pull['mergeable']}",
            f"**Merge state:** {pull['mergeStateStatus']}",
            f"**Review:** {pull['reviewDecision'] or 'none'}",
        ]
    return " · ".join(fields)


def _labels_line(issue: dict) -> str:
    labels = ", ".join(label["name"] for label in issue.get("labels") or []) or "none"
    assignees = ", ".join(_login(assignee) for assignee in issue.get("assignees") or []) or "none"
    milestone = (issue.get("milestone") or {}).get("title") or "none"
    return " · ".join(
        [
            f"**Labels:** {labels}",
            f"**Assignees:** {assignees}",
            f"**Milestone:** {milestone}",
            f"**Comments:** {issue.get('comments', 0)}",
        ]
    )


def _sub_issues_section(repo: str, number: int) -> list[str]:
    rows = _sub_issue_rows(repo, number, depth=1)
    if not rows:
        return []
    return [
        f"## Sub-issues ({len(rows)})",
        "",
        "| Item | State | Title |",
        "| --- | --- | --- |",
        *rows,
        "",
    ]


def _sub_issue_rows(repo: str, number: int, depth: int) -> list[str]:
    rows = []
    url = f"repos/{repo}/issues/{number}/sub_issues?per_page=100"
    for sub_issue in _gh_json(["gh", "api", url, "--paginate"]):
        target = parse_github_item_url(sub_issue["html_url"])
        child_repo = target.repo if target else repo
        reference = f"{child_repo}#{sub_issue['number']}"
        item = f"{'└─ ' * depth}{_link(reference, sub_issue['html_url'])}"
        rows.append(f"| {item} | {sub_issue['state']} | {_cell(sub_issue['title'])} |")
        if (sub_issue.get("sub_issues_summary") or {}).get("total"):
            rows += _sub_issue_rows(child_repo, sub_issue["number"], depth + 1)
    return rows


def _checks_section(repo: str, head: str) -> list[str]:
    owner, _, name = repo.partition("/")
    checks = []
    cursor = None
    while True:
        query = (
            f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ "
            f"object(expression: {json.dumps(head)}) {{ ... on Commit {{ statusCheckRollup {{ "
            f"state contexts(first: 100, after: {json.dumps(cursor)}) {{ "
            "pageInfo { hasNextPage endCursor } nodes { "
            "... on CheckRun { name conclusion status detailsUrl } "
            "... on StatusContext { context state targetUrl } "
            "} } } } } } }"
        )
        payload = _gh_json(["gh", "api", "graphql", "-f", f"query={query}"])
        rollup = payload["data"]["repository"]["object"]["statusCheckRollup"]
        if rollup is None:
            return ["## CI", "", "*No checks reported.*", ""]
        contexts = rollup["contexts"]
        checks.extend(contexts["nodes"])
        if not contexts["pageInfo"]["hasNextPage"]:
            break
        cursor = contexts["pageInfo"]["endCursor"]
    if not checks:
        return ["## CI", "", "*No checks reported.*", ""]
    states = [_check_state(check) for check in checks]
    failing = [
        check for check, state in zip(checks, states, strict=True) if state in _FAILING_CHECKS
    ]
    overall = rollup["state"].lower()
    lines = [
        f"## CI — {overall}",
        "",
        f"{states.count('SUCCESS')} of {len(checks)} checks passing.",
    ]
    if failing:
        lines.append("")
        for check in failing[:_MAX_CHECKS]:
            name = check.get("name") or check.get("context") or "check"
            url = check.get("detailsUrl") or check.get("targetUrl") or ""
            lines.append(f"- {_link(name, url)} — {_check_state(check)}")
    if len(failing) > _MAX_CHECKS:
        lines += ["", f"*Truncated to the first {_MAX_CHECKS} failing checks.*"]
    return [*lines, ""]


def _reviews_section(pull: dict) -> list[str]:
    reviews = pull.get("reviews") or []
    if not reviews:
        return ["## Reviews (0)", "", "*No reviews.*", ""]
    lines = [f"## Reviews ({len(reviews)})", ""]
    for review in reviews:
        lines += [
            f"### {_login(review.get('author'))} — {review['state']}",
            "",
            review.get("body") or "*No review body.*",
            "",
        ]
    return lines


def _review_threads_section(repo: str, number: int) -> list[str]:
    owner, _, name = repo.partition("/")
    # One thread beyond the cap tells a truncated list apart from an exactly-full one.
    query = (
        f"query {{ repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) "
        f"{{ pullRequest(number: {number}) {{ "
        f"reviewThreads(first: {_MAX_REVIEW_THREADS + 1}) {{ "
        "nodes { isResolved isOutdated path line "
        "comments(first: 100) { pageInfo { hasNextPage } nodes { author { login } body diffHunk } } "
        "} } } } }"
    )
    payload = _gh_json(["gh", "api", "graphql", "-f", f"query={query}"])
    threads = payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    if not threads:
        return ["## Review threads (0)", "", "*No review threads.*", ""]
    lines = [f"## Review threads ({min(len(threads), _MAX_REVIEW_THREADS)})", ""]
    for thread in threads[:_MAX_REVIEW_THREADS]:
        comments = (thread.get("comments") or {}).get("nodes") or []
        flags = [
            flag
            for flag, key in (("resolved", "isResolved"), ("outdated", "isOutdated"))
            if thread.get(key)
        ]
        heading = f"### {thread.get('path') or '?'}:{thread.get('line') or '?'}"
        if flags:
            heading += f" — {', '.join(flags)}"
        lines += [heading, ""]
        for comment in comments:
            lines += [
                f"**{_login(comment.get('author'))}**",
                "",
                comment.get("body") or "*No comment body.*",
                "",
            ]
            hunk = "\n".join(
                (comment.get("diffHunk") or "").rstrip().splitlines()[-_DIFF_HUNK_LINES:]
            )
            if hunk:
                lines += ["```diff", hunk, "```", ""]
        if (thread.get("comments") or {}).get("pageInfo", {}).get("hasNextPage"):
            lines += ["*Comments truncated to the first 100.*", ""]
    if len(threads) > _MAX_REVIEW_THREADS:
        lines += [f"*Truncated to the first {_MAX_REVIEW_THREADS} review threads.*", ""]
    return lines


def _comments_section(repo: str, number: int) -> list[str]:
    comments = _gh_json(
        ["gh", "api", f"repos/{repo}/issues/{number}/comments?per_page=100", "--paginate"]
    )
    if not comments:
        return ["## Comments (0)", "", "*No comments.*", ""]
    lines = [f"## Comments ({len(comments)})", ""]
    for comment in comments:
        lines += [
            f"### {_login(comment.get('user'))} · {_date(comment['created_at'])}",
            "",
            comment.get("body") or "*No comment body.*",
            "",
        ]
    return lines


def _login(user: dict | None) -> str:
    return f"@{user['login']}" if user else "unknown"


def _date(timestamp: str) -> str:
    return timestamp[:10]


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _check_state(check: dict) -> str:
    return check.get("conclusion") or check.get("state") or "PENDING"
