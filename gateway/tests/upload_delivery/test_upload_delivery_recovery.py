"""Real native transactions, filesystem faults and guarded delivered-upload consumers."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.agents.messages.inbound_provenance import InboundProvenance
from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import (
    CopyProof,
    ReceiveRequest,
    UnitIdentity,
    UploadDeliveryConflictError,
    current_unit,
)
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)
from gateway.tests.events.test_notices_endpoint import seed_agent
from gateway.tests.support.db_faults.update_fault import failing_update
from gateway.upload_delivery.worker import UploadRecovery
from ops import upload_delivery as receiver

SECRET = "delivered-upload-test-secret"  # noqa: S105 -- isolated credential
HEADERS = {"Idempotency-Key": "intent", "Idempotency-Scope": "principal-v1"}


@pytest.fixture
def uploaded_agent(
    db_conn: psycopg.Connection,
    default_home: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> Iterator[tuple[GatewayTestClient, int]]:
    unit = current_unit(local_machine=machine_identity.name())
    agent = seed_agent(db_conn)
    db_conn.execute(
        "INSERT INTO machines(name,gateway_url) VALUES (%s,'http://runner:7001') "
        "ON CONFLICT(name) DO UPDATE SET gateway_url=excluded.gateway_url",
        (unit.machine,),
    )
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
        "VALUES (%s,%s,true,'http://runner:7001')",
        (unit.machine, unit.home),
    )
    db_conn.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (unit.machine, agent))
    db_conn.commit()
    # Deterministic fault tests call a real round explicitly; no background race.
    config = gateway_test_config(
        upload_home=default_home, cluster_secret=SECRET, upload_recovery_enabled=False
    )
    with gateway_test_client(
        machine=machine_identity, config=config, raise_server_exceptions=False
    ) as client:
        yield client, agent


# meiva:note
# Make the agent's machine serve from a second home too, and return that unit.
#
# `url` is the address the machine's registry row advertises (a `runner_ops` listener's).
# Only the second unit advertises it, so the next upload binds that unit as its target: the
# copy then has to cross to another native home, as a remote receiver would.
def register_second_home(
    db_conn: psycopg.Connection, unit: UnitIdentity, *, url: str
) -> UnitIdentity:
    target = unit.model_copy(update={"home": unit.home + "-second"})
    db_conn.execute("UPDATE machine_units SET url=NULL WHERE machine_name=%s", (unit.machine,))
    db_conn.execute(
        "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
        "VALUES (%s,%s,true,%s)",
        (unit.machine, target.home, url),
    )
    db_conn.commit()
    return target


# meiva:note
# The recovery worker's claim lease for a constructed worker: these tests drive one batch at a
# time, so any positive lease behaves the same here.
CLAIM_SECONDS = 300.0


# meiva:note
# A recovery worker on the gateway's resources, driven by the test one round at a time.
def recovery_for(client: GatewayTestClient, machine: MachineIdentityReader) -> UploadRecovery:
    return UploadRecovery(
        client.app.state.db_pool,
        client.app.state.db,
        client.app.state.bus,
        machine=machine,
        home=client.app.state.gateway_config.upload_home,
        claim_seconds=CLAIM_SECONDS,
        rpc=client.app.state.rpc,
    )


def post(
    client: TestClient,
    agent: int,
    *,
    key: str = "intent",
    data: bytes = b"bytes",
    name: str = "photo.png",
):
    return client.post(
        f"/api/keyed/v1/agents/{agent}/uploads",
        headers={**HEADERS, "Idempotency-Key": key},
        files=[("files", (name, data, "image/png"))],
    )


# meiva:note
# Tests drive one batch at a time, so they claim it and read it back: a zero claim
# keeps the row due for the next caller (claim semantics are covered by
# test_due_claims_a_batch_once).
_NO_CLAIM = 0.0


def request_for(pool: ConnectionPool, batch_id: str, *, local_machine: str) -> ReceiveRequest:
    return next(
        item
        for item in source.due(pool, local_machine=local_machine, claim_seconds=_NO_CLAIM)
        if item.manifest.batch_id == batch_id
    )


def proof_for(request: ReceiveRequest) -> CopyProof:
    path = source.agent_upload_dir(
        request.manifest.agent_id, create=False, home=Path.home()
    ).resolve()
    return CopyProof(
        version=1,
        target=request.target,
        manifest_hash=request.manifest.fingerprint(),
        directory=str(path / ".delivered-v1" / request.manifest.batch_id),
    )


def test_lost_response_replay_retains_original_notification_after_deletion(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    response = post(client, agent)
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]
    request = request_for(client.app.state.db_pool, batch_id, local_machine=machine_identity.name())
    proof = proof_for(request)
    iid = source.complete(client.app.state.db_pool, request, proof)
    assert source.complete(client.app.state.db_pool, request, proof) == iid
    db_conn.execute("DELETE FROM inbound_messages WHERE id=%s", (iid,))
    db_conn.execute("DELETE FROM agents_meta WHERE id=%s", (agent,))
    db_conn.execute("DELETE FROM agent_lifecycle_intervals WHERE agent_id=%s", (agent,))
    db_conn.execute("DELETE FROM agents WHERE id=%s", (agent,))
    db_conn.commit()
    assert source.complete(client.app.state.db_pool, request, proof) == iid
    assert post(client, agent).json() == response.json()
    assert client.get(response.json()["status_url"]).json()["inbound_id"] == iid
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)
    obj = f"{response.json()['status_url']}/objects/0"
    assert client.get(obj).content == b"bytes"


def test_changed_manifest_and_invalid_guard_have_no_effect(
    uploaded_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection[Any]
):
    client, agent = uploaded_agent
    assert post(client, agent).status_code == 202
    assert post(client, agent, data=b"changed").status_code == 409
    assert post(client, agent, key="invalid", name="bad.unsafe%url").status_code == 422
    for headers in (
        {},
        {"Idempotency-Key": "x"},
        {**HEADERS, "Idempotency-Key": ""},
        {**HEADERS, "Idempotency-Scope": "unknown"},
    ):
        assert (
            client.post(
                f"/api/keyed/v1/agents/{agent}/uploads",
                headers=headers,
                files=[("files", ("f", b"x"))],
            ).status_code
            == 422
        )
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_source_fsync_loss_and_concurrent_recovery_use_one_identity(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    # meiva:note
    # The files are durable and fsynced, then the batch's ready write fails: the response is lost.
    with failing_update(db_conn, "upload_delivery_batches", message="fault after source fsync"):
        assert post(client, agent).status_code == 500
        assert db_conn.execute("SELECT state FROM upload_delivery_batches").fetchone() == (
            "receiving",
        )

    def recover(_index: int):
        return source.accept(
            client.app.state.db_pool,
            "direct",
            agent,
            ["f.png"],
            [("f.png", b"native", "image/png")],
            InboundProvenance("cluster_bearer", "http"),
            local_machine=machine_identity.name(),
            upload_home=client.app.state.gateway_config.upload_home,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(recover, range(2)))
    assert values[0] == values[1]
    assert post(client, agent).status_code == 202
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (2,)


def test_placement_or_bad_proof_rejected_before_inbound(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    first = post(client, agent).json()
    request = request_for(
        client.app.state.db_pool,
        first["batch_id"],
        local_machine=machine_identity.name(),
    )
    proof = proof_for(request)
    with pytest.raises(UploadDeliveryConflictError):
        source.complete(
            client.app.state.db_pool,
            request,
            proof.model_copy(update={"manifest_hash": "0" * 64}),
        )
    db_conn.execute("UPDATE agents_meta SET machine='moved' WHERE id=%s", (agent,))
    db_conn.commit()
    with pytest.raises(UploadDeliveryConflictError):
        source.complete(client.app.state.db_pool, request, proof)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


def test_receiver_same_physical_root_different_home_counts_once_and_verifies_ready(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    first = post(client, agent).json()
    request = request_for(
        client.app.state.db_pool,
        first["batch_id"],
        local_machine=machine_identity.name(),
    )
    unit = request.target.model_copy(update={"home": request.target.home + "-other"})
    request = request.model_copy(update={"target": unit})
    config = receiver.ReceiverConfig(
        unit=unit,
        user_home=client.app.state.gateway_config.upload_home,
        access=client.app.state.gateway_access,
        quota_bytes=5,
        quota_files=client.app.state.gateway_config.max_agent_upload_files,
    )
    proof = receiver.copy_batch(
        client.app.state.db_pool,
        request,
        config,
    )
    assert proof.target == unit
    assert (
        receiver.copy_batch(
            client.app.state.db_pool,
            request,
            config,
        )
        == proof
    )
    path = Path(proof.directory) / request.manifest.objects[0].name
    path.write_bytes(b"wrong")
    with pytest.raises(UploadDeliveryConflictError):
        receiver.copy_batch(client.app.state.db_pool, request, config)
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_copies").fetchone() == (1,)


def test_receiver_wrong_actual_unit_cannot_overwrite_nested_final(
    uploaded_agent: tuple[GatewayTestClient, int],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    first = post(client, agent).json()
    request = request_for(
        client.app.state.db_pool,
        first["batch_id"],
        local_machine=machine_identity.name(),
    )
    config = receiver.ReceiverConfig(
        unit=request.target.model_copy(update={"home": "/wrong"}),
        user_home=client.app.state.gateway_config.upload_home,
        access=client.app.state.gateway_access,
        quota_bytes=client.app.state.gateway_config.max_agent_upload_bytes,
        quota_files=client.app.state.gateway_config.max_agent_upload_files,
    )
    with pytest.raises(UploadDeliveryConflictError):
        receiver.copy_batch(client.app.state.db_pool, request, config)
    path = Path(proof_for(request).directory) / request.manifest.objects[0].name
    assert path.read_bytes() == b"bytes"


def test_silent_admission_counts_hidden_files_and_receiving_reservation(
    uploaded_agent: tuple[GatewayTestClient, int],
):
    client, agent = uploaded_agent
    assert post(client, agent).status_code == 202
    small = replace(client.app.state.gateway_config, max_agent_upload_bytes=5)
    with gateway_test_client(
        process=client.app.state.gateway_process, config=small, raise_server_exceptions=False
    ) as limited:
        response = limited.post(
            f"/api/agents/{agent}/uploads",
            headers={**HEADERS, "Idempotency-Key": "silent"},
            files=[("files", ("flat", b"x"))],
        )
    assert response.status_code == 413


def test_name_byte_budget_includes_atomic_primitive_temporary_name(
    uploaded_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection[Any]
):
    client, agent = uploaded_agent
    rejected = post(client, agent, name="x." + "a" * 184)
    assert rejected.status_code == 422
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (0,)
    accepted = post(client, agent, name="x." + "a" * 182)
    assert accepted.status_code == 202, accepted.text
    assert len(accepted.json()["files"][0]["name"].encode()) == 217
    assert client.get(accepted.json()["status_url"] + "/objects/0").content == b"bytes"


@pytest.mark.parametrize("changed", ["source", "target", "manifest"])
def test_completed_receipt_rejects_different_immutable_intent(
    uploaded_agent: tuple[GatewayTestClient, int],
    changed: str,
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    batch = post(client, agent).json()["batch_id"]
    request = request_for(client.app.state.db_pool, batch, local_machine=machine_identity.name())
    iid = source.complete(client.app.state.db_pool, request, proof_for(request))
    raw = request.model_dump()
    if changed == "manifest":
        raw["manifest"]["objects"][0]["filename"] = "different-display.png"
    else:
        raw[changed]["home"] += "-different"
    changed_request = ReceiveRequest.model_validate(raw)
    with pytest.raises(UploadDeliveryConflictError, match="stored intent"):
        source.complete(client.app.state.db_pool, changed_request, proof_for(changed_request))
    assert source.complete(client.app.state.db_pool, request, proof_for(request)) == iid


def test_manifest_serving_preserves_display_filename_and_safe_renderable_headers(
    uploaded_agent: tuple[GatewayTestClient, int],
):
    from urllib.parse import quote

    client, agent = uploaded_agent
    name = "original display café.html"
    response = client.post(
        f"/api/keyed/v1/agents/{agent}/uploads",
        headers=HEADERS,
        files=[("files", (name, b"<script>bad()</script>", "text/html"))],
    )
    assert response.status_code == 202, response.text
    assert response.json()["files"][0]["filename"] == name
    served = client.get(response.json()["status_url"] + "/objects/0")
    assert served.status_code == 200
    assert served.headers["content-type"] == "text/html; charset=utf-8"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["content-disposition"] == "attachment; filename*=utf-8''" + quote(name)
    assert served.content == b"<script>bad()</script>"


@pytest.mark.parametrize("invalid_agent", [-1, 0, 2**63, 2**70])
def test_guarded_routes_reject_invalid_agent_paths_before_native_effects(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    invalid_agent: int,
):
    client, _agent = uploaded_agent
    assert post(client, invalid_agent).status_code == 422
    path = f"/api/keyed/v1/agents/{invalid_agent}/uploads/" + "a" * 32
    assert client.get(path).status_code == 422
    assert client.get(path + "/objects/0").status_code == 422
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (0,)
    home = client.app.state.gateway_config.upload_home
    assert not source.agent_upload_dir(invalid_agent, create=False, home=home).exists()


# meiva:note
# Two workers on one database never copy the same batch (task #5439). The claim lives
# in due() itself: the first caller pushes next_attempt_at a lease ahead and returns
# the batch; a second caller reading at the same instant finds nothing.
def test_due_claims_a_batch_once(
    uploaded_agent: tuple[GatewayTestClient, int],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    response = post(client, agent)
    assert response.status_code == 202, response.text
    batch_id = response.json()["batch_id"]
    pool = client.app.state.db_pool
    first = source.due(pool, local_machine=machine_identity.name(), claim_seconds=300)
    second = source.due(pool, local_machine=machine_identity.name(), claim_seconds=300)
    assert batch_id in [item.manifest.batch_id for item in first]
    assert batch_id not in [item.manifest.batch_id for item in second]
