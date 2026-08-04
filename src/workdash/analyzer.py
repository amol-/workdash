"""Codex issue and pull request analysis generation."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .github import GithubHelper
from .models import WorkItem, WorkItemType, display_repo

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_COMMAND_TOKENS = ("codex", "exec")
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_prompt_template(name: str) -> str:
    return (_PROMPTS_DIR / name).read_text(encoding="utf-8")


class Analyzer:
    """Generate analysis by collecting GitHub context and invoking a coding agent."""

    def __init__(self, agent_command_tokens: list[str] | None = None) -> None:
        self.agent_command_tokens = (
            agent_command_tokens
            if agent_command_tokens is not None
            else list(_DEFAULT_AGENT_COMMAND_TOKENS)
        )

    def _collect_github_context(self, item: WorkItem) -> dict[str, Any]:
        return GithubHelper().fetch_analysis_context(item)

    def _fetch_pr_diff(self, item: WorkItem) -> str:
        """Fetch the unified diff for a pull request via ``gh pr diff``."""

        return GithubHelper().fetch_diff(item)

    def _build_agent_prompt(
        self,
        item: WorkItem,
        github_context: dict[str, Any],
        *,
        diff_path: str | None = None,
    ) -> str:
        template_name = (
            "analyze_issue.txt" if item.item_type == WorkItemType.ISSUE else "analyze_pr.txt"
        )
        template = _load_prompt_template(template_name)
        format_kwargs: dict[str, Any] = {
            "item_type": item.item_type.value,
            "kind": item.kind.value,
            "repo": item.repo,
            # Source links must point at the code being analyzed, which for a
            # targeted todo is the target and not the todo repository.
            "code_repo": display_repo(item),
            "number": item.number,
            "title": item.title,
            "url": item.url,
            "github_context_json": json.dumps(
                github_context, ensure_ascii=True, indent=2, sort_keys=True
            ),
        }
        if item.item_type != WorkItemType.ISSUE:
            format_kwargs["diff_path"] = diff_path or ""
        return template.format(**format_kwargs)

    def _run_analysis_command(
        self, *, item: WorkItem, prompt: str, command_tokens: list[str]
    ) -> str:
        user_shell = os.environ.get("SHELL", "/bin/sh")
        shell_command = shlex.join([*command_tokens, prompt])
        try:
            completed = subprocess.run(
                [user_shell, "-ic", shell_command],
                check=True,
                capture_output=True,
                text=True,
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

    def analyze(self, item: WorkItem, command_tokens: list[str] | None = None) -> str | None:
        """Collect GitHub context, build the prompt, and run analysis.

        For pull requests, the full unified diff is fetched via ``gh pr diff``
        and written to a temp file whose path is injected into the prompt, so
        the agent can read it without its own ``gh`` access and without
        pushing the argv above the kernel's per-string limit.

        :param list[str] | None command_tokens: Override the default command
            tokens used to invoke the analysis backend.
        """

        github_context = self._collect_github_context(item)
        tokens = command_tokens if command_tokens is not None else self.agent_command_tokens

        diff_file_path: str | None = None
        if item.item_type != WorkItemType.ISSUE:
            diff_text = self._fetch_pr_diff(item)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".diff",
                prefix=f"workdash-{item.repo.replace('/', '-')}-{item.number}-",
                delete=False,
            ) as diff_file:
                diff_file.write(diff_text)
                diff_file_path = diff_file.name

        try:
            prompt = self._build_agent_prompt(item, github_context, diff_path=diff_file_path)
            output = self._run_analysis_command(item=item, prompt=prompt, command_tokens=tokens)
        finally:
            if diff_file_path is not None:
                try:
                    os.unlink(diff_file_path)
                except OSError:
                    logger.warning("Failed to remove temporary diff file %s", diff_file_path)

        content = output.strip()
        if not content:
            return None
        return content
