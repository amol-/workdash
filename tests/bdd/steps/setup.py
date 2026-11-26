"""Drive the setup CLI with scripted input and isolated executables."""

import fcntl
import io
import json
import os
import platform
import pty
import shlex
import shutil
import struct
import subprocess
import sys
import tarfile
import termios
import time
import urllib.request
import zipfile
from functools import partial
from pathlib import Path

import pytest
from pytest_bdd import given, parsers, then, when

from workdash import config, launcher, workdash


@pytest.fixture
def setup_cli(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    path = home / ".config" / "workdash" / "config.json"
    path.parent.mkdir(parents=True)
    bin_dir = tmp_path / "system-bin"
    bin_dir.mkdir()
    zellij = shutil.which("zellij")
    monkeypatch.setenv("HOME", str(home))
    for key in ("ZELLIJ", "ZELLIJ_SESSION_NAME", "ZELLIJ_PANE_ID"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1")
    monkeypatch.setattr(workdash, "CONFIG_PATH", path)
    monkeypatch.setattr(workdash, "load_config", partial(config.load_config, path))
    monkeypatch.setattr(launcher, "_WORKDASH_LOCAL_BIN", path.parent / "bin")
    monkeypatch.setattr(
        workdash.WorkdashCommands, "interactive", lambda self: pytest.fail("unexpected dashboard")
    )
    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **kw: pytest.fail("unexpected download")
    )

    for command in ("zellij", "gh", "xdg-open", "open", "claude", "aider", "term-view"):
        script = bin_dir / command
        script.write_text(
            f'#!/bin/sh\ncase "$*" in ("--version") echo "{command} test version";; (*) exit 1;; esac\n'
        )
        if command == "zellij":
            script.write_text('#!/bin/sh\n[ "$*" = "list-sessions --no-formatting" ]\n')
        elif command == "gh":
            script.write_text(
                '#!/bin/sh\ncase "$*" in ("--version") echo "gh test version";; '
                '("auth status") exit 0;; (*) exit 1;; esac\n'
            )
        script.chmod(0o755)

    prompts = []

    def run(args, answers=(), *, cancel=False):
        defaults = {
            "Open command": "",
            "Does the viewer require": "",
            "Analysis viewer": "",
            "Agents for analyze": "",
            "Agents for code": "",
            "GitHub username": "octocat",
            "Work directory": "~/projects",
            "Todo repository": "",
        }
        remaining = list(answers)
        prompts.clear()

        def reply(prompt):
            print(prompt)
            prompts.append(prompt)
            if cancel:
                raise KeyboardInterrupt
            if remaining and prompt.startswith(remaining[0][0]):
                _, answer = remaining.pop(0)
                return "" if answer == "Enter" else answer
            for label, answer in defaults.items():
                if prompt.startswith(label):
                    return answer
            pytest.fail(f"Unexpected wizard prompt: {prompt!r}")

        monkeypatch.setattr(workdash, "configure", partial(config.configure, path, input_fn=reply))
        # Each invocation starts with the user's original PATH, not the previous preflight's.
        with monkeypatch.context() as env:
            env.setenv("PATH", str(bin_dir))
            status = workdash.main(args)
        captured = capsys.readouterr()
        assert not remaining, (remaining, prompts, captured)
        return status, captured.out + captured.err

    return {"path": path, "bin": bin_dir, "run": run, "real_zellij": zellij, "prompts": prompts}


@given("the user has no Workdash configuration")
def no_config(setup_cli):
    assert not setup_cli["path"].exists()


@given("Claude is installed")
def claude_is_installed(setup_cli):
    assert (setup_cli["bin"] / "claude").is_file()


@given("no supported agent is installed")
def remove_detectable_agents(setup_cli):
    (setup_cli["bin"] / "claude").unlink()


@given("the saved URL opener is no longer installed")
def unavailable_opener(setup_cli):
    _save_existing_settings(setup_cli, open="removed-opener", open_needs_pane=None)


@given(
    "the user has saved their settings except for the analysis viewer, work directory, and todo destination"
)
def incomplete_settings(setup_cli):
    _save_existing_settings(setup_cli, analysis_viewer=None, workdir="", todo_repository="")


@given("the user already has a complete configuration with agents")
def complete_settings(setup_cli):
    _save_existing_settings(setup_cli)


@given(
    "the configuration still names `workdash mdview` as the analysis viewer",
    target_fixture="settings_before_viewer_repair",
)
def retired_analysis_viewer(setup_cli):
    _save_existing_settings(
        setup_cli,
        analysis_viewer={
            "command": "workdash mdview",
            "format": "markdown",
            "needs_pane": True,
        },
        listens=[],
    )
    settings = json.loads(setup_cli["path"].read_text())
    del settings["analysis_viewer"]
    return settings


@given("the saved configuration is missing the analysis viewer")
def missing_analysis_viewer(setup_cli):
    _save_existing_settings(setup_cli, analysis_viewer=None)


@given("the user has a complete Workdash configuration but no installed Zellij")
def existing_settings_without_zellij(setup_cli, monkeypatch):
    _save_existing_settings(setup_cli)
    _missing_dependency(setup_cli, monkeypatch, "zellij", real_zellij=True)


@given("Zellij is not on PATH for setup")
def no_zellij(setup_cli, monkeypatch):
    _missing_dependency(setup_cli, monkeypatch, "zellij")


@given("GitHub CLI is not on PATH for setup")
def no_github_cli(setup_cli, monkeypatch):
    _missing_dependency(setup_cli, monkeypatch, "gh")


@when(parsers.re(r"the user runs `workdash(?P<flags> --configure|)` and answers the wizard:"))
def answer_wizard(setup_cli, flags, datatable):
    status, setup_cli["output"] = setup_cli["run"](flags.split(), datatable[1:])
    assert status == 0, setup_cli["output"]


@when("the user completes `workdash --configure`")
def complete_explicit_setup(setup_cli):
    status, setup_cli["output"] = setup_cli["run"](["--configure"])
    assert status == 0, setup_cli["output"]


@when("the user completes the wizard started by `workdash`")
def complete_automatic_setup(setup_cli):
    status, setup_cli["output"] = setup_cli["run"]([])
    assert status == 0, setup_cli["output"]


@when("the user runs `workdash` and presses Ctrl+C at the first wizard prompt")
def cancel_setup(setup_cli):
    _, setup_cli["output"] = setup_cli["run"]([], cancel=True)


@then("the wizard asks again for the required GitHub username")
def asks_for_username_again(setup_cli):
    assert "GitHub username is required." in setup_cli["output"]


@then("the wizard saves the configuration to `~/.config/workdash/config.json`")
def reports_saved_path(setup_cli):
    assert f"Configuration saved to {setup_cli['path']}" in setup_cli["output"]
    config.load_config(setup_cli["path"]).require_valid()


@then(parsers.parse("the saved username is `{username}` and work directory is `{directory}`"))
def saves_username_and_work_directory(setup_cli, username, directory):
    saved = config.load_config(setup_cli["path"])
    assert saved.github_username == username
    assert saved.workdir == directory


@then(parsers.parse("the suggested todo destination `{repo}` is saved"))
@then(parsers.parse("the wizard saves `{repo}` as the todo destination"))
def saves_todo_destination(setup_cli, repo):
    assert config.load_config(setup_cli["path"]).todo_repository == repo


@then("the wizard reports detecting Claude and saves a configuration with Claude")
def saves_detected_claude(setup_cli):
    assert "Detected 'claude' on PATH" in setup_cli["output"]
    assert "claude" in config.load_config(setup_cli["path"]).agents


@then("the wizard explains that an agent is required")
def requires_an_agent(setup_cli):
    assert "At least one agent is required" in setup_cli["output"]


@then(parsers.parse("the wizard saves `{name}` as an analysis agent without a launch command"))
def saves_analysis_agent(setup_cli, name):
    agent = config.load_config(setup_cli["path"]).agents[name]
    assert agent.analyze == "aider --yes"
    assert agent.launch == ""


@then("the wizard saves the replacement URL opener for use in a terminal")
def saves_replacement_opener(setup_cli):
    saved = config.load_config(setup_cli["path"])
    assert saved.open_command == "term-view --quiet" and saved.open_needs_pane is True


@then("the saved repositories are unchanged")
def keeps_repositories(setup_cli):
    assert config.load_config(setup_cli["path"]).repositories == ("owner/repo",)


@then(parsers.parse("the wizard reports a usable local installation of {tool}"))
def reports_installation(setup_cli, tool):
    binary = setup_cli["path"].parent / "bin" / {"GitHub CLI": "gh", "Zellij": "zellij"}[tool]
    assert f"Installed {tool} to: {binary}" in setup_cli["output"]
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=True, timeout=5
    )
    assert binary.name in result.stdout.lower()


