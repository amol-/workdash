"""Git command helpers for Workdash-owned repositories."""

import subprocess
from pathlib import Path

from .models import WorkItem, WorkItemType, accepted_worktree_numbers


class GitHelper:
    """Local git command helpers used to prepare and prove Workdash worktrees."""

    def worktree_proves_item(self, candidate: Path, item: WorkItem) -> bool:
        if not self.is_worktree_root(candidate):
            return False
        origin_repo = self.remote_repo(candidate, "origin")
        if item.todo_target is not None:
            # A targeted todo works on the target's code, so its worktree is a
            # clone of the target under a name of its own.
            return origin_repo == item.todo_target and candidate.name == self.worktree_name(
                item.todo_target, item.number, todo=True
            )
        if origin_repo is None:
            return False
        accepted_names = {
            self.worktree_name(origin_repo, number) for number in accepted_worktree_numbers(item)
        }
        if origin_repo == item.repo:
            return candidate.name in accepted_names
        if item.item_type == WorkItemType.PR:
            if self.remote_repo(candidate, "upstream") != item.repo:
                return False
            return candidate.name in accepted_names
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

    def worktree_name(self, repo: str, number: int, *, todo: bool = False) -> str:
        """Return the Workdash-owned worktree directory name for a work item.

        :param str repo: Repository the worktree is a clone of, as ``owner/repo``.
        :param int number: Issue or pull request number.
        :param bool todo: Name a targeted todo worktree, which must not collide
            with the target repository's own worktree for the same number.
        """
        owner, _, name = repo.partition("/")
        if not owner or not name:
            return ""
        return f"{owner}_{name}_todo_{number}" if todo else f"{owner}_{name}_{number}"

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

    def pull_ff_only(self, worktree: Path) -> None:
        subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=worktree,
            capture_output=True,
            text=True,
        )

    def fetch_remote(self, main_path: Path, remote: str) -> None:
        try:
            subprocess.run(
                ["git", "fetch", "--prune", remote],
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Failed to fetch {remote}: git is not installed or not on PATH."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(
                f"Failed to fetch {remote}: {stderr or f'exit code {exc.returncode}'}"
            ) from exc

    def fast_forward_default_branch(self, main_path: Path) -> None:
        """Switch the root clone to its default branch and fast-forward it.

        The root clone is exclusively managed by Workdash and must always stay
        on the default branch; item branches live in dedicated worktrees.
        """
        try:
            origin_head = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            default_branch = origin_head.partition("/")[2]
            subprocess.run(
                ["git", "switch", default_branch],
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "merge", "--ff-only", "origin/HEAD"],
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Failed to fast-forward the default branch: git is not installed or not on PATH."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(
                "Failed to fast-forward the default branch: "
                f"{stderr or f'exit code {exc.returncode}'}"
            ) from exc

    def ensure_upstream_remote(self, main_path: Path, repo: str) -> None:
        upstream_url = f"https://github.com/{repo}.git"
        try:
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
            subprocess.run(
                command,
                cwd=main_path,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Failed to configure upstream: git is not installed or not on PATH."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(
                f"Failed to configure upstream for {repo}: "
                f"{stderr or f'exit code {exc.returncode}'}"
            ) from exc

    def create_worktree(
        self, main_path: Path, wt_path: Path, branch: str, start_point: str
    ) -> None:
        try:
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=main_path,
                capture_output=True,
            )
            if self.local_branch_exists(main_path, branch):
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
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Failed to create worktree for branch {branch}: "
                "git is not installed or not on PATH."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(f"Failed to create worktree for branch {branch}: {stderr}") from exc
        self.set_branch_upstream(main_path, branch)

    def local_branch_exists(self, main_path: Path, branch: str) -> bool:
        result = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=main_path,
            capture_output=True,
        )
        return result.returncode == 0

    def set_branch_upstream(self, main_path: Path, branch: str) -> None:
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

    def find_worktree_for_branch(self, main_path: Path, branch: str) -> Path | None:
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

    def current_branch(self, worktree: Path) -> str | None:
        """Return the branch checked out in a worktree, or None when detached."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=worktree,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        return branch if branch and branch != "HEAD" else None

    def merge_base_with_origin_default(self, worktree: str) -> str | None:
        """Return the merge-base commit between HEAD and origin's default branch."""
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

    @staticmethod
    def repo_from_remote_url(remote_url: str) -> str:
        normalized = remote_url.strip().rstrip("/").removesuffix(".git")
        if "://" not in normalized and ":" in normalized:
            normalized = normalized.replace(":", "/", 1)
        parts = normalized.rsplit("/", 2)
        if len(parts) < 2:
            return ""
        return f"{parts[-2]}/{parts[-1]}"
