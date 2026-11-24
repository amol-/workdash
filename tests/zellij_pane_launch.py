"""Helpers for tests that fake the Zellij boundary instead of a real session.

A pane launch wraps the pane's command so the pane writes its own
``$ZELLIJ_PANE_ID`` to a private marker before exec. Tests that fake
``subprocess.run`` declare the launch as started by writing that marker, and read
the pane command back from the recorded ``zellij`` invocation.
"""

from __future__ import annotations

from pathlib import Path

_PANE_MARKER_NAME = "workdash-pane"


def pane_marker(argv: list[str]) -> Path | None:
    """The marker path a launch's pane writes, or None when ``argv`` is not one."""

    if _PANE_MARKER_NAME not in argv:
        return None
    return Path(argv[argv.index(_PANE_MARKER_NAME) + 1])


def pane_launch(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split a launch into its ``zellij`` arguments and the command the pane runs."""

    marker = pane_marker(argv)
    assert marker is not None, argv
    return argv[: argv.index("--")], argv[argv.index(_PANE_MARKER_NAME) + 3 :]


def mark_pane_started(
    argv: list[str], pane_id: str = "17", *, server_cache: str | None = None
) -> None:
    """Record that the pane of this launch started, as the pane itself would."""

    marker = pane_marker(argv)
    if marker is not None:
        attempt = argv[argv.index(_PANE_MARKER_NAME) + 2]
        cache = f"{server_cache}/zellij:" if server_cache is not None else ""
        marker.symlink_to(f"{pane_id}:{cache}{attempt}")
