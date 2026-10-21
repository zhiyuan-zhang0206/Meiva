"""Meiva fleet plugin cases: dismissing response notice refreshes inspector snapshot."""

from __future__ import annotations

from datetime import UTC
from uuid import uuid4

import psycopg
import pytest

import meiva
from base.agents.observation.snapshot import select_one
from base.config import settings
from base.lm.catalog import ModelCatalog
from gateway.tests.extensions.fleet.test_fleet_notices_via_gateway import (
    load_activity_plugin as load_activity_plugin,
)
from gateway.tests.extensions.fleet.test_fleet_notices_via_gateway import (
    sdk_via_inprocess_gateway as sdk_via_inprocess_gateway,
)
from gateway.tests.extensions.fleet.test_fleet_notices_via_gateway import seed_agent
from meiva.tests.fixtures.pin_agent import pin_agent
from ops import lifecycle


def test_dismissing_response_notice_refreshes_inspector_snapshot(
    sdk_via_inprocess_gateway: None,
    load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
):
    """Removing the dismiss snapshot refresh leaves the inspector stale."""
    from gateway.agents import notices as notices_router

    published_awaiting: list[list[str]] = []

    def _capture_snapshot(_bus: object, published_agent_id: int) -> None:
        snapshot = select_one(
            db_conn,
            published_agent_id,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )
        assert snapshot is not None
        published_awaiting.append([notice.title for notice in snapshot.notices_awaiting_response])

    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )

    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    meiva.ui.notify("decision needed", require_response=True, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    meiva.ui.dismiss_notice()  # type: ignore[attr-defined]

    # The first snapshot announces the newly posted question. Dismissal
    # must publish a second, now-empty snapshot for the inspector.
    assert published_awaiting == [["decision needed"], []]


def test_a_legacy_fyi_row_does_not_block_and_a_second_notice_is_refused(
    sdk_via_inprocess_gateway: None,
    load_activity_plugin: None,
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    model_catalog: ModelCatalog,
):
    # meiva:link gateway/agents/docs/notice-receipts.meiva.okf.md
    # meiva:link meiva/docs/ui.meiva.okf.md
    from gateway.agents import notices as notices_router

    published_awaiting: list[list[str]] = []
    posted: list[int] = []
    resolved: list[int] = []

    def _capture_snapshot(_bus: object, published_agent_id: int) -> None:
        snapshot = select_one(
            db_conn,
            published_agent_id,
            catalog=model_catalog,
            default_model_reader=lambda: settings.lm.llm_model,
        )
        assert snapshot is not None
        published_awaiting.append([notice.title for notice in snapshot.notices_awaiting_response])

    async def _capture_posted(
        _bus: object, _agent_id: int, notice_id: int, *_args: object, **_kwargs: object
    ) -> None:
        posted.append(notice_id)

    async def _capture_resolved(_bus: object, _agent_id: int, notice_id: int, **_: object) -> None:
        resolved.append(notice_id)

    monkeypatch.setattr(
        notices_router, "publish_agent_updated_sync", _capture_snapshot, raising=False
    )
    monkeypatch.setattr(lifecycle, "publish_notice_posted", _capture_posted)
    monkeypatch.setattr(lifecycle, "publish_notice_resolved", _capture_resolved)

    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_notices "
            "(agent_id, local_id, title, priority, require_response, blocking, expire_at) "
            "VALUES (%s, 0, 'Legacy FYI', 'P2', false, false, now() + interval '1 day')",
            (agent_id,),
        )
    db_conn.commit()
    meiva.ui.notify("question", require_response=True, idempotency_key=str(uuid4()))  # pyright: ignore[reportCallIssue]
    with pytest.raises(ValueError, match="already holds an open notice"):
        meiva.ui.notify("next question", idempotency_key=str(uuid4()))  # pyright: ignore[reportCallIssue]
    meiva.ui.dismiss_notice()  # pyright: ignore[reportCallIssue]
    meiva.ui.notify("next question", idempotency_key=str(uuid4()))  # pyright: ignore[reportCallIssue]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT id, title, resolution FROM agent_notices WHERE agent_id = %s ORDER BY local_id",
            (agent_id,),
        )
        rows = cur.fetchall()
    by_title = {str(title): (int(notice_id), resolution) for notice_id, title, resolution in rows}

    assert by_title["Legacy FYI"][1] is None, "posting must not resolve a legacy FYI row"
    assert by_title["question"][1] == "withdrawn"
    assert resolved == [by_title["question"][0]]
    assert posted == [by_title["question"][0], by_title["next question"][0]]
    assert published_awaiting[0] == ["question"]
    assert published_awaiting[-1] == ["next question"]


def test_notice_return_int_and_edit_dismiss_take_no_id(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    """notify returns a Notice (int subclass); edit/dismiss act on the single
    open notice with no id argument."""
    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    notice = meiva.ui.notify(
        "hold this", content="body", priority="P2", idempotency_key=str(uuid4())
    )  # type: ignore[attr-defined]
    assert isinstance(notice, int)
    assert int(notice) == notice  # int conversion gives the id

    # edit acts on the open notice — no id passed.
    meiva.ui.edit_notice(title="updated title")  # type: ignore[attr-defined]
    # dismiss acts on the open notice — no id passed.
    meiva.ui.dismiss_notice()  # type: ignore[attr-defined]

    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT title, resolved_at, resolution FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, notice),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "updated title"
    assert row[1] is not None
    assert row[2] == "withdrawn"

    # After dismissal, new notify should show pending_count = 1 (the fresh one)
    nid2 = meiva.ui.notify("fresh fyi", idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
    assert nid2.pending_count == 1  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    assert nid2.pending_notices[0]["id"] == nid2  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]


def test_notify_with_expire_at_valid(
    sdk_via_inprocess_gateway: None, load_activity_plugin: None, db_conn: psycopg.Connection
):
    # meiva:note
    # Absolute and relative deadlines both land in the row. One notice stays open
    # per agent (user ruling 2026-10-21), so the second post needs the first one
    # dismissed.
    # meiva:link meiva_builtins/plugins/meiva_fleet/docs/notify.meiva.okf.md
    from datetime import datetime, timedelta

    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    # timedelta
    nid1 = meiva.ui.notify(
        "expires in 1h", expire_at=timedelta(hours=1), idempotency_key=str(uuid4())
    )  # pyright: ignore[reportCallIssue]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT expire_at FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, int(nid1)),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] > datetime.now(UTC)

    meiva.ui.dismiss_notice()  # pyright: ignore[reportCallIssue]

    target = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    nid2 = meiva.ui.notify("expires at ISO", expire_at=target, idempotency_key=str(uuid4()))  # pyright: ignore[reportCallIssue]
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT expire_at FROM agent_notices WHERE agent_id = %s AND local_id = %s",
            (agent_id, int(nid2)),  # pyright: ignore[reportUnknownArgumentType]
        )
        row = cur.fetchone()
    assert row is not None


def test_notify_with_expire_at_in_past_raises_value_error(
    load_activity_plugin: None, db_conn: psycopg.Connection
):
    from datetime import datetime, timedelta

    agent_id = seed_agent(db_conn)
    pin_agent(agent_id)
    past = datetime.now(UTC) - timedelta(minutes=5)
    with pytest.raises(ValueError, match="expire_at is in the past"):
        meiva.ui.notify("past notice", expire_at=past, idempotency_key=str(uuid4()))  # type: ignore[attr-defined]
