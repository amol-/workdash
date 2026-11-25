"""Persistent dashboard startup behavior at the Zellij boundary."""

import os
import subprocess
import sys

import pytest

from workdash import launcher
from workdash.control import WorkdashControlError
from workdash.dashboard_record import DashboardRecord, DashboardRecords
from zellij_pane_launch import mark_pane_started, pane_launch


def test_exited_session_recovery_failure_does_not_create_a_new_session(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")

    def run(command, **kwargs):
        calls.append(command)
        if "attach" in command:
            raise subprocess.CalledProcessError(1, command, stderr="recovery failed")
        return subprocess.CompletedProcess(
            command, 0, "custom [Created now] (EXITED - attach to resurrect)\n", ""
        )

    monkeypatch.setattr(launcher.subprocess, "run", run)
    with pytest.raises(RuntimeError, match="recovery failed"):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom"])
    assert calls == [
        ["/zellij", "list-sessions", "--no-formatting"],
        [
            "/zellij",
            "--layout-string",
            "session_serialization true\ndisable_session_metadata false\nlayout {}",
            "attach",
            "-b",
            "custom",
        ],
    ]


@pytest.mark.parametrize("exited", [False, True], ids=["stopped-dashboard", "exited-session"])
def test_existing_session_restarts_dashboard_in_its_tab(monkeypatch, exited):
    calls = []
    attaches = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(sys, "argv", ["workdash"])
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:9432")

    def load_panes(session):
        assert session == "custom"
        if exited:
            assert calls[-1][-3:] == ["attach", "-b", session]
            assert "-f" not in calls[-1]
        return [
            {"id": 4, "title": "workdash tui", "is_plugin": True},
            {
                "id": 4,
                "title": "workdash tui",
                "tab_name": "dashboard",
                "tab_id": 3,
                "is_plugin": False,
                "pane_command": "/bin/bash",
            },
            {"id": 5, "title": "work", "tab_name": "project", "tab_id": 4},
        ]

    def run(command, **kwargs):
        calls.append(command)
        mark_pane_started(command, "24")
        listing = "custom [Created now]" + (" (EXITED - attach to resurrect)" if exited else "")
        return subprocess.CompletedProcess(command, 0, stdout=f"{listing}\n", stderr="")

    def attach(file, command):
        attaches.append((file, command))
        raise SystemExit(0)

    monkeypatch.setattr(launcher.Zellij, "load_zellij_panes", load_panes)
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", attach)
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom", "--refresh"])
    assert not any(
        {"rename-pane", "close-pane", "terminal_5"}.intersection(command) for command in calls
    )
    launch = next(command for command in calls if "new-pane" in command)
    assert launch[1:3] == ["--session", "custom"]
    assert launch[launch.index("--tab-id") + 1] == "3"
    assert launch[launch.index("--name") + 1] == "workdash tui"
    _, launched = pane_launch(launch)
    assert launched == [
        "env",
        "WORKDASH_HOST=127.0.0.1:9432",
        "workdash",
        "--persistent-pane",
        "--session",
        "custom",
        "--refresh",
    ]
    assert attaches == [("/zellij", ["/zellij", "attach", "custom"])]


def test_recorded_dashboard_for_the_session_attaches_without_starting_another(monkeypatch, capsys):
    calls = []
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:8765")
    DashboardRecords().save(
        DashboardRecord(session="custom", pid=os.getpid(), address="127.0.0.1:9999")
    )
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [{"id": 2, "title": "workdash tui", "pane_command": "/bin/sh"}],
    )
    monkeypatch.setattr(
        "workdash.control.probe_dashboard_session",
        lambda address: {"127.0.0.1:9999": "custom"}[address],
    )

    def run(command, **kwargs):
        assert command == ["/zellij", "list-sessions", "--no-formatting"]
        return subprocess.CompletedProcess(command, 0, "custom [Created now]\n", "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    monkeypatch.setattr(
        launcher.os,
        "execvp",
        lambda file, command: calls.append((file, command)) or (_ for _ in ()).throw(SystemExit()),
    )
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom", "--refresh"])
    assert calls == [("sleep", 1), ("/zellij", ["/zellij", "attach", "custom"])]
    assert "options are ignored" in capsys.readouterr().out
    assert DashboardRecords().load("custom") is not None


def test_dashboard_serving_the_session_at_the_start_address_is_attached(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher.Zellij, "load_zellij_panes", lambda session: [])
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
    monkeypatch.setattr("workdash.control.probe_dashboard_session", lambda address: "custom")
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(
            ["--session", "custom", "--refresh"],
            preflight=lambda: pytest.fail("attachment must not require GitHub authentication"),
        )
    assert calls == [("sleep", 1), ("/zellij", ["/zellij", "attach", "custom"])]
    assert "options are ignored" in capsys.readouterr().out


