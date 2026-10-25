"""Cluster endpoints cases: admin machine delete."""

from __future__ import annotations

import psycopg
from psycopg.rows import TupleRow

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    pinned_session_names as pinned_session_names,
)
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    seed_agent_on_machine,
    seed_away_machine,
    seed_drain_owner,
    seed_in_progress_task,
)
from gateway.tests.support.runner_ops.ops_stub import RunnerOps, completed, runner_ops


def _existing_row(cur: psycopg.Cursor) -> TupleRow:
    row = cur.fetchone()
    assert row is not None
    return row


class TestAdminMachineDelete:
    def test_deletes_existing_row(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="cloud")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("DELETE FROM machines WHERE name = 'laminar-stale'")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('laminar-stale', ARRAY['gateway'], 'https://example.com')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with gateway_test_client(machine=machine) as client:
            r = client.delete("/api/cluster/machines/laminar-stale")
        assert r.status_code == 200
        assert r.json() == {"deleted": True}
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("SELECT COUNT(*) FROM machines WHERE name = 'laminar-stale'")  # pyright: ignore[reportUnknownMemberType]
            (n,) = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert n == 0

    def test_missing_row_returns_deleted_false(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="cloud")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("DELETE FROM machines WHERE name = 'never-existed'")  # pyright: ignore[reportUnknownMemberType]
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with gateway_test_client(machine=machine) as client:
            r = client.delete("/api/cluster/machines/never-existed")
        assert r.status_code == 200
        assert r.json() == {"deleted": False}

    def test_refuses_to_delete_self(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="cloud")
        with gateway_test_client(machine=machine) as client:
            r = client.delete("/api/cluster/machines/cloud")
        assert r.status_code == 400
        assert "refusing" in r.json()["detail"]


