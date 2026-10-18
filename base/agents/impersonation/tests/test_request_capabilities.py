# meiva:note
# What a request declares, and what a renewed lease keeps. An explicit declaration
# is stored as given; an automatic request that declares nothing keeps the machine
# set and records `launcher-default`; a manual request that declares nothing gets
# the minimal set; a pre-vocabulary row renews without gaining a declaration, so
# NULL keeps meaning "the surface this lease was issued with".

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
import pytest

from base.agents import impersonation as leases
from base.agents.impersonation import sessions as sessions
from base.agents.impersonation.capabilities import MACHINE_PROFILE, MINIMAL_PROFILE
from base.agents.impersonation.history import resolve
from base.agents.impersonation.tests.impersonation_support import recorded_tree
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


@pytest.fixture
def owner(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentity
) -> RuntimeIncarnation:
    local_machine = machine_identity.name()
    agent_id = create_agent(db_conn)
    incarnation = RuntimeIncarnation(agent_id, uuid4(), uuid4())
    db_conn.execute(
        "INSERT INTO agents_meta(id,status,machine,runtime_generation,runtime_owner,"
        "runtime_kind,lease_expires_at) VALUES(%s,'idling',%s,%s,%s,'process',"
        "clock_timestamp()+interval '10 minutes')",
        (agent_id, local_machine, incarnation.generation, incarnation.owner),
    )
    db_conn.commit()
    return incarnation


def _request(
    owner: RuntimeIncarnation,
    *,
    bus: EventBus,
    authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    local_machine: str,
    automatic: bool,
    capabilities: Any = None,
) -> dict[str, Any]:
    if automatic:
        result = sessions.request(
            Database.from_settings(gate=database_gate),
            bus,
            owner.agent_id,
            name="Fix login",
            executor_name="Codex: thoughtful squirrel",
            provider="codex",
            thread_id=str(uuid4()),
            process_metadata=recorded_tree(),
            authority=authority,
            capabilities=capabilities,
            local_machine=local_machine,
        )
    else:
        result = leases.request(
            Database.from_settings(gate=database_gate),
            bus,
            owner.agent_id,
            caller=CallerIdentity(kind="external_agent", subject="codex"),
            authority=authority,
            name="Fix login",
            executor_name="Codex: thoughtful squirrel",
            relay_provider="codex",
            relay_thread_id=str(uuid4()),
            process_metadata=recorded_tree(),
            automatic=False,
            capabilities=capabilities,
            local_machine=local_machine,
        )
    return resolve(
        Database.from_settings(gate=database_gate),
        owner.agent_id,
        result["session_id"],
        local_machine=local_machine,
    )


def _activate(
    lease: dict[str, Any],
    owner: RuntimeIncarnation,
    *,
    database: Database,
    event_bus: EventBus,
    local_machine: str,
) -> None:
    leases.accept(
        database,
        event_bus,
        str(lease["id"]),
        owner.agent_id,
        owner,
        "Continue the login fix",
        local_machine=local_machine,
    )
    leases.activate(database, event_bus, str(lease["id"]), owner)


def test_a_manual_request_without_a_declaration_gets_the_minimal_set(
    owner: RuntimeIncarnation,
    *,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease = _request(
        owner,
        bus=event_bus,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
        automatic=False,
    )

    assert lease["capabilities"] == sorted(capability.value for capability in MINIMAL_PROFILE)
    assert lease["capability_declared_by"] == "caller-default"


def test_an_automatic_request_without_a_declaration_keeps_machine_and_says_so(
    owner: RuntimeIncarnation,
    emitted_events: EmittedEvents,
    *,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease = _request(
        owner,
        bus=event_bus,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
        automatic=True,
    )

    assert lease["capabilities"] == sorted(capability.value for capability in MACHINE_PROFILE)
    assert lease["capability_declared_by"] == "launcher-default"
    defaulted = emitted_events.attributes("impersonation_capability_defaulted")
    assert len(defaulted) == 1, "a silent default is the thing this event exists to prevent"
    assert defaulted[0]["declared_by"] == "launcher-default"
    assert defaulted[0]["capabilities"] == sorted(
        capability.value for capability in MACHINE_PROFILE
    )


def test_an_explicit_declaration_is_stored_as_given(
    owner: RuntimeIncarnation,
    emitted_events: EmittedEvents,
    *,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    lease = _request(
        owner,
        bus=event_bus,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=machine_identity.name(),
        automatic=True,
        capabilities=["read", "tasks.write"],
    )

    assert lease["capabilities"] == ["read", "tasks.write"]
    assert lease["capability_declared_by"] == "explicit"
    assert emitted_events.names().count("impersonation_capability_defaulted") == 0


def test_an_unknown_capability_is_refused_before_the_row_is_written(
    owner: RuntimeIncarnation,
    *,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    with pytest.raises(ValueError, match="not a valid Capability"):
        _request(
            owner,
            bus=event_bus,
            authority=config_authority,
            database_gate=database_gate,
            local_machine=machine_identity.name(),
            automatic=True,
            capabilities=["read", "spawn"],
        )


def test_renew_keeps_the_declaration(
    owner: RuntimeIncarnation,
    *,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    lease = _request(
        owner,
        bus=event_bus,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
        automatic=False,
        capabilities=["read"],
    )
    _activate(lease, owner, database=database, event_bus=event_bus, local_machine=local_machine)

    renewed = leases.renew(database, event_bus, str(lease["id"]), local_machine=local_machine)

    assert renewed["capabilities"] == ["read"]
    assert renewed["capability_declared_by"] == "explicit"


def test_renew_marks_a_pre_vocabulary_lease_legacy_without_declaring_anything(
    owner: RuntimeIncarnation,
    db_conn: psycopg.Connection[Any],
    *,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> None:
    local_machine = machine_identity.name()
    lease = _request(
        owner,
        bus=event_bus,
        authority=config_authority,
        database_gate=database_gate,
        local_machine=local_machine,
        automatic=False,
    )
    _activate(lease, owner, database=database, event_bus=event_bus, local_machine=local_machine)
    # meiva:note
    # This is what a lease issued before the vocabulary looks like: no declaration.
    db_conn.execute(
        "UPDATE agent_impersonations SET capabilities=NULL, capability_declared_by=NULL WHERE id=%s",
        (lease["id"],),
    )
    db_conn.commit()

    renewed = leases.renew(database, event_bus, str(lease["id"]), local_machine=local_machine)

    assert renewed["capabilities"] is None, "NULL must keep meaning legacy-full, never minimal"
    assert renewed["capability_declared_by"] == "legacy"
