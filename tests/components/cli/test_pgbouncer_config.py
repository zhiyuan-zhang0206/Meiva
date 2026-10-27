"""PgBouncer config generation + the direct-connection exemption contract.

Unit-level: the rendered pgbouncer.ini content (the userlist is rendered by
`base.host.db_authority.delivery.render_userlist`), and the code paths
that MUST bypass the pooler. The end-to-end wire behaviour (a real pgbouncer in
transaction pooling in front of Postgres) lives in test_pgbouncer_wire.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from psycopg.conninfo import conninfo_to_dict

from base.cluster import provision_database
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.dataplane.pg_admin import owner_conninfo, pg_admin_url
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database, DbConfig, connections
from base.db.code_version_gate import ProcessDbGate
from base.host.net.url_secret import url_with_port
from base.paths import meiva_home
from cli.commands.data_plane import pgbouncer
from cli.commands.data_plane.tests.data_plane_records import data_plane_config as data_plane_config
from cli.commands.lifecycle.tests.start_storage.owned_postgres import (
    owned_pg as owned_pg,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_database as operator_database


def _shape(machine: MachineIdentityReader, db_name: str) -> pgbouncer.PoolerShape:
    return pgbouncer.PoolerShape(
        pg_port=5433,
        listen_port=6433,
        db_name=db_name,
        machine=machine,
        max_client_conn=2000,
        socket_dir=Path("/run/meiva/pg-sock"),
    )


def _assert_transaction_pooling_with_scram_clients(ini: str) -> None:
    # Transaction pooling is the whole point.
    assert "pool_mode = transaction" in ini
    # Client auth is scram against the userlist of generation verifiers.
    assert "auth_type = scram-sha-256" in ini
    assert f"auth_file = {pgbouncer.userlist_path(meiva_home())}" in ini
    assert "listen_port = 6433" in ini


def _assert_database_entry_forwards_over_owner_only_socket(ini: str) -> None:
    # The [databases] entry keys on the cluster db and forwards to the local pg over
    # its owner-only unix socket (host=<socket dir>) with SCRAM pass-through.
    assert "[databases]" in ini
    assert "meiva_main = host=/" in ini
    assert "port=5433 dbname=meiva_main" in ini


def _assert_backends_born_with_statement_ceiling(ini: str) -> None:
    # Every pooled backend is born with the statement ceiling (the pooler drops
    # the client's `options` startup parameter — the connect_query SET is the
    # one pooler-side delivery path; see base.db.PG_STATEMENT_TIMEOUT_SET_SQL).
    assert "connect_query='SET statement_timeout = 60000'" in ini


def _assert_release_reset_is_one_unquoted_discard_all(ini: str) -> None:
    # A backend whose client vanished mid-transaction is scrubbed back to its
    # connect_query-fresh state by the release reset: a session-level GUC (e.g.
    # a polluter's SET default_transaction_read_only = on) must never reach the
    # next borrower of a pooled backend. always=0 (explicit) keeps the reset to
    # that SV_ACTIVE window — always=1 fires after every transaction and its
    # DISCARD ALL wiped the client's own dial/borrow-time SETs (the statement
    # ceiling; measured 2026-09-03, 405 ruling option B). Between-transaction
    # pollution is defended client-side by base/db/__init__.py's baseline restore
    # (2026-09-02 P0 incident).
    assert "server_reset_query = DISCARD ALL" in ini
    # The reset is one statement and unquoted: pgbouncer 1.25.2 runs
    # server_reset_query verbatim, so a quoted value reaches Postgres as a
    # syntax error (2026-09-02 P0, ~18M errors), and a multi-statement value is
    # wrapped in an implicit transaction by transaction pooling — "DISCARD ALL
    # cannot run inside a transaction block" (measured 2026-09-02 02:21).
    assert "server_reset_query = DISCARD ALL; SET" not in ini
    assert "server_reset_query_always = 0" in ini
    # The reset does NOT re-apply the statement ceiling: DISCARD ALL clears the
    # birth-time connect_query SET, and the reset would need a second statement
    # to re-apply it — a shape transaction pooling rejects. The ceiling is
    # delivered by connect_query (every backend at birth) and by base/db/__init__.py's
    # client-side SET on every pooled use.
    reset_line = next(ln for ln in ini.splitlines() if ln.startswith("server_reset_query ="))
    assert reset_line == "server_reset_query = DISCARD ALL"


def _assert_quiet_connection_logs_and_userlist_only_console(ini: str) -> None:
    assert "log_connections = 0" in ini.splitlines()
    assert "log_disconnections = 0" in ini.splitlines()
    # admin/stats console is the userlist-only operator entry, never a database role.
    console = {"admin_users = meiva_pooler_admin", "stats_users = meiva_pooler_admin"}
    assert console <= set(ini.splitlines())


def test_render_ini_is_transaction_scram_and_socket_server(
    machine_identity: MachineIdentityReader,
) -> None:
    ini = pgbouncer.render_ini(_shape(machine_identity, "meiva_main"), home=meiva_home())
    _assert_transaction_pooling_with_scram_clients(ini)
    _assert_database_entry_forwards_over_owner_only_socket(ini)
    _assert_backends_born_with_statement_ceiling(ini)
    _assert_release_reset_is_one_unquoted_discard_all(ini)
    _assert_quiet_connection_logs_and_userlist_only_console(ini)


def test_render_ini_authenticates_and_binds_loopback_on_a_single_box(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    """The internal data plane always authenticates, and a box that declares no
    reachable address (`MEIVA_MACHINE_HOST` unset) binds loopback alone."""
    machine_identity.set(host="localhost")
    ini = pgbouncer.render_ini(_shape(machine_identity, "meiva"), home=meiva_home())
    assert "auth_type = scram-sha-256" in ini.splitlines()
    assert "trust" not in ini
    assert "listen_addr = 127.0.0.1" in ini.splitlines()


def test_render_ini_adds_only_the_declared_reachable_address(
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(host="10.0.0.5")
    ini = pgbouncer.render_ini(_shape(machine_identity, "meiva"), home=meiva_home())
    assert "listen_addr = 127.0.0.1, 10.0.0.5" in ini.splitlines()


def test_render_ini_binds_loopback_never_all_interfaces(
    machine_identity: MachineIdentityReader,
) -> None:
    ini = pgbouncer.render_ini(_shape(machine_identity, "meiva_main"), home=meiva_home())
    listen = next(ln for ln in ini.splitlines() if ln.startswith("listen_addr ="))
    assert "127.0.0.1" in listen
    assert "0.0.0.0" not in listen and "*" not in listen  # noqa: S104 — asserting we do NOT bind all interfaces


# ── direct-connection exemptions (the admin plane must never route through PgBouncer) ──


@pytest.mark.parametrize("remote", [False, True])
def test_backup_defaults_to_a_direct_dump_source(
    owned_pg: int, unit_home: Path, database_gate: ProcessDbGate, remote: bool
) -> None:
    """The explicit plane reader selects either provider or local owner authority."""
    from services.backup import dump as backup

    provision_database(
        "meiva",
        base_admin_url=pg_admin_url(owned_pg, home=meiva_home()),
        expected_data_dir=unit_home / "pg",
    )
    plane_url = f"postgresql://meiva@127.0.0.1:{owned_pg}/meiva"
    database = Database(
        DbConfig(
            db_url=plane_url,
            db_sslmode="disable",
            db_pool_min_size=1,
            db_pool_max_size=2,
            pgbouncer_enabled=True,
        ),
        gate=database_gate,
        direct_url=lambda _cfg: connections.DirectResolution(
            url="postgresql://provider:5432/meiva", is_direct=True
        ),
    )

    result = backup.dump_source(database, is_remote=remote, plane_url=plane_url, home=meiva_home())

    if remote:
        assert result == "postgresql://provider:5432/meiva"
    else:
        assert result == owner_conninfo(
            pg_admin_url(owned_pg, home=meiva_home()), database="meiva", owner="meiva"
        )


@pytest.mark.parametrize("direct", [False, True])
def test_base_db_connect_and_pool_dial_one_url_with_direct_escape(
    tmp_path: Path, db_url: str, database_gate: ProcessDbGate, direct: bool
) -> None:
    """Dial the access URL or its recorded direct port with pooling-safe policy."""
    import base.db

    real_port = int(str(conninfo_to_dict(db_url)["port"]))
    configured_url = url_with_port(db_url, 1) if direct else db_url
    config = DbConfig(
        db_url=configured_url,
        db_sslmode="disable",
        db_pool_min_size=1,
        db_pool_max_size=2,
        pgbouncer_enabled=True,
    )
    (tmp_path / "start-intent.json").write_text(
        json.dumps(
            {
                "record": {
                    "ports": {
                        "pgbouncer": 1 if direct else real_port,
                        "postgres": real_port if direct else 1,
                    },
                    "gateway_home": str(tmp_path),
                    "created_at": "2026-01-01T00:00:00Z",
                }
            }
        )
    )
    resolver = ClusterDirectUrl(lambda: "127.0.0.1", home=tmp_path)

    with base.db.connect(
        config=config, gate=database_gate, direct=direct, direct_url=resolver
    ) as conn:
        assert conn.info.port == real_port
        assert conn.prepare_threshold is None
        row = conn.execute(
            "SELECT current_setting('statement_timeout'), current_setting('application_name')"
        ).fetchone()
        assert row == ("1min", "" if direct else database_gate.application_name())

    db_pool = base.db.pool(config=config, gate=database_gate, direct=direct, direct_url=resolver)
    db_pool.open()
    try:
        with db_pool.connection() as conn:
            assert conn.info.port == real_port
            assert conn.prepare_threshold is None
            row = conn.execute(
                "SELECT current_setting('statement_timeout'), current_setting('application_name')"
            ).fetchone()
            assert row == ("1min", "" if direct else database_gate.application_name())
    finally:
        db_pool.close()

    if direct:
        with base.db.connect(
            config=config,
            gate=database_gate,
            direct=True,
            unbounded=True,
            direct_url=resolver,
        ) as conn:
            assert conn.info.port == real_port
            assert conn.prepare_threshold is None
            row = conn.execute("SELECT current_setting('statement_timeout')").fetchone()
            assert row == ("0",)
