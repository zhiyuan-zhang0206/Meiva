# meiva:note
# Cross-machine terminate forward (`gateway/agents/lifecycle.py:post_agent_terminate`) tests —
#
# Both graceful and force requests route to the home runner. It owns the hosted
# turn and local execution resources; gateway placement must not choose a local
# shortcut for a remote agent. A remote home is a runner served on a real listener
# (`gateway/tests/support/runner_ops`) and registered in `machines`; the gateway's own machine
# is the in-process runner `gateway_local_runner` registers.
#
# `TestTerminateOpenTasksHint` covers the advisory `open_tasks` hint the gateway
# attaches to the response once the forward returns.

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg
import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import MachineIdentitySetter
from gateway.tests.auth.gateway_config import gateway_test_client, spawn_via_http
from gateway.tests.support.runner_ops.ops_stub import (
    RunnerOps,
    completed,
    failed,
    refused,
    runner_ops,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OVERLAY_PROBE = Path(__file__).with_name("child_restart_overlay.py")


@pytest.fixture
def local_machine(
    set_machine_identity: MachineIdentitySetter, gateway_local_runner: RunnerOps
) -> MachineIdentity:
    """The gateway's own machine, serving as an agent-runner through its in-process ops route."""
    return set_machine_identity(role="agent-runner")


def _set_agent_machine(db_conn: psycopg.Connection, agent_id: int, machine: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s WHERE id = %s",
            (machine, agent_id),
        )
    db_conn.commit()


def _terminate_inbound_count(db_conn: psycopg.Connection, agent_id: int) -> int:
    row = db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id = %s AND kind = 'terminate'",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row[0]


class TestTerminateRouting:
    # meiva:note
    # machine == local + force=false → the home runner is the gateway's own machine and
    # executes the termination: it INSERTs the terminate inbound.
    def test_local_graceful_takes_local_path(
        self,
        local_machine: MachineIdentity,
        gateway_local_runner: RunnerOps,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, gateway_local_runner.name)
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json()["status"] == "enqueued"
        assert _terminate_inbound_count(db_conn, agent_id) == 1
        assert [c.kind for c in gateway_local_runner.calls] == ["spawn-launch", "lifecycle"]

    def test_local_force_takes_local_path(
        self,
        local_machine: MachineIdentity,
        gateway_local_runner: RunnerOps,
        db_conn: psycopg.Connection,
    ) -> None:
        """A local force request is accepted by the same home-runner path."""
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, gateway_local_runner.name)
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "user"},
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "enqueued"

    def test_remote_graceful_forwards(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        """machine != local + force=false → forward, local does not INSERT inbound."""
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": completed({"status": "enqueued"})}) as (
                runner
            ),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"message": "retain this note"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
        (call,) = runner.calls_of("lifecycle")
        assert call.payload["path"] == f"/api/agents/{agent_id}/terminate"
        assert call.payload["body"]["message"] == "retain this note"
        # meiva:note
        # The envelope carries the dedup key the ops server replays a retried request by.
        assert call.idempotency_key is not None
        assert call.idempotency_key.startswith("lifecycle:")
        assert _terminate_inbound_count(db_conn, agent_id) == 0

    def test_remote_force_forwards(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        """machine != local + force=true → forward, local does not touch sessions / pid."""
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": completed({"status": "enqueued"})}) as (
                runner
            ),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "source": "user"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
        (body,) = [payload["body"] for payload in runner.payloads("lifecycle")]
        assert body["force"] is True
        assert body["source"] == "user"

    # meiva:note
    # A failed op that names a wire reason is re-raised as that error on the gateway.
    def test_a_runners_wire_error_keeps_its_status_and_reason(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        refusal = failed(
            {
                "error": "MachineNotRegistered: remote-mac",
                "detail": "machine 'remote-mac' not in machines table",
                "reason": "machine_not_registered",
            }
        )
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": refusal}),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 404
        assert resp.json()["reason"] == "machine_not_registered"

    # meiva:note
    # A home machine the registry has never seen cannot be dialed: 502, named.
    def test_an_unregistered_home_machine_is_a_gateway_error(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "ghost-mac")
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"
        assert "ghost-mac" in resp.json()["detail"]

    def test_cross_machine_gateway_unavailable_propagates_502(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": refused(404, "no ops server here")}),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 502
        assert resp.json()["reason"] == "cross_machine_gateway_unavailable"

    def test_nonexistent_agent_404(self, local_machine: MachineIdentity) -> None:
        """Nonexistent → helper itself raises AgentNotFound (404)."""
        with gateway_test_client(machine=local_machine) as client:
            resp = client.post("/api/agents/9999/terminate")
        assert resp.status_code == 404


def test_remote_home_machine_is_forwarded(
    local_machine: MachineIdentity, db_conn: psycopg.Connection
) -> None:
    """A remote agents_meta.machine row is always forwarded to that host's ops
    server — there is no deployment gate on the lifecycle path. Single box is
    just the special case where the ops server lives at localhost."""
    with (
        runner_ops(db_conn, "stale-runner", {"lifecycle": completed({"status": "enqueued"})}) as (
            runner
        ),
        gateway_test_client(machine=local_machine) as client,
    ):
        agent_id = spawn_via_http(client, {}).json()["id"]
        _set_agent_machine(db_conn, agent_id, "stale-runner")
        resp = client.post(f"/api/agents/{agent_id}/terminate")
    assert resp.status_code == 200
    assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}
    assert [call.kind for call in runner.calls] == ["lifecycle"]


# meiva:note
# A restart's overlay is validated at the gateway: a valid one rides to the home runner
# unchanged, an invalid one is a 422 and never leaves the gateway.
def test_restart_overlay_is_validated_before_it_is_forwarded(
    local_machine: MachineIdentity, db_conn: psycopg.Connection
) -> None:
    with (
        runner_ops(db_conn, "remote-runner", {"lifecycle": completed({"status": "enqueued"})}) as (
            runner
        ),
        gateway_test_client(machine=local_machine) as client,
    ):
        agent_id = spawn_via_http(client, {}).json()["id"]
        _set_agent_machine(db_conn, agent_id, "remote-runner")
        valid = client.post(
            f"/api/agents/{agent_id}/restart",
            json={"config_overlay": {"completion_notice_policy": "hourly"}},
        )
        invalid = client.post(
            f"/api/agents/{agent_id}/restart",
            json={"config_overlay": {"completion_notice_policy": "bogus"}},
        )

    assert valid.status_code == 200
    assert valid.json() == {"status": "enqueued"}
    assert invalid.status_code == 422
    assert runner.payloads("lifecycle") == [
        {
            "path": f"/api/agents/{agent_id}/restart",
            "body": {"source": "user", "config_overlay": {"completion_notice_policy": "hourly"}},
        }
    ]


# meiva:note
# The gateway process builds only its own config domains; the restart overlay check must
# not construct the agent domain. Run it in a process with the gateway's profile.
def test_overlay_validation_needs_no_agent_domain_in_a_gateway_process(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(MEIVA_HOME=str(home), MEIVA_CONFIG_FETCH="skip", MEIVA_PROCESS_PROFILE="gateway")
    # meiva:note
    # fixed argv, sys.executable is trusted.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", str(_OVERLAY_PROBE)],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == {
        "valid": "accepted",
        "invalid": "rejected",
    }


def _insert_open_task(
    db_conn: psycopg.Connection,
    *,
    owner: int,
    title: str,
    status: str = "in_progress",
    age_minutes: int = 0,
) -> int:
    """Seed one agent_tasks row for `owner`, `age_minutes` old."""
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, created_by, owner, updated_at) "
            "VALUES (%s, '', %s, 'user', %s, now() - make_interval(mins => %s)) RETURNING id",
            (title, status, owner, age_minutes),
        )
        task_id: int = cur.fetchone()[0]  # type: ignore[index]
    db_conn.commit()
    return task_id


