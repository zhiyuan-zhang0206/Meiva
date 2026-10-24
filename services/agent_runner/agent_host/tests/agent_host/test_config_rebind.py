"""Agent host cases: config rebind."""

from __future__ import annotations

import asyncio
import time
from typing import Any, cast

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import empty_checkpoint
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent.ownership.tests.ownership_support import insert_lifecycle_command
from base.api_contracts.agent_contract import Harness
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.sessions.tests.poll_until import poll_until_async
from services.agent_runner.agent_host.daemon import build_checkpointer
from services.agent_runner.agent_host.dispatcher import (
    HostRestartRequiredError,
    TurnStallTimeoutError,
)
from services.agent_runner.agent_host.runtime import (
    HostPolicy,
    StallTiming,
    StoredConfig,
    config_fingerprint,
)
from services.agent_runner.agent_host.scheduling import wake_screening
from services.agent_runner.agent_host.tests.host_policy import configured_policy
from services.agent_runner.agent_host.tests.runtime.host_support import (
    Build,
    BuildWith,
    EventTap,
    GatedGraph,
    HostPools,
    Row,
    ScriptedGraph,
)

# meiva:note
# Two chat models of the real catalog; the host builds its runtime for whichever the row names.
FIRST_MODEL = "claude-haiku-5-5"
SECOND_MODEL = "claude-opus-5-5"
TURN_TIMEOUT_S = 10

_IDLE_TURN = {"exit_requested": False, "turn_idle": True, "restart_requested": False}
_EXIT = {"exit_requested": True, "turn_idle": False, "restart_requested": False}
_IDLE_RESTART = {
    "exit_requested": False,
    "turn_idle": False,
    "restart_requested": True,
    "halted": True,
}


class TestConfigRebind:
    async def test_a_changed_overlay_rebuilds_the_runtime_and_the_turn_sees_it(
        self, wired: Build
    ) -> None:
        """Write overlay -> wake -> the turn runs on the new value.

        The second turn must NOT reuse the cached runtime: it was built from the
        old config, so reusing it would keep the agent on its old model while
        `agents_meta` says otherwise.
        """
        host, graph, stored = wired({1: Row(overlay={"llm_model": FIRST_MODEL})})

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert graph.observations[-1].model == FIRST_MODEL
        assert host.stats.cache_misses == 1

        # The overlay write, as `meiva.self.restart(config_overlay)` performs it.
        stored.set_overlay(1, {"llm_model": SECOND_MODEL})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert graph.observations[-1].model == SECOND_MODEL
        assert graph.observations[-1].served_model == SECOND_MODEL, (
            "the model must be REBUILT, not just re-read from settings"
        )
        assert host.stats.cache_misses == 2, "a changed config must miss the cache"
        assert host.stats.cache_hits == 0

    async def test_an_unchanged_overlay_hits_the_cache(self, wired: Build) -> None:
        """The other half: re-reading the same row must not look like a change,
        or every turn would pay a cold build."""
        host, _, _ = wired({1: Row(overlay={"llm_model": FIRST_MODEL})})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert (host.stats.cache_misses, host.stats.cache_hits) == (1, 1)

    def test_the_fingerprint_ignores_key_order(self) -> None:
        """JSONB gives no key-order guarantee, so a fingerprint that depended on
        it would rebuild at random."""
        a = config_fingerprint({"x": 1, "y": 2}, None)
        b = config_fingerprint({"y": 2, "x": 1}, None)
        assert a == b
        assert a != config_fingerprint({"x": 1, "y": 3}, None)

    def test_the_fingerprint_separates_the_two_maps(self) -> None:
        """`birth_config` and `config_overlay` are different columns with
        different provenance; a fingerprint that flattened them would call two
        genuinely different states equal."""
        assert config_fingerprint({"x": 1}, None) != config_fingerprint(None, {"x": 1})

    # meiva:note
    # The rebind only works if the read is per turn — caching the row with
    # the runtime would make a change land only after an eviction. A row handed
    # to another machine between two turns must stop the second one.
    async def test_the_row_is_re_read_every_turn(self, wired: Build) -> None:
        host, graph, stored = wired({1: Row()})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert len(graph.observations) == 1

        stored.write(1, Row(machine="elsewhere", status="idling"))
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert len(graph.observations) == 1
        assert host.stats.wakes_skipped == 1


