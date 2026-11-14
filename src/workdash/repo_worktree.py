"""Git worktree management for work items."""

import contextlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .git import GitHelper
from .github_client import GitHubClient
from .models import WorkItem, WorkItemType, accepted_worktree_numbers, worktree_item_number

_ISSUE_BRANCH_RE = re.compile(r"^issue-(\d+)$")


def main_repo_path(workdir: str, repo: str) -> Path:
    """Return the local clone directory for a repository.

    :param str workdir: Base working directory (may use ~ for home).
    :param str repo: Repository in "owner/repo" format.
    """
    name = GitHelper.repo_from_remote_url(repo) if ":" in repo else repo
    parts = name.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(f"Invalid repo format {repo!r}, expected 'owner/repo'")
    return Path(workdir).expanduser() / f"{parts[0]}_{parts[1]}"


def worktree_path(workdir: str, repo: str, number: int, *, todo: bool = False) -> Path:
    """Return the worktree directory for a work item in the given repo.

    :param str workdir: Base working directory (may use ~ for home).
    :param str repo: Repository in "owner/repo" format.
    :param int number: The issue or PR number.
    :param bool todo: Return the targeted todo worktree instead of the
        repository's own worktree for that number.
    """
    main = main_repo_path(workdir, repo)
    return main.parent / GitHelper().worktree_name(repo, number, todo=todo)


