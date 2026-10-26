from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import runtime_config
from base.host.env.tests.env_files import unit_env_path
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.test_config_api import REMOTE
from gateway.tests.routers.test_config_api import (
    clean_overrides as clean_overrides,
)
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    failed,
    refused,
    register_machine,
    runner_ops,
)
from ops import env_audit as audit


def test_get_explicit_self_is_host_view_not_cluster(
    clean_overrides: Path, *, machine_identity: MachineIdentityReader
) -> None:
    runtime_config.write_fields({"cross_machine_transfer_backend": "none"}, set())
    with gateway_test_client(machine=machine_identity) as client:
        cluster = client.get("/api/config").json()["raw_overrides"]
        host = client.get(f"/api/config?machine={machine_identity.name()}").json()["raw_overrides"]
    assert cluster.get("cross_machine_transfer_backend") == "none"
    assert "cross_machine_transfer_backend" not in host


def test_put_remote_rejects_cluster_key(clean_overrides: Path, db_conn: psycopg.Connection) -> None:
    register_machine(db_conn, REMOTE, url="http://remote.invalid:8000")
    with gateway_test_client() as client:
        resp = client.put(f"/api/config?machine={REMOTE}", json={"llm_model": "x"})
    assert resp.status_code == 400, resp.text
    assert "machine-independent" in resp.json()["detail"]


def test_put_remote_host_field_dispatches_config_write(
    clean_overrides: Path, db_conn: psycopg.Connection
) -> None:
    result = completed(
        {
            "machine": REMOTE,
            "results": {"ops_concurrency": {"ok": True, "reason": None}},
            "applied": True,
            "restart_required": ["ops"],
        }
    )

    with (
        runner_ops(db_conn, REMOTE, {"config_write": result}) as runner,
        gateway_test_client() as client,
    ):
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["applied"] is True
    assert body["results"]["ops_concurrency"]["ok"] is True
    assert body["restart_required"] == ["ops"]

    (payload,) = runner.payloads("config_write")
    assert payload["overrides"] == {"ops_concurrency": 12}
    assert payload["local"] is False
    assert payload["actor"] == "cluster_bearer:administrator"
    assert isinstance(payload["trace_id"], str) and payload["trace_id"]


def test_put_remote_rejects_writable_non_remote_host_field(
    clean_overrides: Path, db_conn: psycopg.Connection
) -> None:
    with runner_ops(db_conn, REMOTE) as runner, gateway_test_client() as client:
        resp = client.put(
            f"/api/config?machine={REMOTE}", json={"cross_machine_transfer_backend": "none"}
        )
    assert resp.status_code == 400, resp.text
    assert "unknown or read-only" in resp.json()["detail"]
    assert runner.calls == []


def test_put_remote_rejects_oss_credential_fields(
    clean_overrides: Path, db_conn: psycopg.Connection
) -> None:
    with runner_ops(db_conn, REMOTE) as runner, gateway_test_client() as client:
        resp = client.put(
            f"/api/config?machine={REMOTE}",
            json={
                "backup_offsite_credentials_file": str(
                    clean_overrides / "attacker-credentials.json"
                )
            },
        )
        assert resp.status_code == 400, resp.text
        assert "unknown or read-only" in resp.json()["detail"]
    assert runner.calls == []


@pytest.mark.parametrize(
    "answer", [refused(404, "no ack"), failed({"error": "boom"})], ids=["offline", "failed"]
)
def test_put_remote_unavailable_503(
    clean_overrides: Path, db_conn: psycopg.Connection, answer: OpsReply
) -> None:
    with (
        runner_ops(db_conn, REMOTE, {"config_write": answer}),
        gateway_test_client() as client,
    ):
        resp = client.put(f"/api/config?machine={REMOTE}", json={"ops_concurrency": 12})
    assert resp.status_code == 503, resp.text
    assert REMOTE in resp.json()["detail"]


def test_put_remote_unknown_machine_404(clean_overrides: Path) -> None:
    with gateway_test_client() as client:
        resp = client.put("/api/config?machine=ghost-host", json={"ops_concurrency": 1})
    assert resp.status_code == 404, resp.text
    assert "unknown machine" in resp.json()["detail"]


def test_self_machine_name_is_known_without_a_row(
    *, machine_identity: MachineIdentityReader
) -> None:
    assert machine_identity.name()


