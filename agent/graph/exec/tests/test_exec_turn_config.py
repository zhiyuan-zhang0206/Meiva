import asyncio
import json
import random
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from agent.graph.exec._result import ExecDone
from agent.graph.exec._stream import ExecOutputChunkPublisher
from agent.graph.exec.node import exec_node, run_agent_code
from agent.graph.exec.process import SubprocessExecProcess, read_parent_facts
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.agents.messages.kwargs import ExecStatus, read_meiva_kwargs
from base.clock import Clock
from base.config import settings
from base.config.agent_pins import resolve_agent_config_pins
from base.config.tests.configuration import defaults
from base.db import Database
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import idle_publisher
from base.host.env.agent_env import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home, workspace_dir_readonly
from meiva.tests.fixtures.pin_agent import snapshot_clients_over


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


def _plugin(unit_home: Path) -> None:
    plugin = unit_home / "plugins" / "exec_config_probe"
    plugin.mkdir(parents=True)
    (plugin / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": "exec_config_probe",
                "version": "1.0.0",
                "engines": {"meiva": ">=2026.10.1"},
                "contributions": {"config": {"schema": "default_config.py"}},
            }
        )
    )
    (plugin / "plugin.py").write_text("__description__ = 'Private configuration probe'\n")
    (plugin / "default_config.py").write_text(
        "from pydantic import BaseModel, Field\n"
        "from base.packages.plugins.extensions import PluginContributions\n"
        "class Config(BaseModel):\n"
        "    exec_probe_marker: str = Field(default='default-marker', json_schema_extra={'per_agent': True})\n"
        "def contribute():\n"
        "    return PluginContributions(config=Config)\n"
    )
    (unit_home / "plugins.json").write_text(
        json.dumps({"plugins": {"exec_config_probe": {"enabled": True}}})
    )


# meiva:note
# The parent's environment carries another agent's pins, as an ambient process would: a child
# may only see the explicit maps of the turn it was launched for.
def _ambient_pins_process() -> SubprocessExecProcess:
    facts = read_parent_facts()
    ambient = {
        AGENT_CONFIG_OVERLAY_ENV: json.dumps({"llm_model": "deepseek-v4-pro"}),
        AGENT_BIRTH_CONFIG_ENV: json.dumps(
            {"llm_stream_ttft_timeout_seconds": 99.0, "exec_probe_marker": "ambient-parent"}
        ),
    }
    return SubprocessExecProcess(replace(facts, environ={**facts.environ, **ambient}), rng=_rng())


async def test_concurrent_turn_configs_reach_real_children_without_cross_talk(
    unit_home: Path,
    database: Database,
) -> None:
    _plugin(unit_home)
    code = (
        "import json, os\n"
        "from meiva.sdk_surface.settings import agent_setting, plugins\n"
        "print('CONFIG=' + json.dumps([agent_setting('llm_model'), "
        "agent_setting('llm_stream_ttft_timeout_seconds'), "
        "plugins.exec_config_probe.exec_probe_marker, "
        "os.environ.get('MEIVA_AGENT_CONFIG_OVERLAY', 'GONE')]))\n"
    )

    async def execute(agent_id: int, slices: AgentSlices | None = None) -> list[object]:
        result, *_ = await run_agent_code(
            AgentState(),
            MeivaContext(
                agent=slices or AgentSlices.resolve(default_reader=defaults()),
                db=database,
                bus=EventBus.from_settings(),
                clients=snapshot_clients_over(lambda: database),
                identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
                catalog=build_model_catalog(),
                clock_factory=Clock.from_settings,
            ),
            agent_id,
            code,
            ExecOutputChunkPublisher(idle_publisher(), agent_id, str(agent_id)),
            process=_ambient_pins_process(),
        )
        assert isinstance(result, ExecDone), result
        line = next(line for line in result.output.splitlines() if line.startswith("CONFIG="))
        return json.loads(line.removeprefix("CONFIG="))

    tasks: list[asyncio.Task[list[object]]] = []
    for agent_id, model, timeout, marker in (
        (424201, "deepseek-v4-pro", 3.0, "agent-a"),
        (424202, "deepseek-v4-flash-vision-exp", 7.0, "agent-b"),
    ):
        pins = resolve_agent_config_pins(
            {"llm_model": model},
            {"llm_model": "deepseek-v4-pro", "llm_stream_ttft_timeout_seconds": timeout},
        )
        slices = AgentSlices.resolve(
            pins,
            {"exec_config_probe": {"exec_probe_marker": marker}},
            default_reader=defaults(),
        )
        tasks.append(asyncio.create_task(execute(agent_id, slices)))
    assert await asyncio.gather(*tasks) == [
        ["deepseek-v4-pro", 3.0, "agent-a", "GONE"],
        ["deepseek-v4-flash-vision-exp", 7.0, "agent-b", "GONE"],
    ]
    assert await execute(424203) == [
        settings.lm.llm_model,
        settings.lm.llm_stream_ttft_timeout_seconds,
        "default-marker",
        "GONE",
    ]


