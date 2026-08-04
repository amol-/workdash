"""Shared fake for the ``gh`` boundary that todo capture drives.

Todo capture runs exactly two ``gh`` commands, so both the unit tests and the
BDD steps replace ``subprocess.run`` with the same dispatcher and only vary how
those two commands behave.
"""

from __future__ import annotations

import subprocess

from workdash.todo import TODO_LABEL

TODO_REPOSITORY = "testuser/todos"
TODO_ISSUE_NUMBER = 110
TODO_ISSUE_URL = f"https://github.com/{TODO_REPOSITORY}/issues/{TODO_ISSUE_NUMBER}"
# Real gh output shapes, so tests exercise the stderr matching done in production.
# `gh label create` is a REST call, so a missing repository comes back as a 404,
# while `gh issue create` resolves the repository over GraphQL and names it.
LABEL_EXISTS_STDERR = (
    f'label with name "{TODO_LABEL}" already exists; '
    "use `--force` to update its color and description"
)
LABEL_REPOSITORY_MISSING_STDERR = (
    f"HTTP 404: Not Found (https://api.github.com/repos/{TODO_REPOSITORY}/labels)"
)
ISSUE_REPOSITORY_MISSING_STDERR = (
    f"GraphQL: Could not resolve to a Repository with the name '{TODO_REPOSITORY}'."
)


def gh_todo_failure(stderr: str) -> subprocess.CalledProcessError:
    """Return the failure gh raises when it exits non-zero with *stderr*."""

    return subprocess.CalledProcessError(1, ["gh"], stderr=stderr)


def fake_gh_todo_run(
    recorded: list[list[str]],
    *,
    label_error: Exception | None = None,
    create_error: Exception | None = None,
    create_stdout: str = f"{TODO_ISSUE_URL}\n",
):
    """Return a ``subprocess.run`` replacement recording every gh command issued.

    :param list[list[str]] recorded: Every issued command is appended here.
    :param Exception | None label_error: Failure raised by ``gh label create``.
    :param Exception | None create_error: Failure raised by ``gh issue create``.
    :param str create_stdout: Output ``gh issue create`` prints on success.
    """

    def fake_run(command, **_kwargs):
        recorded.append(list(command))
        if command[:3] == ["gh", "label", "create"]:
            if label_error is not None:
                raise label_error
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[:3] == ["gh", "issue", "create"]:
            if create_error is not None:
                raise create_error
            return subprocess.CompletedProcess(command, 0, stdout=create_stdout, stderr="")
        raise AssertionError(f"Unexpected gh command in a todo test: {command}")

    return fake_run