@then("the wizard asks only for the missing settings")
def asks_only_for_missing_settings(setup_cli):
    assert setup_cli["prompts"] == [
        "Analysis viewer [workdash view]: ",
        "Work directory [~/wrk]: ",
        "Todo repository [octocat/todos]: ",
    ]


@then(
    parsers.parse(
        "the wizard saves `{directory}` as the work directory without changing the saved repositories"
    )
)
def saves_work_directory(setup_cli, directory):
    assert config.load_config(setup_cli["path"]).workdir == directory
    keeps_repositories(setup_cli)


@then("the wizard saves the configuration without prompting")
def saves_without_prompting(setup_cli):
    reports_saved_path(setup_cli)
    assert setup_cli["prompts"] == []


@then("the wizard explains how to hide repositories from the dashboard")
def reports_repository_exclusion_hint(setup_cli):
    assert "Hide repositories from the dashboard" in setup_cli["output"]
    assert "excluded_repositories" in setup_cli["output"]


@then(parsers.parse("the wizard offers and saves `{command}` as the replacement"))
def saves_default_analysis_viewer(setup_cli, command):
    assert f"Analysis viewer [{command}]" in setup_cli["output"]
    assert config.load_config(setup_cli["path"]).analysis_viewer == {
        "command": command,
        "format": "markdown",
        "needs_pane": True,
    }


