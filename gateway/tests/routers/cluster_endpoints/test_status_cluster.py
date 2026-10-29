# meiva:note
# /api/status cluster sub-section integration tests.
#
# Agent-runners do not run a local gateway; probes dial the agent-runner's ops server directly
# to run the status_probe op (same source as the CLI `meiva cluster status`). Each runner here is a
# real listener on port 0, registered in `machines` (`gateway/tests/support/runner_ops`), so the SystemStatus
# cluster data pipeline runs from the registry read to the rendered row.

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.deploy.lifecycle.tests.serving_root import observe, observe_unreadable
from base.deploy.lifecycle.tests.serving_root import serving_root as serving_root
from gateway.cluster.status import StatusCache
from gateway.tests.auth.gateway_config import gateway_test_client, gateway_test_process
from gateway.tests.routers.cluster_endpoints.cluster_support import serving_gate as serving_gate
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    Responder,
    completed,
    refused,
    runner_ops,
)


@pytest.fixture(autouse=True)
def truncate_machines(db_conn: psycopg.Connection) -> None:
    """The conftest db_conn TRUNCATE list does not include machines — this test suite
    clears it itself. autouse so every test starts with an empty machines table."""
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
    db_conn.commit()


@pytest.fixture
def stub_machine_identity(set_machine_identity: MachineIdentitySetter) -> MachineIdentityReader:
    """Set this unit's identity (gateway, name 'cloud-test') at the source
    so every machine_name() / machine_role() call site resolves it without
    per-module patching."""
    return set_machine_identity(role="gateway", name="cloud-test")


# meiva:note
# Direct INSERT of a machines row (bypassing register_self) — for test isolation.
#
# `role` is a comma-separated capability set ("gateway,agent-runner") — the
# machines.role column is a TEXT[] of capability tokens.
def _insert_machine(
    conn: psycopg.Connection,
    name: str,
    gateway_url: str | None,
    role: str,
    description: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO machines (name, gateway_url, role, description) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (name) DO UPDATE SET gateway_url=EXCLUDED.gateway_url, "
            "role=EXCLUDED.role, description=EXCLUDED.description, up_since_at=NOW()",
            (name, gateway_url, [t for t in role.split(",") if t], description),
        )
    conn.commit()


def _git(source: Path, *args: str) -> str:
    # meiva:note
    # The command is a fixed argv of the interpreter and an in-repo script: no shell, no user input.
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(source), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _probe_answer(name: str, *, paused: bool = False, **extra: object) -> OpsReply:
    return completed(
        {
            "machine_name": name,
            "serve_gateway": False,
            "serve_agent_runner": True,
            "paused": paused,
            **extra,
        }
    )


# meiva:note
# A runner that numbers its probes: its n-th answer reports `shell_count = n`, so the answer a
# response carries shows how many probes the gateway had made when it was rendered.
def _numbered_probes(name: str) -> Responder:
    numbers = itertools.count(1)

    def answer(call: OpsCall) -> OpsReply:
        del call
        return _probe_answer(name, shell_count=next(numbers))

    return answer


def _shell_count(response: Any) -> int:
    (machine,) = response.json()["cluster"]["machines"]
    return machine["shell_count"]


def _assert_local_machine_row(row: dict[str, Any]) -> None:
    # meiva:note
    # the local gateway row is a local read — online=True, serving gate open
    assert row["online"] is True
    assert row["paused"] is False
    assert row["serve_gateway"] is True
    assert row["serve_agent_runner"] is False
    assert row["description"] == "central node"


def _assert_remote_machine_rows(by_name: dict[str, dict[str, Any]]) -> None:
    # meiva:note
    # remote rows come from the runners' own answers
    assert by_name["test-host"]["online"] is True
    assert by_name["test-host"]["paused"] is False
    assert by_name["test-host"]["serve_gateway"] is False
    assert by_name["test-host"]["serve_agent_runner"] is True
    assert by_name["runner-test"]["online"] is False
    assert by_name["runner-test"]["paused"] is None
    assert by_name["runner-test"]["serve_gateway"] is False
    assert by_name["runner-test"]["serve_agent_runner"] is True


