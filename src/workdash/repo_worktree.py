"""Git worktree management for work items."""

import contextlib
import subprocess
from pathlib import Path

from .git import GitHelper
from .github import GithubHelper
from .models import WorkItem, WorkItemType


def main_repo_path(workdir: str, repo: str) -> Path:
    """Return the local clone directory for a repository.

    :param str workdir: Base working directory (may use ~ for home).
    :param str repo: Repository in "owner/repo" format.
    """
    parts = repo.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo format {repo!r}, expected 'owner/repo'")
    return Path(workdir).expanduser() / f"{parts[0]}_{parts[1]}"


def worktree_path(workdir: str, repo: str, number: int) -> Path:
    """Return the worktree directory for a work item in the given repo.

    :param str workdir: Base working directory (may use ~ for home).
    :param str repo: Repository in "owner/repo" format.
    :param int number: The issue or PR number.
    """
    main = main_repo_path(workdir, repo)
    return main.parent / f"{main.name}_{number}"


def existing_worktree_path(workdir: str, item: WorkItem) -> Path | None:
    """Find a locally proven worktree without creating, fetching, or querying GitHub."""
    base = Path(workdir).expanduser()
    if not base.is_dir():
        return None
    git = GitHelper()
    matches = [
        candidate
        for candidate in _worktree_candidates(base, item)
        if git.worktree_proves_item(candidate, item)
    ]
    return matches[0] if len(matches) == 1 else None


def ensure_worktree(workdir: str, item: WorkItem) -> str:
    """Ensure a worktree exists for the given work item and return its path.

    Clones the repository if it does not exist locally, fetches the latest
    changes, and creates a worktree checked out to the appropriate branch.

    :param str workdir: Base working directory (may use ~ for home).
    :param WorkItem item: The work item to prepare a worktree for.
    """
    git = GitHelper()
    existing = existing_worktree_path(workdir, item)
    if existing is not None:
        # Local divergence or missing upstream shouldn't block the user.
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            git.pull_ff_only(existing)
        if item.item_type == WorkItemType.PR and existing.name != git.worktree_name(
            item.repo, item.number
        ):
            with contextlib.suppress(RuntimeError):
                git.fetch_remote(existing, "upstream")
        return str(existing)

    if item.item_type == WorkItemType.PR:
        head_ref, head_repo = GithubHelper().fetch_worktree_head(item)
        repo = head_repo
    else:
        repo = item.repo
        head_ref = ""
        head_repo = item.repo

    wt = worktree_path(workdir, repo, item.number)
    main = main_repo_path(workdir, repo)
    branch = head_ref if item.item_type == WorkItemType.PR else f"issue-{item.number}"

    # Check if git already tracks a worktree for this branch before creating another one.
    git_existing = git.find_worktree_for_branch(main, branch)
    if git_existing is not None:
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            git.pull_ff_only(git_existing)
        if item.item_type == WorkItemType.PR and head_repo != item.repo:
            git.ensure_upstream_remote(main, item.repo)
            git.fetch_remote(main, "upstream")
        return str(git_existing)

    Path(workdir).expanduser().mkdir(parents=True, exist_ok=True)
    if not main.exists():
        GithubHelper().clone_repository(repo, main)
    git.fetch_remote(main, "origin")
    if item.item_type == WorkItemType.PR:
        if head_repo != item.repo:
            git.ensure_upstream_remote(main, item.repo)
            git.fetch_remote(main, "upstream")
        git.create_worktree(main, wt, head_ref, f"origin/{head_ref}")
    else:
        git.create_worktree(main, wt, f"issue-{item.number}", "origin/HEAD")
    return str(wt)


def get_merge_base(worktree: str) -> str | None:
    """Return the merge-base commit between HEAD and origin's default branch.

    Returns None if the merge-base cannot be determined (e.g. shallow clone).
    """
    return GitHelper().merge_base_with_origin_default(worktree)


def _worktree_candidates(base: Path, item: WorkItem) -> list[Path]:
    suffix = f"_{item.number}"
    return sorted(
        (
            candidate
            for candidate in base.iterdir()
            if candidate.is_dir() and candidate.name.endswith(suffix)
        ),
        key=lambda candidate: candidate.name,
    )
