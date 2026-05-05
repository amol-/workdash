"""Codex issue and pull request analysis generation."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from .models import WorkItem, WorkItemType

logger = logging.getLogger(__name__)

_DEFAULT_AGENT_COMMAND_TOKENS = ("codex", "exec")
_ISSUE_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,comments"
)
_PR_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
    "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName,"
    "comments,reviews,latestReviews"
)
_REVIEW_DIFF_MAX_CHARS = 120_000
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

    def _run_gh_context_command(
        self,
        *,
        item: WorkItem,
        command: list[str],
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "Failed to gather GitHub context with gh: gh CLI is not installed or not on PATH."
            ) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"Failed to gather gh context for {item.item_type.value} "
                f"{item.repo}#{item.number}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse gh context JSON for {item.item_type.value} "
                f"{item.repo}#{item.number}: {error.msg}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid gh context payload for {item.item_type.value} "
                f"{item.repo}#{item.number}: expected a JSON object."
            )
        return payload

    def _collect_github_context(self, item: WorkItem) -> dict[str, Any]:
        if item.item_type == WorkItemType.ISSUE:
            command = [
                "gh",
                "issue",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _ISSUE_CONTEXT_JSON_FIELDS,
            ]
            return self._run_gh_context_command(item=item, command=command)

        github_context = self._run_gh_context_command(
            item=item,
            command=[
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _PR_CONTEXT_JSON_FIELDS,
            ],
        )

        diff_command = [
            "gh",
            "pr",
            "diff",
            str(item.number),
            "--repo",
            item.repo,
        ]
        try:
            completed = subprocess.run(
                diff_command,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as error:
            raise RuntimeError(
                "Failed to gather GitHub diff context with gh: gh CLI is not installed or not on PATH."
            ) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"Failed to gather gh diff context for {item.item_type.value} "
                f"{item.repo}#{item.number}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error

        github_context["diff"] = (
            completed.stdout
            if len(completed.stdout) <= _REVIEW_DIFF_MAX_CHARS
            else (
                f"{completed.stdout[:_REVIEW_DIFF_MAX_CHARS]}\n"
                "[truncated: diff too large for prompt context]"
            )
        )
        return github_context

    def _build_agent_prompt(self, item: WorkItem, github_context: dict[str, Any]) -> str:
        template_name = (
            "analyze_issue.txt" if item.item_type == WorkItemType.ISSUE else "analyze_pr.txt"
        )
        template = _load_prompt_template(template_name)
        return template.format(
            item_type=item.item_type.value,
            kind=item.kind.value,
            repo=item.repo,
            number=item.number,
            title=item.title,
            url=item.url,
            github_context_json=json.dumps(
                github_context, ensure_ascii=True, indent=2, sort_keys=True
            ),
        )

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

        :param list[str] | None command_tokens: Override the default command
            tokens used to invoke the analysis backend.
        """

        github_context = self._collect_github_context(item)
        prompt = self._build_agent_prompt(item, github_context)
        tokens = command_tokens if command_tokens is not None else self.agent_command_tokens
        output = self._run_analysis_command(item=item, prompt=prompt, command_tokens=tokens)
        content = output.strip()
        if not content:
            return None
        return content