@then("all other settings are unchanged")
def keeps_settings_after_viewer_repair(setup_cli, settings_before_viewer_repair):
    assert setup_cli["prompts"] == ["Analysis viewer [workdash view]: "]
    saved = json.loads(setup_cli["path"].read_text())
    del saved["analysis_viewer"]
    assert saved == settings_before_viewer_repair


@then("the wizard saves the custom HTML analysis viewer for use in a terminal")
def saves_html_viewer(setup_cli):
    viewer = config.load_config(setup_cli["path"]).analysis_viewer
    assert viewer == {"command": "term-view --quiet", "format": "html", "needs_pane": True}


@then("Workdash exits and asks the user to run `workdash` again to open the dashboard")
def reports_relaunch(setup_cli):
    assert "Run 'workdash' again to start the dashboard." in setup_cli["output"]
    assert "Loading work items" not in setup_cli["output"]


@then(parsers.parse("the wizard saves `{selector}` to track and `{todo}` for todos"))
def saves_managed_repositories(setup_cli, selector, todo):
    saved = config.load_config(setup_cli["path"])
    assert saved.repositories == (selector,)
    assert saved.todo_repository == todo


@then("the wizard asks for a todo repository in `owner/repo` form")
def rejects_todo(setup_cli):
    assert "Enter a todo repository in owner/repo form." in setup_cli["output"]


@then("Workdash reports that configuration was cancelled without saving it")
def reports_cancellation(setup_cli):
    assert "Configuration cancelled." in setup_cli["output"]
    assert not setup_cli["path"].exists()


@when("the user runs `workdash` to open the dashboard")
def first_start(setup_cli):
    status, setup_cli["output"] = setup_cli["run"]([])
    assert status != 0, setup_cli["output"]


@then("Workdash reports the missing `analysis_viewer` setting")
def reports_missing_analysis_viewer(setup_cli):
    assert "analysis_viewer" in setup_cli["output"]


@then("Workdash directs them to `workdash --configure`")
def directs_to_setup(setup_cli):
    assert "workdash --configure" in setup_cli["output"]


def _missing_dependency(setup_cli, monkeypatch, command, *, real_zellij=False):
    if real_zellij:
        if not setup_cli["real_zellij"]:
            pytest.skip("Zellij is not installed on the test host")
        payload = Path(setup_cli["real_zellij"]).read_bytes()
    else:
        # The release service is faked, not the installation. This artifact can
        # be executed after the real installer extracts and installs it.
        payload = f'#!/bin/sh\n[ "$1" = --version ] || exit 1\necho "{command} fixture"\n'.encode()
    (setup_cli["bin"] / command).unlink()

    metadata_url = config.GH_LATEST_RELEASE_API_URL
    asset_url = "https://example.test/gh-release"
    archive = io.BytesIO()
    if command == "gh" and platform.system() == "Darwin":
        with zipfile.ZipFile(archive, "w") as release:
            release.writestr("release/bin/gh", payload)
    else:
        with tarfile.open(fileobj=archive, mode="w:gz", compresslevel=1) as release:
            member = tarfile.TarInfo(command)
            member.size = len(payload)
            release.addfile(member, io.BytesIO(payload))

    def release_response(url, *, timeout):
        if command == "gh" and url == metadata_url:
            suffix, _ = config._gh_release_asset_patterns()
            return io.BytesIO(
                json.dumps(
                    {"assets": [{"name": f"gh_fixture{suffix}", "browser_download_url": asset_url}]}
                ).encode()
            )
        assert url == (asset_url if command == "gh" else config._zellij_release_url())
        return io.BytesIO(archive.getvalue())

    monkeypatch.setattr(urllib.request, "urlopen", release_response)


