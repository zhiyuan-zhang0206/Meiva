"""State slot cases: exec node preserves state update on lifecycle."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock

from langchain_core.messages import HumanMessage

from agent.graph.exec.node import exec_node_impl
from agent.graph.exec.process import SubprocessExecProcess
from agent.graph.exec.tests.process_inputs import live_parent_facts
from agent.state import AttachEntry, AttachState, BaseAgentState
from agent.tests.test_state_slot import (
    ai_message_with_code,
    make_runtime_and_config,
    messages_plugin_state_cls,
)
from agent.tests.test_state_slot import (
    reset_state_slot as reset_state_slot,
)
from base.db.code_version_gate import ProcessDbGate
from meiva.sdk_surface import process_context


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


async def test_exec_node_preserves_state_update_on_lifecycle(*, database_gate: ProcessDbGate):
    """Lifecycle (terminate) path: plugin's state_update written before raising still merges.

    Directly raise AgentTermination to avoid `meiva.self.terminate()`'s internal db INSERT
    (test doesn't mock meiva.DB)——the invariant for this test is "plugin state_update in lifecycle
    path still merges into Command", not the full terminate flow."""
    from meiva.self import AgentTermination

    assert AgentTermination is not None  # let import not be removed by ruff

    class _State(BaseAgentState):
        plugin__last_action: str = ""

    code = (
        "import meiva\n"
        "meiva.state_update['plugin__last_action'] = 'about-to-terminate'\n"
        "from meiva.self import AgentTermination\n"
        "raise AgentTermination\n"
    )
    state = _State(messages=[ai_message_with_code(code)], halted=False)
    runtime, config = make_runtime_and_config(AsyncMock(), database_gate=database_gate)

    cmd = await exec_node_impl(
        cast(BaseAgentState, state),
        runtime,
        config,
        process=SubprocessExecProcess(live_parent_facts(), rng=_rng()),
    )

    update = cast(dict[str, object], cmd.update)
    assert update.get("plugin__last_action") == "about-to-terminate"
    assert update["halted"] is True
    assert not process_context.in_exec_turn()


async def test_exec_node_keeps_plugin_notes_after_toolmessage(*, database_gate: ProcessDbGate):
    state_cls = messages_plugin_state_cls()

    code = (
        "import meiva\n"
        "from langchain_core.messages import HumanMessage\n"
        "meiva.state_update['messages'] = [HumanMessage(content='project note', id='p1')]\n"
    )
    state = state_cls(messages=[ai_message_with_code(code)], halted=False)
    runtime, config = make_runtime_and_config(AsyncMock(), database_gate=database_gate)

    cmd = await exec_node_impl(
        state, runtime, config, process=SubprocessExecProcess(live_parent_facts(), rng=_rng())
    )

    update = cast(dict[str, Any], cmd.update)
    msgs = update["messages"]
    assert [m.type for m in msgs] == ["tool", "human"], (
        f"expected [tool, plugin], got {[m.type for m in msgs]}"
    )
    assert msgs[1].id == "p1"
    assert msgs[1].content == "project note"


async def test_exec_node_checkpoints_child_attachment(
    tmp_path: Path, *, database_gate: ProcessDbGate
):
    """A normal child registration drains into a media message in the exec update.

    User ruling 2026-08-26: the attach message lands right after the exec
    output in the SAME turn, so the update must contain the packed media
    HumanMessage and a cleared attach channel — not parked pending entries
    for the claim boundary.
    """
    from base.agents.messages.kwargs import MeivaMsgType

    # The real exec child rejects attach for a text-only model (user ruling
    # 2026-08-28) — boot it with a media-capable model via the per-agent
    # config map the exec path re-emits into the child env (a bare home's
    # env-authority pass drops an inherited MEIVA_MODEL).
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    code = f"import meiva\nmeiva.self.attach({str(image)!r}, label='render result')"
    state = BaseAgentState(messages=[ai_message_with_code(code)], halted=False)
    runtime, config = make_runtime_and_config(
        AsyncMock(), {"llm_model": "claude-sonnet-5"}, database_gate=database_gate
    )

    cmd = await exec_node_impl(
        state, runtime, config, process=SubprocessExecProcess(live_parent_facts(), rng=_rng())
    )

    update = cast(dict[str, Any], cmd.update)
    # Pending is drained (cleared) in the same update — nothing parked.
    assert update["attach"] == AttachState()
    messages = update["messages"]
    assert len(messages) == 2
    attach_msg = messages[-1]
    assert isinstance(attach_msg, HumanMessage)
    assert attach_msg.additional_kwargs["meiva_msg_type"] == MeivaMsgType.ATTACH.value
    # Interleaved pack: the notice leads, then the file's own caption line.
    # The exec-node context model here is a bare MagicMock (no media
    # capability), so the pack is caption-only — no media block.
    content = attach_msg.content
    assert isinstance(content, list)
    assert [cast("dict[str, Any]", b)["type"] for b in content] == ["text", "text"]
    assert "Files attached during this turn" in cast("dict[str, Any]", content[0])["text"]
    caption_block = cast("dict[str, Any]", content[1])
    assert "render.png" in caption_block["text"]


# meiva:note
# The compact path (SystemHalt) writes nothing back — claim REMOVE_ALLs
#     the whole history — so plugin notes must not
#     leak into the update.
async def test_exec_node_compact_path_drops_notes_and_findings(
    tmp_path: Path, *, database_gate: ProcessDbGate
):

    state_cls = messages_plugin_state_cls()
    # The real exec child rejects attach for a text-only model (user ruling
    # 2026-08-28) — boot it with a media-capable model via the per-agent
    # config map the exec path re-emits into the child env (a bare home's
    # env-authority pass drops an inherited MEIVA_MODEL).
    image = tmp_path / "render.png"
    image.write_bytes(b"png")
    code = (
        "import meiva\n"
        "from langchain_core.messages import HumanMessage\n"
        "meiva.state_update['messages'] = [HumanMessage(content='x')]\n"
        f"meiva.self.attach({str(image)!r})\n"
        "from base.agents.lifecycle import SystemHalt\n"
        "raise SystemHalt()\n"
    )
    state = state_cls(
        messages=[ai_message_with_code(code)],
        halted=False,
        attach=AttachState(pending=[AttachEntry(path="/previous.png", label=None)]),
    )
    runtime, config = make_runtime_and_config(
        AsyncMock(), {"llm_model": "claude-sonnet-5"}, database_gate=database_gate
    )

    cmd = await exec_node_impl(
        state, runtime, config, process=SubprocessExecProcess(live_parent_facts(), rng=_rng())
    )

    update = cast(dict[str, object], cmd.update)
    assert update.get("messages") == [], (
        f"compact path must write no messages back, got {update.get('messages')!r}"
    )
    assert update.get("halted") is True
    assert "attach" not in update
