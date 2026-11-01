"""Postgres connection guards, URLs, direct connects, and sync + async pools."""

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Any
from urllib.parse import urlsplit

import psycopg
from psycopg_pool import AsyncConnectionPool, ConnectionPool

from base.db.code_version_gate import ProcessDbGate
from base.db.config import DbConfig, db_config_from_settings
from base.db.pooled_session import (
    PG_STATEMENT_TIMEOUT_OPTIONS,
    restore_pooled_session,
    restore_pooled_session_async,
    retryable_connection_error,
    retryable_restore_error,
)
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

# `base.config.domains.storage.data_plane`'s helpers are imported inside the dial functions
# that call them (`connect` / `pool` / `async_pool`), never at module level:
# this module is on the exec child's boot path (`agent.db` -> `base.db` ->
# here) and a module-level import would pull the full eager config chain
# (pydantic_settings included) back into a lite boot (task #3621). Dialing
# upgrades the config chain anyway (the first `settings.data_plane` read), so
# the deferred import is a sys.modules hit at call time.


class PlaceholderDbUrlError(RuntimeError):
    """A DB connection was attempted by a process that holds no cluster connection facts.

    The process's db_url is the never-dialed placeholder rather than a real
    cluster database: its home declares none (no `.env` with MEIVA_DB_URL), or it
    built settings-lite and never fetched them (see base/config/_lite.py).
    Raised instead of dialing anything.
    """


class NoDatabaseAuthorityError(RuntimeError):
    """This process holds no database login for its write-generation home.

    The home's `.env` carries only the credential-free endpoint; logins are
    delivered by the root launcher, or to an operator process running the
    home's admitted runtime (`base.host.env.dotenv_boot._deliver_operator_authority`).
    Raised at the dial instead of an opaque authentication failure.
    """


# TCP keepalive + connect timeout applied to every cluster Postgres connection —
# the psycopg/libpq mirror of base/events/live/redis_client.py's `RESILIENCE_KWARGS`. A
# laptop-grade runner that sleeps or changes networks wakes holding dead TCP
# flows; without these, a query already in flight on a half-dead socket hangs on
# the OS TCP-retransmit timeout (minutes, no application-level bound). ~30s idle,
# then a probe every 10s, 3 misses = dead (~60s), plus a 5s cap on establishing a
# new connection. These are libpq connection parameters (passed through psycopg's
# `**kwargs` into the conninfo), so they apply to the connect + every borrowed
# pool conn. Typed `dict[str, Any]` (like redis's `RESILIENCE_KWARGS`) so
# `**`-unpacking into psycopg's typed keyword params doesn't trip the type checker.
#
# `connect_timeout` matters most where the connect is the *first* thing a process
# does: against a peer that black-holes packets (dropped, not ECONNREFUSED) an
# unbounded connect never errors, so the caller reads as "hung" rather than
# "failed". This constant is therefore the single definition of that posture:
# `connect()` / `connect_url()` / `pool()` / `async_pool()` apply it, bounded or
# not (a caller may shorten `connect_url`'s connect timeout for a probe).
# Fail-fast behaviour is pinned by meiva/tests/bootstrap/test_connect_fail_fast.py.
PG_KEEPALIVE_KWARGS: dict[str, Any] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "connect_timeout": 5,
}

# The full kwargs the bounded dials (`connect()` / `connect_url()` / `pool()`)
# pass to psycopg. `options` is the direct-connection delivery path (Postgres
# parses it itself); pooled dials additionally run PG_STATEMENT_TIMEOUT_SET_SQL
# (see connect/pool).
PG_STATEMENT_TIMEOUT_KWARGS: dict[str, Any] = {
    **PG_KEEPALIVE_KWARGS,
    "options": PG_STATEMENT_TIMEOUT_OPTIONS,
}

# psycopg_pool's own `ConnectionPool(timeout=...)` default, restated as a name so
# `pool()` can pass it explicitly (a `float | None` sentinel forwarded via `**kwargs`
# is untypeable against ConnectionPool's overloads). Callers that must not block for
# this long pass their own — see `pool`'s docstring.
DEFAULT_POOL_TIMEOUT_S = 30.0


