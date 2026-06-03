"""Local server-backed control surface for Workdash."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from http import HTTPStatus
from socketserver import BaseServer
from wsgiref.simple_server import make_server

from .backend import IncludeResult, SuggestionMarkers, WorkdashBackend, compute_suggestion_markers
from .config import WorkdashConfig
from .github_client import parse_github_item_url
from .launcher import (
    dump_zellij_pane,
    launch_agent_context,
    launch_vscode_context,
    load_zellij_panes,
    prepare_launch_agent_prompt,
    send_zellij_pane_input,
)
from .models import WorkItem, format_type_label
from .repo_worktree import ensure_worktree, existing_worktree_path, get_merge_base

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8765
SERVER_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"


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
            return _pane_info_payload(
                self.zellij_session,
                self.config.workdir,
                self.work_items,
                include_all_panes=include_all_panes,
            )

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

    def analyze(
        self, *, target: str, agent: str | None = None, prefer_cache: bool = True
    ) -> dict[str, object]:
        """Analyze a current dashboard item through the shared action path."""

        with self._lock:
            item = self._require_item(target)
            item_id = format_work_item_id(item)
            cache_used = item.analysis is not None and prefer_cache
            if agent == "cached":
                selected_agent = "cached"
            else:
                agents = self.config.configured_analyze_agents()
                selected_agent = agent or (agents[0] if agents else None)
                if agent is not None and agent not in agents:
                    raise WorkdashControlError(
                        "unknown_agent",
                        f"Analysis agent {agent!r} is not configured.",
                        status=HTTPStatus.BAD_REQUEST,
                    )
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
            return {
                "item_id": item_id,
                "path": path,
                "agent": selected_agent,
                "cache_used": cache_used,
                "status": "cached" if cache_used else "generated",
            }

    def code(
        self, *, target: str, agent: str | None = None, allow_vscode: bool = False
    ) -> dict[str, object]:
        """Launch a coding agent for a current dashboard item."""

        with self._lock:
            item = self._require_item(target)
            item_id = format_work_item_id(item)
            agents = self.config.configured_code_agents()
            selected_agent = agent or (agents[0] if agents else None)
            if selected_agent is None:
                raise WorkdashControlError(
                    "no_agent",
                    "No terminal-backed coding agents are configured.",
                    status=HTTPStatus.CONFLICT,
                )
            if selected_agent not in agents and not (allow_vscode and selected_agent == "vscode"):
                raise WorkdashControlError(
                    "unknown_agent",
                    f"Coding agent {selected_agent!r} is not a configured terminal-backed agent.",
                    status=HTTPStatus.BAD_REQUEST,
                )
            repo_path = ensure_worktree(self.config.workdir, item)
            prompt = prepare_launch_agent_prompt(
                item,
                repo_path,
                analysis_path=str(self.backend.analysis_cache.build_analysis_path(item))
                if item.analysis is not None
                else None,
                merge_base=get_merge_base(repo_path),
            )
            if selected_agent == "vscode":
                launch_vscode_context(repo_path, prompt)
                return {
                    "item_id": item_id,
                    "session": self.zellij_session,
                    "agent": selected_agent,
                    "cwd": repo_path,
                    "pane_title": None,
                    "pane_id": None,
                }
            command_tokens = self.config.code_agent_launch_command_tokens(selected_agent)
            launch = launch_agent_context(
                repo_path,
                prompt,
                agent_command_tokens=command_tokens,
                zellij_session=self.zellij_session,
            )
            return {
                "item_id": item_id,
                "session": launch.session or self.zellij_session,
                "agent": selected_agent,
                "cwd": launch.cwd,
                "pane_title": launch.pane_title,
                "pane_id": launch.pane_id,
            }

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

    def _notify_items_changed(self) -> None:
        if self.items_changed_callback is not None:
            self.items_changed_callback()

    def _analyze_item(self, item: WorkItem, *, tool: str, prefer_cache: bool) -> str | None:
        if prefer_cache:
            cached_path = self.backend.analyze_item(item, tool="cached")
            if cached_path is not None:
                return cached_path
        if tool != "cached":
            ensure_worktree(self.config.workdir, item)
        return self.backend.analyze_item(item, tool=tool)

    def _require_item(self, target: str) -> WorkItem:
        item = resolve_work_item_target(target, self.work_items)
        if item is None:
            raise WorkdashControlError(
                "unknown_item",
                f"No dashboard item matches {target!r}.",
                status=HTTPStatus.NOT_FOUND,
            )
        return item


class WorkdashControlServer:
    """Background localhost HTTP server for a server-backed Workdash session."""

    def __init__(self, session: WorkdashSession) -> None:
        self._session = session
        self._httpd: BaseServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start serving the TurboGears JSON API in a background thread."""

        app = _localhost_only_wsgi_app(_make_turbogears_app(self._session))
        try:
            self._httpd = make_server(SERVER_HOST, SERVER_PORT, app)
        except OSError as error:
            raise RuntimeError(
                f"Workdash server port {SERVER_PORT} is already in use. "
                "Is another `workdash --server` running?"
            ) from error
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="workdash-control-server",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