def _save_existing_settings(setup_cli, **updates):
    settings = {
        "github_username": "octocat",
        "open": "term-view",
        "open_needs_pane": False,
        "analysis_viewer": {"command": "workdash view", "format": "markdown", "needs_pane": True},
        "repositories": ["owner/repo"],
        "excluded_repositories": [],
        "todo_repository": "octocat/todos",
        "workdir": "~/projects",
        "agents": {"claude": {"analyze": "claude -p", "launch": "claude"}},
        **updates,
    }
    setup_cli["path"].write_text(
        json.dumps({key: value for key, value in settings.items() if value is not None})
    )


@when("the user runs `workdash` to open the dashboard again")
def relaunch_dashboard(setup_cli, tmp_path, request):
    from test_zellij_integration import (
        _cleanup_sessions,
        _free_dashboard_address,
        _private_socket_dir,
    )

    if setup_cli["bin"].joinpath("zellij").exists():
        if not setup_cli["real_zellij"]:
            pytest.skip("Zellij is not installed on the test host")
        zellij = setup_cli["bin"] / "zellij"
        zellij.unlink()
        shutil.copy2(setup_cli["real_zellij"], zellij)
    else:
        zellij = setup_cli["path"].parent / "bin" / "zellij"
    dashboard_script = tmp_path / "dashboard.py"
    dashboard_script.write_text(
        "import sys\n"
        "from datetime import UTC, datetime\n"
        "from workdash.github_client import GitHubClient\n"
        "from workdash.workdash import main\n"
        "del sys.argv[0]\n"
        "now = datetime.now(UTC).isoformat()\n"
        "issue = dict(id=1, is_pull_request=False, "
        "repo='owner/repo', number=1, title='Fix the issue', created_at=now, updated_at=now, "
        "url='https://github.com/owner/repo/issues/1')\n"
        "for method in ('list_open_authored_prs', 'list_open_review_requested_prs', "
        "'list_open_reviewed_prs', 'list_open_assigned_issues', 'list_open_todo_issues'):\n"
        "    setattr(GitHubClient, method, lambda *args, **kwargs: [])\n"
        "def tracked(self, repositories, *args, **kwargs):\n"
        "    return [issue] if repositories == 'repo:owner/repo' else []\n"
        "GitHubClient.list_recent_tracked_items = tracked\n"
        "raise SystemExit(main())\n"
    )
    # Workdash uses argv[0] to relaunch itself inside Zellij.
    workdash_entrypoint = setup_cli["bin"] / "workdash"
    workdash_entrypoint.write_text(
        f"#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(dashboard_script))} "
        '"$0" "$@"\n'
    )
    workdash_entrypoint.chmod(0o755)
    sockets = _private_socket_dir()
    request.addfinalizer(sockets.cleanup)
    env = {
        **os.environ,
        "PATH": str(setup_cli["bin"]),
        "PYTHONPATH": str(Path(workdash.__file__).resolve().parents[1]),
        "TERM": "xterm-256color",
        "WORKDASH_HOST": _free_dashboard_address(),
        "ZELLIJ_SOCKET_DIR": sockets.name,
        **{
            f"XDG_{name.upper()}_HOME": str(tmp_path / name) for name in ("config", "cache", "data")
        },
    }
    pid, fd = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
            os.execve(workdash_entrypoint, [str(workdash_entrypoint)], env)
        except OSError as exc:
            os.write(2, f"Could not launch {workdash_entrypoint}: {exc}\n".encode())
        finally:
            os._exit(127)
    request.addfinalizer(lambda: _cleanup_sessions(zellij, env, [("workdash", pid, fd)]))
    setup_cli["dashboard"] = (zellij, env, fd)


@then(parsers.parse('the dashboard shows the tracked issue "{title}"'))
def shows_tracked_issue(setup_cli, title):
    zellij, env, fd = setup_cli["dashboard"]
    os.set_blocking(fd, False)
    output = bytearray()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            output.extend(os.read(fd, 65536))
        except BlockingIOError:
            pass
        except OSError:
            pytest.fail(f"Workdash exited before opening the dashboard: {output[-2000:]!r}")
        result = subprocess.run(
            [zellij, "--session", "workdash", "action", "dump-screen"],
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and title in result.stdout:
            return
        time.sleep(0.1)
    pytest.fail(f"No {title!r} on screen: {result.stdout}\n{result.stderr}\n{output[-2000:]!r}")
