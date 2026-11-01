"""Real native transactions, filesystem faults and guarded delivered-upload consumers."""

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool

from base.agents.upload_delivery import source, staging
from base.agents.upload_delivery.models import (
    PROTOCOL,
    CopyProof,
    ReceiveRequest,
    UnitIdentity,
    UploadDeliveryConflictError,
    current_unit,
)
from base.agents.upload_delivery.paths import delivered_upload_dir, staged_path
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.paths import WORKSPACES_DIR_NAME, resolve_meiva_home
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)
from gateway.tests.events.test_notices_endpoint import seed_agent
from gateway.tests.support.db_faults.update_fault import failing_update
from gateway.tests.support.runner_ops.ops_stub import runner_ops
from gateway.tests.upload_delivery.support import (
    receiver_for as receiver_for,
)
from gateway.tests.upload_delivery.support import (
    receiver_op as receiver_op,
)
from gateway.tests.upload_delivery.support import (
    serving_gateway as serving_gateway,
)
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
    del default_home
    unit = current_unit(local_machine=machine_identity.name(), home=resolve_meiva_home())
    agent = seed_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (unit.machine, agent))
    db_conn.commit()
    # Deterministic fault tests call a real round explicitly; no background race.
    config = gateway_test_config(cluster_secret=SECRET, upload_recovery_enabled=False)
    with (
        gateway_test_client(
            machine=machine_identity, config=config, raise_server_exceptions=False
        ) as client,
        # meiva:note
        # A receiver pulls the staged bytes over HTTP, so the access a copy is built from dials
        # this loopback port rather than the launch configuration's address.
        serving_gateway(client, client.app.state.db_pool) as source_url,
    ):
        client.app.state.gateway_access = replace(
            client.app.state.gateway_access, gateway_url=source_url
        )
        receive = receiver_op(client, receiver_for(client, unit))
        with runner_ops(db_conn, unit.machine, {"upload-receive-v2": receive}) as runner:
            db_conn.execute(
                "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
                "VALUES (%s,%s,true,%s)",
                (unit.machine, unit.home, runner.url),
            )
            db_conn.commit()
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
        home=resolve_meiva_home(),
        claim_seconds=CLAIM_SECONDS,
        rpc=client.app.state.rpc,
    )


def stage(
    client: TestClient,
    agent: int,
    *,
    key: str = "stage-intent",
    data: bytes = b"bytes",
    name: str = "photo.png",
    content_type: str = "image/png",
):
    return client.post(
        f"/api/keyed/v1/agents/{agent}/uploads/staged",
        headers={**HEADERS, "Idempotency-Key": key},
        files={"file": (name, data, content_type)},
    )


def post(
    client: TestClient,
    agent: int,
    *,
    key: str = "intent",
    data: bytes = b"bytes",
    name: str = "photo.png",
    content_type: str = "image/png",
    text: str = "here are the files",
    file_ids: list[str] | None = None,
):
    if file_ids is None:
        digest = hashlib.sha256(name.encode("utf-8", errors="replace") + b":" + data).hexdigest()[
            :12
        ]
        staged_resp = stage(
            client,
            agent,
            key=f"stage-{key}-{digest}",
            data=data,
            name=name,
            content_type=content_type,
        )
        if staged_resp.status_code != 201:
            return staged_resp
        file_ids = [staged_resp.json()["file_id"]]
    return client.post(
        f"/api/keyed/v1/agents/{agent}/uploads",
        headers={**HEADERS, "Idempotency-Key": key},
        json={"file_ids": file_ids, "text": text},
    )


# meiva:note
# Tests drive one batch at a time, so they claim it and read it back: a zero claim
# keeps the row due for the next caller (claim semantics are covered by
# test_due_claims_a_batch_once).
_NO_CLAIM = 0.0


def request_for(pool: ConnectionPool, batch_id: str, *, local_machine: str) -> ReceiveRequest:
    return next(
        item
        for item in source.due(
            pool,
            local_machine=local_machine,
            home=resolve_meiva_home(),
            claim_seconds=_NO_CLAIM,
        )
        if item.manifest.batch_id == batch_id
    )


