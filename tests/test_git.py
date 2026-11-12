import subprocess
from pathlib import Path

import pytest

from workdash.git import GitHelper


def commit_everything(tmp_path: Path, message: str = "initial") -> str:
    """Initialize the repository at ``tmp_path`` when needed, commit everything, and
    return the new head SHA."""

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "--all"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", message], cwd=tmp_path, check=True
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


def test_unpublished_work_is_absent_when_the_worktree_is_what_github_has(tmp_path: Path) -> None:
    head_sha = commit_everything(tmp_path)

    assert GitHelper().has_unpublished_work(str(tmp_path), head_sha) is False


def test_unpublished_work_includes_an_uncommitted_change(tmp_path: Path) -> None:
    (tmp_path / "notes.md").write_text("committed\n")
    head_sha = commit_everything(tmp_path)
    (tmp_path / "notes.md").write_text("work in progress\n")

    assert GitHelper().has_unpublished_work(str(tmp_path), head_sha) is True


def test_unpublished_work_includes_a_commit_github_does_not_have(tmp_path: Path) -> None:
    pushed_sha = commit_everything(tmp_path)
    commit_everything(tmp_path, "local only")

    assert GitHelper().has_unpublished_work(str(tmp_path), pushed_sha) is True


def test_unpublished_work_includes_an_unadded_file(tmp_path: Path) -> None:
    head_sha = commit_everything(tmp_path)
    (tmp_path / "parser.py").write_text("def parse(tokens):\n    return tokens\n")

    assert GitHelper().has_unpublished_work(str(tmp_path), head_sha) is True


def test_unpublished_work_ignores_workdash_artifacts(tmp_path: Path) -> None:
    head_sha = commit_everything(tmp_path)
    (tmp_path / ".workdash").mkdir()
    (tmp_path / ".workdash" / "PR_REVIEW.md").write_text("review\n")

    assert GitHelper().has_unpublished_work(str(tmp_path), head_sha) is False


def test_worktree_name_preserves_owner_repo_number_shape() -> None:
    assert GitHelper().worktree_name("owner/repo", 42) == "owner_repo_42"


def test_get_repo_root_resolves_from_a_subdirectory(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    nested_dir = tmp_path / "pkg" / "module"
    nested_dir.mkdir(parents=True)

    assert GitHelper().get_repo_root(nested_dir) == tmp_path.resolve()


def test_get_repo_root_rejects_a_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Not a git repository."):
        GitHelper().get_repo_root(tmp_path)


def test_get_current_branch_returns_the_checked_out_branch(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "feature"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "initial"], cwd=tmp_path, check=True
    )

    assert GitHelper().get_current_branch(tmp_path) == "feature"


def test_get_current_branch_raises_when_git_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="Failed to determine the current branch."):
        GitHelper().get_current_branch(tmp_path)


def test_repo_from_remote_url_accepts_https_and_ssh_forms() -> None:
    assert GitHelper.repo_from_remote_url("https://github.com/owner/repo.git") == "owner/repo"
    assert GitHelper.repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"


def test_fetch_remote_runs_git_fetch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHelper().fetch_remote(tmp_path, "upstream")

    assert calls == [
        (
            ["git", "fetch", "--prune", "upstream"],
            {"cwd": tmp_path, "check": True, "capture_output": True, "text": True},
        )
    ]


def test_fetch_remote_reports_git_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr="no such remote")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="Failed to fetch upstream: no such remote"):
        GitHelper().fetch_remote(tmp_path, "upstream")


def test_fast_forward_default_branch_switches_to_default_branch_before_merging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(*args, **kwargs):
        calls.append((args[0], kwargs))
        stdout = "origin/main\n" if args[0][1] == "rev-parse" else ""
        return subprocess.CompletedProcess(args[0], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHelper().fast_forward_default_branch(tmp_path)

    expected_kwargs = {"cwd": tmp_path, "check": True, "capture_output": True, "text": True}
    assert calls == [
        (["git", "rev-parse", "--abbrev-ref", "origin/HEAD"], expected_kwargs),
        (["git", "switch", "main"], expected_kwargs),
        (["git", "merge", "--ff-only", "origin/HEAD"], expected_kwargs),
    ]


def test_fast_forward_default_branch_reports_git_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args[0], stderr="not a git repository")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(
        RuntimeError, match="Failed to fast-forward the default branch: not a git repository"
    ):
        GitHelper().fast_forward_default_branch(tmp_path)


@pytest.mark.parametrize(
    ("remote_url", "head_repo", "matches"),
    [
        ("https://github.com/Owner/Repo.git", "owner/repo", True),
        ("https://github.com/Owner/Other.git", "owner/repo", False),
        ("https://example.com/Owner/Repo.git", "https://example.com/owner/repo", False),
    ],
)
def test_branch_tracking_verifies_repository_identity(tmp_path, remote_url, head_repo, matches):
    commit_everything(tmp_path)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/pr-branch", "HEAD"], cwd=tmp_path, check=True
    )
    subprocess.run(
        ["git", "branch", "--set-upstream-to", "origin/pr-branch"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    assert GitHelper().branch_tracks_repository(tmp_path, "pr-branch", head_repo) is matches


def test_git_path_uses_worktree_and_relative_git_path_without_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    worktree = tmp_path / "linked" / "nested"
    exclude = tmp_path / "repo" / ".git" / "info" / "exclude"

    def fake_run(args, **kwargs):
        assert args == ["git", "rev-parse", "--path-format=absolute", "--git-path", "info/exclude"]
        assert kwargs == {"cwd": worktree, "capture_output": True, "text": True, "check": True}
        return subprocess.CompletedProcess(args, 0, stdout=f"{exclude}\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert GitHelper().get_git_path("info/exclude", worktree) == exclude


def test_git_path_resolves_common_exclude_from_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    commit_everything(repo)
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "-q", "-b", "feature", str(worktree)], cwd=repo, check=True
    )
    nested = worktree / "nested"
    nested.mkdir()

    git = GitHelper()
    exclude = git.get_git_path("info/exclude", nested)
    assert exclude == repo / ".git" / "info" / "exclude"
    assert git.get_git_path("info/exclude", repo) == exclude
    with exclude.open("a") as file:
        file.write(".workdash/PR_REVIEW.md\n")
    subprocess.run(
        ["git", "check-ignore", "-q", ".workdash/PR_REVIEW.md"], cwd=worktree, check=True
    )
