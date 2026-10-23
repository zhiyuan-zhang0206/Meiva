# meiva:note
# `/api/cluster/*` endpoint and handler unit tests.

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.git import cluster_drift, running_sha
from base.deploy.lifecycle.start_serving import RootBirth
from base.native_process import loaded_commit
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.root_control import client as root_client
from gateway.app import openapi_schema
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    pinned_session_names as pinned_session_names,
)
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    seed_agent_on_machine as seed_agent_on_machine,
)
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    seed_away_machine as seed_away_machine,
)
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    seed_drain_owner as seed_drain_owner,
)
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    seed_in_progress_task as seed_in_progress_task,
)
from ops import cluster_status
from tests.fixtures.gateway_config import gateway_test_client


# meiva:note
# This case's serving gate: kept under its tmp dir and already at `serving`.
@pytest.fixture
def serving_gate(serving_root: RootBirth, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from base.deploy.lifecycle import start_serving

    def run_dir(_home: Path) -> Path:
        return tmp_path

    monkeypatch.setattr(start_serving, "run_dir", run_dir)
    start_serving.mark_serving(start_serving.begin_start(), runtime=serving_root.runtime)


# meiva:note
# ─── status ───────────────────────────────────────────────────────────────────


class TestClusterStatusEndpoint:
    # meiva:note
    # `/api/cluster/status` answers 200 for a pure gateway and reads its serving gate:
    # a stop clears the gate, and the machine reports `startup` from then on.
    @pytest.mark.usefixtures("serving_gate")
    def test_a_pure_gateway_reports_startup_once_a_stop_has_begun(
        self, set_machine_identity: MachineIdentitySetter
    ) -> None:
        from base.deploy.lifecycle import start_serving

        machine: MachineIdentity = set_machine_identity(role="gateway", name="test-mc")
        with gateway_test_client(machine=machine) as client:
            serving = client.get("/api/cluster/status").json()
            start_serving.clear_serving()
            r = client.get("/api/cluster/status")
        assert serving["paused"] is False
        assert serving["paused_reason"] is None
        assert r.status_code == 200
        body = r.json()
        assert body["paused"] is True
        assert body["paused_reason"] == "startup"
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
    @pytest.mark.usefixtures("serving_gate")
    def test_snapshot_reflects_the_serving_gate_and_role(
        self,
        set_machine_identity: MachineIdentitySetter,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
        from base.deploy.lifecycle import start_serving

        set_machine_identity(role="agent-runner", name="runner-a")
        # meiva:note
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
        # meiva:note
        # a stop clears the gate
        start_serving.clear_serving()
        snap2 = cluster_status.status_snapshot(
            database,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
        )
        assert snap2.paused is True
        assert snap2.paused_reason == "startup"

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
        set_machine_identity: MachineIdentitySetter,
        monkeypatch: pytest.MonkeyPatch,
        online: bool | None,
        database: Database,
        *,
        machine_identity: MachineIdentity,
    ) -> None:
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
