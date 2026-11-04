"""meiva.ui.show / .close panel registration, gateway-internal DB layer.

The agent calls `meiva.ui.show(name)` to start an HTTP server inside its own
process (bound on the runner's reachable host — loopback on a single box),
self-reporting its reachable host; this module writes the registration into
the agent_pages table. The page URL is the gateway's own
`/pages/<id>-<name>/` reverse-proxy address: the gateway forwards
GETs to the page server, so the browser never dials it directly (host:port
stays inside the gateway — it appears in no URL).

Terminalization: closed_at NULL = open, non-NULL = closed (terminal). A
UNIQUE partial index `WHERE closed_at IS NULL` ensures only one open row
per (agent, name); historical closed rows do not block re-registration.

register_page_in_transaction is an idempotent upsert — re-registering the same name
updates host + port (agent restarted the server on a different port /
name reuse), without colliding on the UNIQUE index. close_observed_page_in_transaction
closes the observed row by its immutable id.

Port rule: a live (open, unexpired) row exclusively holds its (host, port).
assert_port_free reports the occupying row before the auto-close, and the
`agent_pages_unique_live_port` partial unique index enforces the same rule
against a registration that races the check.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import psycopg

from base.db.rows import query_all, query_one
from ops.rpc_schemas import PageRow

_SELECT_COLUMNS = (
    "id, agent_id, name, port, host, title, serve_dir, created_at, closed_at, "
    "expires_at, expired_at"
)


def _page_url(agent_id: int, name: str) -> str:
    # Gateway reverse-proxy path: the gateway serves page content at
    # /pages/<agent_id>-<name>/ and the router absolutizes it against
    # the request base URL before returning. The browser never dials the
    # page server directly, so host/port appear in no URL — they stay in
    # the row for the proxy's own dialing.
    #
    # The composite key is parseable because agent_id is numeric: split on
    # the FIRST dash yields (agent_id, name) even when name itself contains
    # dashes (name charset is [a-zA-Z0-9_-]).
    return f"/pages/{agent_id}-{name}/"


# meiva:note
# An `agent_pages` row as `_SELECT_COLUMNS` selects it, named as the table names its columns. `host`
# is nullable in the table; `expires_at` / `expired_at` are read for the row shape only.
@dataclass(frozen=True)
class _PageDbRow:
    id: int
    agent_id: int
    name: str
    port: int
    host: str | None
    title: str | None
    serve_dir: str | None
    created_at: datetime
    closed_at: datetime | None
    expires_at: datetime | None
    expired_at: datetime | None


def _row_to_record(row: _PageDbRow) -> PageRow:
    return PageRow(
        id=row.id,
        agent_id=row.agent_id,
        name=row.name,
        port=row.port,
        title=row.title,
        serve_dir=row.serve_dir,
        url=_page_url(row.agent_id, row.name),
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


# meiva:note
# The row a statement that contractually yields exactly one (an `INSERT ... RETURNING`) answered.
def _exactly_one(row: _PageDbRow | None, context: str) -> _PageDbRow:
    if row is None:
        raise RuntimeError(f"expected exactly one row: {context}")
    return row


# meiva:note
# One column of an `agent_pages` lookup that selects a single name.
@dataclass(frozen=True)
class _NameRow:
    name: str


# meiva:note
# An agent and the page name of another live page holding a (host, port).
@dataclass(frozen=True)
class _OccupantRow:
    agent_id: int
    name: str


# meiva:note
# The dial target of an open page; `host` is nullable in the table.
@dataclass(frozen=True)
class _TargetRow:
    host: str | None
    port: int


_LIVE_PORT_INDEX = "agent_pages_unique_live_port"


class PagePortConflictError(Exception):
    """(host, port) is already held by another live page row.

    Raised by ``assert_port_free`` (the pre-check) and by ``register_page_in_transaction``
    when the live-port unique index rejects a registration that raced the
    pre-check. The message names the occupying page when it is visible; the
    gateway router maps this to HTTP 409.
    """


def _port_conflict_message(host: str, port: int, owner: tuple[int, str] | None) -> str:
    """The one conflict-message shape, for both the pre-check and the raced path."""
    if owner is None:
        return (
            f"port {port} on {host} was taken by another page registration just now"
            " — choose a different port"
        )
    owner_id, owner_name = owner
    return (
        f"port {port} on {host} is already used by page {owner_name!r} of agent {owner_id}"
        " — close that page or choose a different port"
    )


def find_live_page_on_port(
    db: psycopg.Connection, host: str, port: int, *, exclude_agent_id: int
) -> tuple[int, str] | None:
    """(agent_id, name) of another live page holding (host, port), or None.

    Live = open and unexpired: the same predicate the page-server daemon and
    the reverse proxy use, and the predicate of the live-port unique index.
    Rows of ``exclude_agent_id`` are ignored — registration replaces the
    registering agent's own page, so its own row is not a conflict.
    """
    occupant = query_one(
        db,
        _OccupantRow,
        "SELECT agent_id, name FROM agent_pages "
        "WHERE closed_at IS NULL AND expired_at IS NULL "
        "AND host = %s AND port = %s AND agent_id <> %s "
        "ORDER BY id LIMIT 1",
        (host, port, exclude_agent_id),
    )
    return (occupant.agent_id, occupant.name) if occupant is not None else None


def assert_port_free(db: psycopg.Connection, agent_id: int, host: str, port: int) -> None:
    """Raise PagePortConflictError when another live page holds (host, port).

    Runs before the registering agent's own pages are auto-closed, so a
    rejected registration leaves the agent's current page untouched.
    """
    owner = find_live_page_on_port(db, host, port, exclude_agent_id=agent_id)
    if owner is not None:
        raise PagePortConflictError(_port_conflict_message(host, port, owner))


def lock_page_agent(db: psycopg.Connection, agent_id: int) -> None:
    """Serialize page mutations for one agent until the caller commits."""
    db.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"agent-pages:{agent_id}",)
    )


# meiva:note
# Register or update an open page in the caller-owned transaction.
#
# If an open row with the same name exists -> UPDATE host + port + title + serve_dir (agent
# restarted server on different port). None -> INSERT. Returns the final row snapshot without
# committing. The caller owns commit and publishing the PageOpened event.
#
# `serve_dir` is the directory the page server serves, recorded by meiva.ui.serve() so the
# page-server service can admit its initial serving session; NULL for meiva.ui.show() pages (the
# agent manages those servers itself). The page lives for `ttl_seconds` from `now`, which the
# caller reads once per registration. The statements concatenate only this module's fixed column
# list (`_SELECT_COLUMNS`), never a caller value, which is why they carry `noqa: S608`.
def register_page_in_transaction(
    db: psycopg.Connection,
    agent_id: int,
    name: str,
    port: int,
    host: str,
    title: str | None,
    serve_dir: str | None,
    *,
    ttl_seconds: int,
    now: datetime,
) -> PageRow:
    lock_page_agent(db, agent_id)
    expires_at = now + timedelta(seconds=ttl_seconds)
    try:
        with db.transaction():
            row = query_one(
                db,
                _PageDbRow,
                "UPDATE agent_pages SET port = %s, host = %s, title = %s, serve_dir = %s, "  # noqa: S608
                "expires_at = %s, expired_at = NULL, "
                "session_name = CASE WHEN (port, host, serve_dir) IS DISTINCT FROM (%s, %s, %s) "
                "THEN NULL ELSE session_name END, "
                "server_token = CASE WHEN (port, host, serve_dir) IS DISTINCT FROM (%s, %s, %s) "
                "THEN NULL ELSE server_token END "
                "WHERE agent_id = %s AND name = %s AND closed_at IS NULL "
                "RETURNING " + _SELECT_COLUMNS,
                (
                    port,
                    host,
                    title,
                    serve_dir,
                    expires_at,
                    port,
                    host,
                    serve_dir,
                    port,
                    host,
                    serve_dir,
                    agent_id,
                    name,
                ),
            )
            if row is None:
                row = _exactly_one(
                    query_one(
                        db,
                        _PageDbRow,
                        "INSERT INTO agent_pages "
                        "(agent_id, name, port, host, title, serve_dir, expires_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "RETURNING " + _SELECT_COLUMNS,
                        (agent_id, name, port, host, title, serve_dir, expires_at),
                    ),
                    f"insert page agent={agent_id} name={name!r}",
                )
    except psycopg.errors.UniqueViolation as exc:
        # meiva:note
        # The live-port index is the authority; the router's pre-check can be raced by a
        # concurrent registration. Discard the failed write, then name the occupant when a fresh
        # read still sees it (its transaction committed). Other unique violations (the
        # (agent_id, name) index) are not this function's rule: re-raise.
        if exc.diag.constraint_name != _LIVE_PORT_INDEX:
            raise
        raise PagePortConflictError(
            _port_conflict_message(
                host, port, find_live_page_on_port(db, host, port, exclude_agent_id=agent_id)
            )
        ) from exc
    return _row_to_record(row)


def get_open_page_target(
    db: psycopg.Connection,
    agent_id: int,
    name: str,
) -> tuple[str, int] | None:
    """(host, port) of one currently open page — the reverse-proxy dial
    target. None for unknown agent / unknown name / closed page (the proxy
    404s on all three; callers do not need to distinguish).

    Returns the bare host:port instead of a PageRow: the wire schema carries
    no host (it stays inside the gateway), so the proxy reads it here."""
    target = query_one(
        db,
        _TargetRow,
        "SELECT host, port FROM agent_pages "
        "WHERE agent_id = %s AND name = %s "
        "AND closed_at IS NULL AND expired_at IS NULL",
        (agent_id, name),
    )
    if target is None:
        return None
    if target.host is None:
        raise ValueError(f"open page {name!r} of agent {agent_id} has no host to dial")
    return target.host, target.port


def list_open_pages(db: psycopg.Connection, agent_id: int) -> list[PageRow]:
    """Main frontend page-load entry: fetch the agent's currently open page list."""
    rows = query_all(
        db,
        _PageDbRow,
        "SELECT " + _SELECT_COLUMNS + " FROM agent_pages "  # noqa: S608
        "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL "
        "ORDER BY created_at ASC",
        (agent_id,),
    )
    return [_row_to_record(r) for r in rows]


