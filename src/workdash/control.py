"""Local server-backed control surface for Workdash."""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Collection, Sequence
from contextlib import ExitStack
from datetime import UTC, datetime
from http import HTTPStatus
from ipaddress import IPv4Address, ip_address
from pathlib import Path
from socketserver import BaseServer
from wsgiref.simple_server import make_server

from .analysis_cache import AnalysisCache
from .autoclose import AutocloseSupervisor
from .backend import IncludeResult, SuggestionMarkers, WorkdashBackend, compute_suggestion_markers
from .config import WorkdashConfig
from .git import GitHelper
from .github import GithubHelper
from .github_client import parse_github_item_url
from .launcher import (
    _zellij_pane_name_for_work_action,
    close_zellij_pane,
    dump_zellij_pane,
    is_live_zellij_pane,
    launch_agent_context,
    launch_terminal_context,
    launch_vscode_context,
    load_zellij_panes,
    prepare_launch_agent_prompt,
    send_zellij_pane_input,
)
from .models import (
    REVIEW_KINDS,
    WorkItem,
    WorkItemKind,
    WorkItemType,
    display_repo,
    format_type_label,
)
from .repo_resolver import validate_repository_selectors
from .repo_worktree import ensure_worktree, existing_worktree_path, get_merge_base, main_repo_path
from .todo import create_todo, is_repository_name, is_todo_target
from .work_instruction import autostart_briefing
from .work_state import ReadyWork, find_ready_work, work_pull_request

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
# `owner/repo#42`, a Workdash item ID such as `owner/repo#CHECK-42`, or a
# targeted todo ID such as `owner/repo#ISSUE-WT42`.
_ITEM_REFERENCE_RE = re.compile(
    r"^(?P<repo>[^#\s]+)#(?:(?P<label>ISSUE|PR|REVIEW|CHECK)-(?P<todo>WT)?)?(?P<number>\d+)$"
)
_AUTODISPATCH_POLL_SECONDS = 5.0
# Zellij lists a new pane shortly after creating it; a launched agent whose pane
# is still missing after these checks is reported as unconfirmed.
_LAUNCH_VISIBILITY_CHECKS = 10
_LAUNCH_VISIBILITY_INTERVAL_SECONDS = 0.5
# An unconfirmed launch may still have started an agent, so the worktree refuses
# new launches for this long unless its pane shows up first.
_UNCONFIRMED_LAUNCH_TTL_SECONDS = 60.0


def server_address(value: str | None = None) -> tuple[str, int]:
    """Resolve a control address, applying the environment default when omitted."""
    label = "WORKDASH_HOST" if value is None else "--host"
    value = os.environ.get("WORKDASH_HOST", SERVER_HOST) if value is None else value
    host, separator, port = value.partition(":")
    if (
        not host
        or any(c.isspace() or c in "/@?#[]" for c in host)
        or (separator and (not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535))
    ):
        raise ValueError(f"Invalid {label} {value!r}: expected HOST or HOST:PORT")
    if label == "WORKDASH_HOST":
        try:
            IPv4Address(host)
        except ValueError as error:
            raise ValueError(
                f"Invalid {label} {value!r}: expected IPv4 address or IPv4:PORT"
            ) from error
    return host, int(port) if separator else SERVER_PORT


class WorkdashControlError(RuntimeError):
    """A user-visible control API or client failure."""

    def __init__(self, code: str, message: str, *, status: int = 500) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


