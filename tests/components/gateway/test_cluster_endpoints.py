# meiva:note
# `/api/cluster/*` endpoint and handler unit tests.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest

import gateway.cluster.status as gateway_cluster_status
from base import cluster
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.git import cluster_drift, running_sha
from base.deploy.lifecycle.start_serving import RootBirth
from base.lm.catalog import ModelCatalog
from base.native_process import loaded_commit
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.root_control import client as root_client
from gateway.app import openapi_schema
from gateway.cluster import router
from gateway.events import telemetry_rows
from ops import cluster_status
from ops.cluster_status import pause as cluster_pause
from tests.fixtures.gateway_config import gateway_test_client


@pytest.fixture
def fake_admin_events(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[dict[str, Any]]]:
    """Patch telemetry_rows.query_events for the admin-events route; record the
    kwargs and return canned rows."""

    calls: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    def _query(_conn: object, **kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        calls.append(kwargs)
        return rows, False

    monkeypatch.setattr(telemetry_rows, "query_events", _query)
    return {"calls": calls, "rows": rows}


@pytest.fixture
def fake_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, machine_identity: MachineIdentity
) -> Path:
    """A tmp file standing in for the paused posture row, controllable by the tests.

    R1 (Task #1021) moved `is_paused` to the host_deploy_state posture row; the
    readers' bound names are shimmed to the file's existence so this suite keeps
    simulating the pause with a file. The fixture is about "the process agrees on
    one paused signal", not which storage (the old-signal sweep, PR5, retired the
    real flag file).
    The status/cluster routers read the sync `is_paused`."""
    flag = tmp_path / "cluster_paused"

    def _snapshot_paused(
        _db: object, _state: object = None, *, machine_name: Callable[[], str]
    ) -> bool:
        assert machine_name == machine_identity.name
        return flag.exists()

    def paused(_db: Database, *, machine_name: Callable[[], str]) -> bool:
        assert machine_name == machine_identity.name
        return flag.exists()

    monkeypatch.setattr(router, "cluster_is_paused", paused)
    monkeypatch.setattr(gateway_cluster_status, "cluster_is_paused", paused)
    monkeypatch.setattr(cluster_pause, "is_paused", _snapshot_paused)
    return flag


