# meiva:note
# The real configuration owner stays lazy at the root's earlier gates.
from unittest.mock import MagicMock

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from base.clock.tests.fakes import system_clock
from base.config import ConfigBoot
from base.db import Database
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import inert_keys
from base.telemetry.tests.capture.event_capture import EmittedEvents
from services.agent_runner.understanding.chunk_consumer import understanding_loop_forever
from services.agent_runner.understanding.group_consumer import group_model


def _poison_unused_cadence(owner: ConfigBoot) -> None:
    owner.read_process_environment()
    assert owner.environment is not None
    owner.environment["MEIVA_UNDERSTANDING_GROUP_CHECK_DECAY"] = "not-an-integer"


def test_owned_group_models_are_lazy_isolated_and_live(model_catalog: ModelCatalog) -> None:
    first, second = ConfigBoot(), ConfigBoot()
    _poison_unused_cadence(first)
    _poison_unused_cadence(second)
    first.set_field("understanding_group_model", "first-model")
    second.set_field("understanding_group_model", "second-model")
    db = MagicMock()
    assert not first.is_full() and not second.is_full()
    assert group_model(db, 7, catalog=model_catalog, config=first)[0] == "first-model"
    assert group_model(db, 7, catalog=model_catalog, config=second)[0] == "second-model"
    first.set_field("understanding_group_model", "first-update")
    assert group_model(db, 7, catalog=model_catalog, config=first)[0] == "first-update"
    assert group_model(db, 7, catalog=model_catalog, config=second)[0] == "second-model"
    assert not first.is_full() and not second.is_full()
    db.connect.assert_not_called()
    # meiva:note
    # Reading the hierarchy fallback would upgrade these owners and reject the bad delivery.
    for owner in (first, second):
        with pytest.raises(ValueError, match="MEIVA_UNDERSTANDING_GROUP_CHECK_DECAY"):
            owner.ensure_eager()


async def test_disabled_admission_leaves_unused_policy_and_pool_untouched(
    database: Database, model_catalog: ModelCatalog
) -> None:
    owner = ConfigBoot()
    _poison_unused_cadence(owner)
    owner.set_field("understanding_enabled", False)
    pool = database.async_pool(AsyncConnectionPool, min_size=1, max_size=1, timeout=5)
    try:
        await understanding_loop_forever(
            pool,
            database,
            [],
            clock=system_clock(),
            catalog=model_catalog,
            keys=inert_keys(model_catalog),
            llm_override="",
            config=owner,
        )
        assert pool.closed
        assert pool.get_stats().get("requests_num", 0) == 0
        assert not owner.is_full()
        with pytest.raises(ValueError, match="MEIVA_UNDERSTANDING_GROUP_CHECK_DECAY"):
            owner.ensure_eager()
    finally:
        await pool.close()


async def _wait_for_claim_lock(observer: AsyncConnection) -> None:
    import asyncio

    # meiva:note
    # The pool has two slots: the blocker and claimant need both. Observe through
    # the independent connection, with a fresh statistics snapshot for each poll.
    await observer.set_autocommit(True)
    while True:
        cursor = await observer.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
            " AND pid <> pg_backend_pid() AND wait_event_type = 'Lock'"
            " AND query LIKE 'INSERT INTO understanding_group_state%'"
        )
        row = await cursor.fetchone()
        assert row is not None
        if row[0]:
            return
        await asyncio.sleep(0.01)


async def test_cadence_is_read_again_under_the_real_lease_and_on_the_next_operation(
    database: Database,
    aops_pool: AsyncConnectionPool,
    adb_conn: AsyncConnection,
    model_catalog: ModelCatalog,
    emitted_events: EmittedEvents,
) -> None:
    import asyncio
    from dataclasses import replace

    from base.lm.provider_api import ProviderBinding
    from services.agent_runner.understanding.chunk_consumer import ModelCache
    from services.agent_runner.understanding.group_consumer import run_group_checks
    from services.agent_runner.understanding.tests.consumer_helpers import (
        consumer_config,
    )
    from services.agent_runner.understanding.tests.provider_wire import (
        ProviderWire,
        completion,
    )

    _ = emitted_events
    config = consumer_config()
    config.set_field("understanding_group_model", "deepseek-flash")
    config.set_field("understanding_group_reasoning", "high")
    config.set_field("understanding_group_check_open", 6)
    config.set_field("understanding_group_check_decay", 1)
    config.set_field("understanding_group_corrections", 0)
    async with aops_pool.connection() as conn:
        await conn.execute(
            "INSERT INTO understanding_nodes (agent_id, depth, span_start, span_end, start_ts,"
            " end_ts, segment_key, text, text_hash, input_hash, children_count, model,"
            " engine_version, prompt_version, schema_version)"
            " SELECT 7, 1, i*10, i*10+9, '2026-10-05T00:00Z'::timestamptz,"
            " '2026-10-05T00:30Z'::timestamptz, 'k', 'leaf', 'h', 'i', 0, 'm',"
            " 'chunk-0.2', 'p', 1 FROM generate_series(0, 5) AS i"
        )
    with ProviderWire(200, completion("no groups")) as wire:
        binding = ProviderBinding(
            prefix="deepseek-",
            display_name="Local SDK transport",
            key_env="DEEPSEEK_API_KEY",
            build=wire.build,
            effort_levels=("high", "max"),
            provider_key="openai",
        )
        catalog = replace(model_catalog, bindings={**model_catalog.bindings, "deepseek-": binding})
        models = ModelCache(catalog, inert_keys(catalog), "")
        async with aops_pool.connection() as blocker:
            await blocker.execute("BEGIN")
            try:
                await blocker.execute("LOCK TABLE understanding_group_state IN SHARE MODE")
                async with asyncio.TaskGroup() as tasks:
                    task = tasks.create_task(
                        run_group_checks(
                            aops_pool, database, models, 7, clock=system_clock(), config=config
                        )
                    )
                    await asyncio.wait_for(_wait_for_claim_lock(adb_conn), timeout=5)
                    config.set_field("understanding_group_check_open", 60)
                    await blocker.execute("COMMIT")
                    await task
            finally:
                await blocker.execute("ROLLBACK")
        assert wire.requests == [] and wire.build_efforts == []
        async with aops_pool.connection() as conn:
            cursor = await conn.execute(
                "SELECT last_checked_open, claimed_at FROM understanding_group_state"
            )
            assert await cursor.fetchall() == [(0, None)]
        await run_group_checks(aops_pool, database, models, 7, clock=system_clock(), config=config)
        assert wire.requests == []
        config.set_field("understanding_group_check_open", 6)
        await run_group_checks(aops_pool, database, models, 7, clock=system_clock(), config=config)
        assert len(wire.requests) == 1 and wire.build_efforts == ["high"]