class WorkdashSession:
    """Shared in-memory dashboard state used by the TUI and JSON API."""

    def __init__(
        self,
        *,
        config: WorkdashConfig,
        backend: WorkdashBackend,
        work_items: Sequence[WorkItem],
        suggestion_markers: SuggestionMarkers,
        zellij_session: str | None,
        items_changed_callback: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.backend = backend
        self.work_items = list(work_items)
        self.suggestion_markers = dict(suggestion_markers)
        self.zellij_session = zellij_session
        self.items_changed_callback = items_changed_callback
        self._lock = threading.RLock()
        # Normalized worktree path -> monotonic deadline of an unconfirmed agent launch.
        self._unconfirmed_launches: dict[str, float] = {}
        self.autoclose = AutocloseSupervisor(config=config, lock=self._lock)
        self._autodispatch_lock = threading.RLock()
        self._autodispatch: dict[str, _AutodispatchSession] = {}

    def list_items(self, *, refresh: bool = False) -> dict[str, object]:
        """Return the dashboard items currently known to the shared session."""

        notify_items_changed = False
        with self._lock:
            if refresh:
                self.work_items, self.suggestion_markers = self.backend.load_items()
                notify_items_changed = True
            payload = _work_items_payload(self.work_items, self.suggestion_markers)
        if notify_items_changed:
            self._notify_items_changed()
        return payload

    def info(self, *, include_all_panes: bool = False) -> dict[str, object]:
        """Return live pane state for the server-backed Workdash session."""

        if not self.zellij_session:
            raise WorkdashControlError(
                "zellij_session_required",
                "The Workdash server is not attached to a known Workdash Zellij session.",
                status=HTTPStatus.CONFLICT,
            )
        with self._lock:
            result = _pane_info_payload(
                self.zellij_session,
                self.config.workdir,
                self.work_items,
                include_all_panes=include_all_panes,
            )
        result["autodispatch_sessions"] = self.autodispatch_sessions()
        return result

    def show_config(self) -> dict[str, object]:
        """Return configured automation choices visible to local API clients."""

        return show_config_payload(self.config)

    def include_item_by_url(self, url: str) -> IncludeResult:
        """Include a GitHub URL in the shared dashboard session."""

        notify_items_changed = False
        with self._lock:
            existing_identities = {
                (item.item_type, item.repo, item.number) for item in self.work_items
            }
            result = self.backend.include_item_by_url(url, existing_identities)
            if result.duplicate_identity is not None:
                item_type, repo, number = result.duplicate_identity
                existing = next(
                    item
                    for item in self.work_items
                    if item.item_type == item_type and item.repo == repo and item.number == number
                )
                existing.included = True
                notify_items_changed = True
            elif result.fetched_item is not None:
                self.work_items.append(result.fetched_item)
                self.suggestion_markers = compute_suggestion_markers(self.work_items)
                notify_items_changed = True
        if notify_items_changed:
            self._notify_items_changed()
        return result

    def todo(
        self, *, text: str, target: str | None = None, body: str | None = None
    ) -> dict[str, object]:
        """Capture a todo as an issue in the configured todo repository.

        :param str text: Todo text, used as the issue title.
        :param str | None target: Optional repository the todo is about.
        :param str | None body: Optional content appended after the metadata block.
        """

        todo_text = text.strip()
        if not todo_text:
            raise WorkdashControlError(
                "bad_request",
                "The todo text is required.",
                status=HTTPStatus.BAD_REQUEST,
            )
        if target is not None and not is_todo_target(target):
            raise WorkdashControlError(
                "bad_request",
                f"The todo target {target!r} must be a GitHub owner/repo or a supported git URL.",
                status=HTTPStatus.BAD_REQUEST,
            )
        item = create_todo(
            todo_repository=self.config.todo_repository, text=todo_text, target=target, body=body
        )
        with self._lock:
            self.work_items.append(item)
            self.suggestion_markers = compute_suggestion_markers(self.work_items)
        self._notify_items_changed()
        return {
            "item_id": format_work_item_id(item),
            "todo_repository": item.repo,
            "target": item.todo_target,
            "number": item.number,
            "url": item.url,
        }

    def analyze(
        self, *, target: str, agent: str | None = None, prefer_cache: bool = True
    ) -> dict[str, object]:
        """Analyze an explicit target through the shared action path."""

        item = self._require_item(target)
        with self._lock:
            item_id = format_work_item_id(item)
            cache_used = item.analysis is not None and prefer_cache
            if agent == "cached":
                selected_agent = "cached"
            else:
                agents = self.config.configured_analyze_agents()
                if agent is not None and agent not in agents:
                    raise self._unusable_agent_error(agent, "analyze", "analyze")
                selected_agent = agent or (agents[0] if agents else None)
                if selected_agent is None and cache_used:
                    selected_agent = "cached"
                if selected_agent is None:
                    raise WorkdashControlError(
                        "no_agent",
                        "No analysis agents are configured.",
                        status=HTTPStatus.CONFLICT,
                    )
            path = self._analyze_item(
                item,
                tool=selected_agent or "cached",
                prefer_cache=prefer_cache,
            )
            if path is None:
                raise WorkdashControlError(
                    "analysis_failed",
                    f"Analysis failed for {item_id} with agent {selected_agent}.",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            try:
                with open(path, "rb") as analysis_file:
                    file_content = base64.b64encode(analysis_file.read()).decode("ascii")
            except OSError as error:
                raise WorkdashControlError(
                    "analysis_failed",
                    f"Analysis output could not be read for {item_id}.",
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                ) from error
            return {
                "item_id": item_id,
                "path": path,
                "url": self.analysis_url(item),
                "agent": selected_agent,
                "cache_used": cache_used,
                "status": "cached" if cache_used else "generated",
                "content_type": "text/markdown",
                "file_name": os.path.basename(path),
                "file_content": file_content,
            }

    def code(
        self,
        *,
        target: str,
        agent: str | None = None,
        allow_vscode: bool = False,
        autostart: bool = False,
        group: str | None = None,
        autoclose: bool = False,
    ) -> dict[str, object]:
        """Launch a coding agent for an explicit target unless one already works in its worktree.

        The live agent pane in a worktree is the claim on it, so instead of starting
        a second agent there this reports that pane with status ``already_running``.

        :param bool autostart: Brief a newly launched agent to work autonomously from the
            current GitHub state of the work, instead of proposing a plan first.
        :param bool autoclose: Let the server supervise the newly launched pane, nudging it
            from GitHub state and closing it when the work is done. Needs ``autostart``.
        """

        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise WorkdashControlError(
                "bad_request", "Group must be a non-empty string.", status=HTTPStatus.BAD_REQUEST
            )
        if autoclose and not autostart:
            raise WorkdashControlError(
                "bad_request",
                "An autoclosed pane must be autostarted: pass autostart with autoclose.",
                status=HTTPStatus.BAD_REQUEST,
            )
        item = self._require_item(target)
        item_id = format_work_item_id(item)
        agents = self.config.configured_code_agents()
        if agent is not None and agent not in agents and not (allow_vscode and agent == "vscode"):
            raise self._unusable_agent_error(agent, "code", "launch")
        selected_agent = agent or (agents[0] if agents else None)
        if selected_agent is None:
            raise WorkdashControlError(
                "no_agent",
                "No coding agents are configured.",
                status=HTTPStatus.CONFLICT,
            )
        # Without Zellij no agent pane can exist, and VSCode needs none to launch.
        no_zellij_vscode = self.zellij_session is None and selected_agent == "vscode"
        if autostart or item.kind == WorkItemKind.AUTHORED_PR:
            # A live agent answers without reading GitHub, so an outage cannot hide it. Only
            # a worktree known locally is checked here; the briefing may redirect the work to
            # another item, whose claim is checked below.
            with self._lock:
                repo_path = existing_worktree_path(self.config.workdir, item)
                if repo_path is not None:
                    panes = [] if no_zellij_vscode else self._live_panes()
                    claimed = self._claimed_launch(item_id, repo_path, panes, autoclose=autoclose)
                    if claimed is not None:
                        return claimed
        analysis_item = (
            item if item.item_type == WorkItemType.ISSUE or item.kind in REVIEW_KINDS else None
        )
        if item.kind == WorkItemKind.AUTHORED_PR:
            try:
                state = work_pull_request(
                    item,
                    github_username=self.config.github_username,
                    todo_repository=self.config.todo_repository,
                )
                if state and state.linked_issues:
                    linked = min(
                        state.linked_issues, key=lambda issue: (issue.repo.lower(), issue.number)
                    )
                    item = self._require_item(f"{linked.repo}#ISSUE-{linked.number}")
                    analysis_item = item
                    item_id = format_work_item_id(item)
            except RuntimeError as error:
                raise WorkdashControlError(
                    "github_error",
                    f"GitHub lookup for {target!r} failed: {error}",
                    status=HTTPStatus.BAD_GATEWAY,
                ) from error
        if (
            not autostart
            and item.item_type == WorkItemType.ISSUE
            and existing_worktree_path(self.config.workdir, item) is None
        ):
            try:
                state = work_pull_request(
                    item,
                    github_username=self.config.github_username,
                    todo_repository=self.config.todo_repository,
                )
            except RuntimeError as error:
                raise WorkdashControlError(
                    "github_error",
                    f"GitHub lookup for {target!r} failed: {error}",
                    status=HTTPStatus.BAD_GATEWAY,
                ) from error
            if state is not None and state.state == "OPEN":
                item = state.item
        if autostart:
            try:
                item, autostart_text = autostart_briefing(
                    item,
                    github_username=self.config.github_username,
                    todo_repository=self.config.todo_repository,
                )
            except RuntimeError as error:
                raise WorkdashControlError(
                    "github_error",
                    f"GitHub lookup for {target!r} failed: {error}",
                    status=HTTPStatus.BAD_GATEWAY,
                ) from error
        selected_head = None
        if item.kind == WorkItemKind.AUTHORED_PR:
            selected_head = GithubHelper().fetch_worktree_head(item)

        def verify_selected_head(path: str | Path) -> None:
            if selected_head is not None:
                branch, repo = selected_head
                if not GitHelper().branch_tracks_repository(Path(path), branch, repo):
                    raise RuntimeError(
                        f"Cannot verify worktree {path} is on {repo}'s branch {branch}. "
                        "Verify the checkout and configure its upstream to the PR head branch."
                    )

        with self._lock:
            panes = [] if no_zellij_vscode else self._live_panes()
            # Reuse a live agent and verify its checkout before any pull.
            repo_path = existing_worktree_path(self.config.workdir, item)
            if (
                selected_head is not None
                and analysis_item is not None
                and analysis_item.todo_target
            ):
                todo_path = existing_worktree_path(self.config.workdir, analysis_item)
                if todo_path is not None:
                    repo_path = todo_path
            if repo_path is None and selected_head is not None:
                branch, repo = selected_head
                repo_path = GitHelper().find_worktree_for_branch(
                    main_repo_path(self.config.workdir, repo), branch
                )
            if repo_path is not None:
                claimed = self._claimed_launch(item_id, repo_path, panes, autoclose=autoclose)
                if claimed is not None:
                    return claimed
            if repo_path is not None:
                verify_selected_head(repo_path)
            repo_path = ensure_worktree(self.config.workdir, item)
            claimed = self._claimed_launch(item_id, repo_path, panes, autoclose=autoclose)
            if claimed is not None:
                return claimed
            verify_selected_head(repo_path)
            launch_key = _normalized_path(repo_path)
            prompt = prepare_launch_agent_prompt(
                item,
                repo_path,
                analysis_path=(
                    self.analysis_url(analysis_item)
                    if analysis_item is not None and analysis_item.analysis is not None
                    else None
                ),
                merge_base=get_merge_base(repo_path),
            )
            if autostart:
                prompt = f"{prompt.rstrip()}\n\n{autostart_text}"
            if selected_agent == "vscode":
                # VSCode leaves no pane behind, so it never holds the claim.
                launch_vscode_context(repo_path, prompt)
                return {
                    "status": "launched",
                    "item_id": item_id,
                    "session": self.zellij_session,
                    "agent": selected_agent,
                    "cwd": repo_path,
                    "pane_title": None,
                    "pane_id": None,
                }
            command_tokens = self.config.code_agent_launch_command_tokens(selected_agent)
            self._unconfirmed_launches[launch_key] = (
                time.monotonic() + _UNCONFIRMED_LAUNCH_TTL_SECONDS
            )
            launch = launch_agent_context(
                repo_path,
                prompt,
                agent_command_tokens=command_tokens,
                zellij_session=self.zellij_session,
                **({"group": group} if group is not None else {}),
            )
            for check in range(_LAUNCH_VISIBILITY_CHECKS):
                if check:
                    time.sleep(_LAUNCH_VISIBILITY_INTERVAL_SECONDS)
                listed = self._live_panes()
                # No agent held the worktree before launching, so a live one now is ours.
                running = _agent_panes_in_worktree(listed, repo_path)
                if running:
                    break
                # Zellij holds the pane of an agent that exits at once, e.g. a missing binary.
                if any(
                    _format_pane_id(pane.get("id")) == launch.pane_id
                    and pane.get("title") == launch.pane_title
                    for pane in listed
                ):
                    del self._unconfirmed_launches[launch_key]
                    raise WorkdashControlError(
                        "agent_exited",
                        f"The coding agent for {item_id} exited right after launch; see pane "
                        f"{launch.pane_id} and check the agent command.",
                    )
            else:
                raise WorkdashControlError(
                    "launch_unconfirmed",
                    f"Launched a coding agent for {item_id}, but its pane did not appear in "
                    "Zellij; check `workdash info` before retrying.",
                    status=HTTPStatus.CONFLICT,
                )
            del self._unconfirmed_launches[launch_key]
            pane_id = _format_pane_id(running[0]["id"])
            launched = {
                "status": "launched",
                "item_id": item_id,
                "session": launch.session or self.zellij_session,
                "agent": selected_agent,
                "cwd": launch.cwd,
                "pane_title": launch.pane_title,
                "pane_id": pane_id,
                "tab_name": running[0].get("tab_name"),
            }
            if autoclose:
                # Only this confirmed pane is supervised; an existing one never is.
                self.autoclose.supervise(
                    session=launch.session or self.zellij_session,
                    pane_id=pane_id,
                    pane_title=str(running[0].get("title") or ""),
                    item=item,
                    repo_path=repo_path,
                )
                launched["autoclose_enabled"] = True
            return launched

    def _claimed_launch(
        self,
        item_id: str,
        repo_path: os.PathLike[str] | str,
        panes: list[dict[str, object]],
        *,
        autoclose: bool,
    ) -> dict[str, object] | None:
        """Report the live agent holding a worktree, or fail while its launch is unconfirmed.

        Called with the session lock held. ``None`` means nothing holds the worktree.
        """

        running = _agent_panes_in_worktree(panes, repo_path)
        launch_key = _normalized_path(repo_path)
        if running:
            # The live pane now holds the claim, settling any unconfirmed launch.
            self._unconfirmed_launches.pop(launch_key, None)
            claimed = {
                "status": "already_running",
                "item_id": item_id,
                "session": self.zellij_session,
                "agent": None,
                "cwd": running[0].get("pane_cwd"),
                "pane_title": running[0].get("title"),
                "pane_id": _format_pane_id(running[0]["id"]),
                "tab_name": running[0].get("tab_name"),
            }
            if autoclose:
                # The running agent is nobody's supervised session.
                claimed["autoclose_enabled"] = False
            return claimed
        if self._unconfirmed_launches.get(launch_key, 0.0) > time.monotonic():
            raise WorkdashControlError(
                "launch_unconfirmed",
                f"An earlier coding agent launch for {item_id} is still unconfirmed; "
                "check `workdash info` or retry in a minute.",
                status=HTTPStatus.CONFLICT,
            )
        return None

    def terminal(self, *, target: str) -> dict[str, object]:
        """Open a terminal for an explicit target."""

        item = self._require_item(target)
        with self._lock:
            item_id = format_work_item_id(item)
            repo_path = ensure_worktree(self.config.workdir, item)
            launch = launch_terminal_context(
                repo_path,
                zellij_session=self.zellij_session,
            )
            return {
                "item_id": item_id,
                "session": launch.session or self.zellij_session,
                "cwd": launch.cwd,
                "pane_title": launch.pane_title,
                "pane_id": launch.pane_id,
            }

    def ready(self, *, scope: str, fail_unconfirmed: bool = False) -> dict[str, object]:
        """Report the ready work of a scope, ready pull requests first.

        The scope is ``owner/repo``, an issue, ``todo``, or ``any``. Work a live
        agent pane already holds is left out. Nothing is claimed or launched, and
        the dashboard is left alone.
        """

        return {
            "scope": scope,
            "items": [_ready_work_payload(work) for work in self._ready(scope, fail_unconfirmed)],
        }

    def _ready(
        self, scope: str, fail_unconfirmed: bool, *, include_shared_pr_issues: bool = False
    ) -> list[ReadyWork]:
        issue = None
        if scope == "todo":
            repositories = [self.config.todo_repository]
        elif scope == "any":
            repositories = [
                *validate_repository_selectors(list(self.config.repositories)),
                self.config.todo_repository,
            ]
        elif is_repository_name(scope):
            repositories = [scope]
        else:
            reference = _ITEM_REFERENCE_RE.match(scope)
            parsed_url = parse_github_item_url(scope)
            if (reference is not None and reference["label"] not in {None, "ISSUE"}) or (
                parsed_url is not None and parsed_url.item_type == WorkItemType.PR
            ):
                raise WorkdashControlError(
                    "unsupported_item_type",
                    f"{scope!r} names a pull request; ready takes an issue as its scope.",
                    status=HTTPStatus.BAD_REQUEST,
                )
            item = self._require_item(scope)
            repositories, issue = [], (item.repo, item.number)
        try:
            candidates = find_ready_work(
                repositories=repositories,
                issue=issue,
                github_username=self.config.github_username,
                todo_repository=self.config.todo_repository,
                include_shared_pr_issues=include_shared_pr_issues,
            )
        except RuntimeError as error:
            raise WorkdashControlError(
                "github_error",
                f"GitHub lookup for {scope!r} failed: {error}",
                status=HTTPStatus.BAD_GATEWAY,
            ) from error
        with self._lock:
            panes = self._live_panes()
            now = time.monotonic()
            if fail_unconfirmed:
                for work in candidates:
                    for claim in work.claims:
                        path = existing_worktree_path(self.config.workdir, claim)
                        if path is not None:
                            self._claimed_launch(
                                format_work_item_id(claim), path, panes, autoclose=False
                            )
            # An agent launch `code` has not confirmed yet holds its worktree too.
            unclaimed = [
                work
                for work in candidates
                if not any(
                    (path := existing_worktree_path(self.config.workdir, claim)) is not None
                    and (
                        _agent_panes_in_worktree(panes, path)
                        or self._unconfirmed_launches.get(_normalized_path(path), 0.0) > now
                    )
                    for claim in work.claims
                )
            ]
        return unclaimed

    def start_autodispatch(
        self, *, scope: str, group: str, agent: str | None = None
    ) -> dict[str, object]:
        """Start an independent server-owned sequence of dispatches."""
        if (
            not isinstance(scope, str)
            or not scope.strip()
            or not isinstance(group, str)
            or not group.strip()
        ):
            raise WorkdashControlError(
                "bad_request",
                "Scope and group must be non-empty strings.",
                status=HTTPStatus.BAD_REQUEST,
            )
        entry = _AutodispatchSession(self, scope.strip(), group.strip(), agent)
        with self._autodispatch_lock:
            self._autodispatch[entry.id] = entry
            entry.thread.start()
        return entry.snapshot()

    def autodispatch_sessions(self) -> list[dict[str, object]]:
        with self._autodispatch_lock:
            return [entry.snapshot() for entry in self._autodispatch.values()]

    def stop_autodispatch(self, session_id: str) -> dict[str, object]:
        with self._autodispatch_lock:
            entry = self._autodispatch.get(session_id)
            if entry is None:
                raise WorkdashControlError(
                    "not_found", "Autodispatch session not found.", status=HTTPStatus.NOT_FOUND
                )
            entry.stop()
            return entry.snapshot()

    def stop_autodispatch_sessions(self) -> None:
        with self._autodispatch_lock:
            for entry in self._autodispatch.values():
                entry.stop()

    def dispatch_once(
        self, *, scope: str, agent: str | None = None, group: str | None = None
    ) -> dict[str, object]:
        """Launch at most one currently ready issue, in ready order."""
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise WorkdashControlError(
                "bad_request", "Group must be a non-empty string.", status=HTTPStatus.BAD_REQUEST
            )
        candidates = self._ready(scope, True, include_shared_pr_issues=True)
        for candidate in candidates:
            candidate_id = format_work_item_id(candidate.item)
            had_pull_request = candidate.pull_request is not None
            current = self._ready(candidate_id, True)
            candidate = next(
                (work for work in current if format_work_item_id(work.item) == candidate_id), None
            )
            if candidate is None or (had_pull_request and candidate.pull_request is None):
                continue
            implementation = (
                candidate.pull_request.item if candidate.pull_request else candidate.item
            )
            selected_group = group or display_repo(implementation).rsplit("/", 1)[-1]
            launch = self.code(
                target=candidate_id,
                agent=agent,
                group=selected_group,
                autostart=True,
                autoclose=True,
            )
            if launch["status"] == "already_running":
                continue
            return {**_ready_work_payload(candidate), **launch, "group": selected_group}
        return {"status": "no_ready_task", "scope": scope}

    def pane_content(self, *, pane_id: str, full: bool = False) -> dict[str, object]:
        """Return visible or full scrollback content for a Zellij pane."""

        if not self.zellij_session:
            raise WorkdashControlError(
                "zellij_session_required",
                "Pane content requires a known Workdash Zellij session.",
                status=HTTPStatus.CONFLICT,
            )
        content = dump_zellij_pane(self.zellij_session, pane_id, full=full)
        return {"pane_id": pane_id, "content": content, "full": full}

    def pane_send(self, *, pane_id: str, data: str, raw: bool = False) -> dict[str, object]:
        """Send input to a Zellij pane, appending Enter unless raw is requested."""

        if not self.zellij_session:
            raise WorkdashControlError(
                "zellij_session_required",
                "Pane input requires a known Workdash Zellij session.",
                status=HTTPStatus.CONFLICT,
            )
        send_zellij_pane_input(self.zellij_session, pane_id, data, raw=raw)
        return {"pane_id": pane_id, "raw": raw, "accepted": True}

    def pane_close(self, *, pane_id: str) -> dict[str, object]:
        """Close a Zellij pane by its ID."""

        if not self.zellij_session:
            raise WorkdashControlError(
                "zellij_session_required",
                "Pane close requires a known Workdash Zellij session.",
                status=HTTPStatus.CONFLICT,
            )
        close_zellij_pane(self.zellij_session, pane_id)
        return {"pane_id": pane_id, "accepted": True}

    def _notify_items_changed(self) -> None:
        if self.items_changed_callback is not None:
            self.items_changed_callback()

    def _analyze_item(self, item: WorkItem, *, tool: str, prefer_cache: bool) -> str | None:
        if prefer_cache:
            cached_path = self.backend.analyze_item(item, tool="cached")
            if cached_path is not None:
                return cached_path
        cwd = ensure_worktree(self.config.workdir, item) if tool != "cached" else None
        return self.backend.analyze_item(item, tool=tool, cwd=cwd)

    def analysis_url(self, item: WorkItem) -> str:
        """Return the HTTP URL under which this item's analysis markdown is served."""

        return _analysis_http_url(item)

    def _require_item(self, target: str) -> WorkItem:
        """Resolve an explicit target, fetching it from GitHub when the dashboard lacks it.

        The fetched item never joins the dashboard: what the dashboard shows is a
        presentation choice, not a limit on which items can be acted on.
        """

        parsed_url = parse_github_item_url(target)
        reference = _ITEM_REFERENCE_RE.match(target)
        if parsed_url is not None:
            repo, number, item_type = parsed_url.repo, parsed_url.number, parsed_url.item_type
            targeted_todo = False
        elif (
            reference is not None
            and is_repository_name(reference["repo"])
            and (reference["todo"] is None or reference["label"] == "ISSUE")
        ):
            # A targeted todo is an issue of the todo repository, listed under its target.
            targeted_todo = reference["todo"] is not None
            repo = self.config.todo_repository if targeted_todo else reference["repo"]
            number = int(reference["number"])
            item_type = (
                WorkItemType.ISSUE if reference["label"] in {None, "ISSUE"} else WorkItemType.PR
            )
        else:
            raise WorkdashControlError(
                "invalid_target",
                f"{target!r} is not a Workdash item ID, an owner/repo#NUMBER issue, "
                "or a GitHub issue or pull request URL.",
                status=HTTPStatus.BAD_REQUEST,
            )
        item = next(
            (item for item in self.work_items if (item.repo, item.number) == (repo, number)), None
        )
        if item is None:
            try:
                item = self.backend.github_client.fetch_item(
                    repo,
                    number,
                    github_username=self.config.github_username,
                    todo_repository=self.config.todo_repository,
                )
            except RuntimeError as error:
                raise WorkdashControlError(
                    "github_error",
                    f"GitHub lookup for {target!r} failed: {error}",
                    status=HTTPStatus.BAD_GATEWAY,
                ) from error
            if item is not None:
                item.analysis = self.backend.analysis_cache.load_analysis(item)
                item.analyzed_at = self.backend.analysis_cache.load_analysis_date(item)
        if item is None or (targeted_todo and format_work_item_id(item) != target):
            raise WorkdashControlError(
                "unknown_item",
                f"No accessible GitHub issue or pull request matches {target!r}.",
                status=HTTPStatus.NOT_FOUND,
            )
        if item.item_type != item_type:
            kind, label = (
                ("a pull request", "PR")
                if item.item_type == WorkItemType.PR
                else ("an issue", "ISSUE")
            )
            raise WorkdashControlError(
                "unsupported_item_type",
                f"{item.repo}#{number} is {kind}; use {item.repo}#{label}-{number}.",
                status=HTTPStatus.BAD_REQUEST,
            )
        return item

    def _live_panes(self) -> list[dict[str, object]]:
        """Return the Zellij panes, failing closed when they cannot be read."""

        if not self.zellij_session:
            raise WorkdashControlError(
                "zellij_session_required",
                "Coding agents need a known Workdash Zellij session to check for running agents.",
                status=HTTPStatus.CONFLICT,
            )
        try:
            return load_zellij_panes(self.zellij_session)
        except RuntimeError as error:
            raise WorkdashControlError(
                "zellij_unavailable",
                f"Cannot check for running coding agents: {error}",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            ) from error

    def _unusable_agent_error(self, agent: str, action: str, command: str) -> WorkdashControlError:
        """Explain why an agent cannot perform an action: unknown name or missing command."""
        if agent not in self.config.agents:
            return WorkdashControlError(
                "unknown_agent",
                f"Agent {agent!r} is not configured.",
                status=HTTPStatus.BAD_REQUEST,
            )
        return WorkdashControlError(
            "unsupported_agent",
            f"Agent {agent!r} cannot {action}: it has no {command} command.",
            status=HTTPStatus.BAD_REQUEST,
        )

    def get_agent_panes_for_item(self, item: WorkItem) -> list[dict[str, object]]:
        """Return the live agent panes working in the item's worktree, lowest pane ID first."""

        worktree = existing_worktree_path(self.config.workdir, item)
        if worktree is None or not self.zellij_session:
            return []
        item_by_cwd = {_normalized_path(worktree): format_work_item_id(item)}
        return [
            _pane_payload(self.zellij_session, pane, item_by_cwd, kind="agent")
            for pane in _agent_panes_in_worktree(self._live_panes(), worktree)
        ]

    def focus_pane(self, pane_id: str) -> None:
        """Focus a Zellij pane by its ID.

        Raises RuntimeError if the focus command fails.
        """
        if not self.zellij_session:
            raise RuntimeError("No Zellij session available for focusing panes.")
        from .launcher import focus_zellij_pane

        focus_zellij_pane(self.zellij_session, pane_id)


class _AutodispatchSession:
    """One independently running sequence of supervised dispatches."""

    def __init__(self, owner: WorkdashSession, scope: str, group: str, agent: str | None):
        self.owner, self.scope, self.group, self.agent = owner, scope, group, agent
        self.id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.started = time.monotonic()
        self.ended: float | None = None
        self.status = "running"
        self.current_item: str | None = None
        self.error: str | None = None
        self.event = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name=f"autodispatch-{self.id}", daemon=True)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "id": self.id,
                "scope": self.scope,
                "group": self.group,
                "agent": self.agent,
                "started_at": self.started_at,
                "elapsed_seconds": int((self.ended or time.monotonic()) - self.started),
                "current_item": self.current_item,
                "status": self.status,
                "error": self.error,
            }

    def stop(self) -> None:
        with self.lock:
            self.event.set()
            if self.status == "running":
                self.status = "stopped"
                self.ended = time.monotonic()

    def run(self) -> None:
        try:
            while not self.event.is_set():
                result = self.owner.dispatch_once(
                    scope=self.scope, group=self.group, agent=self.agent
                )
                with self.lock:
                    if self.event.is_set():
                        return
                    if result["status"] == "no_ready_task":
                        self.status = "finished"
                        self.current_item = None
                        self.ended = time.monotonic()
                        return
                    self.current_item = str(result["item_id"])
                while not self.event.wait(_AUTODISPATCH_POLL_SECONDS):
                    panes = load_zellij_panes(str(result["session"]))
                    if not any(
                        _format_pane_id(pane.get("id")) == result["pane_id"]
                        and pane.get("is_plugin") is not True
                        for pane in panes
                    ):
                        break
        except Exception as error:
            with self.lock:
                if not self.event.is_set():
                    self.status = "failed"
                    self.error = str(error)
                    self.ended = time.monotonic()


