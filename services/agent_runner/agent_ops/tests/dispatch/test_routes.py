# meiva:note
# `dispatch` routes each operation kind to its real operation and serializes the outcome.

from __future__ import annotations

from dataclasses import replace

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine import MachineIdentityReader
from ops.env_audit import read_env_write_records
from services.agent_runner.agent_ops.tests.ops_support import Ops, config_write


async def test_config_read_reports_this_machine(
    ops: Ops, machine_identity: MachineIdentityReader
) -> None:
    status, result = await ops.dispatch("config_read", {})

    assert status == "completed"
    assert result["machine"] == machine_identity.name()
    assert isinstance(result["host_fields"], dict)


async def test_config_write_applies_the_overrides_and_audits_actor_and_trace(ops: Ops) -> None:
    payload = {**config_write("trace-9", value=5), "actor": "user_session:administrator"}

    status, result = await ops.dispatch("config_write", payload)

    assert status == "completed"
    assert result["applied"] is True
    assert result["results"] == {"ops_concurrency": {"ok": True, "reason": None}}
    assert result["restart_required"] == ["ops"]
    (record,) = [
        r for r in read_env_write_records(10, ops.authority.env_path) if r["trace_id"] == "trace-9"
    ]
    assert record["actor"] == "user_session:administrator"
    assert record["keys_written"] == ["MEIVA_OPS_CONCURRENCY"]


async def test_config_write_reports_a_refused_field_without_writing(ops: Ops) -> None:
    status, result = await ops.dispatch(
        "config_write", {"overrides": {"no_such_field": 1}, "trace_id": "trace-refused"}
    )

    assert status == "completed"
    assert result["applied"] is False
    results = result["results"]
    assert isinstance(results, dict)
    assert results["no_such_field"]["ok"] is False
    assert "no_such_field" in results["no_such_field"]["reason"]
    assert [
        r
        for r in read_env_write_records(10, ops.authority.env_path)
        if r["trace_id"] == "trace-refused"
    ] == []


async def test_config_write_without_overrides_fails_on_validation(ops: Ops) -> None:
    status, result = await ops.dispatch("config_write", {})

    assert status == "failed"
    assert "overrides" in str(result["error"])


async def test_config_audit_read_returns_the_last_records_newest_first(ops: Ops) -> None:
    for index in range(3):
        await ops.dispatch("config_write", config_write(f"audit-{index}", value=3 + index))

    status, result = await ops.dispatch("config_audit_read", {"last": 2})

    assert status == "completed"
    records = result["records"]
    assert isinstance(records, list)
    assert [record["trace_id"] for record in records] == ["audit-2", "audit-1"]


async def test_config_audit_read_rejects_out_of_range_last(ops: Ops) -> None:
    status, result = await ops.dispatch("config_audit_read", {"last": 201})

    assert status == "failed"
    assert "last" in str(result["error"])


async def test_status_probe_serializes_this_machines_status(
    ops: Ops, machine_identity: MachineIdentityReader
) -> None:
    status, result = await ops.dispatch("status_probe", {})

    assert status == "completed"
    assert result["machine_name"] == machine_identity.name()
    assert result["running_sha"] is None
    assert result["schema_mismatch"] is None


async def test_status_probe_completes_degraded_when_its_pool_cannot_serve(
    ops: Ops, machine_identity: MachineIdentityReader
) -> None:
    closed: ConnectionPool = ConnectionPool(open=False)

    status, result = await replace(ops, pool=closed).dispatch("status_probe", {})

    assert status == "completed"
    assert result["machine_name"] == machine_identity.name()
    assert result["paused"] is True
    assert result["paused_reason"] == "no_state"
    assert isinstance(result["schema_mismatch"], dict)
    assert result["schema_mismatch"]["kind"] == "unavailable"


async def test_inventory_read_reports_this_machine(
    ops: Ops, machine_identity: MachineIdentityReader
) -> None:
    status, result = await ops.dispatch("inventory_read", {})

    assert status == "completed"
    assert result["machine"] == machine_identity.name()


@pytest.mark.parametrize(
    "kind", ["shell_probe", "shell_kill", "shell_capture", "agent_skill_view", "inventory_write"]
)
async def test_a_payload_missing_its_fields_fails_before_the_operation(ops: Ops, kind: str) -> None:
    status, result = await ops.dispatch(kind, {})

    assert status == "failed"
    assert "Field required" in str(result["error"])


async def test_an_unknown_kind_fails(ops: Ops) -> None:
    status, result = await ops.dispatch("bogus_kind", {})

    assert status == "failed"
    assert "unknown kind" in str(result["error"])


async def test_agent_skill_view_requires_the_native_pool(ops: Ops) -> None:
    with pytest.raises(RuntimeError, match="agent skill view requires the native DB pool"):
        await replace(ops, pool=None).dispatch("agent_skill_view", {"agent_id": 1})
