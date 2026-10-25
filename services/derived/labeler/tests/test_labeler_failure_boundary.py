from __future__ import annotations

import asyncio
from typing import Any

import httpx
import psycopg
import pytest
from psycopg_pool import ConnectionPool, PoolTimeout

from base.daemon.health import Liveness
from base.db import create_agent, pool
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from services.derived.labeler import daemon
from services.derived.labeler.tests.db_faults import table_missing
from services.derived.labeler.tests.scripted_models import ERRORS, PERMANENT, TRANSIENT
from services.derived.labeler.tests.slices import (
    failing_bus,
    generate,
    labeler_config,
    labeler_db,
    model_inputs,
)

_FAST_MODEL_WITHOUT_RECEIPT = "claude-opus-5-5-fast"


def _label(db: psycopg.Connection, tid: int) -> str | None:
    with db.cursor() as cur:
        cur.execute("SELECT label FROM agents WHERE id = %s", (tid,))
        row = cur.fetchone()
    db.commit()
    assert row is not None
    return row[0]


def _chat(db: psycopg.Connection, tid: int, content: str) -> None:
    db.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, %s, 'chat', 'user')",
        (tid, content),
    )
    db.commit()


@pytest.mark.parametrize("failure", TRANSIENT)
async def test_trusted_invocation_failure_returns_retry_result_without_writing(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    failure: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)

    result = await generate(
        tid, f"fail:{failure}", bus=event_bus, catalog=model_catalog, database_gate=database_gate
    )

    assert result is False
    assert _label(db_conn, tid) is None


@pytest.mark.parametrize("failure", PERMANENT)
async def test_unknown_and_permanent_invocation_failure_preserves_identity(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    failure: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    expected = ERRORS[failure]()

    with pytest.raises(type(expected)) as caught:
        await generate(
            tid,
            f"fail:{failure}",
            bus=event_bus,
            catalog=model_catalog,
            database_gate=database_gate,
        )

    assert str(caught.value) == str(expected)
    assert _label(db_conn, tid) is None


# meiva:note
# A transport-shaped error outside the invocation boundary is not recovered either.
@pytest.mark.parametrize("failure", ["type-error", "http-timeout"])
async def test_a_failure_while_building_the_model_is_never_a_generation_retry(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    failure: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)

    with pytest.raises(type(ERRORS[failure]())):
        await generate(
            tid,
            f"build:{failure}",
            bus=event_bus,
            catalog=model_catalog,
            database_gate=database_gate,
        )

    assert _label(db_conn, tid) is None


# meiva:note
# The model answered, but the call is billed by speed and the reply names no service.
async def test_a_usage_accounting_failure_is_never_a_generation_retry(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)

    with pytest.raises(ValueError) as caught:
        await generate(
            tid,
            _FAST_MODEL_WITHOUT_RECEIPT,
            bus=event_bus,
            catalog=model_catalog,
            database_gate=database_gate,
        )

    assert "InferenceSpeed" in str(caught.value)
    assert _label(db_conn, tid) is None


async def test_a_schema_drift_in_the_label_write_is_never_a_generation_retry(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)

    with table_missing(db_conn, "agents"), pytest.raises(psycopg.errors.UndefinedTable):
        await generate(
            tid,
            "say:Fix label generation",
            bus=event_bus,
            catalog=model_catalog,
            database_gate=database_gate,
        )

    assert _label(db_conn, tid) is None


async def test_notification_failure_cannot_uncommit_or_reselect_a_written_label(
    db_conn: psycopg.Connection,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    _chat(db_conn, tid, "Fix labeler")

    with pytest.raises(ValueError, match="Redis URL"):
        await generate(
            tid,
            "say:Fix label generation",
            bus=failing_bus(),
            catalog=model_catalog,
            database_gate=database_gate,
        )

    assert _label(db_conn, tid) == "Fix label generation"
    with db_conn.cursor() as cur:
        assert tid not in dict(daemon.select_unlabeled(cur, []))


async def test_invocation_cancellation_propagates(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    tid = create_agent(db_conn)
    generation = asyncio.create_task(
        generate(tid, "hang:", bus=event_bus, catalog=model_catalog, database_gate=database_gate)
    )
    await asyncio.sleep(0.3)
    assert not generation.done(), "the scripted model should still be answering"

    generation.cancel()

    with pytest.raises(asyncio.CancelledError):
        await generation
    assert _label(db_conn, tid) is None


async def _run_loop(
    p: ConnectionPool[Any],
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    config_model: str,
    *,
    database_gate: ProcessDbGate,
) -> None:
    await asyncio.wait_for(
        daemon.dispatch_loop(
            p,
            labeler_db(database_gate=database_gate),
            event_bus,
            Liveness(120.0),
            labeler_config(config_model),
            **model_inputs(model_catalog),
        ),
        timeout=20.0,
    )


# meiva:note
# The poll lists the newest agent first: its failing request stops the batch, so the older
# agent behind it is never attempted and no inbound row is touched.
async def test_a_failing_generation_stops_the_batch_without_rewriting_inbounds(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    older = create_agent(db_conn)
    newer = create_agent(db_conn)
    _chat(db_conn, older, "Fix labeler")
    _chat(db_conn, newer, "FAIL this one")
    before = db_conn.execute(
        "SELECT agent_id, to_jsonb(im) FROM inbound_messages im ORDER BY id"
    ).fetchall()
    db_conn.commit()

    p = pool(gate=database_gate)
    try:
        with pytest.raises(TypeError):
            await _run_loop(
                p, event_bus, model_catalog, "failif:FAIL:type-error", database_gate=database_gate
            )
    finally:
        p.close()

    assert _label(db_conn, older) is None
    assert _label(db_conn, newer) is None
    after = db_conn.execute(
        "SELECT agent_id, to_jsonb(im) FROM inbound_messages im ORDER BY id"
    ).fetchall()
    assert after == before


async def test_a_schema_drift_in_the_poll_reaches_the_service_owner_without_retry(
    db_conn: psycopg.Connection,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    p = pool(gate=database_gate)
    try:
        with table_missing(db_conn, "agents"), pytest.raises(psycopg.errors.UndefinedTable):
            await _run_loop(
                p, event_bus, model_catalog, "say:Fix label generation", database_gate=database_gate
            )
    finally:
        p.close()


async def test_an_unavailable_pool_reaches_the_service_owner_without_retry(
    db_url: str,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    *,
    database_gate: ProcessDbGate,
) -> None:
    with (
        ConnectionPool(db_url, min_size=1, max_size=1, timeout=0.2, open=True) as exhausted,
        exhausted.connection(),
        pytest.raises(PoolTimeout),
    ):
        await _run_loop(
            exhausted,
            event_bus,
            model_catalog,
            "say:Fix label generation",
            database_gate=database_gate,
        )


def test_the_http_error_classes_the_scenarios_raise_are_the_real_ones() -> None:
    assert isinstance(ERRORS["http-timeout"](), httpx.ReadTimeout)
