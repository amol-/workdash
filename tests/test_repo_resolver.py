import subprocess

import pytest

import workdash.repo_resolver as repo_resolver
from workdash.repo_resolver import (
    expand_repository_selectors,
    resolve_repositories,
    validate_repository_selectors,
)


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


def test_expand_repository_selectors_expands_owner_wildcards_with_gh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='[{"nameWithOwner":"testuser/public-repo"},{"nameWithOwner":"testuser/private-repo"}]',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert expand_repository_selectors(["bbangert/beaker", "testuser/*"]) == [
        "bbangert/beaker",
        "testuser/public-repo",
        "testuser/private-repo",
    ]


def test_expand_repository_selectors_keeps_owner_repo_selectors_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called for owner/repo selectors")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert expand_repository_selectors(["bbangert/beaker", "turbogears/tg2"]) == [
        "bbangert/beaker",
        "turbogears/tg2",
    ]


def test_expand_repository_selectors_raises_clear_error_when_gh_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args[0],
            stderr="authentication failed",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="Failed to list repositories for owner 'testuser' via gh"):
        expand_repository_selectors(["testuser/*"])


def test_expand_repository_selectors_raises_clear_error_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="gh CLI is not installed or not on PATH"):
        expand_repository_selectors(["testuser/*"])


def test_resolve_repositories_dedupes_and_sorts_concrete_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout='[{"nameWithOwner":"testuser/private-repo"},{"nameWithOwner":"testuser/public-repo"}]',
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert resolve_repositories(
        ["zebra-org/zeta", "testuser/*", "alpha-org/alpha", "testuser/public-repo"]
    ) == [
        "alpha-org/alpha",
        "testuser/private-repo",
        "testuser/public-repo",
        "zebra-org/zeta",
    ]


def test_resolve_repositories_rejects_non_concrete_expansion_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        repo_resolver,
        "expand_repository_selectors",
        lambda selectors: ["testuser/repo", "testuser/*"],
    )

    with pytest.raises(RuntimeError, match="not a concrete owner/repo"):
        resolve_repositories(["testuser/repo"])