def statement_kwargs(url: str) -> dict[str, Any]:
    """The statement ceiling, after any startup options `url` carries itself: a
    psycopg keyword argument would otherwise replace the URL's `options`."""
    from psycopg.conninfo import conninfo_to_dict

    own = conninfo_to_dict(url).get("options")
    if not own:
        return PG_STATEMENT_TIMEOUT_KWARGS
    return {**PG_STATEMENT_TIMEOUT_KWARGS, "options": f"{own} {PG_STATEMENT_TIMEOUT_OPTIONS}"}


def _refuse_placeholder(url: str) -> None:
    """Refuse the never-dialed placeholder, whichever entry point was handed it.

    Raises:
        PlaceholderDbUrlError: url is the placeholder.
    """
    if url == PLACEHOLDER_DB_URL:
        raise PlaceholderDbUrlError(
            "refusing to open a DB connection: MEIVA_DB_URL is the never-dialed "
            "placeholder. Two ways to land here: this process's home "
            "($MEIVA_HOME, else ~/.meiva) has no .env declaring a cluster — start a "
            "cluster there, or set MEIVA_HOME to a home that has one; or this "
            "process built settings-lite "
            "(MEIVA_CONFIG_FETCH=skip, the maintenance verbs' gateway-down mode) and "
            "this operation needs the cluster config a fetch would have provided."
        )


def guard_db_url(url: str, *, refusal: str | None = None) -> str:
    """Refuse the placeholder URL and an undelivered credential-free endpoint;
    return the url otherwise. The single point every settings-resolved connection
    passes through, so both footguns are caught once here rather than at each
    call site.

    Raises:
        PlaceholderDbUrlError: url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger, no
            login was delivered to this process, and url carries no password.
    """
    _refuse_placeholder(url)
    if refusal is not None:
        try:
            password = urlsplit(url).password
        except ValueError:
            password = None
        if not password:
            raise NoDatabaseAuthorityError(
                f"refusing to dial the credential-free database endpoint: {refusal}"
            )
    return url


class DirectUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class DirectResolution:
    url: str = field(repr=False)
    is_direct: bool
    reason: str = ""


DirectUrlResolver = Callable[[DbConfig], DirectResolution]


def require_direct_url(direct_url: DirectUrlResolver | None) -> DirectUrlResolver:
    """The composition root's direct-URL resolver, or a loud refusal when it supplied none."""
    if direct_url is None:
        raise RuntimeError(
            "a direct database dial requires the composition root's direct_url resolver"
        )
    return direct_url


def require_direct(resolution: DirectResolution) -> str:
    if not resolution.is_direct:
        raise DirectUnavailableError(
            "cannot open a direct (non-pooled) database connection: " + resolution.reason
        )
    return resolution.url


def _dial_url(cfg: DbConfig, *, direct: bool, direct_url: DirectUrlResolver | None) -> str:
    """The URL to dial: the access URL, or the resolver's direct URL for an admin-plane dial."""
    if not direct:
        return cfg.db_url
    return require_direct(require_direct_url(direct_url)(cfg))