# meiva:note
# The proof a receiver on `request.target` returns: the batch's send directory inside that agent's
# own workspace on the target machine, named by the cluster's configured directory name.
def proof_for(client: GatewayTestClient, request: ReceiveRequest) -> CopyProof:
    path = delivered_upload_dir(
        request.manifest.agent_id,
        home=Path(request.target.home),
        name=client.app.state.gateway_config.upload_dir_name,
        create=False,
    ).resolve()
    return CopyProof(
        version=PROTOCOL,
        target=request.target,
        manifest_hash=request.manifest.fingerprint(),
        directory=str(path / request.manifest.directory),
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
    proof = proof_for(client, request)
    iid = source.complete(client.app.state.db_pool, request, proof)
    assert source.complete(client.app.state.db_pool, request, proof) == iid
    db_conn.execute("DELETE FROM inbound_messages WHERE agent_id=%s", (agent,))
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
    assert (
        client.post(
            f"/api/keyed/v1/agents/{agent}/uploads",
            headers={**HEADERS, "Idempotency-Key": "invalid"},
            json={"file_ids": [], "text": ""},
        ).status_code
        == 422
    )
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
                json={"file_ids": ["0" * 32], "text": ""},
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
    # The file is durable and fsynced in staging, then the staged row's ready write fails.
    with failing_update(db_conn, "upload_staged_files", message="fault after staged fsync"):
        assert stage(client, agent, key="stage-fault").status_code == 500
        assert db_conn.execute("SELECT state FROM upload_staged_files").fetchone() == ("receiving",)

    def recover(_index: int):
        return staging.stage(
            client.app.state.db_pool,
            "direct",
            agent,
            raw_name="f.png",
            data=b"native",
            content_type="image/png",
            file_id="0" * 32,
            local_machine=machine_identity.name(),
            home=resolve_meiva_home(),
            upload_root=client.app.state.gateway_config.upload_staging_root,
            max_bytes=client.app.state.gateway_config.max_agent_upload_bytes,
            max_files=client.app.state.gateway_config.max_agent_upload_files,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        values = list(executor.map(recover, range(2)))
    assert values[0] == values[1]
    assert stage(client, agent, key="stage-fault").status_code == 201
    assert db_conn.execute("SELECT count(*) FROM upload_staged_files").fetchone() == (2,)


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
    proof = proof_for(client, request)
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


# meiva:note
# A receiver on another home publishes into that home's own workspace, reserves once, and refuses
# a file that no longer matches the manifest — the delivered copy is verified, not trusted.
def test_receiver_on_another_home_copies_once_and_verifies_ready(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
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
    unit = request.target.model_copy(update={"home": str(tmp_path / "other-home")})
    request = request.model_copy(update={"target": unit})
    config = replace(receiver_for(client, unit), quota_bytes=5)
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
    config = receiver_for(client, request.target)
    proof = receiver.copy_batch(client.app.state.db_pool, request, config)
    path = Path(proof.directory) / request.manifest.objects[0].name
    assert path.read_bytes() == b"bytes"

    elsewhere = replace(config, unit=request.target.model_copy(update={"home": "/wrong"}))
    with pytest.raises(UploadDeliveryConflictError, match="different native machine/home"):
        receiver.copy_batch(client.app.state.db_pool, request, elsewhere)
    assert path.read_bytes() == b"bytes"


def test_staged_admission_enforces_agent_quota(
    uploaded_agent: tuple[GatewayTestClient, int],
):
    client, agent = uploaded_agent
    assert stage(client, agent, key="first-stage", data=b"bytes").status_code == 201
    small = replace(client.app.state.gateway_config, max_agent_upload_bytes=5)
    with gateway_test_client(
        process=client.app.state.gateway_process, config=small, raise_server_exceptions=False
    ) as limited:
        response = stage(limited, agent, key="second-stage", data=b"x")
    assert response.status_code == 413


def test_name_byte_budget_includes_atomic_primitive_temporary_name(
    uploaded_agent: tuple[GatewayTestClient, int],
):
    client, agent = uploaded_agent
    accepted = post(client, agent, key="max-len", name="a" * 300 + ".png")
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["files"][0]["name"].endswith(".png")
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
    iid = source.complete(client.app.state.db_pool, request, proof_for(client, request))
    raw = request.model_dump()
    if changed == "manifest":
        raw["manifest"]["objects"][0]["name"] = "different-display.png"
    else:
        raw[changed]["home"] += "-different"
    changed_request = ReceiveRequest.model_validate(raw)
    with pytest.raises(UploadDeliveryConflictError, match="stored intent"):
        source.complete(
            client.app.state.db_pool, changed_request, proof_for(client, changed_request)
        )
    assert source.complete(client.app.state.db_pool, request, proof_for(client, request)) == iid


def test_manifest_serving_preserves_display_filename_and_safe_renderable_headers(
    uploaded_agent: tuple[GatewayTestClient, int],
):
    from urllib.parse import quote

    client, agent = uploaded_agent
    name = "original display café.html"
    response = post(
        client,
        agent,
        key="html-upload",
        name=name,
        data=b"<script>bad()</script>",
        content_type="text/html",
    )
    assert response.status_code == 202, response.text
    assert response.json()["files"][0]["name"] == name
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
    root = client.app.state.gateway_config.upload_staging_root
    assert not source.agent_upload_dir(invalid_agent, create=False, root=root).exists()


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
    first = source.due(
        pool, local_machine=machine_identity.name(), home=resolve_meiva_home(), claim_seconds=300
    )
    second = source.due(
        pool, local_machine=machine_identity.name(), home=resolve_meiva_home(), claim_seconds=300
    )
    assert batch_id in [item.manifest.batch_id for item in first]
    assert batch_id not in [item.manifest.batch_id for item in second]


# meiva:note
# Where a delivered copy lives for the agent in these tests: the agent's own workspace on the
# machine the batch targets, under the cluster's configured directory name.
def delivered_copy(
    client: GatewayTestClient, unit: UnitIdentity, agent: int, *, create: bool = False
) -> Path:
    return delivered_upload_dir(
        agent,
        home=Path(unit.home),
        name=client.app.state.gateway_config.upload_dir_name,
        create=create,
    )


# meiva:note
# Acceptance: a delivered attachment lands inside the agent's own workspace and the notice the
# agent reads names that path. The bytes the gateway received stay in staging until released.
def test_a_delivered_upload_lands_in_the_agents_own_workspace(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    accepted = post(client, agent).json()
    request = request_for(
        client.app.state.db_pool, accepted["batch_id"], local_machine=machine_identity.name()
    )
    config = receiver_for(client, request.target)
    proof = receiver.copy_batch(client.app.state.db_pool, request, config)

    workspace = delivered_copy(client, request.target, agent)
    copy = Path(proof.directory) / request.manifest.objects[0].name
    assert Path(proof.directory).resolve().is_relative_to(workspace.resolve())
    assert copy.read_bytes() == b"bytes"
    assert not workspace.resolve().is_relative_to(
        client.app.state.gateway_config.upload_staging_root.resolve()
    )

    iid = source.complete(client.app.state.db_pool, request, proof)
    rows = db_conn.execute(
        "SELECT id, source, content FROM inbound_messages WHERE agent_id=%s ORDER BY id",
        (agent,),
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][1] == "system:upload"
    assert str(copy) in rows[0][2]
    assert rows[1] == (iid, "user", "here are the files")

    staged = source.agent_upload_dir(
        agent, create=False, root=client.app.state.gateway_config.upload_staging_root
    )
    assert staged_path(staged, request.manifest.objects[0].staged_id).read_bytes() == b"bytes"


# meiva:note
# A proof whose batch namespace is right but which is not inside the target machine's workspace is
# refused: the notice an agent reads must name a file in its own workspace, not an arbitrary
# directory shaped like one — and `..` in the free directory-name segment would put the namespace
# beside the workspace rather than inside it.
@pytest.mark.parametrize("shape", ["not-the-workspace-root", "escape-with-dotdot"])
def test_a_proof_outside_the_workspace_root_is_refused(
    uploaded_agent: tuple[GatewayTestClient, int],
    shape: str,
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    accepted = post(client, agent).json()
    request = request_for(
        client.app.state.db_pool, accepted["batch_id"], local_machine=machine_identity.name()
    )
    proof = receiver.copy_batch(
        client.app.state.db_pool, request, receiver_for(client, request.target)
    )

    parts = Path(proof.directory).parts
    assert WORKSPACES_DIR_NAME in parts
    elsewhere = (
        Path(*(p if p != WORKSPACES_DIR_NAME else "not-workspaces" for p in parts))
        if shape == "not-the-workspace-root"
        else Path(*parts[:-2], "..", parts[-1])
    )
    with pytest.raises(UploadDeliveryConflictError, match="fixed agent batch namespace"):
        source.complete(
            client.app.state.db_pool,
            request,
            proof.model_copy(update={"directory": str(elsewhere)}),
        )
    assert source.complete(client.app.state.db_pool, request, proof)


# meiva:note
# Acceptance: uploading the same file name twice keeps both copies. Each batch is published
# create-only into its own timestamped send directory, so the newer upload never replaces the older one.
def test_the_same_file_name_uploaded_twice_keeps_both_copies(
    uploaded_agent: tuple[GatewayTestClient, int],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    copies: list[Path] = []
    for key, data in (("first", b"first"), ("second", b"second")):
        accepted = post(client, agent, key=key, data=data).json()
        request = request_for(
            client.app.state.db_pool, accepted["batch_id"], local_machine=machine_identity.name()
        )
        proof = receiver.copy_batch(
            client.app.state.db_pool, request, receiver_for(client, request.target)
        )
        copies.append(Path(proof.directory) / request.manifest.objects[0].name)

    assert len({path.parent for path in copies}) == 2
    assert [path.read_bytes() for path in copies] == [b"first", b"second"]


def test_staged_file_discard_and_conflict_when_batch_pending(
    uploaded_agent: tuple[GatewayTestClient, int],
):
    client, agent = uploaded_agent
    draft = stage(client, agent, key="draft-discard", data=b"draft").json()
    delete_resp = client.delete(f"/api/keyed/v1/agents/{agent}/uploads/staged/{draft['file_id']}")
    assert delete_resp.status_code == 204
    assert (
        client.delete(f"/api/keyed/v1/agents/{agent}/uploads/staged/{draft['file_id']}").status_code
        == 204
    )

    claimed = stage(client, agent, key="draft-claimed", data=b"claimed").json()
    sent = post(client, agent, key="send-claimed", file_ids=[claimed["file_id"]])
    assert sent.status_code == 202
    assert (
        client.delete(
            f"/api/keyed/v1/agents/{agent}/uploads/staged/{claimed['file_id']}"
        ).status_code
        == 409
    )
    source.record_failure(
        client.app.state.db_pool, sent.json()["batch_id"], "receiver-gone", hold=True
    )
    assert (
        client.delete(
            f"/api/keyed/v1/agents/{agent}/uploads/staged/{claimed['file_id']}"
        ).status_code
        == 204
    )
