"""Execution of configured Workdash agents."""

import os
import shlex
import subprocess

from . import launcher
from .models import WorkItem


class AgentExecution:
    """Execute agents; all agent execution must go through this object."""

    def launch(
        self,
        repo_path: str,
        prompt: str,
        agent_command_tokens: list[str],
        *,
        zellij_session: str | None = None,
        group: str | None = None,
    ) -> launcher.ZellijPaneLaunch:
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise ValueError("Repository path must be a non-empty string.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string.")

        if not agent_command_tokens or any(
            not isinstance(token, str) or not token.strip() for token in agent_command_tokens
        ):
            raise ValueError("Agent command tokens must be a non-empty list of non-empty strings.")
        user_shell = os.environ.get("SHELL", "/bin/sh")
        agent_command = [user_shell, "-ic", shlex.join([*agent_command_tokens, prompt])]
        return launcher._launch_zellij_command(
            repo_path,
            agent_command,
            context="Failed to launch coding agent in zellij",
            work_action="code",
            zellij_session=zellij_session,
            group=group,
        )

    def launch_vscode(self, repo_path: str, prompt: str) -> None:
        """Open VSCode on the repository and start a Copilot chat session."""
        if not isinstance(repo_path, str) or not repo_path.strip():
            raise ValueError("Repository path must be a non-empty string.")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt must be a non-empty string.")
        launcher._run_launch_command(
            ["code", "--new-window", repo_path],
            context="Failed to open VSCode",
        )
        launcher._run_launch_command(
            ["code", "chat", prompt, "--reuse-window"],
            context="Failed to start Copilot chat",
        )

    def analyze(
        self, *, item: WorkItem, prompt: str, command_tokens: list[str], cwd: str | None
    ) -> str:
        user_shell = os.environ.get("SHELL", "/bin/sh")
        shell_command = shlex.join([*command_tokens, prompt])
        try:
            completed = subprocess.run(
                [user_shell, "-ic", shell_command],
                check=True,
                capture_output=True,
                text=True,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Failed to generate analysis: shell '{user_shell}' not found."
            ) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"Failed to generate analysis for {item.item_type.value} "
                f"{item.repo}#{item.number}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error
        return completed.stdout


executor = AgentExecution()
