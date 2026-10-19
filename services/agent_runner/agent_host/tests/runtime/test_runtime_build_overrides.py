"""A hosted agent's chat model is built with the agent's own tuning pins."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.messages import AIMessage

from base.api_contracts.agent_contract import Harness
from base.host.env.agent_slices import AgentSlices, ModelOverrides
from base.lm.catalog import ModelCatalog
from base.lm.harness import FileSessionRegistry, HarnessTurn
from base.packages.composer_commands import CommandCatalog
from services.agent_runner.agent_host import host as host_module
from services.agent_runner.agent_host import runtime as runtime_module
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import (
    AgentRuntime,
    HostCachePolicy,
    evict_runtimes,
    harness_bridge_for,
)
from services.agent_runner.agent_host.tests.host_policy import configured_machine, configured_policy


@pytest.mark.parametrize("pinned", [True, False])
async def test_the_runtime_is_built_with_the_agents_overrides(
    monkeypatch: pytest.MonkeyPatch, pinned: bool, *, model_catalog: ModelCatalog
) -> None:
    built: list[tuple[str, ModelOverrides]] = []

    async def _boot(
        _agent_id: int,
        llm_model: str,
        overrides: ModelOverrides,
        *,
        catalog: ModelCatalog,
        llm_override: str,
    ) -> tuple[object, None]:
        assert catalog is model_catalog
        assert isinstance(llm_override, str)
        built.append((llm_model, overrides))
        return object(), None

    monkeypatch.setattr(host_module, "consume_claimed_inbounds", AsyncMock())
    monkeypatch.setattr(runtime_module, "boot_agent_scope", _boot)
    host = AgentHost(
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=Mock(),
        bus=Mock(),
        db=Mock(),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    pins = {"reasoning_effort": "low", "claude_thinking_budget_tokens": 777} if pinned else {}
    slices = AgentSlices.resolve(
        {"llm_model": "pinned-model", **pins}, default_reader=configured_policy().default_reader
    )

    await host._build_runtime(1, "fingerprint", slices, incarnation=None)

    assert [model for model, _ in built] == ["pinned-model"]
    overrides = built[0][1]
    assert overrides is slices.overrides
    if pinned:
        assert overrides.reasoning_effort == "low"
        assert overrides.claude_thinking_budget_tokens == 777
    else:
        assert (
            overrides
            == AgentSlices.resolve(default_reader=configured_policy().default_reader).overrides
        )


async def test_runtime_build_does_not_repair_a_dangling_history(
    monkeypatch: pytest.MonkeyPatch, *, model_catalog: ModelCatalog
) -> None:
    """Cold runtime construction never reads or rewrites the agent's checkpoint history.

    A dangling tool_use is repaired by the before_llm hook (`agent/hooks/repair.py`)
    on the first LLM call, not at startup.
    """
    touched: list[str] = []

    class _RecordingGraph:
        async def aget_state(self, *_args: object, **_kwargs: object) -> object:
            touched.append("aget_state")
            raise AssertionError("startup must not read graph state")

        async def aupdate_state(self, *_args: object, **_kwargs: object) -> None:
            touched.append("aupdate_state")
            raise AssertionError("startup must not rewrite graph state")

    async def _boot(*_args: object, **_kwargs: object) -> tuple[object, None]:
        return object(), None

    monkeypatch.setattr(host_module, "consume_claimed_inbounds", AsyncMock())
    monkeypatch.setattr(runtime_module, "boot_agent_scope", _boot)
    host = AgentHost(
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=_RecordingGraph(),  # type: ignore[arg-type]
        bus=Mock(),
        db=Mock(),
        catalog=model_catalog,
        machine=configured_machine(),
        commands=CommandCatalog(skills=list),
    )
    slices = AgentSlices.resolve({}, default_reader=configured_policy().default_reader)

    await host._build_runtime(1, "fingerprint", slices, incarnation=None)

    assert touched == []


class FakeEngine:
    # meiva:note
    # A session bridge that only records being closed: the runtime cache decides when a
    # session dies, and that decision is what these tests pin down.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    def __init__(self) -> None:
        self.harness = Harness.CLAUDE_CODE
        self.model = None
        self.closed = False
        self.reason: str | None = None

    async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
        raise AssertionError("these tests never run a turn")

    async def interrupt(self) -> object:
        raise AssertionError("these tests never interrupt")

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True
        self.reason = reason


class SlowClosingEngine(FakeEngine):
    # meiva:note
    # A close that a test can hold open, so the gap between "runtime dropped" and
    # "CLI gone" is observable instead of a race.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def close(self, *, reason: str = "closed") -> None:
        await self.release.wait()
        await super().close(reason=reason)


def runtime(
    agent_id: int, *, engine: FakeEngine | None = None, last_used: float = 0.0
) -> AgentRuntime:
    return AgentRuntime(
        fingerprint="f",
        llm=Mock(return_value=AIMessage(content="")),
        harness=Harness.CLAUDE_CODE if engine is not None else Harness.MEIVA,
        harness_bridge=engine,
        last_used=last_used,
    )


def test_the_built_in_loop_builds_no_bridge() -> None:
    assert (
        harness_bridge_for(
            1,
            Harness.MEIVA,
            registry=FileSessionRegistry(Path("/registry")),
            registrations=None,
            registration_ttl_seconds=3600.0,
            machine="macmini",
        )
        is None
    )


def test_an_external_runtime_without_a_registry_is_refused() -> None:
    try:
        harness_bridge_for(
            1,
            Harness.CLAUDE_CODE,
            registry=None,
            registrations=None,
            registration_ttl_seconds=3600.0,
            machine="macmini",
        )
    except ValueError as exc:
        assert "session registry" in str(exc)
    else:
        raise AssertionError("a bridge without a registry would resume nothing")


def test_an_external_runtime_carries_a_scoped_bridge() -> None:
    bridge = harness_bridge_for(
        1,
        Harness.CODEX,
        registry=FileSessionRegistry(Path("/registry")),
        registrations=None,
        registration_ttl_seconds=3600.0,
        machine="macmini",
    )
    assert bridge is not None
    assert bridge.harness is Harness.CODEX


def test_eviction_returns_what_it_removed() -> None:
    engine = FakeEngine()
    retained = runtime(1, engine=engine)
    runtimes: OrderedDict[int, AgentRuntime] = OrderedDict({1: retained})

    evicted = evict_runtimes(
        runtimes,
        in_flight=set(),
        policy=HostCachePolicy(idle_ttl_seconds=10.0, size=8),
        now=time.monotonic(),
    )

    assert evicted == [(1, retained)]
    assert not runtimes


def test_eviction_keeps_an_in_flight_runtime() -> None:
    runtimes: OrderedDict[int, AgentRuntime] = OrderedDict({1: runtime(1, engine=FakeEngine())})

    evicted = evict_runtimes(
        runtimes,
        in_flight={1},
        policy=HostCachePolicy(idle_ttl_seconds=0.0, size=0),
        now=time.monotonic(),
    )

    assert evicted == []
    assert list(runtimes) == [1]


async def test_dropping_an_agent_closes_its_session() -> None:
    host = AgentHost(
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=Mock(),
        bus=Mock(),
        db=Mock(),
        catalog=Mock(),
        machine=configured_machine(),
    )
    await host._sessions.start()
    engine = FakeEngine()
    host._runtimes[1] = runtime(1, engine=engine)

    host.drop_agent(1)
    await host._sessions.join(1)

    assert 1 not in host._runtimes
    assert engine.closed
    assert engine.reason == "runtime retired"


# meiva:note
# The window this closes is short and real: an evicted runtime is out of the cache the
# moment `_evict` runs, so a wake a second later builds a fresh bridge. Without the
# join, that bridge would start a second CLI on the resumed session id while the first
# one is still inside its terminate/kill window.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def test_the_next_turn_waits_for_the_retired_session_to_close() -> None:
    host = AgentHost(
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=Mock(),
        bus=Mock(),
        db=Mock(),
        catalog=Mock(),
        machine=configured_machine(),
    )
    await host._sessions.start()
    engine = SlowClosingEngine()
    host._runtimes[1] = runtime(1, engine=engine)

    host.drop_agent(1)
    await asyncio.sleep(0)
    assert not engine.closed, "the close is still running when the runtime is gone"

    engine.release.set()
    await host._sessions.join(1)

    assert engine.closed, "the next turn only proceeds once the old CLI is gone"