class WorkdashControlClient:
    """HTTP client used by thin local CLI commands."""

    def request(self, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object]:
        body = json.dumps(payload or {}).encode("utf-8")
        request = urllib.request.Request(
            f"{SERVER_URL}/api/v0/{endpoint}",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise _client_error_from_body(error) from error
        except OSError as error:
            raise WorkdashControlError(
                "server_unreachable",
                "No Workdash server is reachable at 127.0.0.1:8765. "
                "Start one with `workdash --server`.",
                status=HTTPStatus.SERVICE_UNAVAILABLE,
            ) from error
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError as error:
            raise WorkdashControlError(
                "invalid_response",
                f"Workdash server returned invalid JSON: {error.msg}",
            ) from error
        if not isinstance(envelope, dict) or envelope.get("ok") is not True:
            raise WorkdashControlError(
                "invalid_response", "Workdash server returned an invalid response."
            )
        result = envelope.get("result")
        if not isinstance(result, dict):
            raise WorkdashControlError(
                "invalid_response", "Workdash server result was not an object."
            )
        return result


def format_work_item_id(item: WorkItem) -> str:
    """Return the copy/paste identifier accepted by work-item commands."""

    item_type = format_type_label(item).removesuffix("+")
    return f"{item.repo}#{item_type}-{item.number}"


def resolve_work_item_target(target: str, work_items: Sequence[WorkItem]) -> WorkItem | None:
    for item in work_items:
        if target == format_work_item_id(item):
            return item
    parsed = parse_github_item_url(target)
    if parsed is None:
        return None
    for item in work_items:
        if (
            item.repo == parsed.repo
            and item.item_type == parsed.item_type
            and item.number == parsed.number
        ):
            return item
    return None


def show_config_payload(config: WorkdashConfig) -> dict[str, object]:
    return {
        "agents": {
            "analyze": config.configured_analyze_agents(),
            "code": config.configured_code_agents(),
        },
        "server": {"host": SERVER_HOST, "port": SERVER_PORT},
    }


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
                "repo": item.repo,
                "number": item.number,
                "title": item.title,
                "url": item.url,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat(),
                "suggested": bool(
                    suggestion_markers.get((item.item_type, item.repo, item.number), "")
                ),
            }
        )
    return {"items": items}


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
        elif include_all_panes and _is_live_non_plugin_pane(pane):
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

    class ApiController(TGController):
        def __init__(self, workdash_session: WorkdashSession) -> None:
            self.v0 = V0Controller(workdash_session)

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


def _localhost_only_wsgi_app(app):
    def only_localhost(environ, start_response):
        if environ.get("REMOTE_ADDR") not in {"127.0.0.1", "::1"}:
            body = json.dumps(
                _error_envelope("forbidden", "Workdash V0 only accepts localhost clients.")
            ).encode("utf-8")
            start_response(
                "403 Forbidden",
                [("Content-Type", "application/json"), ("Content-Length", str(len(body)))],
            )
            return [body]
        return app(environ, start_response)

    return only_localhost


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


def _is_live_non_plugin_pane(pane: dict[str, object]) -> bool:
    return (
        pane.get("is_plugin") is not True
        and pane.get("exited") is not True
        and pane.get("is_held") is not True
    )


def _is_workdash_work_pane(pane: dict[str, object]) -> bool:
    if not _is_live_non_plugin_pane(pane):
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