def existing_worktree_path(
    workdir: str,
    item: WorkItem,
    *,
    pinned: contextlib.ExitStack | None = None,
    git: GitHelper | None = None,
) -> Path | tuple[Path, int] | None:
    """Find a locally proven worktree without creating, fetching, or querying GitHub."""
    base = Path(workdir).expanduser()
    if not base.is_dir():
        return None
    git = git if git is not None else GitHelper()
    matches = []
    for candidate in _worktree_candidates(base, item):
        if pinned is None:
            if git.worktree_proves_item(candidate, item):
                matches.append((candidate, None))
            continue
        try:
            fd = os.open(candidate, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            continue
        pinned.callback(os.close, fd)
        directory = Path(f"/dev/fd/{fd}")
        if git.worktree_proves_item(candidate, item, directory=directory):
            matches.append((candidate, fd))
    # A checkout named after the item's own number is beyond doubt this item's,
    # so it wins over any closing issue's directory when several exist. Anything
    # still ambiguous after every accepted number is somebody's guess, not an answer.
    for number in (item.number, *accepted_worktree_numbers(item)):
        preferred = [match for match in matches if match[0].name.endswith(f"_{number}")]
        if len(preferred) == 1:
            candidate, fd = preferred[0]
            return (candidate, fd) if pinned is not None else candidate
    return None


def worktree_branch(
    item: WorkItem, *, github_client: GitHubClient | None = None
) -> tuple[str, str]:
    """Return the repository and branch the item's worktree is checked out on.

    :param WorkItem item: The work item whose worktree branch is named.
    """
    if item.todo_target is not None:
        # A targeted todo is work on the target repository; the issue itself
        # stays in the todo repository, so the branch needs no GitHub lookup.
        return item.todo_target, f"wt-{item.number}"
    if item.item_type == WorkItemType.PR:
        branch, repo = (github_client or GitHubClient()).fetch_worktree_head(item)
        return repo, branch
    return item.repo, f"issue-{item.number}"


def existing_worktree_for_branch(
    workdir: str, repo: str, branch: str, *, git: GitHelper | None = None
) -> Path | None:
    """Return the checkout git already tracks for a branch of a repository.

    :param str workdir: Base working directory (may use ~ for home).
    :param str repo: Repository in "owner/repo" format.
    :param str branch: Branch the checkout is expected to hold.
    """
    git = git if git is not None else GitHelper()
    worktree = git.find_worktree_for_branch(main_repo_path(workdir, repo), branch)
    if worktree is not None:
        _require_repository_paths(repo, worktree, git=git)
    return worktree


def ensure_worktree(
    workdir: str,
    item: WorkItem,
    *,
    git: GitHelper | None = None,
    github_client=None,
    todo_repository: str = "",
) -> str:
    """Ensure a worktree exists for the given work item and return its path.

    Clones the repository if it does not exist locally, fetches the latest
    changes, and creates a worktree checked out to the appropriate branch.

    :param str workdir: Base working directory (may use ~ for home).
    :param WorkItem item: The work item to prepare a worktree for.
    """
    git = git if git is not None else GitHelper()
    github = github_client if github_client is not None else GitHubClient()
    existing = existing_worktree_path(workdir, item, git=git)
    if existing is not None:
        # Local divergence or missing upstream shouldn't block the user.
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            git.pull_ff_only(existing)
        # A fork's base branches are only reachable through the upstream remote.
        if item.item_type == WorkItemType.PR and not git.repository_matches(existing, item.repo):
            with contextlib.suppress(RuntimeError):
                git.fetch_remote(existing, "upstream")
        return str(existing)

    todo_target = item.todo_target
    repo, branch = worktree_branch(item, github_client=github)
    wt = worktree_path(workdir, repo, worktree_item_number(item), todo=todo_target is not None)
    main = main_repo_path(workdir, repo)
    _require_repository_paths(repo, main, wt, git=git)

    # Check if git already tracks a worktree for this branch before creating another one.
    git_existing = existing_worktree_for_branch(workdir, repo, branch, git=git)
    if git_existing is not None:
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            git.pull_ff_only(git_existing)
        if item.item_type == WorkItemType.PR and repo.lower() != item.repo.lower():
            git.ensure_upstream_remote(main, item.repo)
            git.fetch_remote(main, "upstream")
        return str(git_existing)

    if wt.exists():
        raise RuntimeError(f"Worktree path {wt} is already occupied.")

    Path(workdir).expanduser().mkdir(parents=True, exist_ok=True)
    if not main.exists():
        if todo_target is not None and ":" in repo:
            git.clone_repository(repo, main)
        else:
            git.clone_github_repository(repo, main, github_client=github)
    if (
        item.item_type != WorkItemType.PR
        and todo_target is None
        and git.local_branch_exists(main, branch)
    ):
        git.create_worktree(main, wt, branch, "origin/HEAD")
        with contextlib.suppress(subprocess.CalledProcessError, OSError):
            git.pull_ff_only(wt)
        return str(wt)
    git.fetch_remote(main, "origin")
    git.fast_forward_default_branch(main)
    if item.item_type == WorkItemType.PR:
        if repo.lower() != item.repo.lower():
            git.ensure_upstream_remote(main, item.repo)
            git.fetch_remote(main, "upstream")
        local_branch = git.local_branch_exists(main, branch)
        git.create_worktree(main, wt, branch, f"origin/{branch}")
        if local_branch:
            git.set_branch_upstream(main, branch)
            try:
                git.fast_forward_branch(wt, f"origin/{branch}")
            except (subprocess.CalledProcessError, OSError) as exc:
                try:
                    subprocess.run(
                        ["git", "worktree", "remove", str(wt)],
                        cwd=main,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                except (subprocess.CalledProcessError, OSError) as cleanup_exc:
                    raise RuntimeError(
                        f"Failed to update {branch} from origin/{branch}; "
                        f"remove or resolve the worktree at {wt} before retrying. "
                        f"Cleanup failed: {cleanup_exc}"
                    ) from exc
                raise RuntimeError(
                    f"Failed to update {branch} from origin/{branch}; "
                    "reconcile the local branch with the pull request head before retrying."
                ) from exc
    elif todo_target is not None:
        git.create_worktree(main, wt, branch, "origin/HEAD")
    else:
        if not git.fetch_origin_branch(main, branch, missing_ok=True):
            if item.repo.lower() == todo_repository.lower():
                git.create_worktree(main, wt, branch, "origin/HEAD")
                return str(wt)
            try:
                github.create_issue_branch(repo, item.number, branch)
            except RuntimeError:
                found = False
                with contextlib.suppress(OSError, RuntimeError):
                    found = git.fetch_origin_branch(main, branch, missing_ok=True)
                if not found:
                    with contextlib.suppress(subprocess.CalledProcessError, OSError, RuntimeError):
                        _refresh_origin_head(main)
                    git.create_worktree(main, wt, branch, "origin/HEAD")
                    return str(wt)
            else:
                git.fetch_origin_branch(main, branch)
        git.create_worktree(main, wt, branch, f"origin/{branch}")
    return str(wt)


@contextlib.contextmanager
def analysis_worktree(
    workdir: str, item: WorkItem, *, git: GitHelper, github_client, todo_repository: str = ""
):
    """Reuse item work or temporarily check out the target's default branch."""
    if item.item_type == WorkItemType.PR:
        yield ensure_worktree(
            workdir, item, git=git, github_client=github_client, todo_repository=todo_repository
        )
        return
    if existing_worktree_path(workdir, item, git=git) is not None:
        yield ensure_worktree(
            workdir, item, git=git, github_client=github_client, todo_repository=todo_repository
        )
        return

    repo, branch = worktree_branch(item, github_client=github_client)
    main = main_repo_path(workdir, repo)
    wt = worktree_path(workdir, repo, worktree_item_number(item), todo=item.todo_target is not None)
    _require_repository_paths(repo, main, wt, git=git)
    if existing_worktree_for_branch(workdir, repo, branch, git=git) is not None:
        yield ensure_worktree(
            workdir, item, git=git, github_client=github_client, todo_repository=todo_repository
        )
        return
    if wt.exists():
        raise RuntimeError(f"Worktree path {wt} is already occupied.")
    main.parent.mkdir(parents=True, exist_ok=True)
    if not main.exists():
        if item.todo_target is not None and ":" in repo:
            git.clone_repository(repo, main)
        else:
            git.clone_github_repository(repo, main, github_client=github_client)
    _refresh_origin_head(main, configure_fetch=False)
    with tempfile.TemporaryDirectory(prefix="workdash-analysis-") as directory:
        path = Path(directory) / wt.name
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), "origin/HEAD"],
            cwd=main,
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            yield str(path)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=main,
                check=True,
                capture_output=True,
                text=True,
            )


