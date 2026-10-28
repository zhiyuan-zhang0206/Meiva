"""Agent host cases: rejected model config."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from base.clock import SYSTEM_TIMERS
from base.lm.catalog import ModelCatalog
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedServiceResources
from base.packages.tests.model_catalog import AddModels
from base.sessions.tests.poll_until import poll_until_async
from services.agent_runner.agent_host.dispatcher import TurnScheduler
from services.agent_runner.agent_host.runtime import HostCachePolicy
from services.agent_runner.agent_host.scheduling.stored_config import config_fingerprint
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import Build, Row


def _logged(records: LogRecords, level: str) -> list[tuple[str, dict[str, Any]]]:
    """(event, structured fields) of every record logged at `level` with an `event=` field."""
    return [
        (r["extra"]["event"], r["extra"])
        for r in records
        if r["level"].name == level and "event" in r["extra"]
    ]


TURN_TIMEOUT_S = 10


class TestRejectedModelConfig:
    async def test_an_unknown_model_is_rejected_before_the_runtime_build(
        self, wired: Build, loguru_records: LogRecords
    ) -> None:
        """The real registry rejects an unknown model without a provider key."""
        host, graph, _ = wired({1: Row(overlay={"llm_model": "fable"})})

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert host.stats.cache_misses == 0, "no runtime build was attempted"
        assert host._runtimes == {}
        assert graph.observations == []
        assert host.stats.config_rejected == 1
        assert host.stats.as_payload()["config_rejected"] == 1
        assert host.stats.turns_started == 0
        assert [event for event, _ in _logged(loguru_records, "ERROR")] == ["host_config_rejected"]

    async def test_a_fixed_model_config_builds_on_the_next_wake(self, wired: Build) -> None:
        """A valid replacement clears the rejection note and resumes the turn."""
        host, graph, stored = wired({1: Row(overlay={"llm_model": "fable"})})

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert host.stats.cache_misses == 0
        assert host._rejected_configs

        stored.set_overlay(1, {"llm_model": "gpt-5.6-sol"})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert host.stats.cache_misses == 1
        assert [observation.model for observation in graph.observations] == ["gpt-5.6-sol"]
        assert host.stats.turns_started == 1
        assert host.stats.config_rejected == 1
        assert host._rejected_configs == {}

    async def test_the_same_rejected_config_logs_once_per_config_state(
        self, wired: Build, loguru_records: LogRecords
    ) -> None:
        """Repeated pending wakes stay quiet until the stored config changes."""
        host, _, stored = wired({1: Row(overlay={"llm_model": "fable"})})

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        stored.set_overlay(1, {"llm_model": "fable-2"})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert host.stats.config_rejected == 3
        assert [event for event, _ in _logged(loguru_records, "ERROR")] == [
            "host_config_rejected",
            "host_config_rejected",
        ]


class TestNormalizedModelConfig:
    """A stored pin naming a model the registry has withdrawn is bound as its
    registered fallback at the wake, before any consumer reads it — the model
    the turn view, the usage attribution and the exec children re-emitted from
    these pins all see."""

    @pytest.fixture
    def withdrawn_model(
        self, add_models: AddModels, model_catalog: ModelCatalog
    ) -> tuple[str, ModelCatalog]:
        from dataclasses import replace

        model = "deepseek-retired-fixture"
        catalog = add_models(
            model_catalog,
            {
                model: replace(
                    model_catalog.models["deepseek-flash"],
                    spawnable=False,
                    unavailable_fallback="deepseek-flash",
                )
            },
        )
        return model, catalog

    async def test_a_withdrawn_birth_pin_is_normalized_before_the_turn_binds_it(
        self, wired: Build, withdrawn_model: tuple[str, ModelCatalog], loguru_records: LogRecords
    ) -> None:
        """A birth_config pin on a withdrawn model resolves to its
        registered fallback — the same resolution build_chat_model applies only
        at the final build."""
        model, catalog = withdrawn_model

        host, graph, _ = wired({1: Row(birth={"llm_model": model})}, catalog=catalog)

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert [observation.model for observation in graph.observations] == ["deepseek-flash"]
        assert [observation.served_model for observation in graph.observations] == [
            "deepseek-flash"
        ]
        assert host.stats.config_normalized == 1
        # The once-per-state warning carries the agent and both ids, so a usage
        # row attributed to the withdrawn pin can be reconciled against it.
        warnings = _logged(loguru_records, "WARNING")
        assert [event for event, _ in warnings] == ["host_config_normalized"]
        assert warnings[0][1]["agent_id"] == 1
        assert warnings[0][1]["requested"] == model
        assert warnings[0][1]["resolved"] == "deepseek-flash"

    async def test_a_withdrawn_pin_normalizes_and_an_available_pin_is_untouched(
        self, wired: Build, withdrawn_model: tuple[str, ModelCatalog], loguru_records: LogRecords
    ) -> None:
        """A withdrawn pin resolves; an available model passes through."""
        model, catalog = withdrawn_model

        rows = {
            1: Row(overlay={"llm_model": model}),
            2: Row(overlay={"llm_model": "gemini-3.7-flash"}),
        }
        host, graph, _ = wired(rows, catalog=catalog)

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        await asyncio.wait_for(host.run_turn(2), TURN_TIMEOUT_S)

        assert [observation.model for observation in graph.observations] == [
            "deepseek-flash",
            "gemini-3.7-flash",
        ]
        assert host.stats.config_normalized == 1
        assert [event for event, _ in _logged(loguru_records, "WARNING")] == [
            "host_config_normalized"
        ]

    async def test_the_normalization_warns_once_per_stored_config_state(
        self, wired: Build, withdrawn_model: tuple[str, ModelCatalog], loguru_records: LogRecords
    ) -> None:
        """Repeated wakes on the same stale pin stay quiet until the stored
        config changes; the counter still sees every normalized wake."""
        model, catalog = withdrawn_model

        host, graph, stored = wired({1: Row(overlay={"llm_model": model})}, catalog=catalog)

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        stored.set_overlay(1, {"llm_model": "deepseek-flash"})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert [event for event, _ in _logged(loguru_records, "WARNING")] == [
            "host_config_normalized"
        ]
        assert host.stats.config_normalized == 2
        assert [observation.model for observation in graph.observations] == ["deepseek-flash"] * 3

    async def test_an_unregistered_pin_is_rejected_not_normalized(self, wired: Build) -> None:
        """No silent rescue for an id the registry does not know: the wake keeps
        taking the reject path (the real validator), and no turn starts."""
        host, graph, _ = wired({1: Row(overlay={"llm_model": "no-such-model-xyz"})})

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert graph.observations == []
        assert host.stats.config_rejected == 1
        assert host.stats.config_normalized == 0


class TestBounds:
    async def test_disabled_turn_limit_admits_sixty_four_agents(self, wired: Build) -> None:
        """Waiting on another agent's model must not consume a global turn slot."""
        agents = range(1, 65)
        host, graph, _ = wired(
            {agent_id: Row() for agent_id in agents},
            policy=configured_policy(max_concurrent_turns=0),
        )
        for agent_id in agents:
            graph.gate(agent_id)

        # Exercise admission directly so a pre-fix Semaphore(0) can be
        # cancelled during cleanup without bypassing run_turn's resource shield.
        service = HostedServiceResources()
        scopes = {agent_id: await service.turn() for agent_id in agents}
        tasks: list[asyncio.Task[None]] = []
        for agent_id in agents:
            task = asyncio.create_task(host._run_turn(agent_id, resources=scopes[agent_id]))
            service.retain_task(scopes[agent_id], task)
            tasks.append(task)
        try:
            await asyncio.wait_for(
                asyncio.gather(*(graph.arrival(agent_id).wait() for agent_id in agents)),
                TURN_TIMEOUT_S,
            )
            assert host._in_flight == set(agents)
        finally:
            for gate in graph.gates.values():
                gate.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await service.aclose()
            assert service.joined

    async def test_completed_burst_restores_the_warm_cache_bound(self, wired: Build) -> None:
        """Active runtimes survive a burst; completion releases excess warm entries."""
        agents = range(1, 65)
        host, graph, _ = wired(
            {agent_id: Row() for agent_id in agents},
            policy=configured_policy(max_concurrent_turns=64, cache=HostCachePolicy(3600.0, 32)),
        )
        for agent_id in agents:
            graph.gate(agent_id)

        tasks = [asyncio.create_task(host.run_turn(agent_id)) for agent_id in agents]
        try:
            await asyncio.wait_for(
                asyncio.gather(*(graph.arrival(agent_id).wait() for agent_id in agents)),
                TURN_TIMEOUT_S,
            )
            assert set(host._runtimes) == set(agents)
        finally:
            for gate in graph.gates.values():
                gate.set()
            await asyncio.wait_for(asyncio.gather(*tasks), TURN_TIMEOUT_S)

        assert host._in_flight == set()
        assert len(host._runtimes) <= 32

    async def test_long_running_completion_refreshes_cache_recency(self, wired: Build) -> None:
        """Active time is not idle time; the last completion becomes most recent."""
        policy = configured_policy(cache=HostCachePolicy(3600.0, 2))
        host, graph, _ = wired({agent_id: Row() for agent_id in (1, 2, 3)}, policy=policy)
        graph.gate(1)
        task = asyncio.create_task(host.run_turn(1))
        try:
            await asyncio.wait_for(graph.arrival(1).wait(), TURN_TIMEOUT_S)
            host._runtimes[1].last_used -= policy.cache.idle_ttl_seconds + 1
            await asyncio.wait_for(host.run_turn(2), TURN_TIMEOUT_S)
            assert 1 in host._runtimes, "an active runtime must survive idle eviction"
        finally:
            graph.gates[1].set()
            await asyncio.wait_for(task, TURN_TIMEOUT_S)

        assert list(host._runtimes) == [2, 1], "completion must refresh idle time and LRU"
        await asyncio.wait_for(host.run_turn(3), TURN_TIMEOUT_S)
        assert set(host._runtimes) == {1, 3}, "the earlier completion is evicted first"

    async def test_concurrent_turns_are_capped(self, wired: Build) -> None:
        """An explicitly configured admission bound still queues excess agents."""
        rows = {i: Row() for i in (1, 2, 3)}
        host, graph, _ = wired(rows, policy=configured_policy(max_concurrent_turns=2))
        for i in (1, 2, 3):
            graph.gate(i)
            graph.arrival(i)

        tasks = [asyncio.create_task(host.run_turn(i)) for i in (1, 2, 3)]
        await poll_until_async(lambda: len(graph.observations) == 2, timeout=TURN_TIMEOUT_S)
        for _ in range(8):
            await asyncio.sleep(0)
        assert len(graph.observations) == 2, "the third turn must wait for a slot"

        # meiva:note
        # Which two agents win the first slots is the database's race; the third waits.
        graph.gates[graph.observations[0].agent_id or 0].set()
        await poll_until_async(lambda: len(graph.observations) == 3, timeout=TURN_TIMEOUT_S)
        for gate in graph.gates.values():
            gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), TURN_TIMEOUT_S)
        assert host.stats.turns_started == 3

    async def test_a_waiting_turn_holds_no_database_claim(self, wired: Build) -> None:
        """Admission precedes the runtime claim: an agent queued for a slot has
        touched no row, runtime or checkpoint — and the queue is visible while
        it waits (task #3584 review: lock the pre-claim property in)."""
        host, graph, stored = wired(
            {1: Row(status="idling"), 2: Row(status="idling")},
            policy=configured_policy(max_concurrent_turns=1),
        )
        graph.gate(1)
        graph.arrival(1)

        tasks: list[asyncio.Task[None]] = [asyncio.create_task(host.run_turn(1))]
        try:
            await asyncio.wait_for(graph.arrival(1).wait(), TURN_TIMEOUT_S)
            tasks.append(asyncio.create_task(host.run_turn(2)))
            await poll_until_async(
                lambda: host.admission.waiting == 1,
                timeout=TURN_TIMEOUT_S,
                what="agent 2 queued at the admission gate",
            )

            assert not graph.arrival(2).is_set(), "a queued turn must not reach the graph"
            assert 2 not in host._in_flight, "a queued turn holds no runtime claim"
            assert stored.status(2) == "idling", "a queued turn has not been admitted"
            assert host.stats.turns_started == 1, "only the admitted turn started"
        finally:
            graph.gates[1].set()
            await asyncio.wait_for(asyncio.gather(*tasks), TURN_TIMEOUT_S)

        assert host.stats.turns_started == 2
        assert graph.arrived[2].is_set(), "the queued turn runs once a slot frees"

    async def test_the_lru_cap_evicts_the_least_recently_used(self, wired: Build) -> None:
        host, _, _ = wired(
            {i: Row() for i in (1, 2, 3)},
            policy=configured_policy(cache=HostCachePolicy(3600.0, 2)),
        )
        for i in (1, 2):
            await asyncio.wait_for(host.run_turn(i), TURN_TIMEOUT_S)
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)  # 1 is now the most recent
        await asyncio.wait_for(host.run_turn(3), TURN_TIMEOUT_S)
        assert set(host._runtimes) == {1, 3}, "2 was least recently used"

    async def test_the_idle_ttl_drops_a_silent_agent(self, wired: Build) -> None:
        """The size cap alone keeps a long-silent agent warm forever on a
        lightly loaded runner; the TTL is the other half."""
        policy = configured_policy()
        host, _, _ = wired({i: Row() for i in (1, 2)}, policy=policy)
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        host._runtimes[1].last_used -= policy.cache.idle_ttl_seconds + 1
        await asyncio.wait_for(host.run_turn(2), TURN_TIMEOUT_S)
        assert set(host._runtimes) == {2}, "1 aged out; 2 was just used"


