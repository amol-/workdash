"""Execution of configured Workdash agents."""

import os
import shlex
import shutil
import tempfile
from pathlib import Path

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
        return launcher.zellij.launch_pane(
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

    def run_in_pane(
        self,
        *,
        item: WorkItem,
        prompt: str,
        command_tokens: list[str],
        cwd: str,
        work_action: str,
        zellij_session: str,
        group: str | None,
    ) -> str:
        """Run a one-shot agent in a Zellij pane and return what it printed.

        The agent's stdout is teed so the user watches it in the pane while the
        caller reads the same text back.
        """
        user_shell = os.environ.get("SHELL", "/bin/sh")
        context = (
            f"Failed to generate analysis for {item.item_type.value} {item.repo}#{item.number}"
        )
        # Zellij never reports back when the pane command cannot be started, so an
        # unusable shell must fail here instead of blocking the caller forever.
        if shutil.which(user_shell) is None:
            raise RuntimeError(f"{context}: shell '{user_shell}' not found.")
        with tempfile.TemporaryDirectory(prefix="workdash-agent-") as temp_dir:
            stdout_path = Path(temp_dir) / "stdout"
            stderr_path = Path(temp_dir) / "stderr"
            status_path = Path(temp_dir) / "status"
            # The agent runs under the user's $SHELL so aliases work, but the pipeline
            # around it needs a POSIX shell: fish and csh cannot parse brace groups.
            # The braces keep the agent's status out of the pipeline's status, tee's.
            # The pane's stdin is a terminal that never sends EOF, so an agent that
            # reads stdin would hang; /dev/null makes it read EOF instead.
            agent_command = shlex.join([user_shell, "-ic", shlex.join([*command_tokens, prompt])])
            shell_command = (
                f"{{ {agent_command} < /dev/null"
                f" 2> {shlex.quote(str(stderr_path))};"
                f" echo $? > {shlex.quote(str(status_path))}; }}"
                f" | tee {shlex.quote(str(stdout_path))}"
            )
            launcher.zellij.launch_pane_and_wait(
                cwd,
                ["/bin/sh", "-c", shell_command],
                context=context,
                work_action=work_action,
                zellij_session=zellij_session,
                group=group,
            )
            # Zellij can exit zero without running the pane at all, so the files the
            # pane writes are the proof that it ran.
            if not status_path.exists() or not stdout_path.exists():
                raise RuntimeError(f"{context}: the Zellij pane never ran")
            exit_code = int(status_path.read_text(encoding="utf-8").strip())
            if exit_code != 0:
                stderr = stderr_path.read_text(encoding="utf-8").strip()
                raise RuntimeError(
                    f"{context}: {stderr or f'process exited with code {exit_code}'}"
                )
            return stdout_path.read_text(encoding="utf-8")


executor = AgentExecution()