class TestAgentMachineList:
    def test_get_cluster_machines_returns_name_description_live(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity: MachineIdentitySetter,
    ) -> None:  # type: ignore[no-untyped-def]
        # meiva:note
        # One agent-runner row, probed through its own ops server like any other machine
        # (status_probe); the row survives the agent-view filter (only agent-runner machines
        # run agents). The ops server echoes its own machine_name, which the gateway
        # verifies matches the probed row.
        machine: MachineIdentityReader = set_machine_identity(
            role="agent-runner", name="runner-test"
        )
        db_conn.execute("TRUNCATE machines")
        db_conn.commit()
        answer = completed(
            {
                "machine_name": "runner-test",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }
        )
        with runner_ops(db_conn, "runner-test", {"status_probe": answer}):
            db_conn.execute(
                "UPDATE machines SET description = 'voice IO + browser' WHERE name = 'runner-test'"
            )
            db_conn.commit()
            with gateway_test_client(machine=machine) as client:
                r = client.get("/api/cluster/machines")
        assert r.status_code == 200
        body = r.json()
        assert body == [
            {
                "name": "runner-test",
                "description": "voice IO + browser",
                "live": True,
                "is_staging": False,
            }
        ]

    def test_get_cluster_machines_reachable_unknown_is_not_live(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity: MachineIdentitySetter,
    ) -> None:  # type: ignore[no-untyped-def]
        """The agent/config projection must not target a runner whose ops
        server answered but could not provide a determinate status."""
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="cloud-test")
        db_conn.execute("TRUNCATE machines")
        db_conn.commit()

        # meiva:note
        # An answer that is not a ClusterStatus: reachable, status unknown.
        with (
            runner_ops(db_conn, "runner-test", {"status_probe": completed({})}),
            gateway_test_client(machine=machine) as client,
        ):
            response = client.get("/api/cluster/machines")

        assert response.status_code == 200
        assert response.json() == [
            {
                "name": "runner-test",
                "description": None,
                "live": False,
                "is_staging": False,
            }
        ]

    # meiva:note
    # The agent view (`/api/cluster/machines`) lists only machines that run
    # agent processes — the gateway (which runs none) is filtered out.
    def test_get_cluster_machines_excludes_gateway(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity: MachineIdentitySetter,
    ) -> None:  # type: ignore[no-untyped-def]
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="control-test")
        db_conn.execute("TRUNCATE machines")
        db_conn.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES ('control-test', ARRAY['gateway'], 'https://example.com')"
        )
        db_conn.commit()
        answer = completed(
            {
                "machine_name": "runner-test",
                "serve_gateway": False,
                "serve_agent_runner": True,
                "paused": False,
            }
        )

        with runner_ops(db_conn, "runner-test", {"status_probe": answer}):
            db_conn.execute(
                "UPDATE machines SET description = 'voice IO + browser' WHERE name = 'runner-test'"
            )
            db_conn.commit()
            with gateway_test_client(machine=machine) as client:
                r = client.get("/api/cluster/machines")
        assert r.status_code == 200
        body = r.json()
        assert body == [
            {
                "name": "runner-test",
                "description": "voice IO + browser",
                "live": True,
                "is_staging": False,
            }
        ]
        assert all(m["name"] != "control-test" for m in body)

    def test_set_machine_staging_flips_flag_and_excludes_from_roster_targets(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        """POST /api/cluster/machines/{name}/staging flips the operator staging
        flag; a flagged row is still served on the roster (visible) but
        `list_agent_runners`-backed endpoints exclude it. Unknown name → 404."""
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('stage', ARRAY['agent-runner'], NULL)"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]

        with gateway_test_client(machine=machine) as client:
            r = client.post("/api/cluster/machines/stage/staging", json={"is_staging": True})
            assert r.status_code == 200
            assert r.json() == {"deleted": True}

            # unknown machine → 404
            r = client.post("/api/cluster/machines/ghost/staging", json={"is_staging": True})
            assert r.status_code == 404

            # roster still serves the row (staging is visible), with the flag set
            r = client.get("/api/cluster/roster")
            assert r.status_code == 200
            stage_row = next(m for m in r.json() if m["name"] == "stage")
            assert stage_row["is_staging"] is True

            # unmark restores the normal target posture
            r = client.post("/api/cluster/machines/stage/staging", json={"is_staging": False})
            assert r.status_code == 200
            r = client.get("/api/cluster/roster")
            stage_row = next(m for m in r.json() if m["name"] == "stage")
            assert stage_row["is_staging"] is False

    def test_get_cluster_roster_returns_full_status(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        """`/api/cluster/roster` returns the full MachineStatus rows (name/role/
        online/paused), backing the thin `meiva cluster status`."""
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute("TRUNCATE machines")  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], 'https://example.com')"
            )
        db_conn.commit()  # pyright: ignore[reportUnknownMemberType]
        with gateway_test_client(machine=machine) as client:
            r = client.get("/api/cluster/roster")
        assert r.status_code == 200
        body = r.json()
        assert len(body) == 1
        assert body[0]["name"] == "test-host"
        assert body[0]["serve_gateway"] is True
        assert body[0]["serve_agent_runner"] is False
        assert body[0]["online"] is True
        assert "stopped_at" in body[0]


