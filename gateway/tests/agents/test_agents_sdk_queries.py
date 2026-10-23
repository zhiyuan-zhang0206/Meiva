"""`meiva.agents` SDK query/config surface: get_neighbors, get_ancestors, list_agents, spawn config, resurrect, lifecycle enums; split from gateway/tests/agents/test_agents_sdk.py (task #4922)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg
import pytest

import meiva
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from gateway.tests.agents.sdk_support import (
    authority_with,
    gateway_wire,
    inbound_rows,
    spawn_agent,
)
from gateway.tests.agents.sdk_support import sdk_via_gateway as sdk_via_gateway
from gateway.tests.agents.sdk_support import self_agent as self_agent
from meiva import gateway_client
from meiva.agents import AgentNotFound, AgentStatus
from meiva.gateway_client import use_client


class TestGetNeighbors:
    """SDK get_neighbors maps the gateway rows to Neighbor dataclasses (status to
    the AgentStatus enum). The graph behaviors are covered in
    gateway/tests/agents/test_agent_neighbors.py; here we verify the wrapper + wire path
    only, seeding ties as `audit_events` rows."""

    @staticmethod
    def _seed(db: psycopg.Connection, *, status: str = "running") -> int:
        with db.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            row = cur.fetchone()
            assert row is not None
            aid = row[0]
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'test', %s)",
                (aid, status),
            )
        db.commit()
        return aid

    @staticmethod
    def _tie(db: psycopg.Connection, agent_id: int, target: int, *, days_ago: float) -> None:
        db.execute(
            "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, "
            "source, agent_id, target_agent_id) "
            "VALUES (%s, now() - (%s * interval '1 day'), 'test', 'test', 'send_message', "
            "'info', 'test', %s, %s)",
            (uuid.uuid4().int % (1 << 62), days_ago, agent_id, target),
        )
        db.commit()

    def test_returns_ranked_neighbor_dataclasses(self, db_conn: psycopg.Connection) -> None:
        a = self._seed(db_conn)
        fresh = self._seed(db_conn)
        stale = self._seed(db_conn, status="terminated")
        self._tie(db_conn, fresh, a, days_ago=0.0)
        self._tie(db_conn, stale, a, days_ago=60.0)

        rows = meiva.agents.get_neighbors(a)

        assert all(isinstance(r, meiva.agents.Neighbor) for r in rows)
        assert [r.agent_id for r in rows] == [fresh, stale]  # recent ranks above stale
        assert rows[1].status is AgentStatus.TERMINATED  # terminated neighbor included
        assert rows[0].depth == 1
        assert rows[0].score > rows[1].score
        assert f"#{fresh}" in str(rows[0]) and "depth=1" in str(rows[0])

    def test_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            meiva.agents.get_neighbors(9999)


# meiva:note
# Omitted depth/limit resolve from the SDK's configuration authority
# (``display.neighbors_default_depth`` / ``display.neighbors_default_limit``, set by
# ``MEIVA_NEIGHBORS_DEFAULT_DEPTH`` / ``MEIVA_NEIGHBORS_DEFAULT_LIMIT``); the literals 1/20 are
# only the fields' defaults, not hard-coded call parameters.
class TestNeighborDefaultsFollowDisplayConfig:
    @pytest.fixture
    def config_authority(self, unit_home: Path, request: pytest.FixtureRequest) -> ConfigAuthority:
        return authority_with(unit_home, **request.param)

    # meiva:note
    # a - near (today) and near - far (today): `far` is two hops from `a`.
    @staticmethod
    def _chain(db: psycopg.Connection) -> tuple[int, int, int]:
        seed = TestGetNeighbors._seed
        tie = TestGetNeighbors._tie
        a, near, far = seed(db), seed(db), seed(db)
        tie(db, near, a, days_ago=0.0)
        tie(db, far, near, days_ago=0.0)
        return a, near, far

    @pytest.mark.parametrize("config_authority", [{"neighbors_default_depth": 2}], indirect=True)
    def test_depth_default_reaches_two_hops(self, db_conn: psycopg.Connection) -> None:
        a, near, far = self._chain(db_conn)

        rows = meiva.agents.get_neighbors(a)

        assert {(r.agent_id, r.depth) for r in rows} == {(near, 1), (far, 2)}

    @pytest.mark.parametrize("config_authority", [{"neighbors_default_limit": 1}], indirect=True)
    def test_limit_default_caps_the_rows(self, db_conn: psycopg.Connection) -> None:
        a, near, _far = self._chain(db_conn)
        extra = TestGetNeighbors._seed(db_conn)
        TestGetNeighbors._tie(db_conn, extra, a, days_ago=30.0)

        rows = meiva.agents.get_neighbors(a)

        assert [r.agent_id for r in rows] == [near]


class TestGetAncestors:
    """SDK get_ancestors maps the gateway `ancestors` rows to Neighbor
    dataclasses. The chain walk itself is covered in
    gateway/tests/agents/test_agent_neighbors.py; here we verify the wrapper + wire
    path only. Ancestry is the immutable `agents_meta.born_spawner` chain, so
    it is seeded on the row; the recorded spawn event only feeds the tie
    graph."""

    @staticmethod
    def _seed(
        db: psycopg.Connection, *, status: str = "running", born_spawner: str | None = None
    ) -> int:
        with db.cursor() as cur:
            cur.execute("INSERT INTO agents DEFAULT VALUES RETURNING id")
            row = cur.fetchone()
            assert row is not None
            aid = row[0]
            cur.execute(
                "INSERT INTO agents_meta (id, spawner, born_spawner, status) "
                "VALUES (%s, 'test', %s, %s)",
                (aid, born_spawner, status),
            )
        db.commit()
        return aid

    @staticmethod
    def _spawn(db: psycopg.Connection, child: int, parent: int) -> None:
        # Event direction: agent_id = the new agent, target_agent_id = spawner.
        db.execute(
            "INSERT INTO audit_events (event_uid, ts, machine, process, event_name, level, "
            "source, agent_id, target_agent_id) "
            "VALUES (%s, now(), 'test', 'test', 'spawn', 'info', 'test', %s, %s)",
            (uuid.uuid4().int % (1 << 62), child, parent),
        )
        db.commit()

    def test_returns_spawn_chain_as_neighbor_dataclasses(self, db_conn: psycopg.Connection) -> None:
        a = self._seed(db_conn, status="terminated")
        b = self._seed(db_conn, born_spawner=f"agent:{a}")
        self._spawn(db_conn, b, a)

        rows = meiva.agents.get_ancestors(b)

        assert all(isinstance(r, meiva.agents.Neighbor) for r in rows)
        assert [r.agent_id for r in rows] == [a]  # nearest ancestor first
        assert rows[0].depth == 1  # hops UP from the queried agent
        assert rows[0].status is AgentStatus.TERMINATED  # terminated parent included
        assert f"#{a}" in str(rows[0]) and "depth=1" in str(rows[0])

    def test_no_spawner_returns_empty(self, db_conn: psycopg.Connection) -> None:
        a = self._seed(db_conn)

        assert meiva.agents.get_ancestors(a) == []

    def test_nonexistent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            meiva.agents.get_ancestors(9999)


class TestListAgents:
    def test_gateway_client_reads_exactly_one_page(self) -> None:
        calls: list[httpx.Request] = []
        page: dict[str, object] = {"agents": [], "next_cursor": 17}

        def answer(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=page)

        with (
            httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(answer)) as http,
            use_client(http),
        ):
            assert (
                gateway_client.list_agents(
                    scope="terminated", query="research", before_id=42, limit=5
                )
                == page
            )
        [request] = calls
        assert (request.method, request.url.path) == ("GET", "/api/agents")
        assert dict(request.url.params) == {
            "scope": "terminated",
            "query": "research",
            "before_id": "42",
            "limit": "5",
        }

    def test_default_scope_includes_all_nonterminated_states(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        ids = [
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
            for _ in range(3)
        ]
        for agent_id, status in zip(ids, ("running", "idling", "terminated"), strict=True):
            db_conn.execute("UPDATE agents_meta SET status = %s WHERE id = %s", (status, agent_id))
        db_conn.commit()

        page = meiva.agents.list_agents()
        assert isinstance(page, meiva.agents.AgentDirectoryPage)
        assert [row.agent_id for row in page.agents] == list(reversed(ids[:2]))
        assert page.next_cursor is None

    def test_terminated_pages_preserve_cursor_and_search(
        self,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentity,
    ) -> None:
        ids = [
            spawn_agent(
                config_authority=config_authority,
                database_gate=database_gate,
                local_machine=machine_identity,
            )
            for _ in range(5)
        ]
        for agent_id in ids:
            db_conn.execute(
                "UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (agent_id,)
            )
            db_conn.execute(
                "UPDATE agents SET label = %s WHERE id = %s", ("archived worker", agent_id)
            )
        db_conn.execute("UPDATE agents SET label = 'unrelated' WHERE id = %s", (ids[2],))
        db_conn.commit()

        first = meiva.agents.list_agents(scope="terminated", query="archived", limit=2)
        assert [row.agent_id for row in first.agents] == [ids[4], ids[3]]
        assert first.next_cursor == ids[3]
        second = meiva.agents.list_agents(
            scope="terminated",
            query="archived",
            before_id=first.next_cursor,
            limit=2,
        )
        assert [row.agent_id for row in second.agents] == [ids[1], ids[0]]
        assert second.next_cursor is None

    def test_empty_page_has_no_cursor(self, db_conn: psycopg.Connection) -> None:
        page = meiva.agents.list_agents()
        assert page.agents == []
        assert page.next_cursor is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"scope": "active"},
            {"limit": 0},
            {"limit": 201},
            {"before_id": 0},
            {"query": "x" * 201},
        ],
    )
    def test_invalid_page_arguments_fail_before_reading(self, kwargs: dict[str, Any]) -> None:
        with gateway_wire() as requests, pytest.raises(ValueError):
            meiva.agents.list_agents(**kwargs)
        assert requests == []

    def test_get_status_uses_direct_detail_not_directory(self) -> None:
        calls: list[httpx.Request] = []

        def answer(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json={"status": "terminated"})

        with (
            httpx.Client(base_url="http://gateway", transport=httpx.MockTransport(answer)) as http,
            use_client(http),
        ):
            assert meiva.agents.get_status(1) == AgentStatus.TERMINATED
        [request] = calls
        assert (request.method, request.url.path) == ("GET", "/api/agents/1")

    def test_get_status_for_missing_agent_raises(self, db_conn: psycopg.Connection) -> None:
        with pytest.raises(AgentNotFound):
            meiva.agents.get_status(999999)

    @pytest.mark.parametrize("forked", [False, True])
    def test_directory_row_preserves_fork_source(
        self, db_conn: psycopg.Connection, forked: bool
    ) -> None:
        source = TestGetNeighbors._seed(db_conn)
        agent_id = TestGetNeighbors._seed(db_conn)
        db_conn.execute(
            "UPDATE agents_meta SET spawner = 'agent:8', fork_source_agent_id = %s, "
            "fork_source_checkpoint_id = %s WHERE id = %s",
            (source if forked else None, "ck" if forked else None, agent_id),
        )
        db_conn.commit()

        page = meiva.agents.list_agents(query=str(agent_id))

        [row] = page.agents
        assert row.agent_id == agent_id
        assert row.fork_source_agent_id == (source if forked else None)
        assert row.spawner == "agent:8"

    def test_agent_row_keeps_domain_fields(
        self, db_conn: psycopg.Connection, self_agent: int
    ) -> None:
        agent_id = meiva.agents.spawn(idempotency_key=str(uuid4()))
        db_conn.execute("UPDATE agents SET label = 'test-agent' WHERE id = %s", (agent_id,))
        db_conn.execute("UPDATE agents_meta SET status = 'running' WHERE id = %s", (agent_id,))
        db_conn.commit()

        page = meiva.agents.list_agents(query=str(agent_id))
        assert len(page.agents) == 1
        row = page.agents[0]
        assert row.agent_id == agent_id
        assert row.status is AgentStatus.RUNNING
        assert row.spawner == f"agent:{self_agent}"
        assert row.fork_source_agent_id is None
        assert row.label == "test-agent"
        assert row.spawned_at is not None and row.last_active_at is not None
        assert row.machine
        assert f"#{agent_id}" in str(row)
        assert "test-agent" in str(row) and "machine=" in str(row)
        assert "pid=" not in str(row)


class TestSpawnConfig:
    # meiva:note
    # spawn(config_overlay=...) stores the validated overlay on the new agent.
    def test_spawn_passes_validated_config(
        self, db_conn: psycopg.Connection, self_agent: int
    ) -> None:
        agent_id = meiva.agents.spawn(
            config_overlay={"llm_model": "claude-sonnet-5"}, idempotency_key=str(uuid4())
        )

        row = db_conn.execute(
            "SELECT config_overlay FROM agents_meta WHERE id = %s", (agent_id,)
        ).fetchone()
        assert row == ({"llm_model": "claude-sonnet-5"},)

    def test_spawn_rejects_preset_overlay_key(self, self_agent: int) -> None:
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay

        with gateway_wire() as requests, pytest.raises(InvalidConfigOverlay):
            meiva.agents.spawn(
                config_overlay={"preset": "coder", "llm_model": "claude-sonnet-5"},
                idempotency_key=str(uuid4()),
            )
        assert requests == []

    def test_spawn_rejects_non_per_agent_config(self, self_agent: int) -> None:
        """spawn(config_overlay=...) rejects fields not marked per_agent — raises before spawning."""
        from base.packages.plugins.plugin_errors import InvalidConfigOverlay

        with gateway_wire() as requests, pytest.raises(InvalidConfigOverlay):
            meiva.agents.spawn(
                config_overlay={"db_url": "postgres://nope"}, idempotency_key=str(uuid4())
            )
        assert requests == []


class TestResurrect:
    @staticmethod
    def _terminated_peer(db: psycopg.Connection) -> int:
        peer = meiva.agents.spawn(idempotency_key=str(uuid4()))
        db.execute("UPDATE agents_meta SET status = 'terminated' WHERE id = %s", (peer,))
        db.commit()
        return peer

    # meiva:note
    # resurrect(agent_id, prompt) wakes the agent and wraps the status.
    def test_resurrect_returns_resurrect_result(
        self, db_conn: psycopg.Connection, self_agent: int
    ) -> None:
        from base.api_contracts.agent_contract import ResurrectResult

        peer = self._terminated_peer(db_conn)

        result = meiva.agents.resurrect(peer, "wake up!")

        assert result == ResurrectResult.SPAWNED
        # meiva:note
        # resurrected_by is set by the client default (the caller's identity), not by the wrapper
        assert ("wake up!", "chat", f"agent:{self_agent}") in inbound_rows(db_conn, peer)

    def test_resurrect_joins_tuple_prompt_before_gateway_call(
        self, db_conn: psycopg.Connection, self_agent: int
    ) -> None:
        from base.api_contracts.agent_contract import ResurrectResult

        peer = self._terminated_peer(db_conn)

        result = meiva.agents.resurrect(peer, ("wake",))  # pyright: ignore[reportArgumentType]

        assert result == ResurrectResult.SPAWNED
        assert ("wake", "chat", f"agent:{self_agent}") in inbound_rows(db_conn, peer)

    def test_resurrect_already_alive(self, db_conn: psycopg.Connection, self_agent: int) -> None:
        """When the agent is still alive, resurrect returns ALREADY_ALIVE."""
        from base.api_contracts.agent_contract import ResurrectResult

        peer = meiva.agents.spawn(idempotency_key=str(uuid4()))

        assert meiva.agents.resurrect(peer, "ping") == ResurrectResult.ALREADY_ALIVE

    def test_resurrect_prompt_is_required_positional(self) -> None:
        """prompt is a required positional arg at the SDK level."""
        import inspect

        from meiva import agents

        sig = inspect.signature(agents.resurrect)
        params = list(sig.parameters.values())
        # agent_id: positional-only-like (no default), prompt: positional-only-like (no default)
        assert params[0].name == "agent_id"
        assert params[0].default is inspect.Parameter.empty
        assert params[1].name == "prompt"
        assert params[1].default is inspect.Parameter.empty


class TestLifecycleResultEnums:
    """TerminateResult/RestartResult must stay in lockstep with the gateway's
    shared enum ownership and schema values — divergent wire values would make
    the SDK ValueError on a successful response."""

    def test_terminate_result_matches_gateway_schema(self) -> None:
        from typing import get_type_hints

        from base.api_contracts.agent_contract import TerminateResult
        from ops.rpc_schemas import TerminateAgentResponse

        assert get_type_hints(TerminateAgentResponse)["status"] is TerminateResult
        schema = TerminateAgentResponse.model_json_schema()
        reference = schema["properties"]["status"]["$ref"]
        assert reference.startswith("#/$defs/")
        values = schema["$defs"][reference.removeprefix("#/$defs/")]["enum"]
        assert values
        assert {m.value for m in TerminateResult} == set(values)

    def test_resurrect_result_matches_gateway_schema(self) -> None:
        from typing import get_type_hints

        from base.api_contracts.agent_contract import ResurrectResult
        from ops.rpc_schemas import ResurrectAgentResponse

        assert get_type_hints(ResurrectAgentResponse)["status"] is ResurrectResult
        schema = ResurrectAgentResponse.model_json_schema()
        reference = schema["properties"]["status"]["$ref"]
        assert reference.startswith("#/$defs/")
        values = schema["$defs"][reference.removeprefix("#/$defs/")]["enum"]
        assert values
        assert {m.value for m in ResurrectResult} == set(values)

    def test_restart_result_matches_gateway_schema(self) -> None:
        from typing import get_type_hints

        from base.api_contracts.agent_contract import RestartResult
        from ops.rpc_schemas import RestartAgentResponse

        assert get_type_hints(RestartAgentResponse)["status"] is RestartResult
        schema = RestartAgentResponse.model_json_schema()
        reference = schema["properties"]["status"]["$ref"]
        assert reference.startswith("#/$defs/")
        values = schema["$defs"][reference.removeprefix("#/$defs/")]["enum"]
        assert values
        assert {m.value for m in RestartResult} == set(values)