# meiva:note
# Open a new connection to the cluster Postgres.
#
# The single entry point for one-off connections, so call sites stop reading
# `settings.data_plane.db_url` by hand. By default this dials the
# `db_url` of `DbConfig` (built from the live settings unless `config` names
# one; a `Database` handle always passes its own) — the cluster's one access URL (PgBouncer when
# enabled, direct Postgres when off; the port is chosen at URL generation, so
# the dial is a plain connect). The returned connection is a context manager:
# `with base.db.connect() as conn: ...`. `autocommit` is passed through for
# the DDL / advisory-lock call sites that need it.
#
# `direct=True` forces the connection to the real Postgres, bypassing PgBouncer
# (the URL comes from the composition root's `direct_url` resolver, which
# derives it from the registry record). The admin
# plane MUST use it wherever a transaction pooler would break correctness —
# session-level state that outlives a single transaction: the migration applier
# holds a **session** advisory lock (`pg_advisory_lock`, in base.deploy.schema.migrations)
# across its whole apply loop, which transaction pooling would silently drop.
# `prepare_threshold=None` disables server-side prepared statements so the same
# connection is safe across the different backends a transaction pooler hands
# out (a prepared statement made on one backend does not exist on the next); it
# is a harmless no-op on a direct connection. psycopg3 semantics: `None` = never
# prepare; `0` = prepare on the FIRST execution (the opposite of this docstring's
# old claim — with the pooler untracked, every fresh connection prepares its
# first statement as the same `_pg3_0` name, and two of them on one backend
# raise DuplicatePreparedStatement).
#
# On a non-direct dial the session is scrubbed back to baseline — RESET ALL
# plus the statement ceiling as an explicit `SET` (PgBouncer drops the
# `options` startup parameter and never resets backend session state between
# clients, so a borrowed backend may carry another client's session GUCs; see
# restore_pooled_session) — unless `unbounded=True`.
#
# `unbounded=True` (admin plane only — the migration applier) drops the
# statement-timeout ceiling entirely: migration DDL runs may legitimately
# exceed 60s (a large-table rebuild, a partition backfill), and the applier
# must stay unbounded. The keepalives and connect timeout stay: a long DDL on
# a remote link is exactly the flow a dead peer would otherwise pin. On a
# direct dial this means no `options` parameter; on a pooled dial the
# pooler's `connect_query` still bounds the backend at birth, so an unbounded
# pooled dial is not truly unbounded — pair it with `direct=True` wherever the
# ceiling must actually be off.
#
# Raises:
#     PlaceholderDbUrlError: the resolved db_url is the placeholder.
#     NoDatabaseAuthorityError: this home keeps a write-generation ledger and
#         the resolved db_url is a credential-free endpoint this process was
#         given no login for (see `guard_db_url`).
# meiva:note
# `dial` is the effect this door performs: psycopg's own `connect` unless the caller names
# another, so every existing caller dials exactly what it dialed before and a test hands in the
# one it needs. As in psycopg_pool's check hook, a transient dial failure or failed pooled
# session restore closes the connection and retries against a fresh backend.
_CONNECT_ATTEMPTS = 3


def connect(
    *,
    autocommit: bool = False,
    direct: bool = False,
    unbounded: bool = False,
    config: DbConfig | None = None,
    gate: ProcessDbGate,
    direct_url: DirectUrlResolver | None = None,
    dial: Callable[..., psycopg.Connection] = psycopg.connect,
) -> psycopg.Connection:
    from base.config.domains.storage.data_plane import sslmode_for_url

    cfg = config if config is not None else db_config_from_settings()
    url = guard_db_url(
        _dial_url(cfg, direct=direct, direct_url=direct_url),
        refusal=cfg.db_authority_refusal,
    )
    sslmode = sslmode_for_url(url, cfg.db_sslmode)
    dial_kwargs = {
        "autocommit": autocommit,
        "prepare_threshold": None,
        # sslmode only when the URL is silent (config is the fallback, never an override).
        **({"sslmode": sslmode} if sslmode else {}),
        # Through the pooler the connection names its process and code version,
        # so PgBouncer's SHOW CLIENTS shows who holds it.
        **({} if direct else {"application_name": gate.application_name()}),
        **_transport_kwargs(url, unbounded=unbounded),
    }
    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            conn = dial(url, **dial_kwargs)
        except Exception as exc:
            if attempt >= _CONNECT_ATTEMPTS or not retryable_connection_error(exc):
                raise
            continue
        if not direct and not unbounded:
            # Pooled dial: PgBouncer dropped the `options` startup parameter above
            # AND does not reset backend session state between clients, so scrub
            # the session back to baseline (RESET ALL + statement ceiling) before
            # handing the connection to the caller — a backend polluted by another
            # client's session-level SET must not reach this caller's writes
            # (2026-09-02 P0; direct dials already got the ceiling via options and
            # own their backend exclusively, so no scrub is needed there).
            try:
                restore_pooled_session(conn, gate=gate)
            except BaseException as exc:
                conn.close()
                if (
                    attempt >= _CONNECT_ATTEMPTS
                    or not isinstance(exc, Exception)
                    or not retryable_restore_error(exc)
                ):
                    raise
                continue
        return conn
    raise AssertionError("unreachable")


