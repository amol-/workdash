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


def existing_worktree_path(workdir: str, item: WorkItem) -> Path | None:
    """Find a locally proven worktree without creating, fetching, or querying GitHub."""
    base = Path(workdir).expanduser()
    if not base.is_dir():
        return None
    git = GitHelpers()
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
    existing = existing_worktree_path(workdir, item)
    if existing is not None:
        # Local divergence or missing upstream shouldn't block the user.
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=existing,
                capture_output=True,
                text=True,
            )
        if item.item_type == WorkItemType.PR:
            git = GitHelpers()
            if existing.name != git.worktree_name(item.repo, item.number):
                with contextlib.suppress(RuntimeError):
                    _fetch_remote(existing, "upstream")
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
    # Check if git already tracks a worktree for this branch before creating another one.
    git_existing = _find_worktree_for_branch(main, branch)
    if git_existing is not None:
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=git_existing,
                capture_output=True,
                text=True,
            )
        if item.item_type == WorkItemType.PR and head_repo != item.repo:
            _ensure_upstream_remote(main, item.repo)
            _fetch_remote(main, "upstream")
        return str(git_existing)
    Path(workdir).expanduser().mkdir(parents=True, exist_ok=True)
    if not main.exists():
        _clone_repo(repo, main)
    _fetch_remote(main, "origin")
    if item.item_type == WorkItemType.PR:
        if head_repo != item.repo:
            _ensure_upstream_remote(main, item.repo)
            _fetch_remote(main, "upstream")
        _create_worktree(main, wt, head_ref, f"origin/{head_ref}")
    else:
        _create_worktree(main, wt, f"issue-{item.number}", "origin/HEAD")
    return str(wt)


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


class GitHelpers:
    """Local git metadata helpers used to prove Workdash worktrees."""

    def worktree_proves_item(self, candidate: Path, item: WorkItem) -> bool:
        if not self.is_worktree_root(candidate):
            return False
        origin_repo = self.remote_repo(candidate, "origin")
        if origin_repo == item.repo:
            return candidate.name == self.worktree_name(origin_repo, item.number)
        if item.item_type == WorkItemType.PR and origin_repo is not None:
            if self.remote_repo(candidate, "upstream") != item.repo:
                return False
            return candidate.name == self.worktree_name(origin_repo, item.number)
        return False

    def is_worktree_root(self, candidate: Path) -> bool:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=candidate,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        if result.returncode != 0:
            return False
        root = result.stdout.strip()
        return bool(root) and Path(root).resolve() == candidate.resolve()

    def remote_repo(self, candidate: Path, remote_name: str) -> str | None:
        remote_url = self.remote_url(candidate, remote_name)
        if remote_url is None:
            return None
        return self.repo_from_remote_url(remote_url) or None

    def worktree_name(self, repo: str, number: int) -> str:
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return ""
        return f"{owner}_{name}_{number}"

    def remote_url(self, candidate: Path, remote_name: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "config", "--local", "--get", f"remote.{remote_name}.url"],
                cwd=candidate,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None

    @staticmethod
    def repo_from_remote_url(remote_url: str) -> str:
        normalized = remote_url.strip().rstrip("/").removesuffix(".git")
        if "://" not in normalized and ":" in normalized:
            normalized = normalized.replace(":", "/", 1)
        parts = normalized.rsplit("/", 2)
        if len(parts) < 2:
            return ""
        return f"{parts[-2]}/{parts[-1]}"


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


def _fetch_remote(main_path: Path, remote: str) -> None:
    try:
        subprocess.run(
            ["git", "fetch", remote],
            cwd=main_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Failed to fetch {remote}: git is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Failed to fetch {remote}: {stderr or f'exit code {exc.returncode}'}"
        ) from exc


def _ensure_upstream_remote(main_path: Path, repo: str) -> None:
    upstream_url = f"https://github.com/{repo}.git"
    existing = subprocess.run(
        ["git", "remote", "get-url", "upstream"],
        cwd=main_path,
        capture_output=True,
        text=True,
    )
    command = (
        ["git", "remote", "set-url", "upstream", upstream_url]
        if existing.returncode == 0
        else ["git", "remote", "add", "upstream", upstream_url]
    )
    try:
        subprocess.run(
            command,
            cwd=main_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Failed to configure upstream: git is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        raise RuntimeError(
            f"Failed to configure upstream for {repo}: {stderr or f'exit code {exc.returncode}'}"
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
