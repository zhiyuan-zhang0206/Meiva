# meiva:note
# The window names every active impersonation lease before it stops the cluster, in the same words
# under --dry-run.
# meiva:link cli/docs/fleet-update-window.meiva.okf.md
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cli import fleet_update
from cli.commands.management import fleet_leases
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
    assert cluster.lease_reads == [0], "read after an effect was already accepted"
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


def test_no_active_lease_is_one_line_and_never_waits(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    before = cluster.now
    assert run("down", "--lease-confirm-seconds", "30") == 0
    out = capsys.readouterr().out
    assert "leases: no active impersonation lease" in out
    assert "the stop ends every one of them" not in out
    assert cluster.now == before


# meiva:note
# The countdown is bounded -- two lines at least, one a second at most -- so progress is visible
# without flooding the log.
def test_the_confirm_window_holds_the_stops_open(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    before = cluster.now
    assert run("down", "--lease-confirm-seconds", "30") == 0
    out = capsys.readouterr().out
    assert cluster.now - before == 30.0
    assert "leases: 30s left: release or renew them, or let the stop end them" in out
    assert "leases: 5s left: release or renew them, or let the stop end them" in out
    assert 2 <= out.count("s left: release or renew them") <= 30
    assert out.index("leases: 5s left:") < out.index(" stop --yes ")


def test_the_dry_run_names_the_leases_and_does_not_wait(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.leases = list(_LEASES)
    before = cluster.now
    assert run("down", "--dry-run", "--lease-confirm-seconds", "30") == 0
    out = capsys.readouterr().out
    assert _lease(_LEASES[0]) in out
    assert "leases: dry run: not waiting 30s" in out
    assert " left: " not in out and cluster.now == before
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


def test_the_gateway_runs_the_shipped_program_source() -> None:
    owner = Path(fleet_update.__file__).parent / "commands" / "management" / "fleet_leases.py"
    assert owner.read_text(encoding="utf-8") == fleet_update.leases_program()


class _Cursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[tuple[Any, ...]], statements: list[str]) -> None:
        self._rows, self._statements = rows, statements

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> _Cursor:
        self._statements.append(sql)
        return _Cursor(self._rows)


class _Database:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self.statements: list[str] = []
        self._connection = _Connection(rows, self.statements)

    def connect(self, **kwargs: Any) -> _Connection:
        assert kwargs == {"autocommit": True}
        return self._connection


def test_the_reader_returns_the_active_leases_and_no_secret_columns() -> None:
    database = _Database([(7, 4, "Codex: db-work", datetime(2026, 10, 19, 23, 10, tzinfo=UTC))])
    assert fleet_leases.active_leases(database) == [
        {
            "agent_id": 7,
            "session_id": 4,
            "executor": "Codex: db-work",
            "expires_at": "2026-10-19T23:10:00+00:00",
        }
    ]
    sql = database.statements[0].lower()
    assert "from agent_impersonations" in sql and "status='active'" in sql
    assert not [
        column for column in ("token_hash", "handoff_document", "process_metadata") if column in sql
    ]
