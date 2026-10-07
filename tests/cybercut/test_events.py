"""事件测试：event_id 单调递增、只追加（数据库触发器）——真实 PostgreSQL。"""
from __future__ import annotations

import pytest
from psycopg2 import errors as pg_errors

from cybercut.storage import events, projects

TENANT = "tenant-a"


def _project(db, tenant: str = TENANT) -> dict:
    with db.transaction() as conn:
        return projects.create(conn, tenant_id=tenant, name="p")


def test_event_ids_are_strictly_increasing(db) -> None:
    project = _project(db)
    ids = []
    for i in range(5):
        with db.transaction() as conn:
            ids.append(
                events.append(
                    conn, tenant_id=TENANT, event_type="test.event",
                    project_id=project["project_id"], payload={"i": i},
                )
            )
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert ids[0] < ids[-1]


def test_events_are_ordered_and_filtered(db) -> None:
    project = _project(db)
    other = _project(db)
    with db.transaction() as conn:
        events.append(conn, tenant_id=TENANT, event_type="a",
                      project_id=project["project_id"])
    with db.transaction() as conn:
        events.append(conn, tenant_id=TENANT, event_type="b",
                      project_id=other["project_id"])
    with db.transaction() as conn:
        events.append(conn, tenant_id=TENANT, event_type="c",
                      project_id=project["project_id"])

    with db.transaction() as conn:
        rows = events.list_for_scope(conn, TENANT, project_id=project["project_id"])
    assert [r["event_type"] for r in rows] == ["a", "c"]
    assert rows[0]["event_id"] < rows[1]["event_id"]

    with db.transaction() as conn:
        after = events.list_for_scope(
            conn, TENANT, project_id=project["project_id"],
            after_event_id=rows[0]["event_id"],
        )
    assert [r["event_type"] for r in after] == ["c"]


def test_events_are_append_only(db) -> None:
    project = _project(db)
    with db.transaction() as conn:
        event_id = events.append(conn, tenant_id=TENANT, event_type="x",
                                 project_id=project["project_id"])
    with pytest.raises(pg_errors.RestrictViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE cybercut.events SET event_type = %s WHERE event_id = %s",
                    ("hacked", event_id),
                )
    with pytest.raises(pg_errors.RestrictViolation):
        with db.transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM cybercut.events WHERE event_id = %s", (event_id,)
                )
