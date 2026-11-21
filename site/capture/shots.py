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
    "analysis-viewer": (
        "work.html",
        (110, 28),
        [("wait", 15), ("keys", "a"), ("wait", 2), ("keys", "a"), ("hold", 8)],
        False,
    ),
    "viewer": (
        "work.html",
        (110, 28),
        [("wait", 15), ("keys", "v"), ("wait", 2), ("keys", "v"), ("hold", 5)],
        False,
    ),
    "agent-session": (
        "work.html",
        (110, 28),
        [("wait", 15), ("keys", "c"), ("wait", 2), ("keys", "1"), ("hold", 8)],
        False,
    ),
    "branchdiff": (
        "review.html",
        (110, 28),
        [("wait", 15), ("keys", "c"), ("wait", 2), ("keys", "d"), ("hold", 8)],
        False,
    ),
    "code-menu": ("work.html", (110, 28), [("wait", 15), ("keys", "c"), ("wait", 3)], False),
    "loop": (
        "agentic.html",
        (120, 32),
        [
            ("until", ("IS 42", 30)),
            ("keys", "d"),
            ("until", ("Autodispatch with:", 10)),
            ("keys", "1"),
            ("until", ("developing (implement)", 30)),
            ("keys", "m"),
            ("until", ("[automerge]", 10)),
            ("keys", "\x1bl"),
            ("until", ("Published the pull request.", 30)),
            # Close the agent tab, including finished panes Zellij leaves open.
            ("keys", "\x14x"),
            ("until", ("(closed)", 120)),
            ("hold", 1),
        ],
        True,
    ),
}
