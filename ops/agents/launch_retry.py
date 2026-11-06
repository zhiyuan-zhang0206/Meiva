"""Transactional retry-launch intent: one operation, one immutable attempt."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import AgentNotFound
from base.api_contracts.launch_retry import RetryLaunchAccepted, RetryLaunchRequest
from base.db.rows import query_one
from base.db.transaction import write_transaction


class RetryLaunchConflictError(ValueError):
    """The operation changed or its observed attempt is no longer retryable."""


# meiva:note
# A stored retry intent, named as `agent_launch_retry_receipts` names its columns; `acceptance` is
# the stored `RetryLaunchAccepted` as the JSON object it was written as (NOT NULL).
@dataclass(frozen=True)
class _Receipt:
    machine: str
    acceptance: Mapping[str, object]


# meiva:note
# The `agents_meta` columns the retry reads under its row lock. The two JSON columns are copied
# into the receipt as they are; each is a JSON object or NULL.
@dataclass(frozen=True)
class _AgentLaunchState:
    machine: str
    last_launch_attempt_id: UUID | None
    status: str
    last_admission_at: datetime | None
    config_overlay: Mapping[str, object] | None
    birth_config: Mapping[str, object] | None


# meiva:note
# The database clock, as the one-row `SELECT clock_timestamp() AS now` answers.
@dataclass(frozen=True)
class _Clock:
    now: datetime


# meiva:note
# Replay before mutable existence; commit the receipt and pointer together.
#
# `new_attempt_id` becomes the agent's launch attempt only when this call commits the intent; a
# replay returns the stored acceptance and drops it.
def accept_retry_launch(
    pool: ConnectionPool, agent_id: int, body: RetryLaunchRequest, *, new_attempt_id: UUID
) -> tuple[str, RetryLaunchAccepted]:
    attempt_id = body.expected_prior_attempt_id
    key = f"launch-retry:{agent_id}:{attempt_id}"
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (key,))
        prior = query_one(
            conn,
            _Receipt,
            "SELECT machine, acceptance FROM agent_launch_retry_receipts "
            "WHERE agent_id=%s AND prior_attempt_id=%s",
            (agent_id, attempt_id),
        )
        if prior is not None:
            return prior.machine, RetryLaunchAccepted.model_validate(prior.acceptance)
        row = query_one(
            conn,
            _AgentLaunchState,
            "SELECT machine, last_launch_attempt_id, status, last_admission_at, "
            "config_overlay, birth_config FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        attempt = row.last_launch_attempt_id
        if (
            attempt is None
            or attempt != body.expected_prior_attempt_id
            or row.status != "idling"
            or row.last_admission_at is not None
        ):
            raise RetryLaunchConflictError("observed launch attempt is stale or already admitted")
        clock = query_one(conn, _Clock, "SELECT clock_timestamp() AS now")
        if clock is None:
            raise RuntimeError("clock_timestamp() returned no row")
        accepted = RetryLaunchAccepted(
            agent_id=agent_id,
            prior_attempt_id=attempt,
            launch_attempt_id=new_attempt_id,
            accepted_at=clock.now,
        )
        cur.execute(
            "INSERT INTO agent_launch_retry_receipts "
            "(operation_key, agent_id, prior_attempt_id, launch_attempt_id, machine, "
            "config_overlay, birth_config, acceptance) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                key,
                agent_id,
                attempt,
                new_attempt_id,
                row.machine,
                Jsonb(row.config_overlay),
                Jsonb(row.birth_config),
                Jsonb(accepted.model_dump(mode="json")),
            ),
        )
        cur.execute(
            "UPDATE agents_meta SET last_launch_attempt_id=%s WHERE id=%s",
            (new_attempt_id, agent_id),
        )
        return row.machine, accepted
