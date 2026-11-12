"""A hosted agent's chat model is built with the agent's own tuning pins."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from agent.ownership.hosted import admit_hosted_runtime
from base.api_contracts.agent_contract import Harness
from base.config.tests.configuration import defaults
from base.db import Database
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.harness import (
    BackgroundItem,
    BackgroundKind,
    BackgroundWork,
    BridgeEvent,
    EventSubscription,
    FileSessionRegistry,
    HarnessInput,
    Reconciliation,
)
from base.lm.harness.bridge import HarnessBridge
from base.lm.harness.tests.roots import bridge_root_values
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import meiva_home
from services.agent_runner.agent_host.host import AgentHost
from services.agent_runner.agent_host.runtime import (
    AgentRuntime,
    HostCachePolicy,
    build_runtime,
    evict_runtimes,
    harness_bridge_for,
    holds_background_work,
)
from services.agent_runner.agent_host.tests.host_policy import (
    configured_machine,
    configured_policy,
    handoff_timestamps,
)
from services.agent_runner.agent_host.tests.runtime.host_support import (
    BuildWith,
    Graph,
    HostPools,
    Row,
    ScriptedGraph,
)
from services.agent_runner.agent_host.tests.runtime.observation import served_model


@pytest.mark.parametrize(
    ("harness", "model", "effort"),
    [
        (Harness.CLAUDE_CODE, "claude-opus-4-6", "low"),
        (Harness.CLAUDE_CODE, "claude-haiku-4-5-20251001", ""),
        (Harness.CODEX, "gpt-6.1-terra", "ultra"),
        (Harness.ANTIGRAVITY, "gpt-oss-120b", "medium"),
    ],
)
@pytest.mark.parametrize("selection", [False, True])
async def test_external_runtime_needs_no_native_model_or_provider_key(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    harness: Harness,
    model: str,
    effort: str,
    selection: bool,
) -> None:
    if not selection:
        model, effort = "deepseek-flash", "max"
    runtime = await build_runtime(
        71,
        "external-selection",
        AgentSlices.resolve(
            {"llm_model": model, "reasoning_effort": effort}, default_reader=defaults()
        ),
        catalog=model_catalog,
        keys=ProviderKeys({}),
        llm_override="",
        home=tmp_path,
        harness=harness,
        birth_config={"harness_model_selection": 1} if selection else None,
        registry=FileSessionRegistry(tmp_path / "sessions"),
        registrations=None,
        registration_ttl_seconds=60,
        machine="external-test",
        harness_environ={},
        harness_root=bridge_root_values(),
    )
    assert runtime.llm is None
    assert isinstance(runtime.harness_bridge, HarnessBridge)
    assert runtime.harness_bridge.harness is harness


@pytest.fixture
async def admitted(
    wired_with: BuildWith, host_pools: HostPools, database: Database
) -> Callable[[Graph], Awaitable[tuple[AgentHost, RuntimeIncarnation]]]:
    """A host over the given graph and the incarnation it holds for agent 1."""

    async def admit(graph: Graph) -> tuple[AgentHost, RuntimeIncarnation]:
        host, _, _ = wired_with({1: Row(status="idling")}, graph)
        owner = await admit_hosted_runtime(
            host_pools.workload, 1, "this-box", host.runtime_owner, db=database
        )
        assert owner is not None
        return host, owner

    return admit


@pytest.mark.parametrize("pinned", [True, False])
async def test_the_runtime_is_built_with_the_agents_overrides(
    admitted: Callable[[Graph], Awaitable[tuple[AgentHost, RuntimeIncarnation]]], pinned: bool
) -> None:
    host, owner = await admitted(ScriptedGraph({}))
    pins = {"reasoning_effort": "low"} if pinned else {}
    slices = AgentSlices.resolve(
        {"llm_model": "claude-opus-5-5", **pins}, default_reader=defaults()
    )

    runtime = await host._build_runtime(1, "fingerprint", slices, incarnation=owner)

    assert served_model(runtime.llm) == "claude-opus-5-5"
    # meiva:note
    # The model carries the pinned tuning; unpinned, it carries the catalog default.
    assert cast(Any, runtime.llm).effort == ("low" if pinned else "medium")


async def test_runtime_build_does_not_repair_a_dangling_history(
    admitted: Callable[[Graph], Awaitable[tuple[AgentHost, RuntimeIncarnation]]],
) -> None:
    """Cold runtime construction never reads or rewrites the agent's checkpoint history.

    A dangling tool_use is repaired by the before_llm hook (`agent/hooks/repair.py`)
    on the first LLM call, not at startup.
    """
    touched: list[str] = []

    class _RecordingGraph(ScriptedGraph):
        async def aget_state(self, _config: object, /) -> SimpleNamespace:
            touched.append("aget_state")
            raise AssertionError("startup must not read graph state")

        async def aupdate_state(self, config: object, update: dict[str, Any]) -> object:
            touched.append("aupdate_state")
            raise AssertionError("startup must not rewrite graph state")

    host, owner = await admitted(_RecordingGraph({}))
    slices = AgentSlices.resolve({}, default_reader=defaults())

    await host._build_runtime(1, "fingerprint", slices, incarnation=owner)

    assert touched == []


class FakeEngine:
    # meiva:note
    # A session bridge that only records being closed: the runtime cache decides when a
    # session dies, and that decision is what these tests pin down.
    # meiva:link agent/graph/docs/harness-turn.md
    def __init__(self) -> None:
        self.harness = Harness.CLAUDE_CODE
        self.model = None
        self.closed = False
        self.reason: str | None = None
        self.exit_code: int | None = None

    async def subscribe(self) -> EventSubscription:
        raise AssertionError("these tests never watch events")

    async def deliver(self, inputs: Sequence[HarnessInput], *, compaction_note: str = "") -> None:
        raise AssertionError("these tests never deliver")

    @property
    def context_compacted(self) -> bool:
        return False

    @property
    def generation(self) -> int:
        return 0

    @property
    def has_unpresented_turn(self) -> bool:
        return False

    def take_unattached(self) -> list[BridgeEvent]:
        return []

    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        raise AssertionError("these tests never reconcile")

    def adopt(self, keys: Sequence[str]) -> None:
        raise AssertionError("these tests never adopt")

    async def retire(self, reason: str) -> None:
        raise AssertionError("these tests never retire")

    def notify_inbound(self) -> None:
        raise AssertionError("these tests never wake")

    async def wait_inbound(self, timeout: float) -> None:
        raise AssertionError("these tests never wait")

    async def interrupt(self) -> object:
        raise AssertionError("these tests never interrupt")

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True
        self.reason = reason


class SlowClosingEngine(FakeEngine):
    # meiva:note
    # A close that a test can hold open, so the gap between "runtime dropped" and
    # "CLI gone" is observable instead of a race.
    # meiva:link agent/graph/docs/harness-turn.md
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


def test_the_built_in_loop_builds_no_bridge(tmp_path: Path) -> None:
    assert (
        harness_bridge_for(
            1,
            Harness.MEIVA,
            registry=FileSessionRegistry(Path("/registry")),
            registrations=None,
            registration_ttl_seconds=3600.0,
            machine="macmini",
            environ={},
            root=bridge_root_values(),
            home=tmp_path,
        )
        is None
    )


def test_an_external_runtime_without_a_registry_is_refused(tmp_path: Path) -> None:
    try:
        harness_bridge_for(
            1,
            Harness.CLAUDE_CODE,
            registry=None,
            registrations=None,
            registration_ttl_seconds=3600.0,
            machine="macmini",
            environ={},
            root=bridge_root_values(),
            home=tmp_path,
        )
    except ValueError as exc:
        assert "session registry" in str(exc)
    else:
        raise AssertionError("a bridge without a registry would resume nothing")


def test_an_external_runtime_carries_a_scoped_bridge(tmp_path: Path, unit_home: Path) -> None:
    other_home = tmp_path / "other-home"

    bridge = harness_bridge_for(
        1,
        Harness.CODEX,
        registry=FileSessionRegistry(Path("/registry")),
        registrations=None,
        registration_ttl_seconds=3600.0,
        machine="macmini",
        environ={},
        root=bridge_root_values(),
        home=other_home,
    )

    assert bridge is not None
    assert bridge.harness is Harness.CODEX
    assert (other_home / "workspaces" / "1").is_dir()
    assert not (unit_home / "workspaces" / "1").exists()


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


class BusyEngine(FakeEngine):
    # meiva:note
    # An engine whose external CLI holds background work, which the host's keep-alive reads
    # through the same structural question the real bridge answers.
    # meiva:link base/lm/harness/docs/background-work.md
    def __init__(self, *, holds: bool = True) -> None:
        super().__init__()
        self.holds = holds

    def background_work(self) -> BackgroundWork:
        if not self.holds:
            return BackgroundWork()
        return BackgroundWork((BackgroundItem(BackgroundKind.CRON, "cron `* * * * *`: ping"),))


def _long_idle(agent_id: int, engine: FakeEngine) -> AgentRuntime:
    return runtime(agent_id, engine=engine, last_used=0.0)


def test_a_runtime_holding_background_work_survives_the_idle_ttl() -> None:
    busy, quiet = BusyEngine(), BusyEngine(holds=False)
    runtimes: OrderedDict[int, AgentRuntime] = OrderedDict(
        {1: _long_idle(1, busy), 2: _long_idle(2, quiet), 3: _long_idle(3, FakeEngine())}
    )

    evicted = evict_runtimes(
        runtimes,
        in_flight=set(),
        policy=HostCachePolicy(idle_ttl_seconds=1.0, size=8),
        now=time.monotonic() + 3600,
    )

    assert sorted(agent_id for agent_id, _ in evicted) == [2, 3]
    assert list(runtimes) == [1]
    assert holds_background_work(runtimes[1])


def test_the_size_bound_takes_idle_runtimes_before_one_holding_background_work() -> None:
    busy = BusyEngine()
    runtimes: OrderedDict[int, AgentRuntime] = OrderedDict(
        {
            1: _long_idle(1, busy),
            2: _long_idle(2, BusyEngine(holds=False)),
            3: _long_idle(3, BusyEngine(holds=False)),
        }
    )

    evicted = evict_runtimes(
        runtimes,
        in_flight=set(),
        policy=HostCachePolicy(idle_ttl_seconds=10**9, size=1),
        now=time.monotonic(),
    )

    assert [agent_id for agent_id, _ in evicted] == [2, 3]
    assert list(runtimes) == [1]


def test_over_the_bound_with_nothing_idle_left_the_least_recent_busy_runtime_goes() -> None:
    first, second = BusyEngine(), BusyEngine()
    runtimes: OrderedDict[int, AgentRuntime] = OrderedDict(
        {1: _long_idle(1, first), 2: _long_idle(2, second)}
    )

    evicted = evict_runtimes(
        runtimes,
        in_flight=set(),
        policy=HostCachePolicy(idle_ttl_seconds=10**9, size=1),
        now=time.monotonic(),
    )

    assert [agent_id for agent_id, _ in evicted] == [1]
    assert holds_background_work(evicted[0][1]), "the caller can tell it was a forced eviction"
    assert list(runtimes) == [2]


def test_a_runtime_without_a_bridge_or_with_a_blind_bridge_holds_nothing() -> None:
    assert not holds_background_work(runtime(1))
    assert not holds_background_work(runtime(2, engine=FakeEngine()))


async def test_the_host_counts_the_sessions_it_keeps_alive_for_background_work() -> None:
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
        policy=configured_policy(),
        pool=Mock(),
        checkpointer=Mock(),
        graph=Mock(),
        bus=Mock(),
        db=Mock(),
        catalog=Mock(),
        machine=configured_machine(),
    )
    host._runtimes[1] = _long_idle(1, BusyEngine())
    host._runtimes[2] = _long_idle(2, BusyEngine(holds=False))
    host._runtimes[3] = _long_idle(3, BusyEngine())
    host._in_flight.add(3)

    assert host.kept_alive_sessions() == 1


async def test_dropping_an_agent_closes_its_session() -> None:
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
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
# meiva:link agent/graph/docs/harness-turn.md
async def test_the_next_turn_waits_for_the_retired_session_to_close() -> None:
    host = AgentHost(
        runtime_owner=uuid4(),
        handoff_stamping=handoff_timestamps(),
        home=meiva_home(),
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
