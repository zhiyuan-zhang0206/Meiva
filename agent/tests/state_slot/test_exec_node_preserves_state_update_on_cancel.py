# meiva:note
# State slot case: a cancel still merges the plugin's state update into the exec command.

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph.exec.node import exec_node_impl
from agent.graph.exec.process import SubprocessExecProcess, read_parent_facts
from agent.graph.interrupt.tests.queued_commands import queue_command
from agent.state import BaseAgentState
from agent.tests.test_state_slot import ai_message_with_code, make_runtime_and_config
from agent.tests.test_state_slot import reset_state_slot as reset_state_slot
from base.agents.tests.units import spawn_agent
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.native_process.turn_identity import HostedTurnResources
from meiva.sdk_surface import process_context
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


# meiva:note
# In the cancel path, the state_update written by the plugin before the cancel still merges into
# Command. The cancel is a real queued cancel command, written only after the exec code has
# provably started: a marker file the code writes after its state_update (subprocess boot,
# `import meiva` plus plugins, takes about 1-2s, so a fixed short delay would race it on CI).
# The plugin first writes its state update, proves it ran, then sleeps long enough to be cancelled.
async def test_exec_node_preserves_state_update_on_cancel(
    redis_url: str,
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
):
    class _State(BaseAgentState):
        plugin__progress: str = ""

    marker = tmp_path / "started.marker"
    code = (
        "import meiva\n"
        "meiva.state_update['plugin__progress'] = 'before-cancel'\n"
        f"open({str(marker)!r}, 'w').write('x')\n"
        "import time; time.sleep(30)\n"
    )
    state = _State(messages=[ai_message_with_code(code)], halted=False)
    agent_id = spawn_agent(
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    runtime, config = make_runtime_and_config(
        AsyncMock(),
        database_gate=database_gate,
        ops_pool=aops_pool,
        agent_id=agent_id,
        hosted_resources=hosted_resources,
    )

    node_task = asyncio.create_task(
        exec_node_impl(
            cast(BaseAgentState, state),
            runtime,
            config,
            process=SubprocessExecProcess(read_parent_facts()),
        )
    )
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not marker.exists():
        await asyncio.sleep(0.05)
    assert marker.exists(), "exec code never started within 20s — cancel would race boot"
    queue_command(db_conn, agent_id, "cancel")
    cmd = await node_task

    update = cast(dict, cmd.update)
    assert update.get("plugin__progress") == "before-cancel"  # pyright: ignore[reportUnknownMemberType]
    assert update["halted"] is True
    assert not process_context.in_exec_turn()
