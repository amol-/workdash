import pytest

from workdash import github
from workdash.agent_context import render_github_context
from workdash.models import WorkItemType

_COMMENT = {
    "author": {"login": "bob", "id": "x", "is_bot": False},
    "body": "a comment",
    "createdAt": "2026-02-01T00:00:00Z",
    "id": "IC_1",
    "reactionGroups": [],
}
_REVIEW = {
    "author": {"login": "carol"},
    "body": "a review",
    "state": "APPROVED",
    "submittedAt": "2026-02-02T00:00:00Z",
    "id": "R_1",
}
_STRUCTURED_VALUES = {
    "author": {"login": "alice"},
    "assignees": [{"login": "dev1"}],
    "labels": [{"name": "bug", "color": "ff0000"}],
    "comments": [_COMMENT],
    "reviews": [_REVIEW],
    "linkedIssues": [
        {
            "number": 7,
            "repository": "other/repo",
            "title": "Goal",
            "body": "Do the thing",
            "author": {"login": "alice"},
            "comments": [_COMMENT],
        }
    ],
    "unresolvedReviewThreads": [{"path": "a.py", "line": 3, "comments": [_COMMENT]}],
}


@pytest.mark.parametrize(
    "fields",
    [
        github._LAUNCH_ISSUE_CONTEXT_JSON_FIELDS,
        github._LAUNCH_PR_CONTEXT_JSON_FIELDS,
        github._ANALYSIS_ISSUE_CONTEXT_JSON_FIELDS,
        github._ANALYSIS_PR_CONTEXT_JSON_FIELDS,
    ],
)
def test_render_accepts_every_field_requested_from_gh(fields: str) -> None:
    context = {name: _STRUCTURED_VALUES.get(name, "value") for name in fields.split(",")}

    rendered = render_github_context(context, WorkItemType.PR)

    for name in context:
        assert f"<{name}>" in rendered


def test_render_includes_linked_issues_and_unresolved_review_threads() -> None:
    rendered = render_github_context(
        {key: _STRUCTURED_VALUES[key] for key in ("linkedIssues", "unresolvedReviewThreads")},
        WorkItemType.PR,
    )

    assert '<linkedIssue number="7" repository="other/repo">' in rendered
    assert "Do the thing" in rendered
    assert '<thread path="a.py" line="3">' in rendered
    assert '<comment author="bob" createdAt="2026-02-01T00:00:00Z">a comment</comment>' in rendered


def test_render_rejects_unknown_top_level_key() -> None:
    with pytest.raises(ValueError, match="mergeStateStatus"):
        render_github_context({"state": "OPEN", "mergeStateStatus": "CLEAN"}, WorkItemType.PR)


def test_render_drops_noise_fields_of_nested_objects() -> None:
    rendered = render_github_context(
        {"author": _STRUCTURED_VALUES["author"], "labels": _STRUCTURED_VALUES["labels"]}
        | {"comments": [_COMMENT], "reviews": [_REVIEW]},
        WorkItemType.PR,
    )

    assert "alice" in rendered
    assert "bug" in rendered
    for noise in ("ff0000", "IC_1", "R_1", "is_bot", "reactionGroups"):
        assert noise not in rendered


def test_render_shows_null_for_none_value() -> None:
    assert (
        render_github_context({"reviewDecision": None}, WorkItemType.PR)
        == "<reviewDecision>null</reviewDecision>"
    )


def test_render_keeps_empty_text() -> None:
    assert render_github_context({"body": ""}, WorkItemType.PR) == "<body></body>"


def test_render_renders_only_present_keys_sorted() -> None:
    assert (
        render_github_context({"title": "t", "body": "b"}, WorkItemType.PR)
        == "<body>b</body>\n<title>t</title>"
    )


def test_render_shows_null_for_deleted_author_pending_review_and_outdated_thread() -> None:
    deleted_author_comment = _COMMENT | {"author": None}
    pending_review = _REVIEW | {"submittedAt": None}
    outdated_thread = {"path": "a.py", "line": None, "comments": [deleted_author_comment]}

    rendered = render_github_context(
        {"reviews": [pending_review], "unresolvedReviewThreads": [outdated_thread]}, WorkItemType.PR
    )

    assert 'author="null"' in rendered
    assert 'submittedAt="null"' in rendered
    assert '<thread path="a.py" line="null">' in rendered


def test_render_puts_nested_tags_on_their_own_lines() -> None:
    rendered = render_github_context({"comments": [_COMMENT]}, WorkItemType.PR)

    assert rendered == (
        "<comments>\n"
        '<comment author="bob" createdAt="2026-02-01T00:00:00Z">a comment</comment>\n'
        "</comments>"
    )


def test_render_keeps_markdown_body_verbatim() -> None:
    body = 'Fix "quotes", C:\\path, {braces}, <b>tags</b>, café ✓\n\n```py\nx = {"a": 1}\n```'

    rendered = render_github_context({"body": body}, WorkItemType.PR)

    assert rendered == f"<body>\n{body}\n</body>"