# meiva:note
# A graph whose turn writes checkpoints through the host's saver, as the real graph does.
class _CheckpointWritingGraph(ScriptedGraph):
    def __init__(self, saver: Any) -> None:
        super().__init__({})
        self._saver = saver

    async def ainvoke(
        self, _input: dict[str, Any], *, config: dict[str, Any], context: Any
    ) -> dict[str, Any]:
        thread = config["configurable"]["thread_id"]
        saved = await self._saver.aput(
            cast(RunnableConfig, {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}),
            empty_checkpoint(),
            {"source": "input", "step": -1, "parents": {}},
            {},
        )
        await self._saver.aput(
            saved, empty_checkpoint(), {"source": "loop", "step": 1, "parents": {}}, {}
        )
        return await super().ainvoke(_input, config=config, context=context)


def _stored_checkpoints(conn: psycopg.Connection, thread: str) -> int:
    row = conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id=%s", (thread,)).fetchone()
    assert row is not None
    return row[0]


class TestTurnLoop:
    async def test_host_builds_a_turn_scoped_nstep_saver(self) -> None:
        """The shared host saver must install the interval wrapper at boot."""
        checkpointer = await build_checkpointer(
            cast(AsyncConnectionPool[Any], object()), checkpoint_interval=100
        )

        assert "_meiva_nstep_flush" in checkpointer.__dict__

    async def test_completed_turn_flushes_its_own_nstep_checkpoint_tail(
        self, wired_with: BuildWith, host_pools: HostPools, db_conn: psycopg.Connection
    ) -> None:
        """Hosted turns must persist a skipped tail before the task returns."""
        saver = await build_checkpointer(host_pools.workload, checkpoint_interval=100)
        graph = _CheckpointWritingGraph(saver)
        host, _, _ = wired_with({1: Row()}, graph, checkpointer=saver)

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert _stored_checkpoints(db_conn, "1") == 2

    async def test_a_turn_flips_status_running_then_idling(self, wired: Build) -> None:
        host, graph, stored = wired({1: Row(status="idling")})
        graph.gate(1)

        task = asyncio.create_task(host.run_turn(1))
        await asyncio.wait_for(graph.arrival(1).wait(), TURN_TIMEOUT_S)
        assert stored.status(1) == "running"
        graph.gate(1).set()
        await asyncio.wait_for(task, TURN_TIMEOUT_S)

        assert stored.status(1) == "idling"

    async def test_a_crashing_turn_restores_idling(self, wired: Build) -> None:
        host, _, stored = wired({1: Row(status="idling")}, {1: [RuntimeError("turn exploded")]})

        with pytest.raises(RuntimeError, match="turn exploded"):
            await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert stored.status(1) == "idling"
        # The crash is a corpse: the settle is parked (keeps any marker) and
        # the fatal stamp lands even though the turn raised.
        assert stored.corpse_marked(1)

    async def test_a_normal_idle_turn_is_not_stamped_as_a_corpse(self, wired: Build) -> None:
        host, _, stored = wired(
            {1: Row(status="idling")},
            {1: [{"exit_requested": False, "turn_idle": True, "restart_requested": False}]},
        )

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert stored.status(1) == "idling"
        assert not stored.corpse_marked(1)

    async def test_a_cancelled_turn_is_not_stamped_as_a_corpse(self, wired: Build) -> None:
        host, _, stored = wired({1: Row(status="idling")}, {1: [asyncio.CancelledError()]})

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert stored.status(1) == "idling"
        assert not stored.corpse_marked(1)

    # meiva:note
    # The reap announces each corpse itself (services/agent_runner/agent_host/lifecycle/shells.py),
    # without scheduling an execution replay.
    async def test_reap_corpses_reaps_this_machines_corpses(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        host, _, stored = wired(
            {7: Row(status="idling"), 9: Row(status="idling"), 8: Row(status="idling")}
        )
        db_conn.execute(
            "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '1 day' "
            "WHERE id IN (7, 9)"
        )
        db_conn.commit()

        await host.reap_corpses()

        assert [stored.status(agent_id) for agent_id in (7, 8, 9)] == [
            "terminated",
            "idling",
            "terminated",
        ]

    # meiva:note
    # The beat keeps running when the reap explodes for a non-database reason: a missing table
    # raises a programming error, which is neither an unreachable database nor a timeout.
    async def test_reap_corpses_survives_an_unexpected_reap_failure(
        self, wired: Build, db_conn: psycopg.Connection, loguru_records: LogRecords
    ) -> None:
        host, _, _ = wired({1: Row(status="idling")})
        db_conn.execute("ALTER TABLE agents_meta RENAME TO agents_meta_gone")
        db_conn.commit()
        try:
            await host.reap_corpses()
        finally:
            db_conn.execute("ALTER TABLE agents_meta_gone RENAME TO agents_meta")
            db_conn.commit()

        assert [r["extra"]["event"] for r in loguru_records if "event" in r["extra"]] == [
            "corpse_reaper_failed"
        ]

    # meiva:note
    # The beat counts an unreachable database; the host must not swallow it. A pool whose
    # only connection is held times out; a pool whose connection the server closed raises
    # the operational error.
    async def test_reap_corpses_raises_a_pool_timeout_to_the_beat(
        self, wired_with: BuildWith, db_url: str
    ) -> None:
        short = AsyncConnectionPool(
            db_url, min_size=1, max_size=1, timeout=0.2, kwargs={"autocommit": True}, open=False
        )
        async with short:
            host, _, _ = wired_with(
                {1: Row(status="idling")}, ScriptedGraph({}), control_pool=short
            )
            async with short.connection():
                with pytest.raises(PoolTimeout):
                    await host.reap_corpses()

    async def test_reap_corpses_raises_a_closed_connection_to_the_beat(
        self, wired_with: BuildWith, db_url: str, db_conn: psycopg.Connection
    ) -> None:
        unchecked = AsyncConnectionPool(
            db_url,
            min_size=1,
            max_size=1,
            kwargs={"autocommit": True, "application_name": "beat-pool"},
            open=False,
        )
        async with unchecked:
            await unchecked.wait()
            host, _, _ = wired_with(
                {1: Row(status="idling")}, ScriptedGraph({}), control_pool=unchecked
            )
            db_conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = 'beat-pool'"
            )
            db_conn.commit()
            await asyncio.sleep(0.2)
            with pytest.raises(psycopg.OperationalError):
                await host.reap_corpses()

    async def test_exit_requested_does_not_restore_idling(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        host, _, stored = wired({1: Row(status="idling")}, {1: [dict(_EXIT)]})
        insert_lifecycle_command(db_conn, 1, "terminate")

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert stored.status(1) == "terminated"

    # meiva:note
    # The agent is terminated between the host's read of its row and its admission.
    async def test_a_losing_start_flip_skips_the_turn(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        host, graph, stored = wired({1: Row(status="idling")})
        db_conn.execute("SELECT 1 FROM agents_meta WHERE id = 1 FOR UPDATE")

        task = asyncio.create_task(host.run_turn(1))
        await poll_until_async(lambda: stored.lock_waiters() > 0, timeout=TURN_TIMEOUT_S)
        db_conn.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = 1")
        db_conn.commit()
        await asyncio.wait_for(task, TURN_TIMEOUT_S)

        assert stored.status(1) == "terminated"
        assert graph.observations == []
        assert host._runtimes == {}
        assert host.stats.cache_misses == 0

    async def test_restart_requested_restores_idling(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        host, _, stored = wired({1: Row(status="idling")}, {1: [dict(_IDLE_RESTART)]})
        restart = insert_lifecycle_command(db_conn, 1, "restart")

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert stored.status(1) == "idling"
        assert stored.inbound_status(restart) == "done"

    async def test_turn_idle_ends_the_task(self, wired: Build) -> None:
        host, graph, _ = wired(
            {1: Row()},
            {1: [{"exit_requested": False, "turn_idle": True, "restart_requested": False}]},
        )
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert len(graph.observations) == 1

    async def test_a_turn_boundary_re_invokes_on_the_same_thread(self, wired: Build) -> None:
        """Neither flag set means "turn over, more may be pending" — the host
        goes round again rather than ending the task, so a burst drains in one
        task instead of needing a wake per turn."""
        host, graph, _ = wired(
            {1: Row()},
            {
                1: [
                    {"exit_requested": False, "turn_idle": False, "restart_requested": False},
                    {"exit_requested": False, "turn_idle": False, "restart_requested": False},
                    {"exit_requested": False, "turn_idle": True, "restart_requested": False},
                ]
            },
        )
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert len(graph.observations) == 3

    async def test_exit_requested_applies_to_admitted_incarnation(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        """Hosted exit uses the admitted owner's durable apply, not process exit RPC."""
        host, graph, stored = wired({1: Row()}, {1: [dict(_EXIT)]})
        terminate = insert_lifecycle_command(db_conn, 1, "terminate")

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)

        assert len(graph.observations) == 1
        assert stored.inbound_status(terminate) == "done"
        assert stored.status(1) == "terminated"

    async def test_exit_drops_the_cached_runtime(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        """The next wake must start clean — the fresh-process half of a restart."""
        host, _, _ = wired({1: Row()}, {1: [dict(_EXIT)]})
        insert_lifecycle_command(db_conn, 1, "terminate")
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert 1 not in host._runtimes

    # meiva:note
    # A returned graph releases its cache before the owner-fenced application: the apply
    # waits on the agent's row, and the runtime is already gone while it waits.
    async def test_restart_requested_applies_after_continuation_and_cache_release(
        self, wired: Build, db_conn: psycopg.Connection
    ) -> None:
        host, graph, stored = wired({1: Row()}, {1: [dict(_IDLE_RESTART)]})
        restart = insert_lifecycle_command(db_conn, 1, "restart")
        claimed, proceed = graph.hold_after_claim(1)

        task = asyncio.create_task(host.run_turn(1))
        await asyncio.wait_for(claimed.wait(), TURN_TIMEOUT_S)
        assert 1 in host._runtimes
        db_conn.execute("SELECT 1 FROM agents_meta WHERE id = 1 FOR UPDATE")
        proceed.set()
        await poll_until_async(lambda: stored.lock_waiters() > 0, timeout=TURN_TIMEOUT_S)

        assert 1 not in host._runtimes
        assert stored.inbound_status(restart) == "claimed"
        db_conn.commit()
        await asyncio.wait_for(task, TURN_TIMEOUT_S)
        assert stored.inbound_status(restart) == "done"
        assert len(graph.observations) == 1

    async def test_a_cancelled_turn_drops_the_cached_runtime(self, wired: Build) -> None:
        """A cancelled turn must not keep its runtime: the abandoned turn left
        claimed inbounds behind, and the next wake's runtime build re-runs the
        startup reconcile — the hosted equivalent of a fresh process's boot.
        The dispatcher's stale-turn scan depends on exactly this drop."""
        host, graph, _ = wired({11: Row(overlay={"llm_model": FIRST_MODEL})})
        graph.gate(11)
        task = asyncio.create_task(host.run_turn(11))
        await asyncio.wait_for(graph.arrival(11).wait(), TURN_TIMEOUT_S)
        assert 11 in host._runtimes
        task.cancel()
        # The turn-level runner SHIELDS its in-flight invocation, so an outer
        # cancel waits for the turn to settle before it raises: the uncancellable
        # turn contract (Task #2436) keeps the owned resources from being torn
        # meiva:note
        # down under a live invocation. Release the gate so the scripted invocation
        # returns like a bounded real one would, then the wrapper can unwind.
        graph.gate(11).set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert 11 not in host._runtimes

    async def test_a_turn_starts_a_fresh_progress_window(self, wired: Build) -> None:
        """`run_turn` must reset the per-agent turn-progress clock on entry:
        otherwise a long-idle agent's stale entry reads as "stalled" during the
        next turn's cold build and the dispatcher's turn-level scan cancels the
        recovery turn it just scheduled."""
        host, _, _ = wired({11: Row(overlay={"llm_model": FIRST_MODEL})})
        # A stale entry as a long-ago turn would leave behind...
        host.turn_progress._marks[11] = [time.monotonic() - 99999.0]
        await asyncio.wait_for(host.run_turn(11), TURN_TIMEOUT_S)
        age = host.turn_progress.age_s(11)
        assert age is not None and age < 5.0, f"clock not reset, age={age}"

    async def test_a_turn_runs_with_the_hosts_own_shared_state(self, wired_with: BuildWith) -> None:
        """The graph's writers and the host's readers must hold the same objects: a context
        built with private ones would leave the stall guard reading an empty clock."""
        graph = GatedGraph()
        host, _, _ = wired_with({11: Row(overlay={"llm_model": FIRST_MODEL})}, graph)
        task = asyncio.create_task(host.run_turn(11))
        await poll_until_async(graph.entered.is_set)
        graph.release.set()
        await task
        [ctx] = graph.contexts
        assert ctx.turn_progress is host.turn_progress
        assert ctx.relays is host.relays

    async def test_a_skipped_wake_still_starts_a_fresh_progress_window(self, wired: Build) -> None:
        """The reset must precede every await in `_run_turn`.

        A task created between two dispatcher scans — a resurrect wake for a
        terminated agent — is otherwise judged on its predecessor incarnation's
        clock, and the dispatcher's stale-turn cancel lands before the new turn
        can open its window (2026-09-11: the bounded unwind was refused in
        teardown and the host exited). A non-runnable wake skips admission but
        is still a fresh task and must reset the clock.
        """
        host, _, _ = wired({11: Row(status="terminated")})
        host.turn_progress._marks[11] = [time.monotonic() - 99999.0]
        await asyncio.wait_for(host.run_turn(11), TURN_TIMEOUT_S)
        age = host.turn_progress.age_s(11)
        assert age is not None and age < 5.0, f"clock not reset, age={age}"

    async def test_a_crashing_turn_drops_the_runtime(self, wired: Build) -> None:
        """A crash is the hosted equivalent of a process dying mid-turn, and a
        respawn re-runs the startup reconcile. Keeping the cached runtime would
        skip it on the retry and leave `claimed` rows unresolved."""
        host, _, _ = wired({1: Row()}, {1: [dict(_IDLE_TURN), RuntimeError("turn exploded")]})

        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert 1 in host._runtimes
        with pytest.raises(RuntimeError, match="turn exploded"):
            await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert 1 not in host._runtimes


# meiva:note
# Task #2417, half 2: the no-progress stall guard around graph.ainvoke.
#
# A turn that stops making ANY progress (no node enter, no completed LLM
# step) past ``MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS`` is aborted, the
# error lands (one Error event), and the turn task ends; a turn that keeps
# stepping — the days-long autonomous loop the design allows — is never
# touched. All timing is shrunk: the host's policy carries tiny intervals and
# the guard's own poll does the work.
class TestTurnStallGuard:
    def _quick_policy(self, *, unwind_timeout_s: float = 1.0) -> HostPolicy:
        return configured_policy(
            stall=StallTiming(
                scan_interval_s=0.01, no_progress_timeout_s=0.05, unwind_timeout_s=unwind_timeout_s
            )
        )

    async def test_a_stalled_turn_is_aborted_and_its_error_lands(
        self, wired_with: BuildWith, event_tap: EventTap
    ) -> None:
        graph = GatedGraph()
        host, _, _ = wired_with(
            {11: Row(overlay={"llm_model": FIRST_MODEL})}, graph, policy=self._quick_policy()
        )

        with pytest.raises(TurnStallTimeoutError):
            await asyncio.wait_for(host.run_turn(11), timeout=30.0)

        assert graph.cancel_seen
        assert 11 not in host._runtimes, "the runtime must be dropped for the reconcile"
        # meiva:note
        # The guard's own Error says why; the turn loop then reports the abort it raised.
        errors = await event_tap.events("error")
        assert [error["error_class"] for error in errors] == [None, "TurnStallTimeoutError"]
        assert {error["agent_id"] for error in errors} == {11}
        assert "no progress" in errors[0]["content"]

    async def test_a_turn_that_keeps_marking_progress_is_never_aborted(
        self, wired_with: BuildWith, event_tap: EventTap
    ) -> None:
        graph = GatedGraph()
        host, _, _ = wired_with(
            {11: Row(overlay={"llm_model": FIRST_MODEL})}, graph, policy=self._quick_policy()
        )

        async def _keep_stepping() -> None:
            while not graph.release.is_set():
                host.turn_progress.mark(11)
                await asyncio.sleep(0.004)

        keeper = asyncio.create_task(_keep_stepping())
        task = asyncio.create_task(host.run_turn(11))
        await poll_until_async(graph.entered.is_set)
        # Let the timeout pass several times over; the steady marks must keep
        # the turn alive.
        observation_ends_at = asyncio.get_running_loop().time() + 0.2
        await poll_until_async(lambda: asyncio.get_running_loop().time() >= observation_ends_at)
        assert not task.done(), "a progressing turn must not be aborted"
        graph.release.set()
        await poll_until_async(task.done)
        await task
        keeper.cancel()
        assert await event_tap.events("error") == []
        assert 11 in host._runtimes

    async def test_an_external_cancel_still_unwinds_through_the_guard(
        self, wired_with: BuildWith
    ) -> None:
        """An external cancel must not block the guard's bounded unwind.

        The turn-level runner SHIELDS its invocation (the durable-command
        contract, Task #2436), so an outer cancellation flags the turn and
        waits for it to settle rather than interrupting the graph directly.
        For a no-progress turn the guard is what actually stops the work; the
        external cancel must not wedge that abort — the still-unwinds outcome
        is the stall abort (TurnStallTimeoutError), never a stuck task.
        """
        graph = GatedGraph()
        host, _, _ = wired_with(
            {11: Row(overlay={"llm_model": FIRST_MODEL})}, graph, policy=self._quick_policy()
        )

        task = asyncio.create_task(host.run_turn(11))
        await poll_until_async(graph.entered.is_set)
        task.cancel()
        await poll_until_async(task.done)
        with pytest.raises(asyncio.CancelledError):
            await task

        assert graph.cancel_seen
        assert 11 not in host._runtimes, "the runtime must be dropped for the reconcile"

    async def test_a_turn_that_refuses_to_unwind_escalates_to_a_host_restart(
        self, wired_with: BuildWith
    ) -> None:
        graph = GatedGraph(refuse_cancel=True)
        policy = self._quick_policy(unwind_timeout_s=0.05)
        host, _, _ = wired_with({11: Row(overlay={"llm_model": FIRST_MODEL})}, graph, policy=policy)

        with pytest.raises(HostRestartRequiredError, match="did not unwind"):
            await asyncio.wait_for(host.run_turn(11), timeout=30.0)

        assert graph.cancel_seen
        # Release the stuck invocation so the event loop is not left with a
        # pending task at teardown.
        graph.release.set()


class TestRunnability:
    async def test_another_machines_agent_is_never_run(self, wired: Build) -> None:
        """The dispatcher's PSUBSCRIBE is cluster-wide, so on a multi-runner
        cluster every runner sees every wake. Nothing but this check keeps two
        runners from both claiming one agent's inbound."""
        host, graph, _ = wired({1: Row(machine="some-other-box")})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1
        assert host.stats.turns_started == 0

    async def test_a_terminated_agents_wake_is_skipped(self, wired: Build) -> None:
        """A terminated agent's wake belongs to the delivery watchdog's resurrect
        path, so someone else owns this row right now."""
        host, graph, _ = wired({1: Row(status="terminated")})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1

    def test_a_harness_without_a_launcher_is_not_runnable(self) -> None:
        # meiva:note
        # A row born on a harness this release cannot start keeps the value as an
        # intent; it must not quietly run the built-in loop instead. Every harness
        # the enum names has an adapter today, so the gate is given the set a release
        # without one would carry rather than a name that happens to lack it; the
        # wake path itself is covered by the other cases in this class.
        # meiva:link base/agents/docs/harness/harness.meiva.okf.md
        stored = StoredConfig(
            machine="macmini",
            status="idling",
            harness="antigravity",
            config_overlay=None,
            birth_config=None,
        )

        assert (
            wake_screening.is_runnable("macmini", 1, stored, launchable=frozenset({Harness.MEIVA}))
            is False
        )
        assert wake_screening.is_runnable(
            "macmini", 1, stored, launchable=frozenset({Harness.MEIVA, Harness.ANTIGRAVITY})
        )

    async def test_a_missing_row_is_skipped(self, wired: Build) -> None:
        host, graph, _ = wired({})
        await asyncio.wait_for(host.run_turn(99), TURN_TIMEOUT_S)
        assert graph.observations == []
        assert host.stats.wakes_skipped == 1

    async def test_a_skipped_wake_builds_no_runtime(self, wired: Build) -> None:
        """The rejection must come BEFORE the cold build, or a burst of foreign
        wakes would evict every local agent's runtime."""
        host, _, _ = wired({1: Row(machine="elsewhere")})
        await asyncio.wait_for(host.run_turn(1), TURN_TIMEOUT_S)
        assert host._runtimes == {}