@pytest.fixture(autouse=True)
def pinned_session_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the session-name composer for gateway cluster tests so composed
    session names are deterministic regardless of the dev host config.
    Patches ``session_name`` directly so ``machine_name()``
    stay untouched (other gateway tests call them via ``set_machine_identity``
    + ``status_snapshot()`` and assert the injected value).
    Produces names like ``meiva-test-agent-host``.

    ``session_name`` is patched at its source (``base.cluster``) because the
    naming scheme is one fact for the whole process — the ``ops`` cluster modules
    reach it through that module precisely so one setattr pins it for all of them.
    The pause tests patch ``base.sessions.backend.get_backend`` to a recording
    fake, so the composed names are what the assertions read.
    """
    monkeypatch.setattr(cluster, "session_name", lambda svc: f"meiva-test-{svc}")  # pyright: ignore[reportUnknownArgumentType]


# meiva:note
# ─── status ───────────────────────────────────────────────────────────────────


class TestClusterStatusEndpoint:
    # meiva:note
    # `/api/cluster/status` answers 200 and reports the posture of a pure gateway.
    def test_cluster_status_reports_a_paused_posture(
        self, fake_flag: Path, set_machine_identity: MachineIdentitySetter
    ) -> None:
        fake_flag.write_text("")
        machine: MachineIdentity = set_machine_identity(role="gateway", name="test-mc")
        with gateway_test_client(machine=machine) as client:
            r = client.get("/api/cluster/status")
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        # meiva:note
        # The pure-gateway local snapshot resolves paused from the posture row
        # alone; the breakdown names that single cause (task #3404).
        assert body["paused_reason"] == "business_pause"
        assert body["machine_name"] == "test-mc"
        assert body["serve_gateway"] is True
        assert body["serve_agent_runner"] is False
        # meiva:note
        # Retired updater projections are absent from the live status wire.
        assert "current_orchestration" not in body
        assert "last_updater_outcome" not in body


# ─── handler business logic ───────────────────────────────────────────────────


class TestRetiredDeploymentEndpoints:
    # meiva:note
    # The retired update and recovery routes do not exist over HTTP.
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("POST", "/api/cluster/update"),
            ("POST", "/api/cluster/rollout"),
            ("POST", "/api/cluster/restart"),
            ("GET", "/api/cluster/update-check"),
            ("POST", "/api/cluster/recover"),
        ],
    )
    def test_removed_ingress_cannot_dispatch(self, method: str, path: str) -> None:
        with gateway_test_client() as client:
            assert client.request(method, path).status_code == 404
        assert path not in openapi_schema()["paths"]


class TestStatusSnapshot:
    def test_snapshot_reflects_flag_and_role(
        self,
        serving_root: RootBirth,
        fake_flag: Path,
        set_machine_identity: MachineIdentitySetter,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from base.deploy.lifecycle import start_serving
        from base.deploy.state import host_deploy_state

        monkeypatch.setattr(start_serving, "run_dir", lambda _home: fake_flag.parent)  # pyright: ignore[reportUnknownArgumentType]
        set_machine_identity(role="agent-runner", name="runner-a")
        host_deploy_state.set_posture(database, "idle", machine=machine_identity.name())
        start_serving.mark_serving(start_serving.begin_start(), runtime=serving_root.runtime)
        # unpaused
        snap = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
        )
        assert snap.machine_name == "runner-a"
        assert snap.serve_gateway is False
        assert snap.serve_agent_runner is True
        assert snap.paused is False
        # paused
        fake_flag.write_text("")
        snap2 = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
        )
        assert snap2.paused is True

    def test_snapshot_includes_head_sha(
        self,
        set_machine_identity: MachineIdentitySetter,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """status_snapshot threads the prod-source HEAD so the roster can compare
        it against the cluster pin."""
        set_machine_identity(role="agent-runner", name="runner-a")
        monkeypatch.setattr(cluster_drift, "prod_source_head_sha", lambda: "abc1234")
        assert (
            cluster_status.status_snapshot(
                database,
                image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
                local_machine=machine_identity,
            ).head_sha
            == "abc1234"
        )

    @pytest.mark.parametrize("sha", ["def5678", None])
    def test_snapshot_includes_running_sha(
        self,
        set_machine_identity: MachineIdentitySetter,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        sha: str | None,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """status_snapshot threads the commit the answering process froze at its
        own boot — distinct from head_sha (the checkout the pin verdict compares)
        so the roster can expose a node running stale code even when its checkout
        reads on-pin."""
        set_machine_identity(role="agent-runner", name="runner-a")

        def unexpected_process_read(_root: Path) -> str | None:
            raise AssertionError("snapshot must use its supplied image")

        monkeypatch.setattr(loaded_commit, "capture_commit", unexpected_process_read)
        assert (
            cluster_status.status_snapshot(
                database,
                image=LoadedCommit(source_root=Path(__file__).parent, sha=sha),
                local_machine=machine_identity,
            ).running_sha
            == sha
        )

    def test_snapshot_ignores_the_start_bookmark(
        self,
        set_machine_identity: MachineIdentitySetter,
        monkeypatch: pytest.MonkeyPatch,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        """A start that restarted nothing must not be able to make this node look
        current.

        `meiva start` writes `$MEIVA_HOME/running_sha` from the fresh HEAD one line
        before a launcher that skips already-running sessions, so after a
        no-op start the bookmark and the checkout agree while the live processes
        still hold the old code. Sourcing the snapshot from the bookmark made
        that state indistinguishable from a real restart — every roster consumer
        saw running_sha == head_sha for a node on days-old code. The snapshot
        answers from the process, so the stale commit survives the bookmark's
        advance and the divergence every drift renderer keys on is there."""
        set_machine_identity(role="agent-runner", name="runner-a")
        monkeypatch.setattr(running_sha, "get", lambda: "n3wn3w0bbbb")
        monkeypatch.setattr(cluster_drift, "prod_source_head_sha", lambda: "n3wn3w0bbbb")

        snap = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha="0ld0ld0aaaa"),
            local_machine=machine_identity,
        )

        assert snap.running_sha == "0ld0ld0aaaa"
        assert snap.head_sha == "n3wn3w0bbbb"
        assert snap.running_sha != snap.head_sha

    @pytest.mark.parametrize("online", [True, False, None])
    def test_supervisor_status_preserves_unavailable_inspection(
        self,
        fake_flag: Path,
        set_machine_identity: MachineIdentitySetter,
        monkeypatch: pytest.MonkeyPatch,
        online: bool | None,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        del fake_flag
        set_machine_identity(role="agent-runner", name="test-host")

        def inspect_root(*, home: Path) -> object | None:
            if online is None:
                raise RuntimeError("native root inspection unavailable")
            return object() if online else None

        monkeypatch.setattr(root_client, "root_process", inspect_root)
        assert (
            cluster_status.status_snapshot(
                database,
                image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
                local_machine=machine_identity,
            ).supervisor_online
            is online
        )


# ─── cluster endpoints via TestClient ─────────────────────────────────────────


class TestClusterEndpoints:
    @pytest.mark.parametrize("route", ["/api/cluster/stop", "/api/cluster/resume"])
    def test_the_legacy_deploy_lease_transition_is_gone(self, route: str) -> None:
        """The legacy lease-bound stop/resume routes and their ops kinds no longer exist."""
        with gateway_test_client() as client:
            r = client.post(
                route,
                json={"deploy_holder": "g:pid1", "deploy_acquired_at": "2026-08-25T00:00:00Z"},
            )
        assert r.status_code in {404, 405}

    def test_post_stopping_marks_machine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """POST /api/cluster/stopping?machine=<name>&home=<home> retracts that unit."""
        marked: list[tuple[str, str, int | None, str | None]] = []
        from ops.cluster import operations as ops_mod

        def mark_stopping(
            _db: object,
            name: str,
            home: str,
            *,
            maintenance_for_seconds: int | None = None,
            maintenance_reason: str | None = None,
        ) -> None:
            marked.append((name, home, maintenance_for_seconds, maintenance_reason))

        monkeypatch.setattr(ops_mod, "mark_stopping", mark_stopping)
        with gateway_test_client() as client:
            r = client.post(
                "/api/cluster/stopping", params={"machine": "runner-a", "home": "~/.meiva"}
            )
            assert r.status_code == 200
            assert r.json() == {"machine": "runner-a"}
            assert marked == [("runner-a", "~/.meiva", None, None)]

            windowed = client.post(
                "/api/cluster/stopping",
                params={
                    "machine": "runner-b",
                    "home": "~/.meiva",
                    "maintenance_for_seconds": 2700,
                    "maintenance_reason": "pgbouncer audit",
                },
            )
        assert windowed.status_code == 200
        assert marked[-1] == ("runner-b", "~/.meiva", 2700, "pgbouncer audit")


# ─── admin events query ──────────────────────────────────────────────────────


# ─── admin: DELETE /api/cluster/machines/{name} ──────────────────────────────


# ─── agent roster: GET /api/cluster/machines ─────────────────────────────────


# ─── machines pause / resume (Task #1283) ────────────────────────────────────


def seed_away_machine(
    db_conn: psycopg.Connection, *, name: str = "away", local_row: bool = True
) -> None:
    """Machines rows for the pause tests, isolated from other tests' rows: the
    paused target (`name`, agent-runner) plus, when `local_row`, this gateway
    host's own row (gateway-only in the DB — the roster's local lightweight
    path, so no probe dial; the test identity itself carries agent-runner so
    spawn_agent works)."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        if local_row:
            cur.execute(
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], NULL)"
            )
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES (%s, ARRAY['agent-runner'], NULL)",
            (name,),
        )
    db_conn.commit()


