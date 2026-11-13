"""Persistent dashboard startup behavior at the Zellij boundary."""

import subprocess

import pytest

from workdash import launcher


def test_existing_session_restarts_dashboard_in_its_tab(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [
            {
                "id": 4,
                "title": "workdash tui",
                "tab_name": "dashboard",
                "tab_id": 3,
                "is_plugin": False,
                "pane_command": "/bin/bash",
            }
        ],
    )

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="workdash [Created now]\n", stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--server"])
    assert any("rename-pane" in command and "terminal_4" in command for command in calls)
    launch = next(command for command in calls if "new-pane" in command)
    assert "--server" in launch and "--tab-id" in launch and "3" in launch


@pytest.mark.parametrize(
    "pane_command",
    [
        "/usr/bin/python3 /opt/bin/workdash --direct",
        "/usr/bin/python3 -m workdash --direct",
        "/usr/bin/python3 /opt/bin/renamed-dashboard --direct --persistent-pane",
    ],
)
def test_existing_running_dashboard_attaches_without_starting_another(
    monkeypatch, capsys, pane_command
):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [{"id": 2, "title": "workdash tui", "pane_command": pane_command}],
    )
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, "custom [Created now]\n", ""),
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        launcher.os,
        "execvp",
        lambda file, command: calls.append((file, command)) or (_ for _ in ()).throw(SystemExit()),
    )
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom", "--server"])
    assert calls == [("sleep", 1), ("/zellij", ["/zellij", "attach", "custom"])]
    assert "options are ignored" in capsys.readouterr().out


def test_missing_dashboard_tab_is_created_with_new_options(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher.Zellij, "load_zellij_panes", lambda session: [])

    def run(command, **kwargs):
        calls.append(command)
        output = "custom [Created now]\n" if "list-sessions" in command else "17\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session=custom", "--server"])
    assert any("new-tab" in command and "dashboard" in command for command in calls)
    launch = next(command for command in calls if "new-pane" in command)
    assert launch[launch.index("--tab-id") + 1] == "17"
    assert "--server" in launch


def test_inside_start_renames_stale_panes_without_interrupting_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.zellij,
        "load_zellij_panes",
        lambda session: [
            {"id": 2, "title": "workdash tui", "pane_command": "vim notes"},
            {"id": 3, "title": "workdash tui", "pane_command": "/usr/bin/workdash"},
            {"id": 4, "title": "workdash tui", "is_plugin": True},
        ],
    )
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **kwargs: calls.append(command))
    launcher.zellij.name_dashboard_pane("project", "3")
    assert [command[-3:] for command in calls] == [
        ["-p", "terminal_2", "workdash old"],
        ["-p", "terminal_3", "workdash tui"],
    ]


def test_inside_start_preserves_running_dashboard(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.zellij,
        "load_zellij_panes",
        lambda session: [
            {"id": 1, "title": "workdash tui", "pane_command": "/bin/sh"},
            {"id": 2, "title": "workdash tui", "pane_command": "/usr/bin/workdash --direct"},
            {"id": 3, "title": "shell", "pane_command": "/bin/sh"},
        ],
    )
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **kwargs: calls.append(command))
    with pytest.raises(RuntimeError, match="already running"):
        launcher.zellij.name_dashboard_pane("project", "3")
    assert calls == []
