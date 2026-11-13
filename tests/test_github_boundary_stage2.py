from datetime import UTC, datetime
from unittest.mock import MagicMock

from workdash import repo_worktree
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def test_worktree_uses_supplied_client_for_github_clone(tmp_path, monkeypatch):
    client = MagicMock()
    git = MagicMock()
    git.find_worktree_for_branch.return_value = None
    git.clone_github_repository.side_effect = lambda repo, path, *, github_client: (
        github_client.clone_repository(repo, path)
    )
    monkeypatch.setattr(repo_worktree, "GitHelper", lambda: git)
    now = datetime.now(UTC)
    item = WorkItem(
        kind=WorkItemKind.ASSIGNED_ISSUE,
        item_type=WorkItemType.ISSUE,
        repo="owner/repo",
        number=1,
        title="task",
        url="https://github.com/owner/repo/issues/1",
        created_at=now,
        updated_at=now,
    )
    repo_worktree.ensure_worktree(str(tmp_path), item, github_client=client)
    client.clone_repository.assert_called_once_with("owner/repo", tmp_path / "owner_repo")


def test_startup_and_backend_use_same_github_client(monkeypatch):
    import workdash.workdash as app
    from workdash.config import WorkdashConfig

    client = MagicMock()
    client.check_authentication.return_value = None
    monkeypatch.setattr(app, "GitHubClient", lambda: client)
    monkeypatch.setattr(
        app.WorkdashCommands,
        "preload_config",
        lambda self: setattr(self, "_config", WorkdashConfig()) or True,
    )
    monkeypatch.setattr(app, "_should_wrap_interactive_start", lambda options: False)
    seen = []
    monkeypatch.setattr(
        app.WorkdashCommands,
        "interactive",
        lambda self, **kwargs: seen.append(self._load_config_and_backend()[1].github_client) or 0,
    )

    assert app.main(["--direct"]) == 0
    client.check_authentication.assert_called_once()
    assert seen == [client]


def test_configure_bootstraps_binaries_using_supplied_github_client(tmp_path, monkeypatch):
    import tarfile
    from io import BytesIO

    import workdash.config as config

    archive = BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        for name in ("gh", "zellij"):
            data = name.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, BytesIO(data))
    client = MagicMock()
    client.release_asset_url.return_value = "https://example.test/gh.tar.gz"
    client.download_release.return_value = archive.getvalue()
    monkeypatch.setattr(config.platform, "system", lambda: "Linux")
    monkeypatch.setattr(config.platform, "machine", lambda: "x86_64")
    inputs = iter(["", "", "", "", "", "octocat", "", ""])
    config.configure(
        tmp_path / "config.json",
        input_fn=lambda _: next(inputs),
        which_fn=lambda cmd: f"/usr/bin/{cmd}" if cmd in ("xdg-open", "claude") else None,
        github_client=client,
    )
    assert (tmp_path / "bin/gh").read_bytes() == b"gh"
    assert (tmp_path / "bin/zellij").read_bytes() == b"zellij"
    client.release_asset_url.assert_called_once()
    assert client.download_release.call_count == 2
    assert client.download_release.call_args_list[1].args[0] == "https://example.test/gh.tar.gz"
    client.check_authentication.assert_not_called()


def test_configure_entrypoint_uses_application_github_client(monkeypatch):
    import workdash.workdash as app

    client = MagicMock()
    configure = MagicMock()
    monkeypatch.setattr(app, "GitHubClient", lambda: client)
    monkeypatch.setattr(app, "configure", configure)
    assert app.main(["--configure"]) == 0
    configure.assert_called_once_with(github_client=client)
    client.check_authentication.assert_not_called()
