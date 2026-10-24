# meiva:note
# The Postgres plan: authentication files, launch command line and start steps are values.
#
# What the always-authenticated pg_hba admits, what the postmaster is launched with and which
# steps a start performs are asserted on what the pure functions return; the real postmaster
# these describe is started by `test_pg_start_native.py`.

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from cli.commands.data_plane.instance.pg_plan import (
    AwaitReachableBind,
    CheckArchive,
    EnsureData,
    LaunchPostmaster,
    PgIdentity,
    PgLaunch,
    ProveAuthentication,
    WriteAuthentication,
    bind_refusal,
    hba_dial_verdict,
    pg_hba_body,
    pg_ident_body,
    pg_launch_argv,
    plan_pg_start,
)

_IDENTITY = PgIdentity(
    os_user="operator", monitor_role="meiva_monitor", monitor_map="meiva_monitor"
)
_LOOPBACK_LINES = [
    "local all operator peer",
    "local all meiva_monitor peer map=meiva_monitor",
    "local all all scram-sha-256",
    "host all all 127.0.0.1/32 scram-sha-256",
    "host all all ::1/128 scram-sha-256",
]


def test_a_single_box_hba_is_loopback_only_and_never_trusts() -> None:
    body = pg_hba_body(_IDENTITY, reachable_host=None, trusted_cidrs="")
    assert "trust" not in body
    assert body.splitlines() == _LOOPBACK_LINES
    assert body.endswith("\n")


def test_the_declared_reach_and_trusted_cidrs_join_as_scram_lines() -> None:
    body = pg_hba_body(_IDENTITY, reachable_host="10.0.0.5", trusted_cidrs="10.0.0.0/8")
    assert body.splitlines() == [
        *_LOOPBACK_LINES,
        "host all all 10.0.0.5/32 scram-sha-256",
        "host all all 10.0.0.0/8 scram-sha-256",
    ]


def test_trusted_cidrs_are_trimmed_and_blank_entries_dropped() -> None:
    body = pg_hba_body(
        _IDENTITY, reachable_host=None, trusted_cidrs=" 10.1.0.0/16 , ,192.168.0.0/24"
    )
    assert body.splitlines()[-2:] == [
        "host all all 10.1.0.0/16 scram-sha-256",
        "host all all 192.168.0.0/24 scram-sha-256",
    ]


def test_the_ident_map_admits_only_the_os_user_as_the_monitoring_role() -> None:
    assert pg_ident_body(_IDENTITY) == "meiva_monitor operator meiva_monitor\n"


def _launch(**changes: object) -> PgLaunch:
    base = PgLaunch(
        postgres_bin="/pg/bin/postgres",
        data=Path("/home/pg"),
        port=5433,
        listen_addrs=("127.0.0.1",),
        socket_dir=Path("/run/meiva-pg-x"),
        max_connections=500,
        tz_args=("-c", "timezone=UTC"),
        shm_args=("-c", "shared_memory_type=mmap"),
        archive_args=(),
    )
    return dataclasses.replace(base, **changes)


def test_the_postmaster_is_launched_directly_on_an_owner_only_socket() -> None:
    argv = pg_launch_argv(_launch())
    assert argv[:5] == ["/pg/bin/postgres", "-D", "/home/pg", "-p", "5433"]
    assert "unix_socket_permissions=0700" in argv
    assert "unix_socket_directories=/run/meiva-pg-x" in argv
    assert "listen_addresses=127.0.0.1" in argv
    assert "max_connections=500" in argv
    assert "pg_ctl" not in argv


def test_listen_addresses_follow_the_declared_bind_addresses_in_order() -> None:
    argv = pg_launch_argv(_launch(listen_addrs=("127.0.0.1", "10.0.0.5")))
    assert "listen_addresses=127.0.0.1,10.0.0.5" in argv


def test_pinned_settings_and_archive_arguments_close_the_command_line() -> None:
    archive = ("-c", "archive_mode=on", "-c", "archive_command=wal-g wal-push %p")
    argv = pg_launch_argv(_launch(archive_args=archive))
    assert argv[-len(archive) :] == list(archive)
    assert argv.index("timezone=UTC") < argv.index("shared_memory_type=mmap")
    assert argv.index("shared_memory_type=mmap") < argv.index("archive_mode=on")


def test_wal_g_off_adds_no_archive_arguments() -> None:
    assert not [arg for arg in pg_launch_argv(_launch()) if arg.startswith("archive_")]


def test_a_fresh_single_box_start_initializes_authenticates_launches_and_proves() -> None:
    assert plan_pg_start(owner_retained=False, off_box_host=None) == (
        EnsureData(),
        WriteAuthentication(),
        LaunchPostmaster(),
        ProveAuthentication(),
        CheckArchive(),
    )


def test_a_fresh_start_on_a_declared_reach_waits_for_the_address_before_launching() -> None:
    assert plan_pg_start(owner_retained=False, off_box_host="10.0.0.5") == (
        EnsureData(),
        WriteAuthentication(),
        AwaitReachableBind("10.0.0.5"),
        LaunchPostmaster(),
        ProveAuthentication(),
        CheckArchive(),
    )


def test_a_retained_postmaster_never_waits_for_an_address_it_already_holds() -> None:
    steps = plan_pg_start(owner_retained=True, off_box_host="10.0.0.5")
    assert not [step for step in steps if isinstance(step, AwaitReachableBind)]
    assert steps[-3:] == (LaunchPostmaster(), ProveAuthentication(), CheckArchive())


def test_authentication_is_written_before_anything_serves() -> None:
    for retained in (False, True):
        steps = plan_pg_start(owner_retained=retained, off_box_host="10.0.0.5")
        assert steps.index(WriteAuthentication()) < steps.index(LaunchPostmaster())
        assert steps.index(EnsureData()) < steps.index(WriteAuthentication())


def test_the_bind_refusal_names_the_address_and_the_retry() -> None:
    text = bind_refusal("postgres", "10.0.0.5", 60)
    assert "'10.0.0.5'" in text and "not assigned to any local interface" in text
    assert "after 60s" in text and "postgres cannot bind it" in text
    assert "private network" in text and "meiva start --all-services" in text
    assert "redis cannot bind it" in bind_refusal("redis", "10.0.0.5", 60)


@pytest.mark.parametrize(
    "error",
    [
        "connection failed: no password supplied",
        "FATAL: password authentication failed for user x",
    ],
)
def test_a_demanded_password_proves_the_hba(error: str) -> None:
    assert hba_dial_verdict("127.0.0.1", error) is None


def test_a_dial_that_connected_proves_a_trust_line() -> None:
    assert hba_dial_verdict("127.0.0.1", None) == "127.0.0.1: admitted without a password"


def test_any_other_failure_is_unproven_and_names_its_last_line() -> None:
    verdict = hba_dial_verdict(
        "/run/sock", 'connection to server failed\nrole "x" does not exist\n'
    )
    assert verdict == '/run/sock: role "x" does not exist'
    assert hba_dial_verdict("h", "") == "h: connection failed"