# meiva:note
# Open one connection to an explicit Postgres target the caller names.
#
# `connect()` dials the cluster's own URL from settings; this is the admin
# plane's door for every other target — a dump source, a replication login
# under test, a scratch instance a restore check provisions, the pooler's
# admin console, a URL a probe or boot check is handed. The caller owns the
# target: the URL carries its host, credential, database, startup options and
# `sslmode`, and nothing here reads settings, so no cluster login is
# substituted and the configured `MEIVA_DB_SSLMODE` is not injected. (This
# module still resolves a home and imports settings at load, so a process
# that must do neither — the restricted restore worker, the throwaway-Postgres
# tooling — dials on its own; see the `postgres-dial` allowed map in
# meizel/owners/decisions.py.)
#
# The door owns the transport posture, the same as `connect()`'s:
# `prepare_threshold=None`, `PG_KEEPALIVE_KWARGS`, and the statement ceiling
# appended after any startup options the URL carries itself. `connect_timeout`
# bounds establishing the connection (a short probe passes its own);
# `unbounded=True` drops the statement ceiling for a caller whose statements
# may legitimately run long (DDL, full-table reads of a restored copy), and
# keeps the keepalives. The session is never scrubbed: an explicit target is
# a backend the caller dials directly, not a borrowed pooled one (a probe of
# the cluster's pooled URL runs only read-only statements).
#
# Raises:
#     PlaceholderDbUrlError: url is the placeholder.
def connect_url(
    url: str,
    *,
    autocommit: bool = False,
    unbounded: bool = False,
    connect_timeout: int = PG_KEEPALIVE_KWARGS["connect_timeout"],
    dial: Callable[..., psycopg.Connection] = psycopg.connect,
) -> psycopg.Connection:
    _refuse_placeholder(url)
    transport: dict[str, Any] = {
        **_transport_kwargs(url, unbounded=unbounded),
        "connect_timeout": connect_timeout,
    }
    return dial(url, autocommit=autocommit, prepare_threshold=None, **transport)


def _transport_kwargs(url: str, *, unbounded: bool) -> dict[str, Any]:
    """Keepalives always; the statement ceiling unless the dial is unbounded."""
    return PG_KEEPALIVE_KWARGS if unbounded else statement_kwargs(url)


def pool_with_config(
    config: DbConfig,
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    direct: bool = False,
    timeout: float = DEFAULT_POOL_TIMEOUT_S,
    check_connections: bool = False,
    autocommit: bool = False,
    row_factory: object = None,
    gate: ProcessDbGate,
    direct_url: DirectUrlResolver | None = None,
) -> ConnectionPool:
    from base.config.domains.storage.data_plane import resolved_pool_size, sslmode_for_url

    url = guard_db_url(
        _dial_url(config, direct=direct, direct_url=direct_url), refusal=config.db_authority_refusal
    )
    min_size, max_size = resolved_pool_size(
        min_size, max_size, config.db_pool_min_size, config.db_pool_max_size
    )
    sslmode = sslmode_for_url(url, config.db_sslmode)
    connection_kwargs: dict[str, Any] = {
        "prepare_threshold": None,
        **({"sslmode": sslmode} if sslmode else {}),
        **({} if direct else {"application_name": gate.application_name()}),
        **statement_kwargs(url),
    }
    if autocommit:
        connection_kwargs["autocommit"] = True
    if row_factory is not None:
        connection_kwargs["row_factory"] = row_factory
    restore = partial(restore_pooled_session, gate=gate)
    check = (
        restore if not direct else (ConnectionPool.check_connection if check_connections else None)
    )
    return ConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        open=True,
        timeout=timeout,
        kwargs=connection_kwargs,
        configure=restore if not direct else None,
        check=check,
    )


