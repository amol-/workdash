"""Capture recipes for the website."""

SHOTS = {
    "dashboard": ("index.html", (110, 28), [("wait", 15)], False),
    "dashboard-progress": (
        "agentic.html",
        (110, 28),
        [
            ("wait", 15),
            ("keys", "d"),
            ("wait", 3),
            ("keys", "1"),
            ("wait", 15),
            ("keys", "\t"),
            ("wait", 3),
        ],
        False,
    ),
    "og-card": ("index.html", (110, 28), [("wait", 15)], False),
    "analyze-dialog": ("work.html", (110, 28), [("wait", 15), ("keys", "a"), ("wait", 3)], False),
    "viewer": (
        "work.html",
        (110, 28),
        [("wait", 15), ("keys", "v"), ("wait", 2), ("keys", "v"), ("wait", 5)],
        False,
    ),
    "agent-session": (
        "work.html",
        (110, 28),
        [("wait", 15), ("keys", "c"), ("wait", 2), ("keys", "1"), ("wait", 8)],
        False,
    ),
    "branchdiff": (
        "review.html",
        (110, 28),
        [("wait", 15), ("keys", "c"), ("wait", 2), ("keys", "d"), ("wait", 8)],
        False,
    ),
    "code-menu": ("work.html", (110, 28), [("wait", 15), ("keys", "c"), ("wait", 3)], False),
    "loop": (
        "agentic.html",
        (120, 32),
        [
            ("wait", 15),
            ("keys", "d"),
            ("wait", 3),
            ("keys", "1"),
            ("hold", 2),
            ("keys", "m"),
            ("wait", 1),
            # Watch the agent work, then close its finished pane so the loop reviews the work.
            ("keys", "\x1bl"),
            ("hold", 4),
            ("keys", "\x10x"),
            ("hold", 120),
        ],
        True,
    ),
}
