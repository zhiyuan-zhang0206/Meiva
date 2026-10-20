# meiva:note
# What a claimed restart tells the agent and the host about the turn it found. The verdict is
# `restart_preserves_idle`: an agent that was idle stays idle and reads the acceptance marker;
# any other agent had an unfinished turn, which the host's apply continues with one queued
# note, so claim adds no marker for it.
# meiva:link agent/graph/docs/turn-interrupts.meiva.okf.md

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agent.db import ClaimedInbound
from agent.graph.claim._dispatch import BatchState, handle_restart
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.plugin_providers import build_model_catalog


def _context() -> MeivaContext:
    return MeivaContext(
        agent=AgentSlices.resolve(default_reader=defaults()),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


async def _claim_restart(state: AgentState, *, source: str) -> BatchState:
    batch = BatchState()
    await handle_restart(
        _context(),
        state,
        ClaimedInbound(id=1, agent_id=1, content="", kind="restart", source=source, payload={}),
        batch,
    )
    assert batch.restart_requested
    return batch


def _history(**fields: object) -> AgentState:
    return AgentState.model_validate({"messages": [HumanMessage(content="hi")], **fields})


@pytest.mark.parametrize(
    ("state", "source"),
    [
        pytest.param(_history(halted=True), "user", id="idle-after-a-finished-turn"),
        pytest.param(_history(halted=True), "system:restart", id="idle-and-asked-by-the-system"),
        pytest.param(AgentState(), "user", id="no-history-yet"),
    ],
)
async def test_an_idle_agent_stays_idle_and_reads_the_acceptance_marker(
    state: AgentState, source: str
) -> None:
    batch = await _claim_restart(state, source=source)

    assert batch.restart_preserves_idle
    [marker] = batch.new_msgs
    assert isinstance(marker, HumanMessage)
    assert marker.model_dump()["additional_kwargs"]["meiva_note_tag"] == "lifecycle_restart"
    assert f"Restart was accepted from {source}" in str(marker.content)


@pytest.mark.parametrize(
    ("state", "source"),
    [
        pytest.param(_history(halted=False), "user", id="between-steps-of-a-turn"),
        pytest.param(
            _history(halted=True, interrupted_by_restart=True),
            "user",
            id="halted-by-the-interrupt-this-restart-caused",
        ),
        pytest.param(
            _history(halted=True, interrupted_by_restart=True),
            "system:restart",
            id="halted-by-an-interrupt-and-asked-by-the-system",
        ),
        pytest.param(_history(halted=True), "self", id="restart-from-the-agents-own-code"),
        pytest.param(AgentState(), "self", id="self-restart-with-no-history"),
    ],
)
async def test_an_unfinished_turn_resumes_and_gets_no_marker(
    state: AgentState, source: str
) -> None:
    batch = await _claim_restart(state, source=source)

    assert not batch.restart_preserves_idle
    assert batch.new_msgs == []


# meiva:note
# `halted` from a cancel carries no restart mark: the restart must not resume the agent.
async def test_a_pause_by_cancel_is_idle_not_an_interrupted_turn() -> None:
    batch = await _claim_restart(
        AgentState(
            messages=[SystemMessage(content="sys"), HumanMessage(content="stop")], halted=True
        ),
        source="user",
    )

    assert batch.restart_preserves_idle
    assert len(batch.new_msgs) == 1