class TestTerminateOpenTasksHint:
    """The terminate response carries `open_tasks` — the agent's still-open
    tasks, read at the gateway after the home runner accepts the termination."""

    def test_no_open_tasks_reports_null(
        self,
        local_machine: MachineIdentity,
        gateway_local_runner: RunnerOps,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, gateway_local_runner.name)
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        assert resp.json() == {"status": "enqueued", "open_tasks": None, "shell_sessions": None}

    def test_open_tasks_reported_newest_first(
        self,
        local_machine: MachineIdentity,
        gateway_local_runner: RunnerOps,
        db_conn: psycopg.Connection,
    ) -> None:
        """Only in_progress counts; rows come newest first."""
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, gateway_local_runner.name)
            _insert_open_task(
                db_conn, owner=agent_id, title="done task", status="done", age_minutes=1
            )
            _insert_open_task(
                db_conn, owner=agent_id, title="cancelled task", status="cancelled", age_minutes=2
            )
            older = _insert_open_task(
                db_conn, owner=agent_id, title="older open task", age_minutes=30
            )
            newer = _insert_open_task(
                db_conn, owner=agent_id, title="newer in-progress task", age_minutes=5
            )
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        hint = resp.json()["open_tasks"]
        assert hint["count"] == 2
        assert hint["more"] == 0
        assert [(task["id"], task["status"]) for task in hint["tasks"]] == [
            (newer, "in_progress"),
            (older, "in_progress"),
        ]
        assert hint["tasks"][0]["title"] == "newer in-progress task"
        assert datetime.fromisoformat(hint["tasks"][0]["updated_at"])

    def test_more_than_five_truncates_to_the_five_newest(
        self,
        local_machine: MachineIdentity,
        gateway_local_runner: RunnerOps,
        db_conn: psycopg.Connection,
    ) -> None:
        with gateway_test_client(machine=local_machine) as client:
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, gateway_local_runner.name)
            seeded = [
                _insert_open_task(
                    db_conn, owner=agent_id, title=f"open task {i}", age_minutes=10 - i
                )
                for i in range(7)
            ]
            resp = client.post(f"/api/agents/{agent_id}/terminate")
        assert resp.status_code == 200
        hint = resp.json()["open_tasks"]
        assert hint["count"] == 7
        assert hint["more"] == 2
        assert [task["id"] for task in hint["tasks"]] == list(reversed(seeded))[:5]