def pool(
    *,
    min_size: int | None = None,
    max_size: int | None = None,
    direct: bool = False,
    timeout: float = DEFAULT_POOL_TIMEOUT_S,
    check_connections: bool = False,
    autocommit: bool = False,
    row_factory: object = None,
    config: DbConfig | None = None,
    gate: ProcessDbGate,
    direct_url: DirectUrlResolver | None = None,
) -> ConnectionPool:
    """Open a ConnectionPool on the cluster Postgres (opened eagerly).

    Long-lived callers dial the cluster's one access URL (PgBouncer when
    enabled) unless `direct=True`; the caller owns and closes the pool.

    `min_size` / `max_size` default to the config fields (themselves the
    historical 1 / 2), so a remote/SaaS plane tunes its pool from config; an
    explicit caller value always wins.

    `autocommit` and `row_factory` apply to every borrowed connection. They
    exist for short-lived read pools whose callers must preserve a direct-read
    contract while still inheriting the shared pool's transport settings.

    This only sanctioned sync-pool builder applies `prepare_threshold=None`,
    `PG_KEEPALIVE_KWARGS`; the structure gate's `postgres-dial` rule rejects bypasses.

    `timeout` bounds how long `pool.connection()` waits for a connection before
    raising `PoolTimeout`. It is worth knowing about, not just tuning: `open=True`
    does NOT fail here when the DB is unreachable — the pool keeps retrying in
    background workers, so a dead data plane surfaces only at the first
    `pool.connection()`, one full timeout later. A caller on a bounded schedule (the
    watchdog's sequential
    tick, where a long wait delays every check behind it) must pass a short one;
    long-lived daemon pools keep the default.

    Pooled pools restore the baseline session (RESET ALL + the statement
    ceiling) at backend creation AND on every borrow — pgbouncer never resets
    backend session state between clients (2026-09-02 P0), so the borrow-time
    scrub is what keeps a backend polluted by another client's session-level
    SET from failing this borrower's writes. See restore_pooled_session.

    Raises:
        PlaceholderDbUrlError: the resolved db_url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger and
            the resolved db_url is a credential-free endpoint this process was
            given no login for (see `guard_db_url`).
    """
    cfg = config if config is not None else db_config_from_settings()
    return pool_with_config(
        cfg,
        min_size=min_size,
        max_size=max_size,
        direct=direct,
        timeout=timeout,
        check_connections=check_connections,
        autocommit=autocommit,
        row_factory=row_factory,
        gate=gate,
        direct_url=direct_url,
    )


def async_pool_with_config(
    pool_class: Callable[..., AsyncConnectionPool[psycopg.AsyncConnection]],
    config: DbConfig,
    *,
    min_size: int,
    max_size: int,
    timeout: float,
    gate: ProcessDbGate,
    **pool_kwargs: object,
) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    from base.config.domains.storage.data_plane import sslmode_for_url

    sslmode = sslmode_for_url(config.db_url, config.db_sslmode)
    kwargs = {
        "autocommit": True,
        "prepare_threshold": None,
        **({"sslmode": sslmode} if sslmode else {}),
        "application_name": gate.application_name(),
        **PG_KEEPALIVE_KWARGS,
    }
    return pool_class(
        guard_db_url(config.db_url, refusal=config.db_authority_refusal),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=False,
        kwargs=kwargs,
        check=partial(restore_pooled_session_async, gate=gate),
        **pool_kwargs,
    )


def async_pool(
    pool_class: Callable[..., AsyncConnectionPool[psycopg.AsyncConnection]],
    *,
    min_size: int,
    max_size: int,
    timeout: float,
    config: DbConfig | None = None,
    gate: ProcessDbGate,
    **pool_kwargs: object,
) -> AsyncConnectionPool[psycopg.AsyncConnection]:
    """An unopened async pool on the cluster's access URL (the agent host's pools).

    The async twin of `pool()`. It is returned closed because the caller's event
    loop opens it (`await pool.open()`). `pool_class` is the `AsyncConnectionPool`
    subclass to build (the host's `agent.db.LoggingConnectionPool`, which times
    every borrow) so this module never imports the agent layer; `pool_kwargs`
    are that subclass's own arguments.

    The transport posture is fixed here: autocommit (the checkpoint saver
    expects it), `prepare_threshold=None` (borrows hop PgBouncer backends),
    `PG_KEEPALIVE_KWARGS`, the configured sslmode when the URL is silent, and
    the baseline-session restore as the per-borrow `check`, which is also what
    delivers the statement ceiling (PgBouncer drops the `options` startup
    parameter). See restore_pooled_session.

    Raises:
        PlaceholderDbUrlError: the resolved db_url is the placeholder.
        NoDatabaseAuthorityError: this home keeps a write-generation ledger and
            the resolved db_url is a credential-free endpoint this process was
            given no login for (see `guard_db_url`).
    """
    cfg = config if config is not None else db_config_from_settings()
    return async_pool_with_config(
        pool_class,
        cfg,
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        gate=gate,
        **pool_kwargs,
    )
