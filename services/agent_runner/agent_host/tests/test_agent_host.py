from __future__ import annotations

import asyncio

import psycopg
import pytest

import meiva
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from meiva.tests.fixtures.pin_agent import pin_agent, pin_no_identity
from services.agent_runner.agent_host.tests.host_policy import assert_sdk_context
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    BuildWith,
    HostPools,
    Row,
    ScriptedGraph,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    event_tap as event_tap,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_plugin as host_plugin,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    host_pools as host_pools,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    rig as rig,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired as wired,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    wired_with as wired_with,
)

FIRST_MODEL = "claude-haiku-5-5"
SECOND_MODEL = "claude-opus-5-5"
TURN_TIMEOUT_S = 10


def _add_chat(conn: psycopg.Connection, agent_id: int) -> None:
    conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source) "
        "VALUES (%s, 'hello', 'chat', 'user')",
        (agent_id,),
    )
    conn.commit()


class TestPendingInboundBackstop:
    # meiva:note
    # The scan finds this machine's idling or running agents with pending chat.
    async def test_stale_running_rows_qualified_by_the_scan(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        host, _, _ = wired(
            {
                17: Row(status="running"),
                23: Row(status="idling"),
                30: Row(status="running", machine="some-other-box"),
                31: Row(status="terminated"),
                40: Row(status="idling"),
            }
        )
        for agent_id in (17, 23, 30, 31):
            _add_chat(db_conn, agent_id)

        assert sorted(await host.pending_inbound_wakes()) == [17, 23]

    # meiva:note
    # A turn pool that cannot be borrowed from leaves the scan unaffected.
    async def test_scan_uses_the_reserved_control_pool(
        self,
        wired_with: BuildWith,
        host_pools: HostPools,
        db_conn: psycopg.Connection,
        db_url: str,
    ) -> None:
        closed = host_pools.workload
        await closed.close()
        host, _, _ = wired_with({17: Row(status="idling")}, ScriptedGraph({}), workload_pool=closed)
        _add_chat(db_conn, 17)

        assert await host.pending_inbound_wakes() == [17]


class TestConcurrentAgentIsolation:
    @pytest.mark.parametrize("sdk_bound", [False, True])
    async def test_two_overlapping_turns_each_see_their_own_everything(
        self,
        wired: Build,
        sdk_bound: bool,
        machine_identity: MachineIdentity,
    ) -> None:
        if sdk_bound:
            pin_agent(999)
        else:
            pin_no_identity()
        rows = {
            11: Row(overlay={"llm_model": FIRST_MODEL, "marker": "plug-for-11"}),
            22: Row(overlay={"llm_model": SECOND_MODEL, "marker": "plug-for-22"}),
        }
        host, graph, _ = wired(
            rows,
        )
        graph.gate(11)
        graph.gate(22)

        child_context = getattr(meiva, "context", None)
        assert_sdk_context(child_context, bound=sdk_bound)
        t1 = asyncio.create_task(host.run_turn(11))
        t2 = asyncio.create_task(host.run_turn(22))
        await asyncio.wait_for(graph.arrival(11).wait(), TURN_TIMEOUT_S)
        await asyncio.wait_for(graph.arrival(22).wait(), TURN_TIMEOUT_S)

        assert_sdk_context(child_context, bound=sdk_bound)
        graph.gates[11].set()
        graph.gates[22].set()
        await asyncio.wait_for(asyncio.gather(t1, t2), TURN_TIMEOUT_S)
        assert_sdk_context(child_context, bound=sdk_bound)

        seen = {o.agent_id: o for o in graph.observations}
        assert set(seen) == {11, 22}, "identity must not leak between concurrent turns"
        assert seen[11].machine is machine_identity
        assert seen[22].machine is machine_identity
        assert seen[11].model == FIRST_MODEL
        assert seen[22].model == SECOND_MODEL
        assert seen[11].plugin_marker == "plug-for-11"
        assert seen[22].plugin_marker == "plug-for-22"
        assert seen[11].served_model == FIRST_MODEL
        assert seen[22].served_model == SECOND_MODEL
        assert seen[11].publisher_running
        assert seen[22].publisher_running

    async def test_nothing_leaks_after_a_turn_ends(
        self,
        wired: Build,
    ) -> None:
        host, _, _ = wired(
            {11: Row(overlay={"llm_model": FIRST_MODEL})},
        )
        child_context = getattr(meiva, "context", None)
        await asyncio.wait_for(host.run_turn(11), TURN_TIMEOUT_S)
        assert getattr(meiva, "context", None) is child_context
