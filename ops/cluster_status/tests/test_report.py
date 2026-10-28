# meiva:note
# The snapshot a host reports about itself, decided from the facts the shell read.
#
# Values in, `ClusterStatus` out: no database, no sessions and no pidfiles are involved, so each
# clause of the readiness verdict and each field of the payload is one assertion.

from __future__ import annotations

import pytest

from base.api_contracts.status import PausedReason
from base.host.resource.reading import ResourceSample
from ops.cluster_status.report import (
    ClusterStatus,
    DbSnapshot,
    StatusFacts,
    assemble_status,
    paused_reason,
    serving_gate_reason,
    unavailable_db_snapshot,
)
from ops.rpc_schemas import AgentSessionGroup, SessionInfo

_RESOURCE = ResourceSample(
    ts=1.0,
    cpu_pct=2.0,
    mem_used_gb=3.0,
    mem_total_gb=4.0,
    mem_pct=5.0,
    disk_used_gb=6.0,
    disk_total_gb=7.0,
    disk_pct=8.0,
)
_HEALTHY_DB = DbSnapshot(ok=True, agent_count=0, schema_mismatch=None)


def _facts(
    *,
    roles: frozenset[str] = frozenset({"agent-runner"}),
    sessions: tuple[SessionInfo, ...] = (),
    db: DbSnapshot = _HEALTHY_DB,
    serving: bool = True,
    agent_host_online: bool | None = None,
) -> StatusFacts:
    return StatusFacts(
        machine_name="runner-a",
        roles=roles,
        head_sha="head0001",
        running_sha="run00001",
        sessions=sessions,
        agent_host_online=agent_host_online,
        supervisor_online=True,
        db=db,
        resource=_RESOURCE,
        serving=serving,
    )


@pytest.mark.parametrize(
    ("db_ok", "serving", "expected"),
    [
        # meiva:note
        # A start in flight, a stop that has begun, or a failed start that parked the serving
        # gate short of `serving` while the database reads fine: the reason names the gate, so it
        # never reads like a failed database snapshot (task #3404).
        (True, False, "startup"),
        # meiva:note
        # An unreadable database snapshot is the verdict's first clause: it outranks every other
        # cause, including a parked gate.
        (False, True, "no_state"),
        (False, False, "no_state"),
        # meiva:note
        # No clause fired.
        (True, True, None),
    ],
)
def test_the_paused_reason_names_the_first_true_clause(
    db_ok: bool, serving: bool, expected: PausedReason | None
) -> None:
    assert paused_reason(db_ok=db_ok, serving=serving) == expected


def test_the_serving_gate_is_the_startup_clause_alone() -> None:
    assert serving_gate_reason(serving=False) == "startup"
    assert serving_gate_reason(serving=True) is None


@pytest.mark.parametrize(
    ("db", "serving", "reason"),
    [
        (_HEALTHY_DB, True, None),
        (_HEALTHY_DB, False, "startup"),
        (unavailable_db_snapshot("runner-a", "OperationalError"), True, "no_state"),
    ],
)
def test_paused_is_exactly_whether_a_clause_fired(
    db: DbSnapshot, serving: bool, reason: PausedReason | None
) -> None:
    status = assemble_status(_facts(db=db, serving=serving))
    assert status.paused is (reason is not None)
    assert status.paused_reason == reason


def test_an_unreadable_database_reports_why_the_schema_is_unchecked() -> None:
    snapshot = unavailable_db_snapshot("runner-a", "PoolTimeout")
    assert snapshot.ok is False
    assert snapshot.agent_count == 0
    assert snapshot.schema_mismatch is not None
    assert snapshot.schema_mismatch.kind == "unavailable"
    assert snapshot.schema_mismatch.machine == "runner-a"
    assert snapshot.schema_mismatch.detail == (
        "schema comparison unavailable: status database snapshot failed (PoolTimeout)"
    )


@pytest.mark.parametrize(
    ("roles", "gateway", "runner", "station"),
    [
        (frozenset({"gateway"}), True, False, False),
        (frozenset({"agent-runner"}), False, True, False),
        (frozenset({"gateway", "agent-runner"}), True, True, False),
        (frozenset({"observability-station"}), False, False, True),
    ],
)
def test_the_roles_become_three_independent_capability_flags(
    roles: frozenset[str], gateway: bool, runner: bool, station: bool
) -> None:
    status = assemble_status(_facts(roles=roles))
    assert (
        status.serve_gateway,
        status.serve_agent_runner,
        status.serve_observability_station,
    ) == (gateway, runner, station)


def test_the_snapshot_carries_the_facts_it_was_given() -> None:
    sessions = (
        SessionInfo(name="meiva-gateway"),
        SessionInfo(name="meiva-agent-7-shell-0"),
        SessionInfo(name="meiva-agent-7-shell-0-watcher"),
        SessionInfo(name="meiva-agent-12-shell-3"),
    )
    db = DbSnapshot(ok=True, agent_count=3, schema_mismatch=None)

    status = assemble_status(_facts(sessions=sessions, db=db, agent_host_online=True))

    assert status.machine_name == "runner-a"
    assert (status.head_sha, status.running_sha) == ("head0001", "run00001")
    assert status.agent_host_online is True
    assert status.supervisor_online is True
    assert status.agent_count == 3
    assert status.session_count == 4
    assert status.shell_count == 3
    assert status.schema_mismatch is None
    assert status.resource == _RESOURCE
    groups = [AgentSessionGroup.model_validate(group) for group in status.agent_groups]
    assert [(group.agent_id, len(group.shells)) for group in groups] == [(7, 2), (12, 1)]


def test_the_agent_host_liveness_of_a_gateway_stays_unknown() -> None:
    status = assemble_status(_facts(roles=frozenset({"gateway"}), agent_host_online=None))
    assert status.agent_host_online is None
    assert "restarter_online" not in status.model_dump()


def test_the_wire_form_round_trips() -> None:
    status = assemble_status(_facts(sessions=(SessionInfo(name="meiva-agent-7-shell-0"),)))
    assert ClusterStatus.model_validate(status.model_dump(mode="json")) == status