def list_all_open_pages(db: psycopg.Connection) -> list[PageRow]:
    """Fleet view page-load: every agent's currently open pages in one fetch
    (the per-agent GET would be N requests across the fleet). SSE
    PageOpened/PageClosed handles deltas after load."""
    rows = query_all(
        db,
        _PageDbRow,
        "SELECT " + _SELECT_COLUMNS + " FROM agent_pages "  # noqa: S608
        "WHERE closed_at IS NULL AND expired_at IS NULL ORDER BY created_at ASC",
    )
    return [_row_to_record(r) for r in rows]


def close_all_agent_pages_in_transaction(db: psycopg.Connection, agent_id: int) -> list[str]:
    """Close every open page for ``agent_id``, expired rows included.

    Registration closes expired rows too, so a fresh registration cannot revive
    an older numeric identity. This primitive never commits.

    Returns the names of the closed pages so the caller can publish
    PageClosed events for each one (every close needs its own event for
    the frontend to remove the entry from the popover).

    It closes all open rows in one UPDATE, which is safe because the UNIQUE
    partial index already limits an agent to one open row per name, and the
    single-page-per-agent convention (enforced by the SDK and the router)
    guarantees at most one open row at a time in normal operation.
    """
    lock_page_agent(db, agent_id)
    names = [
        row.name
        for row in query_all(
            db,
            _NameRow,
            "SELECT name FROM agent_pages WHERE agent_id = %s AND closed_at IS NULL",
            (agent_id,),
        )
    ]
    if names:
        _ = db.execute(
            "UPDATE agent_pages SET closed_at = now() WHERE agent_id = %s AND closed_at IS NULL",
            (agent_id,),
        )
    return names


