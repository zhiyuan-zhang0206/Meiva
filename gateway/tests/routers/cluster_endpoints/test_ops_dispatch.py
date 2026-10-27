# meiva:note
# `dispatch_op` maps a runner's three outcomes to HTTP: its result, 503 unreachable, 502 failed.

from __future__ import annotations

import psycopg
import pytest
from fastapi import HTTPException

from base.db import Database
from gateway.cluster.ops_dispatch import dispatch_op
from gateway.tests.auth.gateway_config import gateway_test_rpc
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    failed,
    refused,
    runner_ops,
)


# meiva:note
# A runner that answers with the payload it was sent, so the result shows what arrived.
def _echo(call: OpsCall) -> OpsReply:
    return completed({"ok": 1, "received": call.payload})


@pytest.mark.asyncio
async def test_a_completed_op_returns_its_result(
    db_conn: psycopg.Connection, database: Database
) -> None:
    with runner_ops(db_conn, "runner-a", {"status_probe": _echo}):
        result = await dispatch_op(
            database, "runner-a", "status_probe", {"x": 2}, rpc=gateway_test_rpc()
        )

    assert result == {"ok": 1, "received": {"x": 2}}


@pytest.mark.asyncio
async def test_an_unreachable_runner_is_a_503(
    db_conn: psycopg.Connection, database: Database
) -> None:
    with (
        runner_ops(db_conn, "runner-a", {"status_probe": refused(404, "gone")}),
        pytest.raises(HTTPException) as raised,
    ):
        await dispatch_op(database, "runner-a", "status_probe", {}, rpc=gateway_test_rpc())

    assert raised.value.status_code == 503
    assert "runner-a" in str(raised.value.detail) and "status_probe" in str(raised.value.detail)


@pytest.mark.asyncio
async def test_an_op_that_ran_and_failed_is_a_502(
    db_conn: psycopg.Connection, database: Database
) -> None:
    reply = failed({"error": "UpdateInFlight: an update already runs"})
    with (
        runner_ops(db_conn, "runner-a", {"status_probe": reply}),
        pytest.raises(HTTPException) as raised,
    ):
        await dispatch_op(database, "runner-a", "status_probe", {}, rpc=gateway_test_rpc())

    assert raised.value.status_code == 502
    assert "UpdateInFlight" in str(raised.value.detail)