class TestSchedulerIntegration:
    async def test_crash_event_carries_the_hosts_turn_config_fingerprint(
        self, wired: Build, loguru_records: LogRecords
    ) -> None:
        overlay: dict[str, object] = {"llm_model": "claude-haiku-5-5"}
        host, _, _ = wired({1: Row(overlay=overlay)}, {1: [ValueError("turn failed")]})
        sched = TurnScheduler(
            host.run_turn, config_fingerprint=host.turn_fingerprints.get, timers=SYSTEM_TIMERS
        )

        sched.wake(1)
        await poll_until_async(lambda: bool(loguru_records.events("host_turn_crashed")))

        [record] = loguru_records.events("host_turn_crashed")
        assert record["level"].name == "ERROR"
        assert record["exception"] is not None
        assert record["exception"].type is ValueError
        assert record["exception"].traceback is not None
        report = record["extra"]
        assert report["exception_type"] == "ValueError"
        assert report["config_fingerprint"] == config_fingerprint(overlay, None)
        with pytest.raises(ValueError, match="turn failed"):
            await sched.aclose()

    async def test_a_wake_race_during_a_hosted_turn_still_runs_the_agent(
        self, wired: Build
    ) -> None:
        """The dispatcher's wake-pending flag and the host's turn loop have to
        compose: a wake landing while a turn is in flight must produce another
        turn, not a lost one. `test_turn_dispatcher.py` proves the flag against a
        stub `run_turn`; this proves it against the real one.
        """
        host, graph, _ = wired({1: Row()})
        graph.gate(1)
        sched = TurnScheduler(host.run_turn, timers=SYSTEM_TIMERS)

        sched.wake(1)
        await asyncio.wait_for(graph.arrival(1).wait(), TURN_TIMEOUT_S)
        graph.arrived[1].clear()
        # The wake lands while the turn is parked inside the graph.
        sched.wake(1)
        graph.gates[1].set()
        await asyncio.wait_for(graph.arrival(1).wait(), TURN_TIMEOUT_S)
        graph.gates[1].set()
        await poll_until_async(lambda: len(graph.observations) >= 2)
        assert len(graph.observations) >= 2, "the wake during the turn must be served"
        await sched.aclose()
