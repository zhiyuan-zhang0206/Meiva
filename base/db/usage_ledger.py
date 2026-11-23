from collections.abc import Callable
from dataclasses import replace
from functools import partial
from pathlib import Path
from uuid import uuid4

import psycopg
from loguru import logger
from psycopg import sql
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from base.api_contracts.usage import (
    UsageLedger,
    UsageLedgerConflictError,
    UsageLedgerEntry,
    UsageLedgerWriteError,
)
from base.db.handle import Database

_COLUMNS = sql.SQL("""
    call_id, occurred_at, agent_id, machine, cluster, source, billing, usage_kind, model,
    requested_model, provider, provider_request_id, calls, complete, tokens_in, tokens_out,
    tokens_reasoning, cache_read, cache_write_5m, cache_write_1h, cache_write_30m, cost_usd,
    price_snapshot, price_source
""")
_INSERT = sql.SQL("""
    INSERT INTO llm_usage_ledger ({})
    VALUES (
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
    )
    ON CONFLICT (call_id) DO NOTHING
    RETURNING call_id
""").format(_COLUMNS)
_FIND = sql.SQL("SELECT {} FROM llm_usage_ledger WHERE call_id = %s").format(_COLUMNS)


# meiva:link base/lm/usage/README.md
def insert_usage(conn: psycopg.Connection, entry: UsageLedgerEntry) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            _INSERT,
            (
                entry.call_id,
                entry.occurred_at,
                entry.agent_id,
                entry.machine,
                entry.cluster,
                entry.source,
                entry.billing,
                entry.usage_kind,
                entry.model,
                entry.requested_model,
                entry.provider,
                entry.provider_request_id,
                entry.calls,
                entry.complete,
                entry.tokens_in,
                entry.tokens_out,
                entry.tokens_reasoning,
                entry.cache_read,
                entry.cache_write_5m,
                entry.cache_write_1h,
                entry.cache_write_30m,
                entry.cost_usd,
                Jsonb(entry.price_snapshot),
                entry.price_source,
            ),
        )
        if cur.fetchone() is not None:
            return True
    with conn.cursor(row_factory=class_row(UsageLedgerEntry)) as cur:
        cur.execute(_FIND, (entry.call_id,))
        existing = cur.fetchone()
        if existing is None or not _same_report(existing, entry):
            raise UsageLedgerConflictError(f"conflicting LLM usage report for {entry.call_id}")
    return False


def _same_report(existing: UsageLedgerEntry, incoming: UsageLedgerEntry) -> bool:
    if incoming.call_id.startswith("harness:"):
        return existing == replace(
            incoming,
            occurred_at=existing.occurred_at,
            machine=existing.machine,
            cost_usd=existing.cost_usd,
            price_snapshot=existing.price_snapshot,
            price_source=existing.price_source,
        )
    return existing == incoming


def usage_writer(database: Callable[[], Database]) -> Callable[[UsageLedgerEntry], bool]:
    return partial(_write_usage, database)


def bind_usage_ledger(
    database: Callable[[], Database], machine: Callable[[], str], home: Path
) -> UsageLedger:
    return UsageLedger(usage_writer(database), machine, home.name, _new_call_id)


def _new_call_id() -> str:
    return f"api:{uuid4()}"


def _write_usage(database: Callable[[], Database], entry: UsageLedgerEntry) -> bool:
    try:
        with database().connect() as conn:
            conn.execute("SET TRANSACTION READ WRITE")
            conn.execute("SET LOCAL synchronous_commit TO on")
            return insert_usage(conn, entry)
    except Exception as exc:
        conflict = isinstance(exc, UsageLedgerConflictError)
        logger.warning(
            "LLM usage ledger {reason} for {call_id}: {error_type}",
            event="llm_usage_ledger_write_failed",
            reason="conflict" if conflict else "database",
            call_id=entry.call_id,
            error_type=type(exc).__name__,
            failures=1,
            agent_id=entry.agent_id,
        )
        if conflict:
            raise
        raise UsageLedgerWriteError("LLM usage commit failed") from exc
