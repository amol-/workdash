"""Agent and GitHub substitutes for the targeted-todo analysis scenario."""

import json
import subprocess
import sys
from pathlib import Path

from workdash.backend import WorkdashBackend
from workdash.config import AgentConfig
from workdash.control import format_work_item_id
from workdash.models import WorkItem

from .common import _configure_agents, install_pane


def run_source_analysis(scenario_state, monkeypatch, tmp_path):
    from .worktrees import _install_fake_git

    session = scenario_state["api_session"]
    _install_fake_git(scenario_state, monkeypatch, workdir=Path(session.config.workdir))
    _install_analysis_agent(scenario_state, monkeypatch, tmp_path)
    session.backend = WorkdashBackend(config=session.config, cache_root=tmp_path / "analysis-cache")
    session.analyze(target=format_work_item_id(scenario_state["todo_item"]), prefer_cache=False)
    _record_agent_run(scenario_state)


def _install_analysis_agent(scenario_state, monkeypatch, tmp_path):
    """Fake GitHub and Zellij; the analysis agent is a real script the pane runs."""
    record = tmp_path / "agent-record.json"
    agent = tmp_path / "analysis-agent"
    sources = sorted(scenario_state.get("target_source", {}))
    agent.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "root = pathlib.Path.cwd()\n"
        "record = {'cwd': str(root), 'prompt': sys.argv[-1],\n"
        f"          'sources': {{path: (root / path).read_text() for path in {sources!r}}}}}\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(record))\n"
        "print('# Analysis')\n",
        encoding="utf-8",
    )
    agent.chmod(0o755)
    scenario_state["agent_record"] = record
    install_pane(scenario_state, monkeypatch)
    _install_github_discussion(scenario_state, monkeypatch)
    _configure_agents(
        scenario_state,
        {
            "codex": AgentConfig(analyze=str(agent), launch="codex"),
            "claude": AgentConfig(analyze=str(agent), launch="claude"),
            "pi": AgentConfig(launch="pi"),
        },
    )


def _install_github_discussion(scenario_state, monkeypatch, *, issue: WorkItem | None = None):
    """Fake the GitHub discussion the analysis prompt is built from."""

    previous_run = subprocess.run
    expected_issue = [str(issue.number), "--repo", issue.repo] if issue is not None else None

    def fake_run(command, **kwargs):
        if command[:3] == ["gh", "issue", "view"]:
            if expected_issue is not None:
                assert command[3:6] == expected_issue, command
            scenario_state["discussion_command"] = command
            discussion = scenario_state.get(
                "discussion", {"title": "Issue title", "body": "Issue discussion"}
            )
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(discussion), stderr="")
        return previous_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _record_agent_run(scenario_state):
    """Expose what the real analysis agent saw to the scenario steps."""
    record = json.loads(Path(scenario_state["agent_record"]).read_text(encoding="utf-8"))
    scenario_state["agent_cwd"] = Path(record["cwd"])
    scenario_state["agent_prompt"] = record["prompt"]
    scenario_state["agent_sources"] = record["sources"]