def _all_comment_lists(**comment_fields) -> dict:
    """The same comment in a PR conversation, a linked issue and a review thread."""
    comment = {**_COMMENT, **comment_fields}
    return {
        "author": {"login": "alice"},
        "comments": [comment],
        "linkedIssues": [{**_STRUCTURED_VALUES["linkedIssues"][0], "comments": [comment]}],
        "unresolvedReviewThreads": [{"path": "a.py", "line": 3, "comments": [comment]}],
    }


def test_render_marks_comments_by_the_author_of_the_item_they_belong_to() -> None:
    context = _all_comment_lists(author={"login": "alice"})
    context["linkedIssues"][0]["author"] = {"login": "alice"}

    rendered = render_github_context(context, WorkItemType.PR)

    assert rendered.count('pr_author="true"') == 2  # conversation and review thread
    assert rendered.count('issue_author="true"') == 1  # linked issue
    assert '="false"' not in rendered


@pytest.mark.parametrize(
    ("item_type", "fields", "pr_flags", "issue_flags"),
    [
        # item comment and linked issue are flagged by item type; the thread is always a PR thread
        (WorkItemType.ISSUE, github._LAUNCH_ISSUE_CONTEXT_JSON_FIELDS, 1, 2),
        (WorkItemType.PR, github._LAUNCH_PR_CONTEXT_JSON_FIELDS, 2, 1),
    ],
)
def test_render_names_the_author_flag_after_the_item_type(
    item_type: WorkItemType, fields: str, pr_flags: int, issue_flags: int
) -> None:
    by_author = {**_COMMENT, "author": {"login": "alice"}}
    context = {name: _STRUCTURED_VALUES.get(name, "value") for name in fields.split(",")}
    context["comments"] = [by_author]
    context["linkedIssues"] = [{**_STRUCTURED_VALUES["linkedIssues"][0], "comments": [by_author]}]
    context["unresolvedReviewThreads"] = [{"path": "a.py", "line": 3, "comments": [by_author]}]

    rendered = render_github_context(context, item_type)

    assert rendered.count('pr_author="true"') == pr_flags
    assert rendered.count('issue_author="true"') == issue_flags


def test_render_does_not_mark_comments_by_other_users() -> None:
    rendered = render_github_context(_all_comment_lists(), WorkItemType.PR)

    assert "pr_author" not in rendered
    assert "issue_author" not in rendered


@pytest.mark.parametrize(
    ("comment_login", "flagged"),
    [("copilot-swe-agent", True), ("other-bot", False)],
)
def test_render_matches_a_bot_item_author_despite_the_gh_app_prefix(
    comment_login: str, flagged: bool
) -> None:
    context = {
        "author": {"login": "app/copilot-swe-agent"},
        "comments": [{**_COMMENT, "author": {"login": comment_login}}],
    }

    rendered = render_github_context(context, WorkItemType.PR)

    assert ('pr_author="true"' in rendered) is flagged


def test_render_never_marks_a_deleted_author_as_the_item_author() -> None:
    context = _all_comment_lists(author=None)
    context["author"] = None
    context["linkedIssues"][0]["author"] = None

    rendered = render_github_context(context, WorkItemType.PR)

    assert 'author="null"' in rendered
    assert "pr_author" not in rendered
    assert "issue_author" not in rendered


@pytest.mark.parametrize(
    "reason",
    ["SPAM", "abuse", "OFF_TOPIC", "off-topic", "Duplicate"],
)
def test_render_skips_comments_minimized_as_noise_in_every_list(reason: str) -> None:
    kept = {**_COMMENT, "body": "kept"}
    context = _all_comment_lists(body="hidden", isMinimized=True, minimizedReason=reason)
    context["comments"].append(kept)
    context["linkedIssues"][0]["comments"].append(kept)
    context["unresolvedReviewThreads"][0]["comments"].append(kept)

    rendered = render_github_context(context, WorkItemType.PR)

    assert "hidden" not in rendered
    assert rendered.count(">kept</comment>") == 3


@pytest.mark.parametrize(
    "minimized",
    [
        {"isMinimized": True, "minimizedReason": "OUTDATED"},
        {"isMinimized": True, "minimizedReason": "resolved"},
        {"isMinimized": True, "minimizedReason": None},
        {"isMinimized": True},
        {"isMinimized": False, "minimizedReason": "spam"},
        {"isMinimized": None, "minimizedReason": None},
    ],
)
def test_render_keeps_other_comments_in_every_list_without_minimized_fields(
    minimized: dict,
) -> None:
    rendered = render_github_context(
        _all_comment_lists(body="visible", **minimized), WorkItemType.PR
    )

    assert rendered.count(">visible</comment>") == 3
    assert "isMinimized" not in rendered
    assert "minimizedReason" not in rendered