class WorkdashControlServer:
    """Background HTTP listeners for a server-backed Workdash session."""

    def __init__(
        self, session: WorkdashSession, *, listeners: Sequence[tuple[str, Sequence[str]]] = ()
    ) -> None:
        self._session = session
        self._listeners = listeners
        self._httpds: list[BaseServer] = []
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """Bind every listener before serving the shared JSON API."""

        primary_host, port = server_address()
        accepted_ips = {
            "127.0.0.1",
            primary_host,
            *(ip for _host, ips in self._listeners for ip in ips),
        }
        host = primary_host
        try:
            app = _source_filtered_wsgi_app(_make_turbogears_app(self._session), accepted_ips)
            for host in [
                primary_host,
                *(host for host, _ips in self._listeners if host != primary_host),
            ]:
                httpd = make_server(host, port, app)
                self._httpds.append(httpd)
                if ip_address(httpd.server_address[0]).is_unspecified:
                    raise OSError("Wildcard listener binds are not allowed")
        except OSError as error:
            self.stop()
            raise RuntimeError(
                f"Cannot bind Workdash server at {host}:{port}: {error}. "
                "Is another `workdash --server` running?"
            ) from error
        for httpd in self._httpds:
            thread = threading.Thread(
                target=httpd.serve_forever,
                name="workdash-control-server",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)
        self._session.autoclose.start()

    def stop(self) -> None:
        self._session.stop_autodispatch_sessions()
        self._session.autoclose.stop()
        for httpd in self._httpds[: len(self._threads)]:
            httpd.shutdown()
        for httpd in self._httpds:
            httpd.server_close()
        for thread in self._threads:
            thread.join()
        self._httpds.clear()
        self._threads.clear()


