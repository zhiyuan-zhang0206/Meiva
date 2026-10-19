from __future__ import annotations

from typing import Any

import psycopg
import pytest
from fastapi import HTTPException

from base.api_contracts.agent_contract import Harness
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.db import Database
from gateway.cluster.harness_probe import availability_rows, launchable, probe_harnesses
from ops.rpc_schemas import HarnessProbeResult, HarnessProbeRow, OpKind
from tests.fixtures.gateway_config import gateway_test_client

REMOTE = "remote-host"

_WIRE: dict[str, Any] = {
    "machine": REMOTE,
    "harnesses": [
        {
            "harness": "meiva",
            "status": "available",
            "reason": "built-in runtime; needs no external CLI or login",
        },
        {
            "harness": "claude_code",
            "status": "available",
            "reason": "signed-in account recorded in ~/.claude.json",
            "binary": "/usr/local/bin/claude",
            "on_path": True,
        },
        {
            "harness": "codex",
            "status": "unknown",
            "reason": "installed; login state unknown: no Codex configuration found",
            "binary": "/home/agent/.local/bin/codex",
            "on_path": False,
        },
        {"harness": "antigravity", "status": "unavailable", "reason": "not installed"},
    ],
}


def _seed(conn: psycopg.Connection, name: str, role: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) VALUES (%s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role",
            (name, role, f"http://{name}.invalid:8000"),
        )
    conn.commit()


class _Dispatch:
    def __init__(self, wire: dict[str, Any]) -> None:
        self.wire = wire
        self.calls: list[tuple[str, OpKind, dict[str, Any]]] = []

    async def __call__(
        self, _db: Database, target: str, kind: OpKind, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((target, kind, payload))
        return self.wire


# meiva:note
# The probe runs where the agent would be born, so the gateway dials that machine's
# runner instead of reading its own box, and the probe's own sentence comes back
# unchanged — it is what the form shows.
# meiva:link base/agents/docs/harness.meiva.okf.md
@pytest.mark.asyncio
async def test_a_remote_machine_answers_from_its_own_runner(
    db_conn: psycopg.Connection,
    database: Database,
    set_machine_identity: MachineIdentitySetter,
) -> None:
    _seed(db_conn, REMOTE, ["agent-runner"])
    local: MachineIdentity = set_machine_identity(role="gateway", name="gw-only")
    dispatch = _Dispatch(_WIRE)

    probed = await probe_harnesses(REMOTE, db=database, local_machine=local, dispatch=dispatch)

    assert dispatch.calls == [(REMOTE, "harness_probe", {})]
    assert [row.harness for row in probed.harnesses] == list(Harness)
    assert probed.harnesses[2].reason == _WIRE["harnesses"][2]["reason"]


# meiva:note
# Installed-but-unstartable must not read as a working choice, and an uninstalled
# harness must not read as launchable either. This build ships no launcher for an
# external CLI yet, so even a signed-in one is not launchable; the day a harness
# leaves LAUNCHABLE_HARNESSES' complement, this test changes with it.
# meiva:link base/agents/docs/harness.meiva.okf.md
def test_launchable_needs_a_launcher_and_a_machine_that_can_run_it() -> None:
    available = HarnessProbeRow(harness=Harness.CLAUDE_CODE, status="available", reason="signed in")
    unknown = HarnessProbeRow(harness=Harness.CLAUDE_CODE, status="unknown", reason="login unknown")
    missing = HarnessProbeRow(
        harness=Harness.CLAUDE_CODE, status="unavailable", reason="not installed"
    )
    builtin = HarnessProbeRow(harness=Harness.MEIVA, status="available", reason="built in")

    assert launchable(available) is False
    assert launchable(unknown) is False
    assert launchable(missing) is False
    assert launchable(builtin) is True


def test_rows_carry_status_and_reason_through() -> None:
    probed = availability_rows(
        HarnessProbeResult(
            machine=REMOTE,
            harnesses=[HarnessProbeRow.model_validate(row) for row in _WIRE["harnesses"]],
        )
    )
    assert [row.status for row in probed] == ["available", "available", "unknown", "unavailable"]
    assert [row.reason for row in probed] == [row["reason"] for row in _WIRE["harnesses"]]


@pytest.mark.asyncio
async def test_unknown_machine_is_404(
    database: Database, set_machine_identity: MachineIdentitySetter
) -> None:
    local: MachineIdentity = set_machine_identity(role="gateway", name="gw-only")
    with pytest.raises(HTTPException) as raised:
        await probe_harnesses(
            "ghost-host", db=database, local_machine=local, dispatch=_Dispatch(_WIRE)
        )
    assert raised.value.status_code == 404
    assert "unknown machine" in str(raised.value.detail)


@pytest.mark.asyncio
async def test_a_remote_box_without_a_runner_cannot_report(
    db_conn: psycopg.Connection,
    database: Database,
    set_machine_identity: MachineIdentitySetter,
) -> None:
    _seed(db_conn, REMOTE, ["gateway"])
    local: MachineIdentity = set_machine_identity(role="gateway", name="gw-only")
    with pytest.raises(HTTPException) as raised:
        await probe_harnesses(REMOTE, db=database, local_machine=local, dispatch=_Dispatch(_WIRE))
    assert raised.value.status_code == 503
    assert "no agent-runner ops server" in str(raised.value.detail)


# meiva:note
# A gateway-only box is its own answer: the probe reads the filesystem it sits on
# instead of dialing a runner that does not exist.
# meiva:link base/agents/docs/harness.meiva.okf.md
@pytest.mark.asyncio
async def test_this_box_is_probed_here_when_it_runs_no_runner(
    set_machine_identity: MachineIdentitySetter, database: Database
) -> None:
    local: MachineIdentity = set_machine_identity(role="gateway", name="gw-only")
    dispatch = _Dispatch(_WIRE)

    probed = await probe_harnesses("gw-only", db=database, local_machine=local, dispatch=dispatch)

    assert dispatch.calls == []
    assert [row.harness for row in probed.harnesses] == list(Harness)
    assert probed.harnesses[0].status == "available"


# meiva:note
# A fresh cache entry answers without any probe, so the route echoes exactly what
# was stored rather than re-reading the box.
# meiva:link base/agents/docs/harness.meiva.okf.md
def test_route_serves_the_cached_rows(set_machine_identity: MachineIdentitySetter) -> None:
    machine: MachineIdentity = set_machine_identity(role="gateway", name="gw-only")
    seeded = availability_rows(
        HarnessProbeResult(
            machine="gw-only",
            harnesses=[HarnessProbeRow(harness=Harness.MEIVA, status="available", reason="seeded")],
        )
    )
    with gateway_test_client(machine=machine) as client:
        client.app.state.harness_cache.store("gw-only", seeded)
        resp = client.get("/api/cluster/machines/gw-only/harnesses")
    assert resp.status_code == 200, resp.text
    assert resp.json() == [
        {"harness": "meiva", "status": "available", "reason": "seeded", "launchable": True}
    ]


def test_route_reports_every_known_harness(
    set_machine_identity: MachineIdentitySetter,
) -> None:
    machine: MachineIdentity = set_machine_identity(role="gateway", name="gw-only")
    with gateway_test_client(machine=machine) as client:
        resp = client.get("/api/cluster/machines/gw-only/harnesses")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert [row["harness"] for row in rows] == ["meiva", "claude_code", "codex", "antigravity"]
    assert rows[0]["launchable"] is True
    assert all(row["reason"] for row in rows)