class TestClusterPanel:
    # meiva:note
    # current_* fields reflect local machine perspective.
    def test_current_machine_role_paused_from_local(
        self, serving_gate: None, stub_machine_identity: MachineIdentityReader
    ) -> None:
        # meiva:note
        # `current_paused` is the marked birth as the process's observation confirms it: answered
        # it is open, unanswerable the same marker reads paused.
        with (
            gateway_test_process(
                machine=stub_machine_identity, observe=observe_unreadable
            ) as unreadable_process,
            gateway_test_client(process=unreadable_process) as unreadable_client,
        ):
            unreadable = unreadable_client.get("/api/status")
        with (
            gateway_test_process(machine=stub_machine_identity, observe=observe) as process,
            gateway_test_client(process=process) as client,
        ):
            r = client.get("/api/status")
        assert unreadable.json()["cluster"]["current_paused"] is True
        assert r.status_code == 200
        data = r.json()
        # meiva:note
        # Scheduler panel removed in watcher-unify; shells folded per-machine into
        # cluster.machines — /api/status carries only services / cluster now.
        assert "scheduler" not in data
        assert set(data) >= {"services", "cluster"}
        c = data["cluster"]
        assert c["current_machine"] == "cloud-test"
        assert c["current_serve_gateway"] is True
        assert c["current_serve_agent_runner"] is False
        assert c["current_paused"] is False

    # meiva:note
    # A serving gate that has not opened (a start in flight, a stop begun) shows in the cluster
    # sub-section as `current_paused`.
    def test_current_paused_true_while_the_gate_is_not_serving(
        self, stub_machine_identity: MachineIdentityReader
    ) -> None:
        with gateway_test_client(machine=stub_machine_identity) as client:
            r = client.get("/api/status")
        assert r.json()["cluster"]["current_paused"] is True

    # meiva:note
    # gateway row -> the local read (always online);
    # agent-runner rows -> the runner's own status_probe answer.
    def test_machines_list_local_plus_remote(
        self,
        db_conn: psycopg.Connection,
        serving_gate: None,
        stub_machine_identity: MachineIdentityReader,
    ) -> None:
        _insert_machine(
            db_conn, "cloud-test", "https://meiva.example.com", "gateway", "central node"
        )
        with (
            runner_ops(db_conn, "test-host", {"status_probe": _probe_answer("test-host")}),
            runner_ops(db_conn, "runner-test", {"status_probe": refused(404, "probe failed")}),
            gateway_test_process(machine=stub_machine_identity, observe=observe) as process,
            gateway_test_client(process=process) as client,
        ):
            r = client.get("/api/status")
        assert r.status_code == 200
        machines = r.json()["cluster"]["machines"]
        assert len(machines) == 3
        by_name = {m["name"]: m for m in machines}
        _assert_local_machine_row(by_name["cloud-test"])
        _assert_remote_machine_rows(by_name)
        # meiva:note
        # The local row's `paused` is that same observation: an unanswerable root pauses it.
        with (
            runner_ops(db_conn, "test-host", {"status_probe": _probe_answer("test-host")}),
            runner_ops(db_conn, "runner-test", {"status_probe": refused(404, "probe failed")}),
            gateway_test_process(
                machine=stub_machine_identity, observe=observe_unreadable
            ) as unreadable_process,
            gateway_test_client(process=unreadable_process) as unreadable_client,
        ):
            unreadable = unreadable_client.get("/api/status")
        local = {m["name"]: m for m in unreadable.json()["cluster"]["machines"]}["cloud-test"]
        assert local["paused"] is True

    # meiva:note
    # A pure observability-station row (machines.role carries only the
    # station token) renders serve_observability_station=True and the two
    # legacy flags False — the roster must not lose the station capability
    # when deriving the flag triple from the role column.
    def test_station_machine_row_carries_station_capability(
        self,
        db_conn: psycopg.Connection,
        serving_gate: None,
        stub_machine_identity: MachineIdentityReader,
    ) -> None:
        _insert_machine(db_conn, "station-a", None, "observability-station")
        answer = _probe_answer("combo")
        with (
            runner_ops(
                db_conn,
                "combo",
                {"status_probe": answer},
                roles=("gateway", "observability-station"),
            ),
            gateway_test_client(machine=stub_machine_identity) as client,
        ):
            r = client.get("/api/status")
        assert r.status_code == 200
        by_name = {m["name"]: m for m in r.json()["cluster"]["machines"]}
        assert by_name["station-a"]["serve_observability_station"] is True
        assert by_name["station-a"]["serve_gateway"] is False
        assert by_name["station-a"]["serve_agent_runner"] is False
        assert by_name["combo"]["serve_observability_station"] is True
        assert by_name["combo"]["serve_gateway"] is True
        assert by_name["combo"]["serve_agent_runner"] is False

    # meiva:note
    # Regression: PR #466 made machines.gateway_url nullable for agent-runner;
    # pre-fix `_probe_machine(url.rstrip(...))` crashed with AttributeError
    # and the outer try/except masked it as cluster.machines=[].
    def test_agent_runner_gateway_url_null_no_crash(
        self,
        db_conn: psycopg.Connection,
        serving_gate: None,
        stub_machine_identity: MachineIdentityReader,
    ) -> None:
        _insert_machine(db_conn, "runner-test", None, "agent-runner")

        with gateway_test_client(machine=stub_machine_identity) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        machines = r.json()["cluster"]["machines"]
        assert len(machines) == 1
        assert machines[0]["name"] == "runner-test"
        assert machines[0]["online"] is False
        assert machines[0]["paused"] is None
        # meiva:note
        # NULL → ""
        assert machines[0]["gateway_url"] == ""

    def test_empty_machines_table(self, stub_machine_identity: MachineIdentityReader) -> None:
        with gateway_test_client(machine=stub_machine_identity) as client:
            r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["cluster"]["machines"] == []

    def test_response_cache_reuses_remote_probe(
        self, db_conn: psycopg.Connection, stub_machine_identity: MachineIdentityReader
    ) -> None:
        with (
            runner_ops(db_conn, "runner-test", {"status_probe": _numbered_probes("runner-test")}),
            gateway_test_client(machine=stub_machine_identity) as client,
        ):
            first = client.get("/api/status")
            second = client.get("/api/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert (_shell_count(first), _shell_count(second)) == (1, 1)

    def test_response_cache_ttl_zero_reprobes(
        self, db_conn: psycopg.Connection, stub_machine_identity: MachineIdentityReader
    ) -> None:
        with (
            runner_ops(db_conn, "runner-test", {"status_probe": _numbered_probes("runner-test")}),
            gateway_test_client(machine=stub_machine_identity) as client,
        ):
            client.app.state.status_cache = StatusCache(ttl_s=0.0)
            first = client.get("/api/status")
            second = client.get("/api/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert (_shell_count(first), _shell_count(second)) == (1, 2)

    # meiva:note
    # A transport reset recovered by the roster's retry is cached as the
    # successful final verdict, not as a transient offline row.
    def test_response_cache_keeps_retry_recovered_result(
        self, db_conn: psycopg.Connection, stub_machine_identity: MachineIdentityReader
    ) -> None:
        answers = iter([refused(503, "connection reset"), _probe_answer("runner-test")])

        def reset_then_answer(call: OpsCall) -> OpsReply:
            del call
            return next(answers)

        with (
            runner_ops(db_conn, "runner-test", {"status_probe": reset_then_answer}),
            gateway_test_client(machine=stub_machine_identity) as client,
        ):
            first = client.get("/api/status")
            second = client.get("/api/status")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["cluster"]["machines"][0]["online"] is True
        assert second.json()["cluster"]["machines"][0]["online"] is True


class TestPanelCarriesNoFrozenPin:
    # meiva:note
    # The panel reports each node's checkout and carries no cluster target or
    # known-good verdict.
    def test_panel_reports_the_checkout_without_a_pin_verdict(
        self,
        db_conn: psycopg.Connection,
        tmp_path: Path,
        stub_machine_identity: MachineIdentityReader,
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        for args in (
            ["init", "-b", "main"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["commit", "--allow-empty", "-m", "init"],
        ):
            _git(source, *args)
        head = _git(source, "rev-parse", "HEAD")
        _insert_machine(db_conn, "cloud-test", "https://meiva.example.com", "gateway")

        with gateway_test_client(machine=stub_machine_identity, home=tmp_path) as client:
            body = client.get("/api/status").json()["cluster"]

        assert "cluster_target_sha" not in body
        assert "cluster_last_known_good_sha" not in body
        (machine,) = body["machines"]
        assert machine["head_sha"] == head
        assert "on_pin" not in machine
