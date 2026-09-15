"""Repository selector resolution."""

import re

_OWNER_PATTERN = r"[a-z0-9][a-z0-9-]*"
_REPOSITORY_PATTERN = r"[a-z0-9._-]+"
_REPOSITORY_SELECTOR_PATTERN = re.compile(
    rf"^(?:{_OWNER_PATTERN}/\*|{_OWNER_PATTERN}/{_REPOSITORY_PATTERN})$"
)
_OWNER_WILDCARD_PATTERN = re.compile(rf"^(?P<owner>{_OWNER_PATTERN})/\*$")


def validate_repository_selectors(selectors: list[str]) -> list[str]:
    """Validate repository selectors and return the cleaned list."""

    validated: list[str] = []
    for position, selector in enumerate(selectors, start=1):
        stripped = selector.strip()
        if not stripped:
            continue
        if not _REPOSITORY_SELECTOR_PATTERN.fullmatch(stripped):
            raise ValueError(f"Invalid repository selector at position {position}: {stripped!r}")
        validated.append(stripped)
    return validated


def build_tracked_search_scope(selectors: list[str]) -> str:
    """Build the GitHub search qualifiers naming every tracked selector's scope.

    An ``owner/*`` selector becomes a ``user:owner`` qualifier, which GitHub
    search matches the same set of repositories ``gh repo list owner`` would
    have expanded to. A concrete ``owner/repo`` selector becomes a
    ``repo:owner/repo`` qualifier. The qualifiers are space-joined so the
    whole tracked scope is one search query string; an empty selector list
    yields an empty scope.
    """

    scope_terms: list[str] = []
    for selector in selectors:
        owner_wildcard_match = _OWNER_WILDCARD_PATTERN.fullmatch(selector)
        if owner_wildcard_match is not None:
            scope_terms.append(f"user:{owner_wildcard_match.group('owner')}")
        else:
            scope_terms.append(f"repo:{selector}")
    return " ".join(scope_terms)
