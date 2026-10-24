# meiva:note
# The window names every active impersonation lease before it stops the cluster, in the same words
# under --dry-run.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import psycopg
import pytest

from cli.commands.management import fleet_leases
from cli.fleet_update.session import Pacing
from cli.tests.fleet_update.fleet_update_cluster import Cluster

_LEASES: list[dict[str, Any]] = [
    {
        "agent_id": 6161,
        "session_id": 4,
        "executor": "Codex: db-work",
        "expires_at": "2026-10-19T23:10:00+00:00",
    },
    {
        "agent_id": 6993,
        "session_id": 0,
        "executor": "claude",
        "expires_at": "2026-10-19T23:40:12+00:00",
    },
]


def _lease(lease: dict[str, Any]) -> str:
    return (
        f"leases:   agent {lease['agent_id']} session {lease['session_id']} "
        f"executor {lease['executor']!r} expires {lease['expires_at']}"
    )


# meiva:note
# The lease lines themselves, without the countdown and the dry-run note.
def _report(out: str) -> list[str]:
    return [
        line
        for line in out.splitlines()
        if line.startswith(("leases: ", "WARNING: active impersonation"))
        and " left: " not in line
        and "not waiting" not in line
    ]


@pytest.mark.parametrize("half", ["down", "run"])
def test_the_leases_are_named_before_the_first_stop(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], half: str
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    assert run(half) == 0
    out = capsys.readouterr().out
    assert f"leases: {len(_LEASES)} active impersonation lease(s):" in out
    for lease in _LEASES:
        assert _lease(lease) in out
    assert "the stop ends every one of them" in out and "aborted mid-task" in out
    # meiva:note
    # The helper gate fetches the target commit before anything stops; a lease read must still
    # precede every stop, which is the guarantee this test is about.
    before_first_stop = cluster.effects[: cluster.lease_reads[0]]
    assert "stop" not in [kind for kind, _ in before_first_stop], "read after a stop was accepted"
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


def test_no_active_lease_is_one_line_and_never_waits(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = env
    assert run("down", "--lease-confirm-seconds", "30") == 0
    out = capsys.readouterr().out
    assert "leases: no active impersonation lease" in out
    assert "the stop ends every one of them" not in out
    assert " left: " not in out


# meiva:note
# The shell holds the stops open for the whole window and says one line per tick before the first
# stop. The ticks themselves -- their count, their spacing and what each says -- are the plan's
# (`plan/tests`); this runs them for real at a half-second pace.
def test_the_confirm_window_holds_the_stops_open(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    cluster.pacing = Pacing(roster_poll_s=0.01, lease_tick_s=0.5)
    started = time.monotonic()
    assert run("down", "--lease-confirm-seconds", "1") == 0
    out = capsys.readouterr().out
    assert time.monotonic() - started >= 1.0
    assert out.count("s left: release or renew them, or let the stop end them") == 2
    assert "leases: 1s left: release or renew them, or let the stop end them" in out
    assert out.index("s left: release or renew them") < out.index(" stop --yes ")


def test_the_dry_run_names_the_leases_and_does_not_wait(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    assert run("down", "--dry-run", "--lease-confirm-seconds", "30") == 0
    out = capsys.readouterr().out
    assert _lease(_LEASES[0]) in out
    assert "leases: dry run: not waiting 30s" in out
    assert " left: " not in out
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == []


def test_both_paths_print_the_same_lease_report(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    assert run("down", "--dry-run") == 0
    dry = _report(capsys.readouterr().out)
    assert run("down") == 0
    assert _report(capsys.readouterr().out) == dry
    assert len(dry) == len(_LEASES) + 2


@pytest.mark.parametrize(
    "reply",
    [
        "LEASES failed: OperationalError: connection refused",
        "no json here at all",
    ],
)
def test_a_lease_read_that_fails_warns_and_the_update_goes_on(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], reply: str
) -> None:
    cluster, run = env
    cluster.lease_reply = reply
    assert run("down") == 0
    out = capsys.readouterr().out
    assert "WARNING: active impersonation leases were not read" in out
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


# meiva:note
# A shipped program that exited non-zero raises FailedError out of `s.run`; that must land on the
# same warning, never on the refusal that aborts the window.
def test_a_lease_program_that_exits_nonzero_warns_and_the_update_goes_on(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.lease_rc = 71
    assert run("down") == 0
    out = capsys.readouterr().out
    assert "WARNING: active impersonation leases were not read" in out
    assert "gw: exit 71" in out
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


def test_a_negative_window_is_refused_before_any_host_is_contacted(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    with pytest.raises(SystemExit) as refused:
        run("down", "--lease-confirm-seconds", "-1")
    assert refused.value.code == 2
    assert cluster.commands == [] and cluster.lease_reads == []


def test_up_never_reads_the_lease_table(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    assert run("down") == 0
    reads = len(cluster.lease_reads)
    assert run("up") == 0
    assert len(cluster.lease_reads) == reads


def _lease_row(conn: psycopg.Connection, *, status: str, executor: str) -> tuple[int, int]:
    created = conn.execute("INSERT INTO agents DEFAULT VALUES RETURNING id").fetchone()
    assert created is not None
    agent_id = int(created[0])
    conn.execute(
        "INSERT INTO agents_meta(id,status,machine,harness) VALUES(%s,'idling','lease-test','meiva')",
        (agent_id,),
    )
    lease = conn.execute(
        "INSERT INTO agent_impersonations(id,agent_id,source,machine,status,"
        "ttl_seconds,expires_at,executor_name,token_hash,handoff_document) "
        "VALUES(%s,%s,'external_agent:codex','lease-test',%s,300,%s,%s,'secret-token-hash',"
        '\'{"secret": "handoff"}\'::jsonb) RETURNING session_id',
        (str(uuid4()), agent_id, status, "2026-10-19T23:10:00+00:00", executor),
    ).fetchone()
    assert lease is not None
    conn.commit()
    return agent_id, int(lease[0])


# meiva:note
# The reader runs against the real table: only the leases still marked active, ordered by expiry,
# and none of the columns the log must not carry.
def test_the_reader_returns_the_active_leases_and_no_secret_columns(
    db_conn: psycopg.Connection,
) -> None:
    kept, session = _lease_row(db_conn, status="active", executor="Codex: db-work")
    _lease_row(db_conn, status="released", executor="ended")
    leases = fleet_leases.active_leases(db_conn)
    assert leases == [
        {
            "agent_id": kept,
            "session_id": session,
            "executor": "Codex: db-work",
            "expires_at": "2026-10-19T23:10:00+00:00",
        }
    ]
    assert all("secret" not in str(value) for lease in leases for value in lease.values())