def test_request_actor_reads_only_middleware_state() -> None:
    from types import SimpleNamespace

    from gateway.routers.configuration.runtime import request_actor

    anon = cast("Any", SimpleNamespace(state=SimpleNamespace()))
    assert request_actor(anon) == (None, None)

    authed = cast(
        "Any",
        SimpleNamespace(
            state=SimpleNamespace(
                source_verified_by="user_session",
                auth_principal=SimpleNamespace(subject="administrator"),
                trace_id="trace-7",
            ),
        ),
    )
    assert request_actor(authed) == ("user_session:administrator", "trace-7")

    bearer = cast(
        "Any",
        SimpleNamespace(
            state=SimpleNamespace(source_verified_by="cluster_bearer", auth_principal=None)
        ),
    )
    assert request_actor(bearer) == ("cluster_bearer", None)


def test_put_records_write_audit_with_value_diff(clean_overrides: Path) -> None:
    with gateway_test_client() as client:
        resp = client.put("/api/config", json={"llm_model": "audit-model-1"})
    assert resp.status_code == 200, resp.text
    records = [
        json.loads(line) for line in (clean_overrides / ".env.audit.jsonl").read_text().splitlines()
    ]
    latest = records[-1]
    entries = cast("list[dict[str, object]]", latest["changed"])
    changed = {str(entry["alias"]): entry for entry in entries}
    assert changed["MEIVA_MODEL"]["new"] == "audit-model-1"
    assert "actor" in latest


def test_get_config_audit_last_is_bounded() -> None:
    with gateway_test_client() as client:
        assert client.get("/api/config/audit?last=0").status_code == 422
        assert client.get("/api/config/audit?last=201").status_code == 422


def _record_two_writes() -> None:
    env_path = unit_env_path()
    env_path.write_text("MEIVA_MODEL=audit-one\n")
    audit.record_env_write(
        env_path,
        {"MEIVA_MODEL"},
        set(),
        site="test-one",
        changes=[{"alias": "MEIVA_MODEL", "old": "old", "new": "audit-one"}],
    )
    audit.record_env_write(
        env_path,
        {"MEIVA_MODEL"},
        set(),
        site="test-two",
        changes=[{"alias": "MEIVA_MODEL", "old": "audit-one", "new": "audit-two"}],
    )


def test_get_config_audit_default_last_comes_from_display_config(clean_overrides: Path) -> None:
    _record_two_writes()

    with gateway_test_client(fields={"config_audit_default_last": 1}) as client:
        resp = client.get("/api/config/audit")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["records"]) == 1


def test_get_config_audit_reads_own_records(
    clean_overrides: Path, *, machine_identity: MachineIdentityReader
) -> None:
    env_path = unit_env_path()
    env_path.write_text("MEIVA_MODEL=audit-self\n")
    audit.record_env_write(
        env_path,
        {"MEIVA_MODEL"},
        set(),
        site="test-self",
        changes=[{"alias": "MEIVA_MODEL", "old": "old", "new": "audit-self"}],
    )

    with gateway_test_client(machine=machine_identity) as client:
        resp = client.get("/api/config/audit")

    assert resp.status_code == 200, resp.text
    records = resp.json()["records"]
    assert records[-1]["site"] == "test-self"
    assert records[-1]["machine"] == machine_identity.name()
    changed = records[-1]["changed"]
    assert changed[0]["alias"] == "MEIVA_MODEL"
    assert changed[0]["new"] == "audit-self"


def test_get_config_audit_all_merges_runners_newest_first(
    db_conn: psycopg.Connection, *, machine_identity: MachineIdentityReader
) -> None:
    def audit_read(call: OpsCall, *, machine: str, stamp: str) -> OpsReply:
        assert call.payload == {"last": 2}
        return completed(
            {
                "machine": machine,
                "records": [{"ts": f"2026-09-16T00:{stamp}:00+00:00", "site": f"s-{machine}"}],
            }
        )

    with (
        runner_ops(
            db_conn,
            "m1",
            {"config_audit_read": lambda call: audit_read(call, machine="m1", stamp="01")},
        ) as m1,
        runner_ops(
            db_conn,
            "m2",
            {"config_audit_read": lambda call: audit_read(call, machine="m2", stamp="02")},
        ) as m2,
        runner_ops(
            db_conn,
            machine_identity.name(),
            {
                "config_audit_read": lambda call: audit_read(
                    call, machine=machine_identity.name(), stamp="00"
                )
            },
            roles=("gateway", "agent-runner"),
        ) as local,
        gateway_test_client(machine=machine_identity) as client,
    ):
        resp = client.get("/api/config/audit?machine=all&last=2")

    assert resp.status_code == 200, resp.text
    records = resp.json()["records"]
    assert [record["machine"] for record in records] == ["m2", "m1"]
    assert [len(runner.calls_of("config_audit_read")) for runner in (m1, m2, local)] == [1, 1, 1]
