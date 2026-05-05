"""Repository selector resolution."""

import json
import re
import subprocess

_OWNER_PATTERN = r"[a-z0-9][a-z0-9-]*"
_REPOSITORY_PATTERN = r"[a-z0-9._-]+"
_REPOSITORY_SELECTOR_PATTERN = re.compile(
    rf"^(?:{_OWNER_PATTERN}/\*|{_OWNER_PATTERN}/{_REPOSITORY_PATTERN})$"
)
_FULL_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_OWNER_WILDCARD_PATTERN = re.compile(rf"^(?P<owner>{_OWNER_PATTERN})/\*$")
_DEFAULT_REPO_LIST_LIMIT = 1000


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


def _list_owner_repositories(owner: str, limit: int = _DEFAULT_REPO_LIST_LIMIT) -> list[str]:
    """List repositories accessible for an owner using gh."""

    command = [
        "gh",
        "repo",
        "list",
        owner,
        "--json",
        "nameWithOwner",
        "--limit",
        str(limit),
    ]
    try:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            "Failed to run gh repo listing: gh CLI is not installed or not on PATH."
        ) from error
    except subprocess.CalledProcessError as error:
        stderr = (error.stderr or "").strip()
        raise RuntimeError(
            f"Failed to list repositories for owner {owner!r} via gh: "
            f"{stderr or f'process exited with code {error.returncode}'}"
        ) from error
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Failed to parse gh repository list JSON for owner {owner!r}: {error.msg}"
        ) from error
    if not isinstance(payload, list):
        raise RuntimeError(
            f"Invalid gh repository list payload for owner {owner!r}: expected a JSON array."
        )
    repositories: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            raise RuntimeError(
                f"Invalid gh repository list payload for owner {owner!r}: expected objects in array."
            )
        name_with_owner = entry.get("nameWithOwner")
        if not isinstance(name_with_owner, str) or not _FULL_REPOSITORY_PATTERN.fullmatch(
            name_with_owner
        ):
            raise RuntimeError(
                f"Invalid gh repository list entry for owner {owner!r}: missing or invalid nameWithOwner."
            )
        repositories.append(name_with_owner)
    return repositories


def expand_repository_selectors(selectors: list[str]) -> list[str]:
    """Expand owner wildcards into full owner/repo names."""

    repositories: list[str] = []
    for selector in selectors:
        owner_wildcard_match = _OWNER_WILDCARD_PATTERN.fullmatch(selector)
        if owner_wildcard_match is None:
            repositories.append(selector)
            continue
        repositories.extend(_list_owner_repositories(owner_wildcard_match.group("owner")))
    return repositories


def resolve_repositories(selectors: list[str]) -> list[str]:
    """Resolve selectors into deterministic concrete owner/repo names."""

    repositories = sorted(
        set(expand_repository_selectors(validate_repository_selectors(selectors)))
    )
    for repository in repositories:
        if _FULL_REPOSITORY_PATTERN.fullmatch(repository) is None:
            raise RuntimeError(
                f"Expanded repository entry is not a concrete owner/repo value: {repository!r}"
            )
    return repositories