def list_open_page_names(db: psycopg.Connection, agent_id: int) -> list[str]:
    """Return the pages a terminate cascade will close.

    Daemon-supervised serve() pages have a ``serve_dir`` and remain open across
    termination; only agent-owned show() pages are captured for PageClosed
    events. After the caller UPDATEs agents_meta.status='terminated', the SQL
    trigger silently closes these rows and the frontend removes them in real time.

    Does not return full PageRow — publishing only needs name (PageClosed
    fields are only agent_id + name). Separate SELECT to avoid the
    url-composition in _row_to_record (not needed by the terminate
    path)."""
    return [
        row.name
        for row in query_all(
            db,
            _NameRow,
            "SELECT name FROM agent_pages "
            "WHERE agent_id = %s AND closed_at IS NULL AND expired_at IS NULL "
            "AND serve_dir IS NULL",
            (agent_id,),
        )
    ]


class PageTargetChangedError(ValueError):
    """The named page no longer denotes the caller's observed registry row."""


def close_observed_page_in_transaction(
    db: psycopg.Connection, agent_id: int, name: str, expected_page_id: int
) -> tuple[PageRow, bool]:
    """Close the latest named row only when its immutable ID matches.

    An already closed matching row is accepted without another effect. A newer
    registration makes the observation stale even when that newer row is closed.
    """
    lock_page_agent(db, agent_id)
    row = query_one(
        db,
        _PageDbRow,
        "SELECT " + _SELECT_COLUMNS + " FROM agent_pages "  # noqa: S608
        "WHERE agent_id = %s AND name = %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
        (agent_id, name),
    )
    if row is None or row.id != expected_page_id:
        raise PageTargetChangedError(
            "observed page is no longer the latest registration for this name"
        )
    changed = row.closed_at is None
    if changed:
        row = _exactly_one(
            query_one(
                db,
                _PageDbRow,
                "UPDATE agent_pages SET closed_at = now() WHERE id = %s RETURNING "  # noqa: S608 — owned fixed projection
                + _SELECT_COLUMNS,
                (expected_page_id,),
            ),
            "close observed page",
        )
    return _row_to_record(row), changed
