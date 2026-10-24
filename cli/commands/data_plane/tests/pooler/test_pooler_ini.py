# meiva:note
# The pooler's configuration text: transaction pooling with SCRAM client auth, the configured
# client cap, binds on loopback plus the declared address only, and the statement ceiling every
# pooled backend is born with.

from __future__ import annotations

import dataclasses

import pytest

from cli.commands.data_plane.pooler.ini import IniInputs, listen_port, render_ini

_INPUTS = IniInputs(
    db_name="meiva_main",
    pg_port=5433,
    socket_dir="/run/meiva/pg-sock",
    listen_addrs=("127.0.0.1",),
    listen_port=6433,
    userlist_path="/home/meiva/pgbouncer/userlist.txt",
    logfile="/home/meiva/pgbouncer/pgbouncer.log",
    pidfile="/home/meiva/pgbouncer/pgbouncer.pid",
    max_client_conn=2000,
    admin_user="meiva_pooler_admin",
    statement_timeout_sql="SET statement_timeout = 60000",
)


def _lines(inputs: IniInputs = _INPUTS) -> list[str]:
    return render_ini(inputs).splitlines()


# meiva:note
# The client cap is configuration, not a module constant: one fixed 500 sat below the fleet's
# summed client-pool maxima and ran out under a single burst (2026-10-17 19:17).
@pytest.mark.parametrize("cap", [500, 2000, 12345])
def test_the_configured_client_cap_is_rendered(cap: int) -> None:
    assert f"max_client_conn = {cap}" in _lines(dataclasses.replace(_INPUTS, max_client_conn=cap))


def test_the_database_entry_forwards_over_the_owner_only_socket_with_the_birth_ceiling() -> None:
    assert (
        "meiva_main = host=/run/meiva/pg-sock port=5433 dbname=meiva_main "
        "connect_query='SET statement_timeout = 60000'"
    ) in _lines()


def test_clients_authenticate_with_scram_against_the_userlist_in_transaction_mode() -> None:
    lines = _lines()

    assert "auth_type = scram-sha-256" in lines
    assert "auth_file = /home/meiva/pgbouncer/userlist.txt" in lines
    assert "pool_mode = transaction" in lines
    assert "listen_port = 6433" in lines
    assert not any("trust" in line for line in lines)


# meiva:note
# The reset is one unquoted statement: pgbouncer runs the value verbatim, so a quoted value
# reaches Postgres as a syntax error and a second statement is rejected inside the implicit
# transaction transaction pooling wraps it in. `always = 0` keeps it to the window where a
# client vanished mid-transaction, so a client's own SETs survive between its transactions.
def test_the_release_reset_is_one_unquoted_discard_all_that_is_not_always_run() -> None:
    lines = _lines()

    assert "server_reset_query = DISCARD ALL" in lines
    assert "server_reset_query_always = 0" in lines


def test_the_admin_console_belongs_to_the_userlist_only_entry() -> None:
    lines = _lines()

    assert {"admin_users = meiva_pooler_admin", "stats_users = meiva_pooler_admin"} <= set(lines)


def test_connection_logging_is_quiet_and_the_files_are_this_homes() -> None:
    lines = _lines()

    assert {"log_connections = 0", "log_disconnections = 0"} <= set(lines)
    assert "logfile = /home/meiva/pgbouncer/pgbouncer.log" in lines
    assert "pidfile = /home/meiva/pgbouncer/pgbouncer.pid" in lines


# meiva:note
# The `0.0.0.0` in the assertion is the value the pooler must not bind, not a bind the test opens.
def test_the_binds_are_exactly_the_addresses_given_never_all_interfaces() -> None:
    both = dataclasses.replace(_INPUTS, listen_addrs=("127.0.0.1", "10.0.0.5"))

    assert "listen_addr = 127.0.0.1" in _lines()
    assert "listen_addr = 127.0.0.1, 10.0.0.5" in _lines(both)
    assert not any("0.0.0.0" in line or "= *" in line for line in _lines(both))  # noqa: S104


def test_a_pooler_with_no_address_to_listen_on_is_refused() -> None:
    with pytest.raises(ValueError, match="listen address"):
        render_ini(dataclasses.replace(_INPUTS, listen_addrs=()))


@pytest.mark.parametrize("cap", [0, -1])
def test_a_pooler_with_no_client_slots_is_refused(cap: int) -> None:
    with pytest.raises(ValueError, match="client cap"):
        render_ini(dataclasses.replace(_INPUTS, max_client_conn=cap))


def test_the_port_a_rendered_file_listens_on_reads_back() -> None:
    assert listen_port(render_ini(_INPUTS)) == 6433


@pytest.mark.parametrize(
    "text",
    ["[pgbouncer]\nlisten_port = 0\n", "[pgbouncer]\nlisten_port = 70000\n"],
)
def test_a_port_outside_the_valid_range_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="invalid owned PgBouncer listen port"):
        listen_port(text)


@pytest.mark.parametrize(
    "text",
    ["", "[databases]\nx = y\n", "[pgbouncer]\nlisten_port = abc\n", "not an ini at all"],
)
def test_a_file_that_names_no_port_is_refused(text: str) -> None:
    with pytest.raises(ValueError, match="invalid owned PgBouncer listen port"):
        listen_port(text)