def seed_agent_on_machine(
    db_conn: psycopg.Connection,
    machine: str,
    *,
    status: str = "idling",
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentity,
) -> int:
    """One live agent row homed on `machine` (test spawn helper + machine
    stamp; the row-creation path moved gateway-side, Task #1236 follow-up)."""
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
            (machine, status, aid),
        )
    db_conn.commit()
    return aid


# meiva:note
# The pause endpoint reassigns a paused machine's tasks to a fixed drain owner
# (`_MACHINE_PAUSE_DRAIN_OWNER`, #405), so the row has to exist here as the FK
# target. Writing an id by hand does not move the id sequence, and the next spawn
# then takes a number that is already in the table: with two spawns, the second
# took 405 and the insert failed on `agents_pkey`. Whether that happens depends on
# how many agents the tests before this one created, which is how a green suite
# went red after unrelated tests were added. Moving the sequence past every id the
# table holds makes the manual row a reservation instead of a trap.
# meiva:link gateway/tests/routers/cluster_endpoints/test_admin_machine_delete.py
def seed_drain_owner(db_conn: psycopg.Connection, agent_id: int = 405) -> None:
    """The drain-owner agent row the pause endpoint reassigns tasks to (the
    FK target `agent_tasks.owner -> agents(id)`)."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (agent_id,),
        )
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('agents', 'id'), (SELECT max(id) FROM agents))"
        )
    db_conn.commit()


def seed_in_progress_task(db_conn: psycopg.Connection, owner: int, title: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by) "
            "VALUES (%s, 'desc', 'in_progress', %s, 'user') RETURNING id",
            (title, owner),
        )
        row = cur.fetchone()
        assert row is not None
        (task_id,) = row
    db_conn.commit()
    return task_id