class TestMachinePauseResume:
    def test_pause_drains_terminates_and_hides_from_roster(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity: MachineIdentitySetter,
        *,
        gateway_local_runner: RunnerOps,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:  # type: ignore[no-untyped-def]
        """The full pause contract: tasks of the machine's live agents are
        drained to #405 with a note, every agent is terminated (graceful via
        the in-process lifecycle stub), and the machine vanishes from the
        roster + agent machine list. The row keeps its registration info."""
        # identity carries agent-runner so spawn_agent works; the DB row for
        # the local host stays gateway-only (roster's local lightweight path)
        machine: MachineIdentityReader = set_machine_identity(role="agent-runner", name="test-host")
        seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        seed_drain_owner(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        aid = seed_agent_on_machine(
            db_conn,
            "away",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            machine_identity=machine_identity,
        )  # pyright: ignore[reportUnknownArgumentType]
        seed_agent_on_machine(
            db_conn,
            "away",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            machine_identity=machine_identity,
        )  # pyright: ignore[reportUnknownArgumentType]
        seed_in_progress_task(db_conn, aid, "task-on-away")  # pyright: ignore[reportUnknownArgumentType]
        # meiva:note
        # The away machine's ops server is the runner that executes lifecycle ops in this process.
        db_conn.execute(
            "UPDATE machines SET gateway_url = %s WHERE name = 'away'", (gateway_local_runner.url,)
        )
        db_conn.commit()

        with gateway_test_client(machine=machine) as client:
            r = client.post(
                "/api/cluster/machines/away/pause", json={"reason": "\u4f11\u5047\u4e00\u5468"}
            )
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        assert body["terminated_agents"] == 2
        assert body["force_marked_agents"] == 0
        assert body["reassigned_tasks"] == 1
        assert body["pause_reason"] == "\u4f11\u5047\u4e00\u5468"
        assert body["paused_at"] is not None

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT COUNT(*) FROM agents_meta WHERE machine = 'away' AND status != 'terminated'"
            )
            live_row = _existing_row(cur)
            (n_live,) = live_row  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT owner, results FROM agent_tasks WHERE title = 'task-on-away'"
            )
            task_row = _existing_row(cur)
            owner, results = task_row  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT gateway_url, role FROM machines WHERE name = 'away'"
            )
            machine_row = _existing_row(cur)
            gateway_url, role = machine_row  # pyright: ignore[reportUnknownMemberType]
        assert n_live == 0
        assert owner == 405
        assert "machine pause" in results or "paused" in results
        assert gateway_url == gateway_local_runner.url and role == [
            "agent-runner"
        ]  # registration kept

        # roster + agent machine list hide the paused machine — the cluster
        # shows only its active members (the gateway host itself)
        with gateway_test_client(machine=machine) as client:
            roster = client.get("/api/cluster/roster").json()
            machines_list = client.get("/api/cluster/machines").json()
        assert [m["name"] for m in roster] == ["test-host"]
        assert machines_list == []

    def test_pause_already_paused_is_idempotent(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        """Re-pausing an already-paused machine is a safe no-op: nothing left
        to drain/terminate, the original latch values are returned."""
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with gateway_test_client(machine=machine) as client:
            first = client.post("/api/cluster/machines/away/pause", json={"reason": "once"})
            second = client.post("/api/cluster/machines/away/pause", json={"reason": "twice"})
        assert first.status_code == 200 and second.status_code == 200
        assert first.json()["paused_at"] == second.json()["paused_at"]
        assert second.json()["pause_reason"] == "once"  # first reason preserved
        assert second.json()["terminated_agents"] == 0

    def test_pause_force_marks_when_ops_unreachable(
        self,
        db_conn: psycopg.Connection,
        set_machine_identity: MachineIdentitySetter,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:  # type: ignore[no-untyped-def]
        """A machine whose ops server cannot take the graceful terminate (already
        unreachable) gets its agent rows force-marked terminated in the shared
        DB — pause must not leave agents 'running' on a machine that is leaving."""
        machine: MachineIdentityReader = set_machine_identity(role="agent-runner", name="test-host")
        seed_away_machine(db_conn)
        aid = seed_agent_on_machine(
            db_conn,
            "away",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            machine_identity=machine_identity,
        )
        from base.agents.inbox.insert import insert_inbound_message

        old_chat_id = insert_inbound_message(
            db_conn, aid, "queued before pause", source="user", bus=event_bus, database=database
        )

        # meiva:note
        # The away machine registers no ops URL (`seed_away_machine`), so the graceful terminate
        # cannot be dispatched: the machine is already unreachable.
        with gateway_test_client(machine=machine) as client:
            r = client.post("/api/cluster/machines/away/pause", json={})
        assert r.status_code == 200
        body = r.json()
        assert body["terminated_agents"] == 0
        assert body["force_marked_agents"] == 1
        with db_conn.cursor() as cur:
            cur.execute("SELECT status, termination_source FROM agents_meta WHERE machine = 'away'")
            status_row = cur.fetchone()
            assert status_row is not None
            status, source = status_row
            cur.execute(
                "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='terminate' "
                "ORDER BY id DESC LIMIT 1",
                (aid,),
            )
            terminate_row = cur.fetchone()
            assert terminate_row is not None
            terminate_id = terminate_row[0]
        assert status == "terminated"
        assert source == "user"
        assert old_chat_id < terminate_id

    def test_pause_unknown_machine_404(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        with gateway_test_client(machine=machine) as client:
            r = client.post("/api/cluster/machines/ghost/pause", json={})
        assert r.status_code == 404

    def test_pause_refuses_gateway_own_machine(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        """Pausing the gateway host itself is refused — the cluster needs its
        gateway member online to answer anything."""
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        seed_away_machine(db_conn, name="test-host", local_row=False)  # pyright: ignore[reportUnknownArgumentType]
        with gateway_test_client(machine=machine) as client:
            r = client.post("/api/cluster/machines/test-host/pause", json={})
        assert r.status_code == 400
        assert "refusing" in r.json()["detail"]

    def test_resume_restores_roster_and_is_idempotent(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        """Resume clears the latch: the machine is served on the roster and the
        agent machine list again; resuming a not-paused machine is a no-op
        (resumed=False)."""
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        seed_away_machine(db_conn)  # pyright: ignore[reportUnknownArgumentType]
        with gateway_test_client(machine=machine) as client:
            client.post("/api/cluster/machines/away/pause", json={})
            r = client.post("/api/cluster/machines/away/resume", json={})
            again = client.post("/api/cluster/machines/away/resume", json={})
        assert r.status_code == 200
        assert r.json() == {"name": "away", "resumed": True}
        assert again.json() == {"name": "away", "resumed": False}

        with db_conn.cursor() as cur:  # pyright: ignore[reportUnknownMemberType]
            cur.execute(  # pyright: ignore[reportUnknownMemberType]
                "SELECT paused_at, pause_reason FROM machines WHERE name = 'away'"
            )
            paused_at, pause_reason = cur.fetchone()  # pyright: ignore[reportUnknownMemberType]
        assert paused_at is None and pause_reason is None

        with gateway_test_client(machine=machine) as client:
            roster = client.get("/api/cluster/roster").json()
            machines_list = client.get("/api/cluster/machines").json()
        assert "away" in [m["name"] for m in roster]
        assert "away" in [m["name"] for m in machines_list]

    def test_resume_unknown_machine_404(
        self, db_conn, set_machine_identity: MachineIdentitySetter
    ) -> None:  # type: ignore[no-untyped-def]
        machine: MachineIdentityReader = set_machine_identity(role="gateway", name="test-host")
        with gateway_test_client(machine=machine) as client:
            r = client.post("/api/cluster/machines/ghost/resume", json={})
        assert r.status_code == 404


# meiva:note
# The reserved drain-owner id must not be a number the id sequence hands out. The
# failure this pins is real: with the sequence where the shard's earlier tests left
# it, the second spawn of the pause test took the reserved id and the insert failed
# on `agents_pkey`. The sequence is placed by hand here so the collision is a test
# outcome, not a property of how many agents happened to be born before this file.
# meiva:link gateway/tests/routers/cluster_endpoints/cluster_support.py
def test_a_spawn_never_takes_the_reserved_drain_owner_id(
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute("SELECT setval(pg_get_serial_sequence('agents', 'id'), 404, true)")
    db_conn.commit()

    seed_away_machine(db_conn)
    seed_drain_owner(db_conn)
    born = [
        seed_agent_on_machine(
            db_conn,
            "away",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            machine_identity=machine_identity,
        )
        for _ in range(2)
    ]

    assert born == [406, 407]
