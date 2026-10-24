# meiva:note
# A keyed pass that dies on a lost connection is attempted again, a bounded number of times.

from __future__ import annotations

import logging

import psycopg
import pytest

from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops import daemon
from services.agent_runner.agent_ops.tests.db_outage import failing_claims
from services.agent_runner.agent_ops.tests.ops_support import Ops, config_write, executions


async def test_a_lost_connection_is_retried_and_the_pass_then_succeeds(
    ops: Ops, db_conn: psycopg.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    with failing_claims(db_conn, errcode="08006", times=2) as attempts:
        with caplog.at_level(logging.WARNING, logger="services.agent_runner.agent_ops.daemon"):
            status, _result = await ops.idempotent("config_write", config_write("retried"), "key-1")

        assert attempts() == 3

    assert status is OpStatus.COMPLETED
    assert executions("retried", env_path=ops.authority.env_path) == 1
    assert [r.getMessage() for r in caplog.records if "retrying" in r.getMessage()] == [
        "dispatch idempotent pass died with OperationalError (attempt 1/3); retrying",
        "dispatch idempotent pass died with OperationalError (attempt 2/3); retrying",
    ]


async def test_a_connection_that_stays_lost_gives_up_after_the_attempt_limit(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    with failing_claims(db_conn, errcode="08006", times=100) as attempts:
        with pytest.raises(psycopg.OperationalError):
            await ops.idempotent("config_write", config_write("gives-up"), "key-2")

        assert attempts() == daemon.DISPATCH_RETRY_ATTEMPTS

    assert executions("gives-up", env_path=ops.authority.env_path) == 0


async def test_a_failure_that_is_not_a_lost_connection_is_not_retried(
    ops: Ops, db_conn: psycopg.Connection
) -> None:
    with failing_claims(db_conn, errcode="P0001", times=100) as attempts:
        with pytest.raises(psycopg.errors.RaiseException):
            await ops.idempotent("config_write", config_write("not-retried"), "key-3")

        assert attempts() == 1

    assert executions("not-retried", env_path=ops.authority.env_path) == 0
