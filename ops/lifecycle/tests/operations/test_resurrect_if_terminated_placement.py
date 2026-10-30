# meiva:note
# `resurrect_if_terminated` runs the wake on the agent's home machine (`agents_meta.machine`).
#
# A remote home is asked through its ops server, a home that is this machine falls back to
# the in-process resurrection when that server is unreachable, and an unreachable remote home
# is skipped with the input still queued: launching locally for a remote-homed agent trips the
# boot placement gate and crash-loops (the agent-1513 incident). The decisions are
# `resurrect_plan`'s own tests; these run the shell over a real agent row, real gate reads, a
# real machine registry and a loopback ops server that runs `lifecycle_op` for the home.

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import cast

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message
from base.agents.messages.inbound import InboundKind, WakeTriggerKind
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database, DbConfig
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.paths import resolve_meiva_home
from ops import lifecycle
from ops.cluster.rpc import ClusterOpFailed, RpcClient
from ops.lifecycle.tests.loopback.scripted_post import Reply, Route, serve_post
from ops.lifecycle.tests.resurrection_setup import spawn_agent
from ops.rpc_schemas import LifecyclePayload
from ops.tests.rpc_support import rpc_client

_RESURRECT_PATH = "/api/agents/{agent_id}/resurrect-if-pending-work"


# meiva:note
# A terminated agent placed on its home machine, and the input that wants it awake.
@dataclass(frozen=True)
class Waiting:
    agent_id: int
    trigger_id: int


