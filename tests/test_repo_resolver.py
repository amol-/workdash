import pytest

from workdash.repo_resolver import build_tracked_search_scope, validate_repository_selectors


def test_validate_repository_selectors_accepts_valid_owner_wildcard_and_owner_repo_with_trimming() -> (
    None
):
    assert validate_repository_selectors(["  testuser/*  ", "\tbbangert/beaker\t"]) == [
        "testuser/*",
        "bbangert/beaker",
    ]


def test_validate_repository_selectors_skips_blank_entries() -> None:
    assert validate_repository_selectors(["", "  ", "testuser/public-repo"]) == [
        "testuser/public-repo"
    ]


def test_validate_repository_selectors_raises_value_error_with_invalid_selector_position() -> None:
    with pytest.raises(ValueError, match=r"Invalid repository selector at position 2"):
        validate_repository_selectors(["testuser/public-repo", "bad selector"])


def test_build_tracked_search_scope_converts_owner_wildcard_to_a_user_qualifier() -> None:
    assert build_tracked_search_scope(["testuser/*"]) == "user:testuser"


def test_build_tracked_search_scope_converts_owner_repo_to_a_repo_qualifier() -> None:
    assert build_tracked_search_scope(["bbangert/beaker"]) == "repo:bbangert/beaker"


def test_build_tracked_search_scope_joins_multiple_selectors_with_spaces() -> None:
    assert (
        build_tracked_search_scope(["testuser/*", "bbangert/beaker", "turbogears/tg2"])
        == "user:testuser repo:bbangert/beaker repo:turbogears/tg2"
    )


def test_build_tracked_search_scope_returns_empty_string_for_no_selectors() -> None:
    assert build_tracked_search_scope([]) == ""