def get_merge_base(worktree: str, *, git: GitHelper | None = None) -> str | None:
    """Return the merge-base commit between HEAD and origin's default branch.

    Returns None if the merge-base cannot be determined (e.g. shallow clone).
    """
    return (git if git is not None else GitHelper()).merge_base_with_origin_default(worktree)


def issue_branch_number(branch: str) -> int | None:
    """Return the issue number of a branch named the way ``ensure_worktree`` names an issue's."""
    match = _ISSUE_BRANCH_RE.match(branch)
    return int(match.group(1)) if match else None


def _refresh_origin_head(main: Path, *, configure_fetch: bool = True) -> None:
    remote_head = subprocess.run(
        ["git", "ls-remote", "--symref", "origin", "HEAD"],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    default_ref = next(
        (line.split()[1] for line in remote_head.splitlines() if line.startswith("ref: ")),
        None,
    )
    if default_ref is None or not default_ref.startswith("refs/heads/"):
        raise RuntimeError("Failed to determine origin's default branch.")
    branch = default_ref.removeprefix("refs/heads/")
    fetch_spec = f"+{default_ref}:refs/remotes/origin/{branch}"
    if configure_fetch:
        fetch_specs = subprocess.run(
            ["git", "config", "--get-all", "remote.origin.fetch"],
            cwd=main,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if (
            fetch_spec not in fetch_specs
            and "+refs/heads/*:refs/remotes/origin/*" not in fetch_specs
        ):
            subprocess.run(
                [
                    "git",
                    "config",
                    "--add",
                    "remote.origin.fetch",
                    "+refs/heads/*:refs/remotes/origin/*",
                ],
                cwd=main,
                check=True,
                capture_output=True,
                text=True,
            )
    subprocess.run(
        ["git", "fetch", "origin", fetch_spec],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "remote", "set-head", "origin", branch],
        cwd=main,
        check=True,
        capture_output=True,
        text=True,
    )


def _worktree_candidates(base: Path, item: WorkItem) -> list[Path]:
    suffixes = tuple(f"_{number}" for number in accepted_worktree_numbers(item))
    return sorted(
        (
            candidate
            for candidate in base.iterdir()
            if candidate.is_dir() and candidate.name.endswith(suffixes)
        ),
        key=lambda candidate: candidate.name,
    )


def _require_repository_paths(repo: str, *paths: Path, git: GitHelper) -> None:
    """Reject occupied paths before any repository reuse can pull or fetch."""
    for path in paths:
        if path.exists() and not git.repository_matches(path, repo):
            raise RuntimeError(
                f"Local directory {path} belongs to a different repository than {repo}."
            )