def _execution_context(
    agent: AgentSlices,
    agent_id: int,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    request: pytest.FixtureRequest,
) -> MeivaContext:
    context = MeivaContext(
        agent=agent,
        db=database,
        bus=event_bus,
        clients=snapshot_clients_over(lambda: database),
        identity=AgentIdentity(agent_id=agent_id, owns_loop=True),
        event_publisher=idle_publisher(),
        catalog=model_catalog,
        clock_factory=Clock.from_settings,
    )
    request.addfinalizer(context.clients.close)
    return context


async def _execution_message(context: MeivaContext, code: str) -> ToolMessage:
    agent_id = context.require_identity().agent_id
    state = AgentState(
        messages=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_code",
                        "args": {"code": code},
                        "id": "call",
                        "type": "tool_call",
                    }
                ],
            )
        ]
    )
    command = await exec_node(
        state,
        Runtime(context=context),
        {"configurable": {"thread_id": str(agent_id)}},
        process=SubprocessExecProcess(read_parent_facts(), rng=_rng()),
    )
    update = cast(dict[str, Any], command.update)
    assert update["halted"] is False
    message = update["messages"][0]
    assert isinstance(message, ToolMessage)
    return message


async def test_exec_shield_follows_each_turns_own_node_timeout(
    unit_home: Path,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    request: pytest.FixtureRequest,
) -> None:
    short, long = (
        AgentSlices.resolve(default_reader=defaults(sandbox__exec_node_timeout_seconds=seconds))
        for seconds in (0.6, 30.0)
    )
    contexts = [
        _execution_context(short, 424204, database, event_bus, model_catalog, request),
        _execution_context(long, 424205, database, event_bus, model_catalog, request),
    ]

    timed_out, fast = await asyncio.gather(
        _execution_message(contexts[0], "import time; time.sleep(60)"),
        _execution_message(contexts[1], "print('done')"),
    )

    assert read_meiva_kwargs(timed_out).get("meiva_exec_status") == ExecStatus.TIMED_OUT
    assert "exec node timeout after 1s" in timed_out.text
    assert read_meiva_kwargs(fast).get("meiva_exec_status") == ExecStatus.COMPLETED
    assert "done" in fast.text


async def test_crop_limits_follow_each_turns_own_settings(
    unit_home: Path,
    database: Database,
    event_bus: EventBus,
    model_catalog: ModelCatalog,
    request: pytest.FixtureRequest,
) -> None:
    def crop_after(lines: int) -> AgentSlices:
        return AgentSlices.resolve(
            default_reader=defaults(
                sandbox__exec_output_crop_head_lines=1,
                sandbox__exec_output_crop_tail_lines=1,
                sandbox__exec_output_crop_after_lines=lines,
            )
        )

    contexts = [
        _execution_context(crop_after(2), 424206, database, event_bus, model_catalog, request),
        _execution_context(crop_after(7), 424207, database, event_bus, model_catalog, request),
    ]
    code = "for index in range(6): print(f'line {index} ' + 'content ' * 40)"
    body = "".join(f"line {index} " + "content " * 40 + "\n" for index in range(6))

    first_result, second_result = await asyncio.gather(
        _execution_message(contexts[0], code),
        _execution_message(contexts[1], code),
    )

    assert "[output cropped:" in first_result.text
    assert "[output cropped:" not in second_result.text
    assert body in second_result.text
    archives = list(
        (workspace_dir_readonly(424206, home=meiva_home()) / ".exec_output").glob("crop_*.txt")
    )
    assert len(archives) == 1 and archives[0].read_text() == body
    assert str(archives[0]) in first_result.text
    assert (
        list(
            (workspace_dir_readonly(424207, home=meiva_home()) / ".exec_output").glob("crop_*.txt")
        )
        == []
    )
