"""Render the GitHub context handed to agents as tagged markup."""

from typing import Any

from .models import WorkItemType

_SCALAR_KEYS = frozenset(
    {
        "number",
        "title",
        "body",
        "url",
        "state",
        "createdAt",
        "updatedAt",
        "isDraft",
        "reviewDecision",
        "additions",
        "deletions",
        "changedFiles",
        "headRefName",
        "baseRefName",
    }
)

# GitHub reasons for minimizing a comment as noise; outdated, resolved and low quality ones stay.
_NOISE_MINIMIZED_REASONS = frozenset({"spam", "abuse", "off_topic", "duplicate"})


def render_github_context(context: dict[str, Any], item_type: WorkItemType) -> str:
    """Render the GitHub context as one tag per key, with text kept verbatim.

    Only the keys present are rendered, sorted by key. None renders as null.
    Nested objects are reduced to the fields an agent needs; the rest of what
    ``gh`` returns is dropped on purpose. A comment by the author of the item it
    belongs to gets ``pr_author="true"`` or ``issue_author="true"``, and comments
    minimized as spam, abuse, off-topic or duplicate are skipped.

    :param dict context: Launch or analysis context for an issue or pull request.
    :param WorkItemType item_type: Type of the item the context belongs to.
    """

    parts = []
    for key, value in sorted(context.items()):
        if value is None:
            text = "null"
        elif key == "author":
            text = _login(value)
        elif key == "assignees":
            text = ", ".join(_login(assignee) for assignee in value)
        elif key == "labels":
            text = ", ".join(label["name"] for label in value)
        elif key == "comments":
            text = _comments(value, context.get("author"), f"{item_type.value}_author")
        elif key == "reviews":
            text = "\n".join(
                _tag(
                    "review",
                    review["body"],
                    author=_login(review["author"]),
                    state=review["state"],
                    submittedAt=review["submittedAt"],
                )
                for review in value
            )
        elif key == "linkedIssues":
            text = "\n".join(
                _tag(
                    "linkedIssue",
                    "\n".join(
                        [
                            _tag("title", issue["title"]),
                            _tag("body", issue["body"]),
                            _tag(
                                "comments",
                                _comments(issue["comments"], issue["author"], "issue_author"),
                            ),
                        ]
                    ),
                    number=issue["number"],
                    repository=issue["repository"],
                )
                for issue in value
            )
        elif key == "unresolvedReviewThreads":
            text = "\n".join(
                _tag(
                    "thread",
                    _comments(thread["comments"], context.get("author"), "pr_author"),
                    path=thread["path"],
                    line=thread["line"],
                )
                for thread in value
            )
        elif key in _SCALAR_KEYS:
            text = str(value)
        else:
            raise ValueError(f"Unsupported GitHub context key: {key!r}")
        parts.append(_tag(key, text))
    return "\n".join(parts)


def _tag(name: str, text: str, **attributes: Any) -> str:
    attrs = "".join(
        f' {key}="{"null" if value is None else value}"' for key, value in attributes.items()
    )
    # Wrapping text that starts with a tag keeps nested tags on their own lines.
    if "\n" in text or text.startswith("<"):
        return f"<{name}{attrs}>\n{text}\n</{name}>"
    return f"<{name}{attrs}>{text}</{name}>"


def _login(user: dict[str, Any] | None) -> str:
    return user["login"] if user else "null"


def _comments(comments: list[dict[str, Any]], item_author: dict[str, Any] | None, flag: str) -> str:
    """Render comments, marking those by ``item_author`` with ``flag`` and skipping noise."""
    rendered = []
    for comment in comments:
        reason = (comment.get("minimizedReason") or "").lower().replace("-", "_")
        if comment.get("isMinimized") and reason in _NOISE_MINIMIZED_REASONS:
            continue
        # Only the item author from `gh ... --json author` carries the "app/" bot prefix.
        is_item_author = bool(
            comment["author"]
            and item_author
            and comment["author"]["login"] == item_author["login"].removeprefix("app/")
        )
        attributes = {"author": _login(comment["author"]), "createdAt": comment.get("createdAt")}
        if is_item_author:
            attributes[flag] = "true"
        rendered.append(_tag("comment", comment["body"], **attributes))
    return "\n".join(rendered)