class WorkdashControlClient:
    """HTTP client used by thin CLI commands."""

    def __init__(self, host: str | None = None) -> None:
        selected_host, port = server_address(host)
        self._address = f"{selected_host}:{port}"

    def request(self, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"http://{self._address}/api/v0/{endpoint}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            api_error = _client_error_from_body(error)
            raise WorkdashControlError(
                api_error.code,
                f"Workdash server at {self._address}: {api_error}",
                status=api_error.status,
            ) from error
        except OSError as error:
            raise WorkdashControlError(
                "server_unreachable",
                f"No Workdash server is reachable at {self._address}. "
                "Start one with `workdash --server`.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            ) from error
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as error:
            raise WorkdashControlError(
                "invalid_response",
                f"Workdash server at {self._address} returned invalid JSON: {error.msg}",
            ) from error
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise WorkdashControlError(
                "invalid_response",
                f"Workdash server at {self._address} returned an invalid response.",
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise WorkdashControlError(
                "invalid_response", f"Workdash server at {self._address} result was not an object."
            )
        return result


def format_work_item_id(item: WorkItem) -> str:
    """Return the copy/paste identifier accepted by work-item commands."""

    item_type = format_type_label(item).removesuffix("+")
    if item.todo_target is not None:
        # A targeted todo is listed under its target, so the ID is marked to
        # never collide with the target's own issue of the same number.
        return f"{display_repo(item)}#{item_type}-WT{item.number}"
    return f"{item.repo}#{item_type}-{item.number}"


def show_config_payload(config: WorkdashConfig) -> dict[str, object]:
    host, port = server_address()
    return {
        "agents": {
            "analyze": config.configured_analyze_agents(),
            "code": config.configured_code_agents(),
        },
        "server": {"host": host, "port": port},
    }


def _analysis_http_url(item: WorkItem) -> str:
    """Return the absolute HTTP URL serving ``item``'s cached analysis markdown."""

    host, port = server_address()
    return f"http://{host}:{port}{AnalysisCache.build_analysis_url_path(item)}"


def _work_items_payload(
    work_items: Sequence[WorkItem], suggestion_markers: SuggestionMarkers
) -> dict[str, object]:
    items = []
    for item in sorted(work_items, key=lambda entry: entry.updated_at, reverse=True):
        items.append(
            {
                "id": format_work_item_id(item),
                "type": item.item_type.value,
                "display_type": format_type_label(item),
                "kind": item.kind.value,
                "repo": display_repo(item),
                "number": item.number,
                "title": item.title,
                "url": item.url,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
                "suggested": bool(
                    suggestion_markers.get((item.item_type, item.repo, item.number), "")
                ),
                "analysis_available": item.analysis is not None,
                "analysis_url": _analysis_http_url(item) if item.analysis is not None else None,
            }
        )
    return {"items": items}


def _ready_work_payload(work: ReadyWork) -> dict[str, object]:
    item, pull_request = work.item, work.pull_request
    payload: dict[str, object] = {
        "id": format_work_item_id(item),
        "type": item.item_type.value,
        "repo": display_repo(item),
        "number": item.number,
        "title": item.title,
        "url": item.url,
        "created_at": item.created_at.isoformat(),
        "action": work.action,
        "reasons": list(work.reasons),
        "issue_id": format_work_item_id(item),
        "issue_url": item.url,
    }
    if pull_request is not None:
        payload |= {
            "pr_id": format_work_item_id(pull_request.item),
            "pr_url": pull_request.item.url,
            "pr_head_sha": pull_request.head_sha,
            "base_ref": pull_request.base_ref,
            "base_head_sha": pull_request.base_head_sha,
            "mergeability": pull_request.mergeability,
            "ci_status": pull_request.ci_status,
            "failing_checks": list(pull_request.failing_checks),
            "feedback": [
                {
                    "kind": feedback.kind,
                    "author": feedback.author,
                    "url": feedback.url,
                    "created_at": feedback.created_at.isoformat(),
                }
                for feedback in pull_request.feedback
            ],
        }
    return payload


def _pane_info_payload(
    session: str,
    workdir: str | None,
    work_items: Sequence[WorkItem],
    *,
    include_all_panes: bool = False,
) -> dict[str, object]:
    item_by_cwd = {}
    if workdir is not None:
        for item in work_items:
            item_path = existing_worktree_path(workdir, item)
            if item_path is not None:
                item_by_cwd[_normalized_path(item_path)] = format_work_item_id(item)
    panes = []
    for pane in load_zellij_panes(session):
        if _is_workdash_work_pane(pane):
            panes.append(
                _pane_payload(selected_session=session, pane=pane, item_by_cwd=item_by_cwd)
            )
        elif include_all_panes and is_live_zellij_pane(pane):
            panes.append(
                _pane_payload(
                    selected_session=session,
                    pane=pane,
                    item_by_cwd={},
                    kind="unknown",
                )
            )
    return {"session": session, "panes": panes}


def _make_turbogears_app(session: WorkdashSession):
    # TurboGears is imported only when `--server` starts so ordinary client commands
    # can still report "server not running" without constructing the web app.
    from tg import MinimalApplicationConfigurator, TGController, expose, request, response

    class PaneController(TGController):
        def __init__(self, workdash_session: WorkdashSession) -> None:
            self._session = workdash_session

        @expose("json")
        def content(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.pane_content(
                    pane_id=_required_text(payload, "pane_id"),
                    full=bool(payload.get("full", False)),
                ),
            )

        @expose("json")
        def send(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.pane_send(
                    pane_id=_required_text(payload, "pane_id"),
                    data=_required_text(payload, "data"),
                    raw=bool(payload.get("raw", False)),
                ),
            )

        @expose("json")
        def close(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.pane_close(
                    pane_id=_required_text(payload, "pane_id"),
                ),
            )

    class V0Controller(TGController):
        def __init__(self, workdash_session: WorkdashSession) -> None:
            self._session = workdash_session
            self.pane = PaneController(workdash_session)

        @expose("json")
        def list(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.list_items(
                    refresh=bool(payload.get("refresh", False))
                ),
            )

        @expose("json")
        def info(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.info(
                    include_all_panes=bool(payload.get("include_all_panes", False))
                ),
            )

        @expose("json")
        def analyze(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.analyze(
                    target=_required_text(payload, "target"),
                    agent=_optional_text(payload, "agent"),
                ),
            )

        @expose("json")
        def code(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.code(
                    target=_required_text(payload, "target"),
                    agent=_optional_text(payload, "agent"),
                    autostart=_optional_bool(payload, "autostart"),
                    **({"group": _required_text(payload, "group")} if "group" in payload else {}),
                    autoclose=_optional_bool(payload, "autoclose"),
                ),
            )

        @expose("json")
        def terminal(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.terminal(
                    target=_required_text(payload, "target"),
                ),
            )

        @expose("json")
        def ready(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.ready(scope=_required_text(payload, "scope")),
            )

        @expose("json")
        def dispatch(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.dispatch_once(
                    scope=_required_text(payload, "scope"),
                    agent=_optional_text(payload, "agent"),
                    **({"group": _required_text(payload, "group")} if "group" in payload else {}),
                ),
            )

        @expose("json")
        def autodispatch(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.start_autodispatch(
                    scope=_required_text(payload, "scope"),
                    group=_required_text(payload, "group"),
                    agent=_optional_text(payload, "agent"),
                ),
            )

        @expose("json")
        def autodispatch_stop(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.stop_autodispatch(_required_text(payload, "id")),
            )

        @expose("json")
        def todo(self):
            return _handle_json_request(
                request,
                response,
                lambda payload: self._session.todo(
                    text=_required_text(payload, "text"),
                    target=_optional_text(payload, "target"),
                    body=_optional_text(payload, "body"),
                ),
            )

        @expose("json")
        def show_config(self):
            return _handle_json_request(
                request, response, lambda _payload: self._session.show_config()
            )

        @expose("json")
        def _default(self, *path):
            if path == ("show-config",):
                return _handle_json_request(
                    request, response, lambda _payload: self._session.show_config()
                )
            response.status_int = HTTPStatus.NOT_FOUND
            return _error_envelope("not_found", "Unknown Workdash API endpoint.")

    class ItemsController(TGController):
        def __init__(self, workdash_session: WorkdashSession) -> None:
            self._session = workdash_session

        @expose()
        def _default(self, *path):
            try:
                if request.method != "GET":
                    raise WorkdashControlError(
                        "method_not_allowed", "Only GET is supported.", status=405
                    )
                if len(path) == 3 and path[2] == "analysis":
                    return self._serve_analysis(path)
                if len(path) < 3 or path[2] != "files":
                    raise WorkdashControlError("not_found", "Unknown endpoint.", status=404)
                item = self._session._require_item(f"{path[0]}/{path[1]}")
                parts = tuple(request.path_info.split("/")[6:])
                listing = not parts or parts == ("",)
                if not listing and (
                    parts[0] != ".workdash" or any(p in (".", "..", "") for p in parts)
                ):
                    raise WorkdashControlError("not_found", "Artifact not found.", status=404)
                with ExitStack() as stack:
                    selected = existing_worktree_path(
                        self._session.config.workdir, item, pinned=stack
                    )
                    if selected is None:
                        raise WorkdashControlError("not_found", "Worktree not found.", status=404)
                    worktree, fd = selected
                    worktree = worktree.resolve(strict=True)
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    fd = os.open(".workdash", flags, dir_fd=fd)
                    stack.callback(os.close, fd)
                    root = worktree / ".workdash"
                    if listing:
                        entries = []
                        for name in os.listdir(fd):
                            try:
                                with ExitStack() as entry_stack:
                                    directories = [fd]
                                    pending = [name]
                                    links = 0
                                    while pending:
                                        part = pending.pop(0)
                                        if part in ("", "."):
                                            continue
                                        if part == "..":
                                            if len(directories) == 1:
                                                raise ValueError("Symlink escapes artifact root")
                                            directories.pop()
                                            continue
                                        info = os.stat(
                                            part, dir_fd=directories[-1], follow_symlinks=False
                                        )
                                        if stat.S_ISLNK(info.st_mode):
                                            links += 1
                                            if links > 40:
                                                raise ValueError("Too many symlinks")
                                            target = os.readlink(part, dir_fd=directories[-1])
                                            if os.path.isabs(target):
                                                target = str(Path(target).relative_to(root))
                                                directories = directories[:1]
                                            pending = target.split("/") + pending
                                        elif pending:
                                            child = os.open(part, flags, dir_fd=directories[-1])
                                            entry_stack.callback(os.close, child)
                                            directories.append(child)
                            except (OSError, ValueError):
                                continue
                            entries.append(name)
                        entries.sort()
                        response.content_type = "application/json"
                        return json.dumps({"ok": True, "result": {"entries": entries}}).encode()
                    if len(parts) < 2:
                        raise WorkdashControlError("not_found", "Artifact not found.", status=404)
                    # Resolve aliases first, then reopen only beneath the pinned root without links.
                    resolved = (root.joinpath(*parts[1:])).resolve(strict=True).relative_to(root)
                    if not resolved.parts:
                        raise WorkdashControlError("not_found", "Artifact not found.", status=404)
                    for part in resolved.parts[:-1]:
                        fd = os.open(part, flags, dir_fd=fd)
                        stack.callback(os.close, fd)
                    fd = os.open(
                        resolved.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd
                    )
                    with os.fdopen(fd, "rb") as artifact:
                        if not stat.S_ISREG(os.fstat(fd).st_mode):
                            raise WorkdashControlError(
                                "not_found", "Artifact not found.", status=404
                            )
                        content = artifact.read()
                    response.content_type = "text/plain"
                    return content
            except WorkdashControlError as error:
                response.status_int = error.status
                response.content_type = "application/json"
                return json.dumps(_error_envelope(error.code, str(error))).encode()
            except (OSError, ValueError, RuntimeError):
                response.status_int = HTTPStatus.NOT_FOUND
                response.content_type = "application/json"
                return json.dumps(_error_envelope("not_found", "Artifact not found.")).encode()

        def _serve_analysis(self, path) -> bytes:
            """Serve the cached analysis markdown of an item as text/markdown."""

            item = self._session._require_item(f"{path[0]}/{path[1]}")
            analysis = self._session.backend.analysis_cache.load_analysis(item)
            if analysis is None:
                raise WorkdashControlError(
                    "not_found", "No cached analysis for this item.", status=404
                )
            response.content_type = "text/markdown; charset=utf-8"
            return analysis.encode("utf-8")

    class ApiController(TGController):
        def __init__(self, workdash_session: WorkdashSession) -> None:
            self.v0 = V0Controller(workdash_session)
            self.items = ItemsController(workdash_session)

    class RootController(TGController):
        def __init__(self, workdash_session: WorkdashSession) -> None:
            self.api = ApiController(workdash_session)

    config = MinimalApplicationConfigurator()
    config.update_blueprint({"root_controller": RootController(session)})
    return config.make_wsgi_app()


def _handle_json_request(request, response, action) -> dict[str, object]:
    if request.method != "POST":
        response.status_int = HTTPStatus.METHOD_NOT_ALLOWED
        return _error_envelope(
            "method_not_allowed", "Workdash API endpoints accept POST JSON only."
        )
    try:
        body = request.body or b"{}"
        if isinstance(body, str):
            body = body.encode("utf-8")
        payload = json.loads(body.decode("utf-8") or "{}")
        if not isinstance(payload, dict):
            raise WorkdashControlError(
                "invalid_json",
                "Request JSON body must be an object.",
                status=HTTPStatus.BAD_REQUEST,
            )
        return {"ok": True, "result": action(payload)}
    except WorkdashControlError as error:
        response.status_int = error.status
        return _error_envelope(error.code, str(error))
    except RuntimeError as error:
        response.status_int = HTTPStatus.INTERNAL_SERVER_ERROR
        return _error_envelope("server_error", str(error))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        response.status_int = HTTPStatus.BAD_REQUEST
        return _error_envelope("invalid_json", f"Request body is not valid JSON: {error}")


def _source_filtered_wsgi_app(app, accepted_ips: Collection[str] = ()):
    allowed = {ip_address(ip) for ip in accepted_ips} | {ip_address("127.0.0.1"), ip_address("::1")}

    def source_filtered(environ, start_response):
        try:
            accepted = ip_address(environ.get("REMOTE_ADDR", "")) in allowed
        except ValueError:
            accepted = False
        if not accepted:
            body = json.dumps(
                _error_envelope("forbidden", "Workdash server does not accept this source IP.")
            ).encode("utf-8")
            start_response(
                "403 Forbidden",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]
        return app(environ, start_response)

    return source_filtered


def _client_error_from_body(error: urllib.error.HTTPError) -> WorkdashControlError:
    raw = error.read().decode("utf-8", errors="replace")
    try:
        envelope = json.loads(raw)
        api_error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(api_error, dict):
            return WorkdashControlError(
                str(api_error.get("code") or "server_error"),
                str(api_error.get("message") or error.reason),
                status=error.code,
            )
    except json.JSONDecodeError:
        pass
    return WorkdashControlError("server_error", raw or str(error.reason), status=error.code)


def _error_envelope(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _required_text(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise WorkdashControlError(
            "bad_request",
            f"Missing required string field {field!r}.",
            status=HTTPStatus.BAD_REQUEST,
        )
    return value


def _optional_text(payload: dict[str, object], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise WorkdashControlError(
            "bad_request",
            f"Optional field {field!r} must be a non-empty string.",
            status=HTTPStatus.BAD_REQUEST,
        )
    return value


def _optional_bool(payload: dict[str, object], field: str) -> bool:
    value = payload.get(field)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise WorkdashControlError(
            "bad_request",
            f"Optional field {field!r} must be a boolean.",
            status=HTTPStatus.BAD_REQUEST,
        )
    return value


def _is_workdash_work_pane(pane: dict[str, object]) -> bool:
    if not is_live_zellij_pane(pane):
        return False
    title = pane.get("title")
    return isinstance(title, str) and (title.startswith("code_") or title.startswith("terminal_"))


def _pane_payload(
    selected_session: str,
    pane: dict[str, object],
    item_by_cwd: dict[str, str],
    *,
    kind: str | None = None,
) -> dict[str, object]:
    title = str(pane.get("title") or "")
    pane_id = pane.get("id")
    kind = kind or ("agent" if title.startswith("code_") else "terminal")
    cwd = pane.get("pane_cwd")
    mapped_item = None
    if isinstance(cwd, str) and cwd:
        normalized_cwd = _normalized_path(cwd)
        matches = [
            (root, item_id)
            for root, item_id in item_by_cwd.items()
            if normalized_cwd == root or normalized_cwd.startswith(root + os.sep)
        ]
        if matches:
            mapped_item = max(matches, key=lambda match: len(match[0]))[1]
    state = pane.get("state")
    if not isinstance(state, str) or not state:
        state = "exited" if pane.get("exited") is True else "running"
    return {
        "session": selected_session,
        "tab_id": pane.get("tab_id"),
        "tab_name": pane.get("tab_name"),
        "pane_id": _format_pane_id(pane_id),
        "title": title,
        "cwd": cwd,
        "command": pane.get("pane_command") or pane.get("terminal_command"),
        "kind": kind,
        "state": state,
        "exited": pane.get("exited"),
        "focused": pane.get("is_focused"),
        "floating": pane.get("is_floating"),
        "item": mapped_item or "unknown",
    }


def _format_pane_id(pane_id: object) -> str:
    pane_id_text = str(pane_id)
    if pane_id_text.startswith("terminal_"):
        return pane_id_text
    return f"terminal_{pane_id_text}"


def _normalized_path(path: os.PathLike[str] | str) -> str:
    return os.path.realpath(os.path.expanduser(os.fspath(path)))


def _agent_panes_in_worktree(
    panes: Sequence[dict[str, object]], worktree: os.PathLike[str] | str
) -> list[dict[str, object]]:
    """Return the live agent panes holding the claim on ``worktree``, lowest pane ID first.

    Only the launch title ties a pane to its worktree: an agent may change directory
    into another worktree, and Zellij keeps the launch title even when it sets its own.
    """

    title = _zellij_pane_name_for_work_action("code", os.fspath(worktree))
    claiming = []
    for pane in panes:
        if not _is_workdash_work_pane(pane) or pane.get("title") != title:
            continue
        if not isinstance(pane.get("id"), int):
            # A pane that cannot be named can neither be reused nor ruled out.
            raise WorkdashControlError(
                "zellij_unavailable",
                f"Zellij reported a live agent pane in {worktree} without a pane ID.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            )
        claiming.append(pane)
    return sorted(claiming, key=lambda pane: pane["id"])
