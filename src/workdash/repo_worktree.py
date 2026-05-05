"""Git worktree management for work items."""

import contextlib
import json
import subprocess
from pathlib import Path

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


def _clone_repo(repo: str, target: Path) -> None:
    try:
        subprocess.run(
            ["gh", "repo", "clone", repo, str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Failed to clone repository: gh CLI is not installed or not on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Failed to clone {repo}: {stderr or f'exit code {exc.returncode}'}"
        ) from exc


def _fetch_origin(main_path: Path) -> None:
    try:
        subprocess.run(
            ["git", "fetch", "origin"],
            cwd=main_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Failed to fetch origin: git is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Failed to fetch origin: {stderr or f'exit code {exc.returncode}'}"
        ) from exc


def _get_pr_head_info(item: WorkItem) -> tuple[str, str]:
    """Return (head_ref_name, head_repo) for a PR.

    For same-repo PRs head_repo equals item.repo; for fork PRs it is
    the contributor's fork (e.g. "contributor/repo").
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                "headRefName,headRepository,headRepositoryOwner",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Failed to get PR head info: gh CLI is not installed or not on PATH."
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Failed to get PR head info for {item.repo}#{item.number}: "
            f"{stderr or f'exit code {exc.returncode}'}"
        ) from exc
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Invalid gh output for {item.repo}#{item.number}: expected a JSON object"
        )
    head_ref = payload.get("headRefName")
    if not isinstance(head_ref, str):
        raise RuntimeError(
            f"Missing or invalid headRefName in gh output for {item.repo}#{item.number}"
        )
    # Reconstruct head repo from headRepositoryOwner.login + headRepository.name
    head_repo_owner = payload.get("headRepositoryOwner", {})
    head_repo_info = payload.get("headRepository", {})
    owner_login = head_repo_owner.get("login", "") if isinstance(head_repo_owner, dict) else ""
    repo_name = head_repo_info.get("name", "") if isinstance(head_repo_info, dict) else ""
    head_repo = f"{owner_login}/{repo_name}" if owner_login and repo_name else item.repo
    return head_ref, head_repo


def _local_branch_exists(main_path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=main_path,
        capture_output=True,
    )
    return result.returncode == 0


def _create_worktree(main_path: Path, wt_path: Path, branch: str, start_point: str) -> None:
    subprocess.run(
        ["git", "worktree", "prune"],
        cwd=main_path,
        capture_output=True,
    )
    try:
        if _local_branch_exists(main_path, branch):
            subprocess.run(
                ["git", "branch", "-f", branch, start_point],
                cwd=main_path,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", str(wt_path), branch],
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            subprocess.run(
                ["git", "worktree", "add", "-b", branch, str(wt_path), start_point],
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            )
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(f"Failed to create worktree for branch {branch}: {stderr}") from exc
    _set_branch_upstream(main_path, branch)


def _set_branch_upstream(main_path: Path, branch: str) -> None:
    """Configure the local branch to track origin/<branch> for push/pull."""
    subprocess.run(
        ["git", "config", f"branch.{branch}.remote", "origin"],
        cwd=main_path,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", f"branch.{branch}.merge", f"refs/heads/{branch}"],
        cwd=main_path,
        capture_output=True,
    )


def get_merge_base(worktree: str) -> str | None:
    """Return the merge-base commit between HEAD and origin's default branch.

    Returns None if the merge-base cannot be determined (e.g. shallow clone).
    """
    # Resolve default branch name from origin/HEAD
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    default_branch = result.stdout.strip()
    if not default_branch:
        return None
    result = subprocess.run(
        ["git", "merge-base", "HEAD", default_branch],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit if commit else None


def _find_worktree_for_branch(main_path: Path, branch: str) -> Path | None:
    """Find an existing worktree checked out on the given branch via git."""
    if not main_path.is_dir():
        return None
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=main_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    current_path = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree ") :]
        elif line.startswith("branch refs/heads/"):
            wt_branch = line[len("branch refs/heads/") :]
            if wt_branch == branch and current_path is not None:
                wt = Path(current_path)
                if wt.is_dir():
                    return wt
    return None


def _find_existing_worktree(workdir: str, item: WorkItem) -> Path | None:
    """Find an existing worktree without a network call.

    Checks the item.repo-based path first (issues and same-repo PRs),
    then scans for fork worktrees by matching on repo name and number.
    """
    base = Path(workdir).expanduser()
    if not base.exists():
        return None
    # Direct match covers issues and same-repo PRs
    direct = worktree_path(workdir, item.repo, item.number)
    if direct.is_dir():
        return direct
    if item.item_type == WorkItemType.PR:
        _, _, repo_name = item.repo.partition("/")
        suffix = f"_{repo_name}_{item.number}"
        for candidate in base.iterdir():
            if candidate.is_dir() and candidate.name.endswith(suffix):
                return candidate
    return None


def ensure_worktree(workdir: str, item: WorkItem) -> str:
    """Ensure a worktree exists for the given work item and return its path.

    Clones the repository if it does not exist locally, fetches the latest
    changes, and creates a worktree checked out to the appropriate branch.

    :param str workdir: Base working directory (may use ~ for home).
    :param WorkItem item: The work item to prepare a worktree for.
    """
    existing = _find_existing_worktree(workdir, item)
    if existing is not None:
        # Local divergence or missing upstream shouldn't block the user.
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=existing,
                capture_output=True,
                text=True,
            )
        return str(existing)
    # Worktree doesn't exist — resolve the target repo and create it
    if item.item_type == WorkItemType.PR:
        head_ref, head_repo = _get_pr_head_info(item)
        repo = head_repo
    else:
        repo = item.repo
    wt = worktree_path(workdir, repo, item.number)
    main = main_repo_path(workdir, repo)
    branch = head_ref if item.item_type == WorkItemType.PR else f"issue-{item.number}"
    # Check if git already tracks a worktree for this branch (covers cases
    # where the directory-name scan missed, e.g. fork repo name mismatch).
    git_existing = _find_worktree_for_branch(main, branch)
    if git_existing is not None:
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=git_existing,
                capture_output=True,
                text=True,
            )
        return str(git_existing)
    Path(workdir).expanduser().mkdir(parents=True, exist_ok=True)
    if not main.exists():
        _clone_repo(repo, main)
    _fetch_origin(main)
    if item.item_type == WorkItemType.PR:
        _create_worktree(main, wt, head_ref, f"origin/{head_ref}")
    else:
        _create_worktree(main, wt, f"issue-{item.number}", "origin/HEAD")
    return str(wt)
