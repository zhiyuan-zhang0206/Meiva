# meiva:note
# The pgbouncer.ini of this cluster's pooler, as a function of values.
#
# One [databases] entry maps the cluster database to the local Postgres over its owner-only
# unix socket; [pgbouncer] sets transaction pooling, SCRAM client auth against the userlist
# (always) and loopback plus reachable binds (never all interfaces), matching Postgres's
# posture. The shell reads the addresses, paths and the statement-ceiling statement and hands
# them in, so a test passes the values it wants.

from __future__ import annotations

import configparser
from dataclasses import dataclass

__all__ = ["DEFAULT_POOL_SIZE", "IniInputs", "listen_port", "render_ini"]

# meiva:note
# `default_pool_size` is the real Postgres backend pool per (database, user) pair. The
# client-side cap (`max_client_conn`) is configuration, MEIVA_POOLER_MAX_CLIENT_CONN, because
# the client slot budget belongs to the whole fleet's pool maxima, not to this module.
DEFAULT_POOL_SIZE = 25

# meiva:note
# libpq and psycopg send these startup parameters; PgBouncer rejects unknown ones unless told
# to ignore them. Real GUCs (search_path and the like) are deliberately not here.
_IGNORE_STARTUP_PARAMETERS = "extra_float_digits,options"


# meiva:note
# Everything the rendered file depends on.
#
# `socket_dir` is the directory the RUNNING Postgres actually listens on (the exact
# directory the admin dial uses): a Postgres that was already up before a path-only cutover
# still serves the old name-keyed directory, and rendering the canonical one there would break
# every pooled query while admin readiness stayed green. `listen_addrs` is loopback plus the
# declared reachable address, never all interfaces. `statement_timeout_sql` is the statement
# every pooled backend runs at birth.
@dataclass(frozen=True)
class IniInputs:
    db_name: str
    pg_port: int
    socket_dir: str
    listen_addrs: tuple[str, ...]
    listen_port: int
    userlist_path: str
    logfile: str
    pidfile: str
    max_client_conn: int
    admin_user: str
    statement_timeout_sql: str


# meiva:note
# The pooler's configuration text.
#
# Every pooled backend is born with the statement ceiling through `connect_query`, so a query
# is bounded whichever code path the client came through: the pooler drops the libpq `options`
# startup parameter and `track_extra_parameters` cannot deliver statement_timeout (only
# GUC_REPORT parameters Postgres reports to clients can be tracked), so the connect_query SET
# is the one pooler-side path that reaches the backend.
#
# `server_reset_query = DISCARD ALL` is one unquoted statement: pgbouncer 1.25.2 runs the
# value verbatim, a quoted value was a syntax error (2026-09-02 P0, about 18M errors) and a
# multi-statement value is rejected inside the implicit transaction transaction pooling wraps
# it in. `server_reset_query_always = 0` keeps the reset to the window where a client vanished
# mid-transaction and to error returns; clean releases never run it, so a client's own dial
# and borrow SETs (the statement ceiling) survive between its transactions. always=1 was tried
# and rejected (405 ruling 2026-09-03, option B): firing after every transaction end, its
# DISCARD ALL wiped the client's SETs too. Between-transaction pollution is defended
# client-side by the baseline restore on every dial and borrow.
def render_ini(inputs: IniInputs) -> str:
    if not inputs.listen_addrs:
        raise ValueError("the pooler needs at least one listen address")
    if inputs.max_client_conn < 1:
        raise ValueError("the pooler needs a positive client cap")
    return "\n".join(
        [
            "[databases]",
            f"{inputs.db_name} = host={inputs.socket_dir} port={inputs.pg_port} "
            f"dbname={inputs.db_name} connect_query='{inputs.statement_timeout_sql}'",
            "",
            "[pgbouncer]",
            f"listen_addr = {', '.join(inputs.listen_addrs)}",
            f"listen_port = {inputs.listen_port}",
            "auth_type = scram-sha-256",
            f"auth_file = {inputs.userlist_path}",
            "pool_mode = transaction",
            "server_reset_query = DISCARD ALL",
            "server_reset_query_always = 0",
            f"max_client_conn = {inputs.max_client_conn}",
            f"default_pool_size = {DEFAULT_POOL_SIZE}",
            f"ignore_startup_parameters = {_IGNORE_STARTUP_PARAMETERS}",
            f"admin_users = {inputs.admin_user}",
            f"stats_users = {inputs.admin_user}",
            "log_connections = 0",
            "log_disconnections = 0",
            f"logfile = {inputs.logfile}",
            f"pidfile = {inputs.pidfile}",
            "",
        ]
    )


# meiva:note
# The port a rendered pgbouncer.ini listens on. A file that names no valid port is not this
# home's pooler configuration, and the custody that reads it refuses.
def listen_port(text: str) -> int:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
        port = parser.getint("pgbouncer", "listen_port")
    except (configparser.Error, ValueError):
        raise ValueError("invalid owned PgBouncer listen port") from None
    if not 0 < port < 65536:
        raise ValueError("invalid owned PgBouncer listen port")
    return port
