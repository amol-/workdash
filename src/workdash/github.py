"""GitHub CLI command helpers for Workdash."""

import json
import subprocess
from pathlib import Path
from typing import Any

from .models import WorkItem, WorkItemType

_LAUNCH_ISSUE_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt"
)
_LAUNCH_PR_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
    "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName"
)
_ANALYSIS_ISSUE_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,comments"
)
_ANALYSIS_PR_CONTEXT_JSON_FIELDS = (
    "number,title,body,author,assignees,labels,url,state,createdAt,updatedAt,"
    "isDraft,reviewDecision,additions,deletions,changedFiles,headRefName,baseRefName,"
    "comments,reviews,latestReviews"
)
_HEAD_METADATA_JSON_FIELDS = "headRefName,headRepository,headRepositoryOwner"
_BASE_METADATA_JSON_FIELDS = "baseRefName,headRepository,headRepositoryOwner"


class GithubHelper:
    """Run GitHub CLI commands and translate command/JSON failures."""

    def fetch_item_context(
        self,
        item: WorkItem,
        *,
        include_discussion: bool = False,
        context_label: str = "launch context",
    ) -> dict[str, Any]:
        if item.item_type == WorkItemType.ISSUE:
            fields = (
                _ANALYSIS_ISSUE_CONTEXT_JSON_FIELDS
                if include_discussion
                else _LAUNCH_ISSUE_CONTEXT_JSON_FIELDS
            )
            command = [
                "gh",
                "issue",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                fields,
            ]
        else:
            fields = (
                _ANALYSIS_PR_CONTEXT_JSON_FIELDS
                if include_discussion
                else _LAUNCH_PR_CONTEXT_JSON_FIELDS
            )
            command = [
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                fields,
            ]
        return self._run_json_command(item=item, command=command, context_label=context_label)

    def fetch_diff(self, item: WorkItem) -> str:
        command = ["gh", "pr", "diff", str(item.number), "--repo", item.repo]
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise RuntimeError(
                "Failed to gather GitHub diff context with gh: "
                "gh CLI is not installed or not on PATH."
            ) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"Failed to gather gh diff context for {item.item_type.value} "
                f"{item.repo}#{item.number}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error
        return completed.stdout

    def fetch_head_metadata(self, item: WorkItem) -> tuple[str, str]:
        payload = self._run_json_command(
            item=item,
            command=[
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _HEAD_METADATA_JSON_FIELDS,
            ],
            context_label="PR head info",
        )
        head_ref = payload.get("headRefName")
        if not isinstance(head_ref, str):
            raise RuntimeError(
                f"Missing or invalid headRefName in gh output for {item.repo}#{item.number}"
            )
        return head_ref, self._head_repo_from_payload(payload, item.repo)

    def fetch_base_metadata(self, item: WorkItem) -> tuple[str, str]:
        payload = self._run_json_command(
            item=item,
            command=[
                "gh",
                "pr",
                "view",
                str(item.number),
                "--repo",
                item.repo,
                "--json",
                _BASE_METADATA_JSON_FIELDS,
            ],
            context_label="branchdiff base branch",
        )
        base_ref_name = payload.get("baseRefName")
        if not isinstance(base_ref_name, str) or not base_ref_name.strip():
            raise RuntimeError(
                f"Invalid branchdiff base branch payload for {item.item_type.value} "
                f"{item.repo}#{item.number}: expected a non-empty baseRefName."
            )
        return base_ref_name.strip(), self._head_repo_from_payload(payload, item.repo)

    def clone_repository(self, repo: str, target: Path) -> None:
        try:
            subprocess.run(
                ["gh", "repo", "clone", repo, str(target)],
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Failed to clone repository: gh CLI is not installed or not on PATH."
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            raise RuntimeError(
                f"Failed to clone {repo}: {stderr or f'exit code {exc.returncode}'}"
            ) from exc

    def _run_json_command(
        self,
        *,
        item: WorkItem,
        command: list[str],
        context_label: str,
    ) -> dict[str, Any]:
        try:
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as error:
            raise RuntimeError(
                f"Failed to gather {context_label} with gh: gh CLI is not installed or not on PATH."
            ) from error
        except subprocess.CalledProcessError as error:
            stderr = (error.stderr or "").strip()
            raise RuntimeError(
                f"Failed to gather {context_label} for {item.item_type.value} "
                f"{item.repo}#{item.number}: "
                f"{stderr or f'process exited with code {error.returncode}'}"
            ) from error
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                f"Failed to parse {context_label} JSON for {item.item_type.value} "
                f"{item.repo}#{item.number}: {error.msg}"
            ) from error
        if not isinstance(payload, dict):
            raise RuntimeError(
                f"Invalid {context_label} payload for {item.item_type.value} "
                f"{item.repo}#{item.number}: expected a JSON object."
            )
        return payload

    @staticmethod
    def _head_repo_from_payload(payload: dict[str, Any], fallback_repo: str) -> str:
        head_repo_owner = payload.get("headRepositoryOwner", {})
        head_repo_info = payload.get("headRepository", {})
        owner_login = head_repo_owner.get("login", "") if isinstance(head_repo_owner, dict) else ""
        repo_name = head_repo_info.get("name", "") if isinstance(head_repo_info, dict) else ""
        return f"{owner_login}/{repo_name}" if owner_login and repo_name else fallback_repo
