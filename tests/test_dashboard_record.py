"""Per-session dashboard records."""

import json

import pytest

from workdash import dashboard_record


def test_a_saved_record_answers_for_its_session_only() -> None:
    records = dashboard_record.DashboardRecords()
    records.save(dashboard_record.DashboardRecord(session="work/dash", pid=42, address="a:1"))

    assert records.load("work/dash") == dashboard_record.DashboardRecord(
        session="work/dash", pid=42, address="a:1"
    )
    assert records.load("work_dash") is None
    assert (dashboard_record.SESSIONS_DIR / "work%2Fdash.json").exists()


@pytest.mark.parametrize(
    "document",
    [
        "not json",
        json.dumps([1, 2]),
        json.dumps({"session": "project", "dashboard": {"pid": 1, "address": "a:1"}}),
        json.dumps(
            {"schema_version": 2, "session": "project", "dashboard": {"pid": 1, "address": "a:1"}}
        ),
        json.dumps(
            {"schema_version": 1, "session": "other", "dashboard": {"pid": 1, "address": "a:1"}}
        ),
        json.dumps({"schema_version": 1, "session": "project", "dashboard": "nope"}),
        json.dumps(
            {"schema_version": 1, "session": "project", "dashboard": {"pid": "1", "address": "a:1"}}
        ),
        json.dumps(
            {"schema_version": 1, "session": "project", "dashboard": {"pid": 0, "address": "a:1"}}
        ),
        json.dumps(
            {"schema_version": 1, "session": "project", "dashboard": {"pid": 1, "address": ""}}
        ),
    ],
)
def test_a_malformed_record_loads_as_no_record(document) -> None:
    dashboard_record.SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    (dashboard_record.SESSIONS_DIR / "project.json").write_text(document, encoding="utf-8")

    assert dashboard_record.DashboardRecords().load("project") is None


def test_removing_a_record_leaves_no_record_and_forgives_a_missing_one() -> None:
    records = dashboard_record.DashboardRecords()
    records.save(dashboard_record.DashboardRecord(session="project", pid=42, address="a:1"))
    records.remove("project")
    records.remove("project")

    assert records.load("project") is None


def test_session_names_that_differ_only_in_separators_keep_their_own_records() -> None:
    records = dashboard_record.DashboardRecords()
    records.save(dashboard_record.DashboardRecord(session="foo", pid=1, address="a:1"))
    records.save(dashboard_record.DashboardRecord(session="foo-", pid=2, address="a:2"))

    assert records.load("foo") == dashboard_record.DashboardRecord(
        session="foo", pid=1, address="a:1"
    )
    assert records.load("foo-") == dashboard_record.DashboardRecord(
        session="foo-", pid=2, address="a:2"
    )
