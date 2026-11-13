"""Shared fake for the Zellij pane that one-shot agent commands run in.

The fake runs each pane command in its working directory, as Zellij does, and
records the launch so tests can assert on the pane name, tab, session, and the
output the pane showed the user.
"""

from __future__ import annotations

import subprocess

import pytest

from workdash import launcher

# The pane command is a real process even when a test fakes ``subprocess.run`` for
# the other boundaries the agent path crosses.
_REAL_RUN = subprocess.run


def install_pane_launch(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Run each pane command in its cwd, as Zellij does, recording the launch."""

    launches: list[dict[str, object]] = []

    def launch_pane_and_wait(repo_path, command, *, context, work_action, zellij_session, group):
        completed = _REAL_RUN(command, cwd=repo_path, capture_output=True, text=True)
        launches.append(
            {
                "repo_path": repo_path,
                "command": command,
                "context": context,
                "work_action": work_action,
                "pane_name": launcher._zellij_pane_name_for_work_action(work_action, repo_path),
                "zellij_session": zellij_session,
                "group": group,
                "stdout": completed.stdout,
            }
        )

    monkeypatch.setattr(launcher.zellij, "launch_pane_and_wait", launch_pane_and_wait)
    return launches
