from datetime import UTC, datetime

from workdash.config import WorkdashConfig
from workdash.control import WorkdashSession, format_work_item_id
from workdash.models import WorkItem, WorkItemKind, WorkItemType


def _work_item(*, number: int, title: str, item_type: WorkItemType = WorkItemType.ISSUE) -> WorkItem:
    created_at = datetime(2026, 2, number, tzinfo=UTC)
    return WorkItem(
        kind=WorkItemKind.TRACKED_PR if item_type == WorkItemType.PR else WorkItemKind.TRACKED_ISSUE,
        item_type=item_type,
        repo="owner/repo",
        number=number,
        title=title,
        created_at=created_at,
        updated_at=created_at,
        url=f"https://example.com/{number}",
    )


def test_session_refresh_schedules_tui_repaint_through_call_from_thread() -> None:
    initial_item = _work_item(number=1, title="Initial")
    refreshed_item = _work_item(number=2, title="Refreshed", item_type=WorkItemType.PR)
    marker = {(WorkItemType.PR, "owner/repo", 2): "*"}
    repainted_item_ids: list[list[str]] = []

    class FakeBackend:
        def load_items(self, progress_callback=None):
            assert progress_callback is None
            return [refreshed_item], marker

    class FakeTuiApp:
        def __init__(self) -> None:
            self.scheduled_callbacks: list[str] = []
            self._inside_call_from_thread = False

        def call_from_thread(self, callback):
            self.scheduled_callbacks.append(callback.__name__)
            self._inside_call_from_thread = True
            try:
                callback()
            finally:
                self._inside_call_from_thread = False

        def _refresh_from_session(self) -> None:
            assert self._inside_call_from_thread
            repainted_item_ids.append([format_work_item_id(item) for item in session.work_items])

    session = WorkdashSession(
        config=WorkdashConfig(),
        backend=FakeBackend(),  # type: ignore[arg-type]
        work_items=[initial_item],
        suggestion_markers={},
        zellij_session=None,
    )
    tui_app = FakeTuiApp()
    session.tui_app = tui_app  # type: ignore[assignment]

    result = session.list_items(refresh=True)

    assert [item["id"] for item in result["items"]] == ["owner/repo#PR-2"]
    assert [format_work_item_id(item) for item in session.work_items] == ["owner/repo#PR-2"]
    assert session.suggestion_markers == marker
    assert tui_app.scheduled_callbacks == ["_refresh_from_session"]
    assert repainted_item_ids == [["owner/repo#PR-2"]]
