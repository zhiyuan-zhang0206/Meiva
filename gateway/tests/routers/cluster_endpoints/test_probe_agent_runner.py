# meiva:note
# probe_agent_runner maps transport and reached-host outcomes distinctly, over a real dial.

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from gateway.cluster import status as status_router
from gateway.cluster.roster.roster_rows import RosterRowSpec
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.cluster_endpoints.cluster_support import roster_env
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    refused,
    runner_ops,
)

_UP = datetime(2026, 5, 24, tzinfo=UTC)


def _row(
    url: str | None, *, description: str | None = None, stopped_at: datetime | None = None
) -> RosterRowSpec:
    return RosterRowSpec(
        name="runner-a",
        gateway_url=url,
        role=["agent-runner"],
        up_since_at=_UP,
        description=description,
        stopped_at=stopped_at,
    )


def _status(**extra: object) -> dict[str, object]:
    return {
        "machine_name": "runner-a",
        "serve_gateway": False,
        "serve_agent_runner": True,
        "paused": False,
        **extra,
    }


# meiva:note
# The roster row is the address authority for the fan-out: a row with no URL is offline
# even when the registry could resolve the machine — it is not re-read from Postgres.
@pytest.mark.asyncio
async def test_a_missing_registered_url_is_offline_without_a_dial(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(db_conn, "runner-a", {"status_probe": completed(_status())}):
        row = await status_router.probe_agent_runner(
            roster_env(database, machine_identity, home=tmp_path), _row(None)
        )

    assert row.online is False


# meiva:note
# Retry is inside one roster deadline: a blackholed first attempt is cancelled at the
# configured budget rather than getting a second full timeout window. Configuration
# construction is test setup, outside the measured probe interval.
@pytest.mark.asyncio
async def test_a_blackhole_stays_inside_the_single_total_budget(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    release = threading.Event()

    def never_answers(call: OpsCall) -> OpsReply:
        del call
        release.wait(timeout=5.0)
        return completed({})

    with runner_ops(db_conn, "runner-a", {"status_probe": never_answers}) as runner:
        env = roster_env(database, machine_identity, home=tmp_path, probe_timeout_s=0.05)
        target = _row(runner.url)
        started = time.monotonic()
        try:
            row = await status_router.probe_agent_runner(env, target)
        finally:
            release.set()
        elapsed = time.monotonic() - started

    assert row.online is False
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_an_unreachable_runner_is_offline_and_threads_the_row_through(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    stopped = datetime(2026, 5, 25, tzinfo=UTC)
    with runner_ops(db_conn, "runner-a", {"status_probe": refused(404, "gone")}) as runner:
        r = await status_router.probe_agent_runner(
            roster_env(database, machine_identity, home=tmp_path),
            _row(runner.url, description="voice IO + browser", stopped_at=stopped),
        )

    assert r.online is False
    assert r.paused is None
    assert r.description == "voice IO + browser"
    # meiva:note
    # offline + stopped_at threads through so the UI can show "stopped" not "offline"
    assert r.stopped_at == stopped


# meiva:note
# The runner's status_probe response carries paused and head_sha; the probe threads them
# onto MachineStatus so the roster can show them (and compute drift).
@pytest.mark.asyncio
async def test_a_reachable_runner_reports_its_own_paused_verdict_and_head_sha(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    answer = completed(_status(paused=True, head_sha="def5678"))
    with runner_ops(db_conn, "runner-a", {"status_probe": answer}) as runner:
        r = await status_router.probe_agent_runner(
            roster_env(database, machine_identity, home=tmp_path),
            _row(runner.url, description="voice IO + browser"),
        )

    assert r.online is True
    assert r.paused is True
    assert r.head_sha == "def5678"
    assert r.description == "voice IO + browser"


# meiva:note
# A reachable host whose status_probe body does NOT validate as ClusterStatus (a
# version-skewed / wrong server) lands in the documented online=True + paused=None abnormal
# state — never disguised as a determinate paused verdict, and distinct from offline.
@pytest.mark.asyncio
async def test_a_malformed_response_is_online_unknown(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    # meiva:note
    # Missing the required serve_gateway / serve_agent_runner fields.
    answer = completed({"machine_name": "runner-a", "paused": True})
    with runner_ops(db_conn, "runner-a", {"status_probe": answer}) as runner:
        r = await status_router.probe_agent_runner(
            roster_env(database, machine_identity, home=tmp_path), _row(runner.url)
        )

    assert r.online is True
    assert r.paused is None


# meiva:note
# The deadline is `gateway.status_probe_timeout_seconds`, not a hardcoded literal — a
# slow-but-healthy runner must not read offline because its snapshot outgrew a fixed 3s budget
# (task #1200). A runner slower than the configured budget reads offline within it.
def test_the_probe_budget_is_the_configured_status_probe_timeout(
    db_conn: psycopg.Connection,
) -> None:
    release = threading.Event()

    def slow(call: OpsCall) -> OpsReply:
        del call
        release.wait(timeout=5.0)
        return completed(_status())

    with (
        runner_ops(db_conn, "runner-a", {"status_probe": slow}),
        gateway_test_client(fields={"status_probe_timeout_seconds": 0.1}) as client,
    ):
        started = time.monotonic()
        try:
            roster = client.get("/api/cluster/roster?fresh=true").json()
        finally:
            release.set()
        elapsed = time.monotonic() - started

    row = next(m for m in roster if m["name"] == "runner-a")
    assert row["online"] is False
    assert elapsed < 1.5
