"""Todo capture in the configured todo repository.

A todo is an ordinary GitHub issue in one configured repository, assigned to
the user and labeled so Workdash recognizes it again on later refreshes. The
issue body is reserved for the metadata block written here, which is the only
place a todo's target repository is recorded.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime

from .models import WorkItem, WorkItemKind, WorkItemType

TODO_LABEL = "workdash-todo"

_TODO_METADATA_VERSION = 1
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_METADATA_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_ISSUE_NUMBER_RE = re.compile(r"/issues/(\d+)$")


def create_todo(*, todo_repository: str, text: str, target: str | None = None) -> WorkItem:
    """Create a labeled, self-assigned todo issue and return it as a dashboard item.

    :param str todo_repository: Repository that hosts todos, as ``owner/repo``.
    :param str text: Todo text, used as the issue title.
    :param str | None target: Optional repository the todo is about.
    """

    # gh exits non-zero when the label is already there, which is the steady
    # state after the first capture and not a failure for us.
    _run_gh(
        [
            "gh",
            "label",
            "create",
            TODO_LABEL,
            "--repo",
            todo_repository,
            "--description",
            "Captured with workdash",
        ],
        operation=f"create the {TODO_LABEL} label in {todo_repository}",
        todo_repository=todo_repository,
        tolerate_stderr="already exists",
    )
    output = _run_gh(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            todo_repository,
            "--title",
            text,
            "--body",
            _encode_todo_metadata(target),
            "--assignee",
            "@me",
            "--label",
            TODO_LABEL,
        ],
        operation=f"create the todo issue in {todo_repository}",
        todo_repository=todo_repository,
    )
    url = output.strip().splitlines()[-1].strip() if output.strip() else ""
    match = _ISSUE_NUMBER_RE.search(url)
    if match is None:
        raise RuntimeError(
            f"Failed to read the new todo issue URL from gh output: {output.strip()!r}"
        )
    captured_at = datetime.now(UTC)
    # A todo is assigned to the user, so it joins the dashboard in the same work
    # category as any other assigned issue; only its target sets it apart.
    return WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo=todo_repository,
        number=int(match.group(1)),
        title=text,
        created_at=captured_at,
        updated_at=captured_at,
        url=url,
        todo_target=target,
    )


def todo_target_from_body(body: object) -> str | None:
    """Read the todo target recorded in a todo issue body.

    Absent, unparseable, or unexpected metadata means the todo has no target;
    a hand-edited issue body must never break a dashboard refresh.
    """

    if not isinstance(body, str):
        return None
    match = _METADATA_BLOCK_RE.search(body)
    if match is None:
        return None
    try:
        metadata = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    target = metadata.get("target")
    if isinstance(target, str) and is_repository_name(target):
        return target
    return None


def is_repository_name(value: str) -> bool:
    """Report whether ``value`` names a repository as ``owner/repo``."""

    return bool(_REPOSITORY_RE.match(value))


def is_missing_repository_error(gh_message: str) -> bool:
    """Report whether a gh failure message says the repository does not exist.

    The todo repository is allowed to not exist until the first capture, so
    callers need to tell this failure apart from every other gh failure. gh
    names the same missing repository differently per endpoint: GraphQL-backed
    issue commands fail to resolve it, while REST calls such as label creation
    report an HTTP 404. Only those two shapes count, so an unrelated failure
    that merely mentions 404 or a not-found reference, and authentication or
    permission failures, are not mistaken for a missing repository.
    """

    message = gh_message.lower()
    return any(marker in message for marker in ("could not resolve to a repository", "http 404"))


def _encode_todo_metadata(target: str | None) -> str:
    metadata: dict[str, object] = {"workdash_todo_version": _TODO_METADATA_VERSION}
    if target is not None:
        metadata["target"] = target
    return f"```json\n{json.dumps(metadata, indent=2, ensure_ascii=True)}\n```\n"


def _run_gh(
    command: list[str],
    *,
    operation: str,
    todo_repository: str,
    tolerate_stderr: str | None = None,
) -> str:
    """Run a gh command, translating its failures into user-facing advice.

    :param str | None tolerate_stderr: Marker that makes a gh failure a success.
    """

    try:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Failed to {operation}: gh CLI is not installed or not on PATH."
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        if tolerate_stderr is not None and tolerate_stderr in stderr.lower():
            return ""
        message = f"Failed to {operation}: {stderr or f'exit code {error.returncode}'}"
        if is_missing_repository_error(stderr):
            message = (
                f"{message.rstrip('.')}. Create the todo repository {todo_repository} "
                "on GitHub if it does not exist yet."
            )
        raise RuntimeError(message) from error
