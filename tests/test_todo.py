import pytest

import workdash.todo as todo_module
from gh_todo_fake import (
    ISSUE_REPOSITORY_MISSING_STDERR,
    LABEL_EXISTS_STDERR,
    LABEL_REPOSITORY_MISSING_STDERR,
    fake_gh_todo_run,
    gh_todo_failure,
)
from workdash.models import WorkItemKind, WorkItemType
from workdash.todo import TODO_LABEL, create_todo, is_repository_name, todo_target_from_body


def test_create_todo_files_a_labeled_self_assigned_issue_carrying_its_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []
    monkeypatch.setattr(todo_module.subprocess, "run", fake_gh_todo_run(recorded))

    item = create_todo(
        todo_repository="testuser/todos", text="Fix the flaky test", target="owner/repo"
    )

    # A todo lands on the dashboard as an assigned issue of the todo repository.
    assert item.kind == WorkItemKind.ASSIGNED_ISSUE
    assert item.item_type == WorkItemType.ISSUE
    assert item.repo == "testuser/todos"
    assert item.number == 110
    assert item.title == "Fix the flaky test"
    assert item.url == "https://github.com/testuser/todos/issues/110"
    assert item.todo_target == "owner/repo"
    label_command, create_command = recorded
    assert label_command[:6] == ["gh", "label", "create", TODO_LABEL, "--repo", "testuser/todos"]
    assert create_command[:5] == ["gh", "issue", "create", "--repo", "testuser/todos"]
    assert create_command[create_command.index("--title") + 1] == "Fix the flaky test"
    assert create_command[create_command.index("--assignee") + 1] == "@me"
    assert create_command[create_command.index("--label") + 1] == TODO_LABEL
    body = create_command[create_command.index("--body") + 1]
    assert todo_target_from_body(body) == "owner/repo"


def test_create_todo_without_target_records_no_target_in_its_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[list[str]] = []
    monkeypatch.setattr(todo_module.subprocess, "run", fake_gh_todo_run(recorded))

    assert create_todo(todo_repository="testuser/todos", text="Buy milk").todo_target is None
    body = recorded[1][recorded[1].index("--body") + 1]
    assert todo_target_from_body(body) is None
    assert "workdash_todo_version" in body


def test_create_todo_tolerates_an_existing_todo_label(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[list[str]] = []
    monkeypatch.setattr(
        todo_module.subprocess,
        "run",
        fake_gh_todo_run(recorded, label_error=gh_todo_failure(LABEL_EXISTS_STDERR)),
    )

    assert create_todo(todo_repository="testuser/todos", text="Buy milk").number == 110
    assert recorded[1][:3] == ["gh", "issue", "create"]


@pytest.mark.parametrize(
    ("failing_command", "stderr", "expected_message"),
    [
        (
            "label_error",
            LABEL_REPOSITORY_MISSING_STDERR,
            f"Failed to create the {TODO_LABEL} label in testuser/todos: "
            f"{LABEL_REPOSITORY_MISSING_STDERR}. Create the todo repository "
            "testuser/todos on GitHub if it does not exist yet.",
        ),
        (
            "create_error",
            ISSUE_REPOSITORY_MISSING_STDERR,
            "Failed to create the todo issue in testuser/todos: GraphQL: Could not resolve "
            "to a Repository with the name 'testuser/todos'. Create the todo repository "
            "testuser/todos on GitHub if it does not exist yet.",
        ),
    ],
)
def test_create_todo_reports_a_missing_todo_repository_and_asks_for_it(
    failing_command: str, stderr: str, expected_message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        todo_module.subprocess,
        "run",
        fake_gh_todo_run([], **{failing_command: gh_todo_failure(stderr)}),
    )

    with pytest.raises(RuntimeError) as error:
        create_todo(todo_repository="testuser/todos", text="Buy milk")

    assert str(error.value) == expected_message


@pytest.mark.parametrize(
    "stderr", ["HTTP 401: Bad credentials", "HTTP 403: Resource not accessible by integration"]
)
def test_create_todo_passes_an_unrelated_github_failure_through(
    stderr: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a missing repository earns the "create the todo repository" advice."""

    monkeypatch.setattr(
        todo_module.subprocess,
        "run",
        fake_gh_todo_run([], create_error=gh_todo_failure(stderr)),
    )

    with pytest.raises(RuntimeError) as error:
        create_todo(todo_repository="testuser/todos", text="Buy milk")

    assert stderr in str(error.value)
    assert "Create the todo repository" not in str(error.value)


def test_create_todo_reports_when_gh_output_has_no_issue_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        todo_module.subprocess,
        "run",
        fake_gh_todo_run([], create_stdout="something went sideways\n"),
    )

    with pytest.raises(RuntimeError, match="Failed to read the new todo issue URL"):
        create_todo(todo_repository="testuser/todos", text="Buy milk")


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "plain text with no metadata",
        "```json\n{not json}\n```",
        "```json\n{}\n```",
        '```json\n{"target": "not-a-repo"}\n```',
        '```json\n{"target": 7}\n```',
        '```json\n[{"target": "owner/repo"}]\n```',
    ],
)
def test_todo_target_from_body_treats_unusable_metadata_as_no_target(body: object) -> None:
    assert todo_target_from_body(body) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("owner/repo", True),
        ("owner/repo.py", True),
        ("owner", False),
        ("owner/repo/extra", False),
        ("owner /repo", False),
        ("", False),
    ],
)
def test_is_repository_name_accepts_only_owner_slash_repo(value: str, expected: bool) -> None:
    assert is_repository_name(value) is expected
