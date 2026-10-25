"""Real database contracts for agent forks and their committed wake receipts.

Tests verify durable birth configuration, lifecycle messages, exact resurrection
triggers, and caller fences. Host wake publication follows committed state.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import cast

import psycopg
import pytest
from redis.client import PubSub

from base.api_contracts.agent_contract import ForkCheckpointNotFound
from base.cluster import inbound_channel, redis_channel_prefix
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from ops.agents.tests.birth_faults import hold_chat_inserts
from ops.agents.tests.test_agents_internals import (
    blob_rows,
    checkpoint_ids,
    inbound_count,
    inbound_rows,
    insert_blob,
    insert_checkpoint,
    spawn_agent,
)


class TestSpawnFork:
    def test_fork_copies_target_and_ancestor_chain(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """With no compaction boundary in the chain, fork copies target ckpt + all ancestors
        (recursively via parent_checkpoint_id), so the new agent sees the full history."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        # construct chain: a (root) → b → c
        insert_checkpoint(db_conn, source, "a-ckpt", parent_id=None)
        insert_checkpoint(db_conn, source, "b-ckpt", parent_id="a-ckpt")
        insert_checkpoint(db_conn, source, "c-ckpt", parent_id="b-ckpt")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="c-ckpt",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        # new agent gets all three a/b/c
        assert sorted(checkpoint_ids(db_conn, new_id)) == ["a-ckpt", "b-ckpt", "c-ckpt"]
        # source is untouched
        assert sorted(checkpoint_ids(db_conn, source)) == ["a-ckpt", "b-ckpt", "c-ckpt"]

    def test_fork_does_not_copy_descendants_after_target(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """fork at b → new agent only gets a + b, not c (checkpoints after b).
        Verify the semantic: "fork cuts the state before the fork point"."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "a", parent_id=None)
        insert_checkpoint(db_conn, source, "b", parent_id="a")
        insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="b",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert sorted(checkpoint_ids(db_conn, new_id)) == ["a", "b"]

    def test_fork_records_source_in_agents_row(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """fork_source_agent_id + fork_source_checkpoint_id are written to the agents row."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "x")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="x",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT fork_source_agent_id, fork_source_checkpoint_id FROM agents_meta WHERE id = %s",
                (new_id,),
            )
            row = cur.fetchone()
        assert row == (source, "x")

    def test_fork_copies_blobs_referenced_by_the_copied_chain(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A blob the copied checkpoints reference through channel_versions is copied —
        the actual message data lives in blobs."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "1"})
        insert_blob(db_conn, source, "messages", "1", b"\xde\xad\xbe\xef")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert blob_rows(db_conn, new_id) == [("messages", "1", b"\xde\xad\xbe\xef")]

    def test_fork_skips_blobs_no_copied_checkpoint_references(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Superseded blob versions are left behind: PostgresSaver reads blobs only through
        the copied checkpoints' channel_versions, so an unreferenced version is unreachable."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "ck", channel_versions={"messages": "2"})
        insert_blob(db_conn, source, "messages", "1", b"\x01stale")
        insert_blob(db_conn, source, "messages", "2", b"\x02live")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert blob_rows(db_conn, new_id) == [("messages", "2", b"\x02live")]

    def test_fork_stops_at_the_newest_compact_boundary(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A fork above a boundary copies down to (and including) it and stops: its ancestors
        stay behind, so the copy stays bounded by the fork point's segment window."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "a", parent_id=None)
        insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)
        insert_checkpoint(db_conn, source, "c", parent_id="b")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="c",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        # "a" is pre-boundary history and is NOT copied.
        assert sorted(checkpoint_ids(db_conn, new_id)) == ["b", "c"]
        assert sorted(checkpoint_ids(db_conn, source)) == ["a", "b", "c"]

    def test_fork_at_a_boundary_walks_to_the_root_without_a_previous_boundary(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """The fork checkpoint never terminates its own walk. Forking exactly at a boundary
        continues down to the next boundary below it — with no boundary below, the window is
        the full chain (the old cut-at-the-boundary window read back empty; the read-back
        assertions live in base/agents/history/tests/test_delta_read_compat.py)."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "a", parent_id=None)
        insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="b",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert checkpoint_ids(db_conn, new_id) == ["a", "b"]

    def test_fork_at_a_boundary_stops_at_the_previous_boundary(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """With a boundary below the fork point, the walk descends to it and stops: the copy
        spans at most two compacted segments, not the whole chain."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "a", parent_id=None, compact_boundary=True)
        insert_checkpoint(db_conn, source, "b", parent_id="a")
        insert_checkpoint(db_conn, source, "c", parent_id="b", compact_boundary=True)

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="c",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert sorted(checkpoint_ids(db_conn, new_id)) == ["a", "b", "c"]

    def test_fork_stops_at_the_nearest_boundary_when_several_exist(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Only the newest boundary at or below the fork point terminates the walk; older
        segments are not copied."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "a", parent_id=None, compact_boundary=True)
        insert_checkpoint(db_conn, source, "b", parent_id="a")
        insert_checkpoint(db_conn, source, "c", parent_id="b", compact_boundary=True)
        insert_checkpoint(db_conn, source, "d", parent_id="c")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="d",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert sorted(checkpoint_ids(db_conn, new_id)) == ["c", "d"]

    def test_fork_copies_writes_only_for_copied_checkpoints(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Delta-written threads keep their message content in writes, so writes must follow the
        copied checkpoints exactly — including the boundary cut."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "a", parent_id=None)
        insert_checkpoint(db_conn, source, "b", parent_id="a", compact_boundary=True)
        insert_checkpoint(db_conn, source, "c", parent_id="b")
        for ckpt in ("a", "b", "c"):
            with db_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, "
                    "task_id, idx, channel, type, blob) "
                    "VALUES (%s, '', %s, 'task', 0, 'messages', 'msgpack', %s)",
                    (str(source), ckpt, ckpt.encode()),
                )
        db_conn.commit()

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="c",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT checkpoint_id FROM checkpoint_writes WHERE thread_id = %s ORDER BY checkpoint_id",
                (str(new_id),),
            )
            assert [r[0] for r in cur.fetchall()] == ["b", "c"]

    def test_fork_unknown_checkpoint_raises_and_rolls_back(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """fork_checkpoint does not exist in source → raise + transaction rollback (no orphan agents /
        agents_meta rows)."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        # intentionally do not INSERT any checkpoint

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            count_before = row[0]

        with pytest.raises(ForkCheckpointNotFound, match="bogus-ckpt"):
            spawn_agent(
                fork_from=source,
                fork_checkpoint="bogus-ckpt",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agents_meta")
            row = cur.fetchone()
            assert row is not None
            assert row[0] == count_before  # no new agents row

    def test_fork_from_without_checkpoint_raises_value_error(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """fork_from / fork_checkpoint must be provided as a pair."""
        with pytest.raises(ValueError, match="must be provided as a pair"):
            spawn_agent(
                fork_from=1,
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        with pytest.raises(ValueError, match="must be provided as a pair"):
            spawn_agent(
                fork_checkpoint="x",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )

    def test_fork_inserts_identity_inbound(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """fork inserts a kind='fork' lifecycle inbound (source='agent:{fork_from}', content='')
        within the same transaction that copies the checkpoint, so the new process receives
        the identity marker on its first claim. The insert is committed before _launch_agent_process."""
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "ck")

        new_id = spawn_agent(
            fork_from=source,
            fork_checkpoint="ck",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

        assert inbound_rows(db_conn, new_id) == [("", "fork", f"agent:{source}")]

    def test_plain_spawn_inserts_no_inbound(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """Non-fork spawn does not insert any inbound (the agent starts from nothing, no 'who am I' to correct)."""
        new_id = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert inbound_count(db_conn, new_id) == 0

    # meiva:note
    # Fork plus prompt: the prompt chat inbound is committed before the first wake is published,
    # so it lands together with the fork marker in the forked agent's first claim batch (otherwise
    # the agent would first run a turn on inherited history plus the fork marker, and the prompt
    # would arrive in the next batch, which is logically wrong). The prompt INSERT is parked on a
    # lock after the fork marker's transaction has committed, and a subscriber on every inbound
    # wake channel sees nothing while it waits; once it goes on, the wake arrives and both rows,
    # marker first, are in the database.
    def test_fork_prompt_is_committed_before_any_wake_is_published(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        event_bus: EventBus,
        *,
        database_gate: ProcessDbGate,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        machine_identity: MachineIdentityReader,
    ) -> None:
        source = spawn_agent(
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        insert_checkpoint(db_conn, source, "ck")
        wake_pattern = f"{redis_channel_prefix(event_bus.channel)}:inbound:*"

        with (
            ThreadPoolExecutor(max_workers=1) as spawner,
            event_bus.sync_redis(decode_responses=True) as redis,
            PubSub(redis.connection_pool) as received,
            hold_chat_inserts(db_conn) as hold,
        ):
            received.psubscribe(wake_pattern)
            subscribed = cast(dict[str, object] | None, received.get_message(timeout=2))
            assert subscribed is not None and subscribed["type"] == "psubscribe"
            spawning = spawner.submit(
                spawn_agent,
                fork_from=source,
                fork_checkpoint="ck",
                prompt="go do X",
                prompt_source="user",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
            deadline = time.monotonic() + 10
            while not hold.waiting():
                assert time.monotonic() < deadline, "the prompt INSERT never reached the lock"
                time.sleep(0.02)

            assert received.get_message(timeout=0.3) is None

            hold.release()
            new_id = spawning.result(timeout=10)
            wake = cast(dict[str, object] | None, received.get_message(timeout=2))

        assert wake is not None
        assert wake["channel"] == inbound_channel(new_id, event_bus.channel)
        assert inbound_rows(db_conn, new_id) == [
            ("", "fork", f"agent:{source}"),
            ("go do X", "chat", "user"),
        ]

    def test_spawn_prompt_pairing_validated(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """prompt / prompt_source must be provided as a pair."""
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            spawn_agent(
                prompt="hi",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
        with pytest.raises(ValueError, match="prompt and prompt_source must be provided as a pair"):
            spawn_agent(
                prompt_source="user",
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