def _seed_shell_ttls(db_conn: psycopg.Connection, agent_id: int, session_ids: list[int]) -> None:
    with db_conn.cursor() as cur:
        for session_id in session_ids:
            cur.execute(
                "INSERT INTO agent_shell_ttls (agent_id, session_id, expires_at) "
                "VALUES (%s, %s, now() + interval '1 hour')",
                (agent_id, session_id),
            )
    db_conn.commit()


def _shell_ttl_ids(db_conn: psycopg.Connection, agent_id: int) -> list[int]:
    rows = db_conn.execute(
        "SELECT session_id FROM agent_shell_ttls WHERE agent_id = %s ORDER BY session_id",
        (agent_id,),
    ).fetchall()
    return [row[0] for row in rows]


class TestTerminateShellSessions:
    """`kill_all_shell_sessions` rides to the home runner, whose report comes
    back verbatim in `shell_sessions`; the gateway then drops the TTL rows of
    the sessions it reports killed (the runner holds no DELETE on them)."""

    def test_forwards_the_option_and_drops_killed_sessions_ttl_rows(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        report = completed(
            {"status": "enqueued", "shell_sessions": {"when": "now", "killed": [0, 1]}}
        )
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": report}) as runner,
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            _seed_shell_ttls(db_conn, agent_id, [0, 1, 2])
            resp = client.post(
                f"/api/agents/{agent_id}/terminate",
                json={"force": True, "kill_all_shell_sessions": True},
            )
        assert resp.status_code == 200
        assert resp.json()["shell_sessions"] == {"when": "now", "killed": [0, 1]}
        (call,) = runner.calls_of("lifecycle")
        assert call.payload["body"]["kill_all_shell_sessions"] is True
        # Session 2 is gone too, but not by this kill: the TTL reaper retires it.
        assert _shell_ttl_ids(db_conn, agent_id) == [2]

    def test_at_exit_kill_leaves_ttl_rows_to_the_reaper(
        self, local_machine: MachineIdentity, db_conn: psycopg.Connection
    ) -> None:
        report = completed(
            {"status": "enqueued", "shell_sessions": {"when": "at_exit", "killed": []}}
        )
        with (
            runner_ops(db_conn, "remote-mac", {"lifecycle": report}),
            gateway_test_client(machine=local_machine) as client,
        ):
            agent_id = spawn_via_http(client, {}).json()["id"]
            _set_agent_machine(db_conn, agent_id, "remote-mac")
            _seed_shell_ttls(db_conn, agent_id, [0])
            resp = client.post(
                f"/api/agents/{agent_id}/terminate", json={"kill_all_shell_sessions": True}
            )
        assert resp.status_code == 200
        assert resp.json()["shell_sessions"] == {"when": "at_exit", "killed": []}
        assert _shell_ttl_ids(db_conn, agent_id) == [0]