# meiva:note
# Builds terminated agents, each with a pending input, in the test database.
class Waitings:
    def __init__(
        self,
        conn: psycopg.Connection,
        database: Database,
        bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        self._conn = conn
        self._database = database
        self._bus = bus
        self._config_authority = config_authority
        self._model_catalog = model_catalog
        self._database_gate = database_gate
        self._machine_identity = machine_identity

    def make(
        self, *, home: str, kind: str = "chat", source: str = "user", status: str = "terminated"
    ) -> Waiting:
        agent_id = spawn_agent(
            config_authority=self._config_authority,
            model_catalog=self._model_catalog,
            database_gate=self._database_gate,
            local_machine=self._machine_identity,
        )
        self._conn.execute(
            "UPDATE agents_meta SET status=%s, termination_source='user', machine=%s WHERE id=%s",
            (status, home, agent_id),
        )
        self._conn.commit()
        trigger_id = insert_inbound_message(
            self._conn,
            agent_id,
            "wake me",
            source,
            kind,
            bus=self._bus,
            database=self._database,
        )
        return Waiting(agent_id, trigger_id)


@pytest.fixture
def waitings(
    db_conn: psycopg.Connection,
    database: Database,
    event_bus: EventBus,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> Waitings:
    return Waitings(
        db_conn,
        database,
        event_bus,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        machine_identity=machine_identity,
    )


def _register(
    conn: psycopg.Connection, name: str, url: str | None, *, paused: bool = False
) -> None:
    conn.execute(
        "INSERT INTO machines (name, role, gateway_url, paused_at) "
        "VALUES (%s, %s, %s, CASE WHEN %s THEN now() END)",
        (name, ["agent-runner"], url, paused),
    )
    conn.commit()


# meiva:note
# The home machine's ops server: it runs the lifecycle op it is sent, for real.
def _home_ops_server(
    db: Database, bus: EventBus, pool: ConnectionPool, catalog: ModelCatalog
) -> Route:
    def route(envelope: dict[str, object]) -> Reply:
        payload = LifecyclePayload.model_validate(envelope["payload"])
        response = asyncio.run(
            lifecycle.lifecycle_op(
                db,
                bus,
                payload.path,
                payload.body,
                pool,
                trigger_inbound_id=payload.trigger_inbound_id,
                trigger_inbound_kind=payload.trigger_inbound_kind,
                catalog=catalog,
                agent_host_port=8114,
                home=resolve_meiva_home(),
            )
        )
        return Reply({"status": "completed", "result": response.model_dump(mode="json")})

    return route


def _status(conn: psycopg.Connection, agent_id: int) -> str:
    row = conn.execute("SELECT status FROM agents_meta WHERE id=%s", (agent_id,)).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def _lifecycle_rows(conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT kind, source FROM inbound_messages WHERE agent_id=%s AND kind='resurrect'",
        (agent_id,),
    ).fetchall()
    conn.commit()
    return rows


def _suppress_wakes(conn: psycopg.Connection, agent_id: int) -> None:
    conn.execute(
        "UPDATE agents_meta SET wake_suppressed_until=now()-interval '1 second', "
        "wake_suppress_reason='resurrect_failed' WHERE id=%s",
        (agent_id,),
    )
    conn.commit()


def _suppression(conn: psycopg.Connection, agent_id: int) -> tuple[object, object]:
    row = conn.execute(
        "SELECT wake_suppressed_until, wake_suppress_reason FROM agents_meta WHERE id=%s",
        (agent_id,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row


class TestPlacement:
    async def test_a_remote_home_is_asked_through_its_ops_server(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        db_pool: ConnectionPool,
        model_catalog: ModelCatalog,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a")
        _suppress_wakes(db_conn, waiting.agent_id)
        routes = {"/ops": _home_ops_server(database, event_bus, db_pool, model_catalog)}
        with serve_post(routes) as home:
            _register(db_conn, "runner-a", home.url)

            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.IDLING
        [posted] = home.posted
        assert posted.path == "/ops"
        assert posted.body["kind"] == "lifecycle"
        assert str(posted.body["idempotency_key"]).startswith("lifecycle:")
        assert posted.body["payload"] == {
            "path": _RESURRECT_PATH.format(agent_id=waiting.agent_id),
            "body": {"resurrected_by": "system", "prompt": None},
            "trigger_inbound_id": waiting.trigger_id,
            "trigger_inbound_kind": "chat",
        }
        assert _lifecycle_rows(db_conn, waiting.agent_id) == [("resurrect", "system")]
        assert _suppression(db_conn, waiting.agent_id) == (None, None)

    # meiva:note
    # The dispatch runs on the client it is handed: a transient 5xx from the home's ops server is
    # retried after a wait on that client's own timers (a manual clock, so the wait costs no time),
    # not on a client the shell would build itself.
    async def test_a_transient_failure_is_retried_on_the_handed_clients_timers(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        db_pool: ConnectionPool,
        model_catalog: ModelCatalog,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a")
        _suppress_wakes(db_conn, waiting.agent_id)
        home_ops = _home_ops_server(database, event_bus, db_pool, model_catalog)
        answers = iter([Reply({}, status=503)])

        def flaky(envelope: dict[str, object]) -> Reply:
            return next(answers, None) or home_ops(envelope)

        rpc, timers = rpc_client()
        with serve_post({"/ops": flaky}) as home:
            _register(db_conn, "runner-a", home.url)

            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.IDLING
        assert len(home.posted) == 2
        assert len(timers.sleeps) == 1

    async def test_a_local_home_without_an_ops_server_resurrects_in_process(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="home-a")
        waiting = waitings.make(home="home-a")
        _suppress_wakes(db_conn, waiting.agent_id)

        status = await lifecycle.resurrect_if_terminated(
            database,
            event_bus,
            waiting.agent_id,
            trigger_inbound_id=waiting.trigger_id,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=machine_identity.name(),
            rpc=rpc,
        )

        assert status is AgentStatus.IDLING
        assert _status(db_conn, waiting.agent_id) == "idling"
        assert _lifecycle_rows(db_conn, waiting.agent_id) == [("resurrect", "system")]
        assert _suppression(db_conn, waiting.agent_id) == (None, None)

    @pytest.mark.parametrize("registered_without_url", [False, True])
    async def test_an_unreachable_remote_home_is_skipped_with_the_input_queued(
        self,
        registered_without_url: bool,
        caplog: pytest.LogCaptureFixture,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a")
        if registered_without_url:
            _register(db_conn, "runner-a", None)

        with caplog.at_level(logging.INFO):
            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.TERMINATED
        assert "home machine unreachable" in caplog.text
        assert _lifecycle_rows(db_conn, waiting.agent_id) == []
        assert db_conn.execute(
            "SELECT status FROM inbound_messages WHERE id=%s", (waiting.trigger_id,)
        ).fetchone() == ("pending",)

    async def test_a_failed_op_on_the_home_machine_reaches_the_sender(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        loguru_records: LogRecords,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a")
        failed = Reply(
            {"status": "failed", "result": {"error": "launch failed on the home machine"}}
        )
        with serve_post({"/ops": lambda _envelope: failed}) as home:
            _register(db_conn, "runner-a", home.url)

            with pytest.raises(ClusterOpFailed):
                await lifecycle.resurrect_if_terminated(
                    database,
                    event_bus,
                    waiting.agent_id,
                    trigger_inbound_id=waiting.trigger_id,
                    trigger_inbound_kind=InboundKind.CHAT,
                    local_machine=machine_identity.name(),
                    rpc=rpc,
                )

        assert loguru_records.events("auto_resurrect_failed") == []
        assert _status(db_conn, waiting.agent_id) == "terminated"

    async def test_a_paused_home_is_reported_and_the_input_stays_queued(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        loguru_records: LogRecords,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="home-a")
        waiting = waitings.make(home="home-a")
        _register(db_conn, "home-a", None, paused=True)

        status = await lifecycle.resurrect_if_terminated(
            database,
            event_bus,
            waiting.agent_id,
            trigger_inbound_id=waiting.trigger_id,
            trigger_inbound_kind=InboundKind.CHAT,
            local_machine=machine_identity.name(),
            rpc=rpc,
        )

        assert status is AgentStatus.TERMINATED
        assert [r["extra"]["agent_id"] for r in loguru_records.events("auto_resurrect_failed")] == [
            waiting.agent_id
        ]
        assert _lifecycle_rows(db_conn, waiting.agent_id) == []


class TestGates:
    @pytest.mark.parametrize("kind", ["no-such-kind", InboundKind.TERMINATE])
    async def test_an_invalid_trigger_is_refused_for_a_live_agent(
        self,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        kind: WakeTriggerKind,
        rpc: RpcClient,
    ) -> None:
        waiting = waitings.make(home="runner-a", status="running")
        with pytest.raises(ValueError, match=r"no-such-kind|cannot trigger resurrection"):
            await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=kind,
                local_machine="gateway-host",
                rpc=rpc,
            )

    @pytest.mark.parametrize("kind", ["no-such-kind", InboundKind.TERMINATE])
    async def test_an_invalid_trigger_is_refused_before_a_database_dial(
        self,
        event_bus: EventBus,
        database_gate: ProcessDbGate,
        kind: WakeTriggerKind,
        rpc: RpcClient,
    ) -> None:
        database = Database(
            DbConfig(
                db_url="postgresql://denied@127.0.0.1:1/denied",
                db_sslmode="disable",
                db_pool_min_size=1,
                db_pool_max_size=1,
                pgbouncer_enabled=False,
                db_authority_refusal="this test has no database authority",
            ),
            gate=database_gate,
        )
        with pytest.raises(ValueError, match=r"no-such-kind|cannot trigger resurrection"):
            await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                1,
                trigger_inbound_id=1,
                trigger_inbound_kind=kind,
                local_machine="gateway-host",
                rpc=rpc,
            )

    async def test_a_live_agent_ends_the_call_after_its_status_read(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a", status="running")
        with serve_post({"/ops": lambda _envelope: Reply({})}) as home:
            _register(db_conn, "runner-a", home.url)

            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.RUNNING
        assert home.posted == []

    async def test_a_tripped_breaker_stops_a_background_trigger_before_any_home_contact(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a", kind="heartbeat", source="system")
        db_conn.execute(
            "UPDATE agents_meta SET permanent_reject_streak=1,last_permanent_reject_reason='billing' "
            "WHERE id=%s",
            (waiting.agent_id,),
        )
        db_conn.commit()
        with serve_post({"/ops": lambda _envelope: Reply({})}) as home:
            _register(db_conn, "runner-a", home.url)

            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=InboundKind.HEARTBEAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.TERMINATED
        assert home.posted == []

    async def test_a_system_notice_never_wakes_its_owner(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a", source="system")
        with serve_post({"/ops": lambda _envelope: Reply({})}) as home:
            _register(db_conn, "runner-a", home.url)

            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=waiting.trigger_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.TERMINATED
        assert home.posted == []

    # meiva:note
    # No row reads as no notice, so the wake is forwarded and the home runner's final CAS
    # adjudicates the stale input.
    async def test_a_missing_trigger_row_is_not_a_notice_and_goes_to_the_home_cas(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        db_pool: ConnectionPool,
        model_catalog: ModelCatalog,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a")
        routes = {"/ops": _home_ops_server(database, event_bus, db_pool, model_catalog)}
        with serve_post(routes) as home:
            _register(db_conn, "runner-a", home.url)

            status = await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                waiting.agent_id,
                trigger_inbound_id=10**9,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

        assert status is AgentStatus.TERMINATED
        assert [posted.body["payload"] for posted in home.posted] == [
            {
                "path": _RESURRECT_PATH.format(agent_id=waiting.agent_id),
                "body": {"resurrected_by": "system", "prompt": None},
                "trigger_inbound_id": 10**9,
                "trigger_inbound_kind": "chat",
            }
        ]
        assert _lifecycle_rows(db_conn, waiting.agent_id) == []

    async def test_a_read_failure_reaches_the_caller_instead_of_becoming_a_skip(
        self,
        database: Database,
        event_bus: EventBus,
        machine_identity: MachineIdentityReader,
        rpc: RpcClient,
    ) -> None:
        with pytest.raises(AgentNotFound):
            await lifecycle.resurrect_if_terminated(
                database,
                event_bus,
                10**9,
                trigger_inbound_id=1,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=machine_identity.name(),
                rpc=rpc,
            )

    async def test_a_kind_that_cannot_wake_an_agent_is_refused_before_any_contact(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        waitings: Waitings,
        machine_identity: ConfiguredMachineIdentity,
        rpc: RpcClient,
    ) -> None:
        machine_identity.set(name="gateway-host")
        waiting = waitings.make(home="runner-a", kind="restart", source="user")
        with serve_post({"/ops": lambda _envelope: Reply({})}) as home:
            _register(db_conn, "runner-a", home.url)

            with pytest.raises(ValueError, match="cannot trigger resurrection"):
                await lifecycle.resurrect_if_terminated(
                    database,
                    event_bus,
                    waiting.agent_id,
                    trigger_inbound_id=waiting.trigger_id,
                    trigger_inbound_kind=cast(WakeTriggerKind, InboundKind.RESTART),
                    local_machine=machine_identity.name(),
                    rpc=rpc,
                )

        assert home.posted == []
