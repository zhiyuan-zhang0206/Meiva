from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import psycopg

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.impersonation import history as history
from base.agents.impersonation import manifest as capture
from base.agents.impersonation.manifest import (
    capture_local_event,
    close_local_participant_admission,
    seal_local_participant,
)
from base.agents.sdk.tally import SdkCallTally
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.agents.tests.capture_inputs import capture_rows, capture_state, open_capture, sdk_event
from base.agents.tests.capture_inputs import lease as lease
from base.agents.tests.capture_inputs import owner as owner
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry import Event
from meiva.sdk_surface import metering, process_context, skill_sources


class _CaptureOwner:
    def __init__(self, gate: capture.LocalCaptureGate) -> None:
        self.gate = gate

    def admit_sdk_call(self) -> capture.LocalCaptureAdmission | None:
        admission = self.gate.admit()
        if admission is None:
            raise RuntimeError("external attachment is closing")
        return admission

    def capture_local_event(self, event: Event) -> Event:
        return capture_local_event(event, gate=self.gate)


async def test_concurrent_calls_capture_the_original_gate_after_context_rebind(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database_gate: ProcessDbGate,
) -> None:
    previous = open_capture(
        lease, owner.agent_id, "concurrent-original", database_gate=database_gate
    )
    current = open_capture(
        lease, owner.agent_id, "concurrent-replacement", database_gate=database_gate
    )
    previous_gate = capture.LocalCaptureGate(previous)
    current_gate = capture.LocalCaptureGate(current)

    entered = asyncio.Event()
    release = asyncio.Event()
    started = 0

    async def held() -> None:
        nonlocal started
        started += 1
        if started == 2:
            entered.set()
        await release.wait()

    namespace = SimpleNamespace(held=held, __all_for_meiva__=["held"])
    root = ModuleType("receipt_root")
    root.__dict__.update(receipt_probe=namespace, __all_for_meiva__=["receipt_probe"])
    prior = getattr(meiva, "context", None)
    tally = SdkCallTally()
    context = MeivaContext(
        identity=AgentIdentity(owner.agent_id, True),
        sdk_calls=tally,
        sdk_capture=_CaptureOwner(previous_gate),
    )
    process_context.bind_context(context)
    sampling = fixed_sampling_owner()
    ledger = metering.install(sampling, clients=None, surface=metering.read_surface(root))
    calls = [asyncio.create_task(namespace.held()) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert previous_gate.in_flight == 2
        assert not close_local_participant_admission(previous_gate, timeout=0)
        process_context.bind_context(replace(context, sdk_capture=_CaptureOwner(current_gate)))
        release.set()
        await asyncio.gather(*calls)
        assert previous_gate.in_flight == current_gate.in_flight == 0
        assert capture_state(db_conn, previous) == ("sealed",)
        assert capture_state(db_conn, current) == ("open",)
        assert capture_rows(db_conn, lease["id"], previous.source_key) == 2
        assert capture_rows(db_conn, lease["id"], current.source_key) == 0
        assert tally.snapshot() == {"receipt_probe.held": 2}
    finally:
        release.set()
        await asyncio.gather(*calls, return_exceptions=True)
        metering.uninstall(ledger)
        sampling.close()
        if prior is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(prior)
        close_local_participant_admission(current_gate, timeout=0)
        seal_local_participant(current_gate)


def test_sdk_skill_read_captures_borrowed_audit_source_explicitly(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    tmp_path: Path,
    database: Database,
    database_gate: ProcessDbGate,
) -> None:
    from base.agents.context.identity import ExternalLease

    participant = open_capture(
        lease, owner.agent_id, "explicit-skill-audit", database_gate=database_gate
    )
    gate = capture.LocalCaptureGate(participant)
    skill_dir = tmp_path / "capture_test"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: capture_test\ndescription: test\n---\n\n# Capture test\n"
    )
    prior = getattr(meiva, "context", None)
    borrowed = ExternalLease(
        agent_id=owner.agent_id,
        validate=lambda: owner.agent_id,
        config=lambda: None,
    )
    clients = ClientSet(database=lambda: database)
    process_context.bind_context(
        MeivaContext(
            identity=AgentIdentity(None, True, lease=borrowed),
            sdk_capture=_CaptureOwner(gate),
            clients=clients,
        )
    )
    try:
        with skill_sources.scoped(lambda: [tmp_path]):
            assert "# Capture test" in meiva.files.read(str(skill_dir / "SKILL.md"))
        rows = history.entries(participant.lease_id, db_conn)
        audits = [row for row in rows if row["kind"] == "api_event"]
        assert capture_rows(db_conn, lease["id"], participant.source_key) == 1
        assert len(audits) == 1
        event = audits[0]["payload"]
        assert event["event_name"] == "skill_invoked" and event["source"] == "self"
        assert event["attributes"]["skill"] == "capture_test"
        assert (
            event["attributes"]["impersonation_session"]
            == f"{owner.agent_id}:{lease['session_id']}"
        )
        assert close_local_participant_admission(gate, timeout=0)
        seal_local_participant(gate)
        assert capture_state(db_conn, participant) == ("sealed",)
    finally:
        clients.close()
        if prior is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(prior)


# meiva:note
# A temporary `BEFORE INSERT` trigger on `agent_impersonation_entries` raises SQLSTATE `08006`
# (`connection_failure`, mapped by psycopg to a retryable `psycopg.OperationalError`) so the
# event-insert transaction fails while the follow-up failure-recording transaction against
# `agent_impersonation_event_participants` and `agent_impersonations` succeeds on real Postgres.
# meiva:link base/agents/impersonation/docs/impersonation.meiva.okf.md
def test_known_capture_database_outage_retains_the_failed_receipt_recovery(
    db_conn: psycopg.Connection[Any],
    owner: RuntimeIncarnation,
    lease: dict[str, Any],
    database_gate: ProcessDbGate,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    local_machine = machine_identity.name()
    participant = open_capture(
        lease, owner.agent_id, "known-capture-outage", database_gate=database_gate
    )
    gate = capture.LocalCaptureGate(participant)
    with participant.db.write_transaction() as conn:
        conn.execute(
            """
            CREATE OR REPLACE FUNCTION fail_impersonation_entry_insert() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'temporary connection loss' USING ERRCODE = '08006';
            END;
            $$;
            CREATE TRIGGER fail_impersonation_entry_insert_trig
            BEFORE INSERT ON agent_impersonation_entries
            FOR EACH ROW EXECUTE FUNCTION fail_impersonation_entry_insert();
            """
        )
    try:
        with capture.admitted_local_sdk_call(gate.admit) as admission:
            assert admission is not None
            tagged = admission.capture(sdk_event(owner.agent_id, "known-outage"))
            assert tagged.attributes["impersonation_session"] == f"{owner.agent_id}:0"
            assert not close_local_participant_admission(gate, timeout=0)
    finally:
        with participant.db.write_transaction() as conn:
            conn.execute(
                """
                DROP TRIGGER IF EXISTS fail_impersonation_entry_insert_trig
                ON agent_impersonation_entries;
                DROP FUNCTION IF EXISTS fail_impersonation_entry_insert();
                """
            )
    assert gate.in_flight == 0
    assert capture_state(db_conn, participant) == ("failed",)
    assert capture_rows(db_conn, lease["id"], participant.source_key) == 0
    assert (
        history.resolve(
            Database.from_settings(gate=database_gate),
            owner.agent_id,
            0,
            local_machine=local_machine,
        )["events_completed_at"]
        is None
    )