@pytest.mark.parametrize(
    "pid_kind, recorded_session",
    [("dead", "custom"), ("alive", "previous")],
)
def test_a_record_that_no_longer_matches_reality_is_discarded(
    monkeypatch, pid_kind, recorded_session
):
    calls = []
    pid = os.getpid() if pid_kind == "alive" else _finished_process_pid()
    DashboardRecords().save(DashboardRecord(session="custom", pid=pid, address="127.0.0.1:9999"))
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher.Zellij, "load_zellij_panes", lambda session: [])

    def probe(address):
        return recorded_session if address == "127.0.0.1:9999" else None

    monkeypatch.setattr("workdash.control.probe_dashboard_session", probe)

    def run(command, **kwargs):
        calls.append(command)
        mark_pane_started(command, "17")
        output = "custom [Created now]\n" if "list-sessions" in command else "17\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom", "--refresh"])
    assert any("new-pane" in command and "--refresh" in command for command in calls)
    assert DashboardRecords().load("custom") is None


@pytest.mark.parametrize("session_exists", [True, False])
def test_dashboard_serving_another_session_is_reported(monkeypatch, session_exists):
    calls = []
    sessions = "custom [Created now]\n" if session_exists else ""
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher.Zellij, "load_zellij_panes", lambda session: [])
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda command, **kw: subprocess.CompletedProcess(command, 0, sessions, ""),
    )
    monkeypatch.setattr("workdash.control.probe_dashboard_session", lambda address: "other")
    monkeypatch.setenv("WORKDASH_HOST", "127.0.0.1:9999")
    with pytest.raises(RuntimeError) as error:
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom"])
    assert "127.0.0.1:9999" in str(error.value)
    assert "'other'" in str(error.value)
    assert not any("new-pane" in command for command in calls)


def test_an_address_that_does_not_answer_starts_nothing_and_keeps_the_record(monkeypatch):
    calls = []
    DashboardRecords().save(
        DashboardRecord(session="custom", pid=os.getpid(), address="127.0.0.1:9999")
    )
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher.Zellij, "load_zellij_panes", lambda session: [])
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda command, **kw: (
            calls.append(command)
            or subprocess.CompletedProcess(command, 0, "custom [Created now]\n", "")
        ),
    )

    def probe(address):
        raise WorkdashControlError(
            "server_unresponsive", f"The server at {address} did not answer."
        )

    monkeypatch.setattr("workdash.control.probe_dashboard_session", probe)
    with pytest.raises(RuntimeError, match="Cannot tell whether a Workdash dashboard is running"):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom"])

    assert not any("new-pane" in command for command in calls)
    assert DashboardRecords().load("custom") is not None


def _finished_process_pid() -> int:
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def test_a_pane_named_like_a_dashboard_does_not_count_as_a_running_one(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [
            {
                "id": 2,
                "title": "workdash tui",
                "tab_name": "dashboard",
                "tab_id": 3,
                "pane_command": "/usr/bin/python3 -m workdash --persistent-pane",
            }
        ],
    )

    def run(command, **kwargs):
        calls.append(command)
        mark_pane_started(command, "3")
        output = "custom [Created now]\n" if "list-sessions" in command else "3\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session", "custom", "--refresh"])

    launch = next(command for command in calls if "new-pane" in command)
    assert "--refresh" in launch


def test_missing_dashboard_tab_is_created_with_new_options(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [{"id": 5, "tab_name": "keep", "tab_id": 3}],
    )

    def run(command, **kwargs):
        calls.append(command)
        mark_pane_started(command, "17")
        output = "custom [Created now]\n" if "list-sessions" in command else "17\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash(["--session=custom", "--refresh"])
    assert any("new-tab" in command and "dashboard" in command for command in calls)
    assert not any("close-tab" in command or "close-pane" in command for command in calls)
    launch = next(command for command in calls if "new-pane" in command)
    assert launch[launch.index("--tab-id") + 1] == "17"
    assert "--refresh" in launch


def test_existing_plugin_only_dashboard_tab_is_reused(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [{"id": 4, "is_plugin": True, "tab_name": "dashboard", "tab_id": 3}],
    )

    def run(command, **kwargs):
        calls.append(command)
        mark_pane_started(command, "4")
        return subprocess.CompletedProcess(command, 0, "workdash [Created now]\n", "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.os, "execvp", lambda *args: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        launcher.zellij.exec_zellij_wrapped_workdash([])
    assert not any("new-tab" in command for command in calls)
    launch = next(command for command in calls if "new-pane" in command)
    assert launch[launch.index("--tab-id") + 1] == "3"


def test_inside_start_names_only_the_dashboard_own_pane(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher.subprocess, "run", lambda command, **kwargs: calls.append(command))
    launcher.zellij.name_dashboard_pane("project", "3")
    assert [command[-3:] for command in calls] == [["-p", "terminal_3", "workdash tui"]]


def test_a_dashboard_launch_that_never_starts_reports_the_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "_resolve_zellij_binary", lambda: "/zellij")
    monkeypatch.setattr(launcher, "_ZELLIJ_PANE_MARKER_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(
        launcher.Zellij,
        "load_zellij_panes",
        lambda session: [{"id": 4, "is_plugin": True, "tab_name": "dashboard", "tab_id": 3}],
    )

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "workdash [Created now]\n", "")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(
        launcher.os,
        "execvp",
        lambda *args: pytest.fail("attached to a dashboard that never started"),
    )

    with pytest.raises(
        RuntimeError, match="Failed to start dashboard in Zellij: the pane never ran"
    ):
        launcher.zellij.exec_zellij_wrapped_workdash([])
    assert any("new-pane" in command for command in calls)
