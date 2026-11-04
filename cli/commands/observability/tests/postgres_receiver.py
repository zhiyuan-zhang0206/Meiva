# meiva:note
# What a test needs to stand where otelcol-contrib's PostgreSQL receiver stands: the shape of the
# receiver the collector template renders, and the two read passes it makes. `endpoint` is
# `host:port`, or a `/`-prefixed socket path when `transport` is `unix`; `username` and `password`
# are the scrape login; `databases` are the ones it scrapes. `test_single_box.py` renders the
# config, validates it into this shape before anything dials, and hands it here — so a template
# that renames or retypes a field fails at the validation, not at the connect. Nothing in this
# module knows about a `Born` home.
# meiva:link cli/commands/observability/docs/operations.md

from __future__ import annotations

from typing import TypedDict

import psycopg
from psycopg.rows import TupleRow
from pydantic import TypeAdapter


class PostgresReceiver(TypedDict):
    endpoint: str
    transport: str
    username: str
    password: str
    databases: list[str]


RECEIVER: TypeAdapter[PostgresReceiver] = TypeAdapter(PostgresReceiver)


# meiva:note
# Dial exactly as otelcol-contrib's postgresql receiver builds its lib/pq DSN: the `host:port`
# endpoint, with the host `/`-prefixed for the unix transport.
def dial_as_receiver(receiver: PostgresReceiver, database: str) -> psycopg.Connection[TupleRow]:
    host, _, port = receiver["endpoint"].rpartition(":")
    if receiver["transport"] == "unix":
        host = "/" + host.lstrip("/")
    return psycopg.connect(
        host=host,
        port=port,
        user=receiver["username"],
        password=receiver["password"],
        dbname=database,
        sslmode="disable",
        connect_timeout=5,
        autocommit=True,
    )


# meiva:note
# The receiver's two read passes: the cluster-wide queries on `postgres` — other sessions'
# activity and replication rows need the stats role, so the receiver's login must hold it — and
# then the per-database table and index reads.
def scrape_like_the_receiver(receiver: PostgresReceiver) -> None:
    with dial_as_receiver(receiver, "postgres") as conn:
        assert conn.execute("SELECT pg_has_role('pg_read_all_stats', 'USAGE')").fetchone() == (
            True,
        )
        sizes = TypeAdapter(list[tuple[str, int]]).validate_python(
            conn.execute(
                "SELECT datname, pg_database_size(datname) FROM pg_catalog.pg_database"
                " WHERE datistemplate = false"
            ).fetchall()
        )
        assert {name for name, _size in sizes} >= {"postgres", "meiva"}
        conn.execute("SELECT datname, count(*) FROM pg_stat_activity GROUP BY datname")
        conn.execute(
            "SELECT coalesce(pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn), -1)"
            " FROM pg_stat_replication"
        )
        conn.execute("SELECT coalesce(last_archived_time, CURRENT_TIMESTAMP) FROM pg_stat_archiver")
        conn.execute("SHOW max_connections")
    for database in receiver["databases"]:
        with dial_as_receiver(receiver, database) as conn:
            tables = TypeAdapter(list[tuple[str, int]]).validate_python(
                conn.execute(
                    "SELECT relname, pg_relation_size(relid) FROM pg_stat_user_tables"
                ).fetchall()
            )
            assert "agents" in {name for name, _size in tables}
            conn.execute("SELECT * FROM pg_statio_user_tables")
            conn.execute("SELECT pg_relation_size(indexrelid) FROM pg_stat_user_indexes")
