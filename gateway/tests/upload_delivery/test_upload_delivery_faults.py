"""Remote copy and transactional inbound response-loss fault boundaries."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx2 as httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import CopyProof, ReceiveRequest
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.service_read import ConfigAuthority
from base.host.db_authority.unit import UnitIdentity
from base.host.system.tests.fake_commands import fake_host
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.db_faults.insert_fault import failing_insert
from gateway.tests.support.db_faults.update_fault import failing_update
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    completed,
    failed,
    refused,
    runner_ops,
)
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    SECRET,
    post,
    proof_for,
    recovery_for,
    register_second_home,
    request_for,
)
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    uploaded_agent as uploaded_agent,
)
from ops import upload_delivery as receiver
from ops.rpc_schemas import OpStatus
from services.agent_runner.agent_ops.dispatch_sync import dispatch_sync


def _recovered_source_ready(
    client: GatewayTestClient, db_conn: psycopg.Connection[Any], agent: int
):
    recovered = post(client, agent)
    assert recovered.status_code == 202
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (1,)
    assert client.get(recovered.json()["status_url"] + "/objects/0").content == b"bytes"


def test_source_ready_commit_failure_recovers(
    uploaded_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection[Any]
):
    client, agent = uploaded_agent
    with failing_update(db_conn, "upload_delivery_batches", message="source ready commit failure"):
        assert post(client, agent).status_code == 500
        assert db_conn.execute("SELECT state FROM upload_delivery_batches").fetchone() == (
            "receiving",
        )
    _recovered_source_ready(client, db_conn, agent)


# meiva:note
# The ready write commits, then the caller sees a failure: Postgres cannot lose an ack on
# demand, so the transaction wrapper raises once after its commit (needs base: a transaction
# port). Every other transaction runs through untouched.
def test_source_ready_lost_commit_ack_recovers(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
):
    client, agent = uploaded_agent
    original = source.write_transaction
    count = 0

    @contextmanager
    def ack_lost_once(pool: ConnectionPool):
        nonlocal count
        count += 1
        this = count
        with original(pool) as conn:
            yield conn
        if this == 2:
            raise psycopg.OperationalError("source ready response lost")

    monkeypatch.setattr(source, "write_transaction", ack_lost_once)
    assert post(client, agent).status_code == 500
    assert db_conn.execute("SELECT state FROM upload_delivery_batches").fetchone() == ("pending",)
    _recovered_source_ready(client, db_conn, agent)


def _request_and_proof(
    client: GatewayTestClient, agent: int, machine_identity: MachineIdentity
) -> tuple[ReceiveRequest, CopyProof]:
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    return request, proof_for(request)


def _assert_one_system_notice(db_conn: psycopg.Connection[Any], agent: int, iid: int) -> None:
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    # meiva:note
    # The delivery is framework machinery: the agent reads it as a system note, not as the
    # human speaking.
    # meiva:link gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md
    assert db_conn.execute(
        "SELECT source, source_verified_by, source_transport FROM inbound_messages WHERE id=%s",
        (iid,),
    ).fetchone() == ("system:upload", "cluster_bearer", "http")


def test_inbound_transaction_rollback_recovers(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request, proof = _request_and_proof(client, agent, machine_identity)
    with failing_insert(db_conn, "inbound_messages", message="inbound commit failure"):
        with pytest.raises(psycopg.errors.RaiseException):
            source.complete(client.app.state.db_pool, request, proof)
        assert db_conn.execute(
            "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
        ).fetchone() == (0,)
    iid = source.complete(client.app.state.db_pool, request, proof)
    assert source.complete(client.app.state.db_pool, request, proof) == iid
    _assert_one_system_notice(db_conn, agent, iid)


# meiva:note
# The inbound commits, then the caller sees a failure once (needs base: a transaction port).
def test_inbound_lost_commit_ack_recovers(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request, proof = _request_and_proof(client, agent, machine_identity)
    original = source.write_transaction
    lost = False

    @contextmanager
    def ack_lost_once(pool: ConnectionPool):
        nonlocal lost
        with original(pool) as conn:
            yield conn
        if not lost:
            lost = True
            raise psycopg.OperationalError("inbound commit ack lost")

    monkeypatch.setattr(source, "write_transaction", ack_lost_once)
    with pytest.raises(psycopg.OperationalError):
        source.complete(client.app.state.db_pool, request, proof)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    iid = source.complete(client.app.state.db_pool, request, proof)
    assert source.complete(client.app.state.db_pool, request, proof) == iid
    _assert_one_system_notice(db_conn, agent, iid)


@pytest.mark.parametrize("version", [None, True, 1.0, 2, "1"])
def test_explicit_native_request_and_proof_versions_fail_fast(
    uploaded_agent: tuple[GatewayTestClient, int],
    version: object,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    for model, payload in (
        (ReceiveRequest, request.model_dump()),
        (CopyProof, proof_for(request).model_dump()),
    ):
        if version is None:
            del payload["version"]
        else:
            payload["version"] = version
        with pytest.raises(ValidationError):
            model.model_validate(payload)
    payload = request.model_dump()
    if version is None:
        del payload["version"]
    else:
        payload["version"] = version
    with pytest.raises(ValidationError):
        dispatch_sync(
            "upload-receive-v1",
            payload,
            pool=client.app.state.db_pool,
            db=client.app.state.db,
            authority=config_authority,
            image=client.app.state.process_image,
            local_machine=machine_identity,
            host=fake_host(),
        )


# meiva:note
# The receiver runner's `upload-receive-v1` op: the real receive code, in this process.
#
# With `lose_first_response` the first call completes its work (the bytes are published and
# the ready marker committed) and the caller then sees the connection answer 404.
def _receiver_op(
    client: GatewayTestClient,
    authority: ConfigAuthority,
    machine: MachineIdentity,
    *,
    lose_first_response: bool = False,
):
    calls = 0

    def receive(call: OpsCall) -> OpsReply:
        nonlocal calls
        calls += 1
        assert call.idempotency_key is None
        status, proof = dispatch_sync(
            call.kind,
            call.payload,
            image=client.app.state.process_image,
            pool=client.app.state.db_pool,
            db=client.app.state.db,
            authority=authority,
            local_machine=machine,
            host=fake_host(),
        )
        assert status == OpStatus.COMPLETED
        if lose_first_response and calls == 1:
            return refused(404, "remote fsync/ready succeeded but response lost")
        return completed(proof)

    return receive


@pytest.mark.asyncio
async def test_real_remote_receiver_after_fsync_response_loss_one_inbound(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    source_unit = receiver.current_unit(local_machine=machine_identity.name())
    target = source_unit.model_copy(update={"machine": "remote", "home": "/remote/unit"})
    remote_dir = tmp_path / "remote-disk" / f"MeivaAgent-{agent}"

    def current_target(*, local_machine: str) -> UnitIdentity:
        assert local_machine == machine_identity.name()
        return target

    def directory(_id: int, **_kwargs: Any) -> Path:
        return remote_dir

    def get(url: str, **_kwargs: Any):
        # A receiver file/network phase has released its own DB connection.
        with client.app.state.db_pool.connection(timeout=1) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        result = client.get(
            url.removeprefix(receiver.gateway_api_base(settings.gateway.gateway_url).rstrip("/"))
        )
        return httpx.Response(
            result.status_code, content=result.content, request=httpx.Request("GET", url)
        )

    # meiva:note
    # needs ops: `receive` reads its unit, its upload directory and the gateway fetch ambiently,
    # so the in-process receiver is made to be another machine with these three patches.
    monkeypatch.setattr(receiver, "current_unit", current_target)
    monkeypatch.setattr(receiver, "agent_upload_dir", directory)
    monkeypatch.setattr(receiver, "http_get", get)
    receive = _receiver_op(client, config_authority, machine_identity, lose_first_response=True)
    with runner_ops(db_conn, "remote", {"upload-receive-v1": receive}) as runner:
        db_conn.execute(
            "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
            "VALUES ('remote','/remote/unit',true,%s)",
            (runner.url,),
        )
        db_conn.execute("UPDATE agents_meta SET machine='remote' WHERE id=%s", (agent,))
        db_conn.commit()
        first = post(client, agent).json()
        request = request_for(
            client.app.state.db_pool,
            first["batch_id"],
            local_machine=machine_identity.name(),
        )
        recovery = recovery_for(client, machine_identity)
        await recovery.copy(request)
        assert source.status(client.app.state.db_pool, agent, first["batch_id"]).state == "pending"
        assert db_conn.execute(
            "SELECT count(*) FROM upload_delivery_copies WHERE ready_at IS NOT NULL"
        ).fetchone() == (1,)
        await recovery.copy(request)
        assert source.status(client.app.state.db_pool, agent, first["batch_id"]).state == "accepted"
        await recovery.close()
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    assert (
        remote_dir / ".delivered-v1" / request.manifest.batch_id / request.manifest.objects[0].name
    ).read_bytes() == b"bytes"


@pytest.mark.asyncio
async def test_unknown_remote_kind_holds(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    unit = receiver.current_unit(local_machine=machine_identity.name())
    # meiva:note
    # A runner of the previous version answers the way its dispatcher does for a kind it lacks.
    old_runner = failed({"error": "unknown kind: 'upload-receive-v1'"})
    with runner_ops(db_conn, unit.machine, {"upload-receive-v1": old_runner}) as runner:
        register_second_home(db_conn, unit, url=runner.url)
        request = request_for(
            client.app.state.db_pool,
            post(client, agent).json()["batch_id"],
            local_machine=machine_identity.name(),
        )
        # Source does not reinterpret an unsupported version as another op kind.
        recovery = recovery_for(client, machine_identity)
        await recovery.copy(request)
        await recovery.close()
    held = source.status(client.app.state.db_pool, agent, request.manifest.batch_id)
    assert (held.state, held.attempts) == ("hold", 1)


def _receive_twice_after_failure(
    client: GatewayTestClient,
    db_conn: psycopg.Connection[Any],
    request: ReceiveRequest,
    machine: MachineIdentity,
) -> None:
    proof = receiver.receive(client.app.state.db_pool, request, local_machine=machine.name())
    assert proof == receiver.receive(
        client.app.state.db_pool, request, local_machine=machine.name()
    )
    assert db_conn.execute(
        "SELECT count(*) FROM upload_delivery_copies WHERE ready_at IS NOT NULL"
    ).fetchone() == (1,)


def test_receiver_ready_commit_failure_recovers_without_second_copy(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    with (
        failing_update(db_conn, "upload_delivery_copies", message="receiver ready rollback"),
        pytest.raises(psycopg.errors.RaiseException),
    ):
        receiver.receive(client.app.state.db_pool, request, local_machine=machine_identity.name())
    _receive_twice_after_failure(client, db_conn, request, machine_identity)


# meiva:note
# The ready marker commits, then the receiver sees a failure once (needs ops: a transaction
# port).
def test_receiver_ready_lost_commit_ack_recovers_without_second_copy(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    original = receiver.write_transaction
    count = 0

    @contextmanager
    def ack_lost_once(pool: ConnectionPool):
        nonlocal count
        count += 1
        current = count
        with original(pool) as conn:
            yield conn
        if current == 2:
            raise psycopg.OperationalError("receiver ready ack lost")

    monkeypatch.setattr(receiver, "write_transaction", ack_lost_once)
    with pytest.raises(psycopg.OperationalError):
        receiver.receive(client.app.state.db_pool, request, local_machine=machine_identity.name())
    _receive_twice_after_failure(client, db_conn, request, machine_identity)


def test_concurrent_receivers_and_inbound_acceptance_one_identity(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentity,
):
    from concurrent.futures import ThreadPoolExecutor

    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )

    def copied(_index: int) -> CopyProof:
        return receiver.receive(
            client.app.state.db_pool,
            request,
            local_machine=machine_identity.name(),
        )

    def completed(proof: CopyProof) -> int:
        return source.complete(client.app.state.db_pool, request, proof)

    with ThreadPoolExecutor(max_workers=3) as executor:
        proofs = list(executor.map(copied, range(3)))
        receipts = list(executor.map(completed, proofs))
    assert proofs[0] == proofs[1] == proofs[2]
    assert len(set(receipts)) == 1
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_copies").fetchone() == (1,)
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)


@pytest.mark.usefixtures("gateway_local_runner")
def test_receiver_changed_manifest_and_deleted_source_url_fail_without_false_ready(
    uploaded_agent: tuple[GatewayTestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    proof = receiver.receive(
        client.app.state.db_pool,
        request,
        local_machine=machine_identity.name(),
    )
    item = request.manifest.objects[0]
    changed = request.model_copy(
        update={
            "manifest": request.manifest.model_copy(
                update={"objects": [item.model_copy(update={"sha256": "0" * 64})]}
            )
        }
    )
    from base.agents.upload_delivery.models import UploadDeliveryConflictError

    with pytest.raises(UploadDeliveryConflictError):
        receiver.receive(client.app.state.db_pool, changed, local_machine=machine_identity.name())
    path = Path(proof.directory) / item.name
    path.unlink()

    def gone(url: str, **_kwargs: Any):
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(receiver, "http_get", gone)
    status, result = dispatch_sync(
        "upload-receive-v1",
        request.model_dump(),
        pool=client.app.state.db_pool,
        db=client.app.state.db,
        authority=config_authority,
        image=client.app.state.process_image,
        local_machine=machine_identity,
        host=fake_host(),
    )
    assert status == OpStatus.FAILED and result["reason"] == "upload-source-unavailable-v1"
    assert not path.exists()


def test_actual_old_gateway_has_no_guarded_upload_admission(
    uploaded_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection[Any]
):
    from fastapi import FastAPI

    from gateway.routers.upload.router import router as old_routes

    _client, agent = uploaded_agent
    old = FastAPI()
    old.include_router(old_routes)
    with TestClient(old) as client:
        result = post(client, agent)
    assert result.status_code in (404, 405)
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (0,)


def test_single_connection_receiver_releases_pool_across_http(
    uploaded_agent: tuple[GatewayTestClient, int],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    remote_dir = tmp_path / "single-native" / f"MeivaAgent-{agent}"

    def directory(_id: int, **_kwargs: Any) -> Path:
        return remote_dir

    monkeypatch.setattr(receiver, "agent_upload_dir", directory)
    with client.app.state.db.pool(min_size=1, max_size=1) as pool:

        def get(url: str, **_kwargs: Any):
            with pool.connection(timeout=0.5) as conn:
                assert conn.execute("SELECT 1").fetchone() == (1,)
            result = client.get(
                url.removeprefix(
                    receiver.gateway_api_base(settings.gateway.gateway_url).rstrip("/")
                )
            )
            return httpx.Response(
                result.status_code, content=result.content, request=httpx.Request("GET", url)
            )

        monkeypatch.setattr(receiver, "http_get", get)
        proof = receiver.receive(pool, request, local_machine=machine_identity.name())
    assert (Path(proof.directory) / request.manifest.objects[0].name).read_bytes() == b"bytes"


def test_auth_revocation_and_missing_target_never_recover_by_bypassing_admission(
    uploaded_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection[Any]
):
    client, agent = uploaded_agent
    response = post(client, agent)
    assert response.status_code == 202
    assert post(client, agent + 999).status_code == 404
    with gateway_test_client(
        process=client.app.state.gateway_process,
        config=replace(
            client.app.state.gateway_config,
            cluster_secret="new-test-secret",  # noqa: S106 -- isolated test credential
        ),
        headers={"Authorization": f"Bearer {SECRET}"},
        raise_server_exceptions=False,
    ) as rotated:
        assert post(rotated, agent).status_code in (401, 403)
        assert rotated.get(response.json()["status_url"]).status_code in (401, 403)
    assert db_conn.execute("SELECT count(*) FROM upload_delivery_batches").fetchone() == (1,)


@pytest.mark.asyncio
async def test_same_machine_other_home_requires_native_rpc_not_local_shortcut(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentity,
):
    client, agent = uploaded_agent
    unit = receiver.current_unit(local_machine=machine_identity.name())
    target = unit.model_copy(update={"home": unit.home + "-second"})

    def current_target(*, local_machine: str) -> UnitIdentity:
        assert local_machine == machine_identity.name()
        return target

    # meiva:note
    # needs ops: the receiver's unit and its quota constant are read ambiently.
    monkeypatch.setattr(receiver, "current_unit", current_target)
    monkeypatch.setattr(receiver, "MAX_AGENT_UPLOAD_BYTES", 5)
    receive = _receiver_op(client, config_authority, machine_identity)
    with runner_ops(db_conn, unit.machine, {"upload-receive-v1": receive}) as runner:
        assert register_second_home(db_conn, unit, url=runner.url) == target
        response = post(client, agent)
        assert response.status_code == 202
        request = request_for(
            client.app.state.db_pool,
            response.json()["batch_id"],
            local_machine=machine_identity.name(),
        )
        assert request.source != request.target and request.target == target
        recovery = recovery_for(client, machine_identity)
        await recovery.copy(request)
        await recovery.close()
    assert len(runner.calls_of("upload-receive-v1")) == 1
    assert (
        source.status(client.app.state.db_pool, agent, request.manifest.batch_id).state
        == "accepted"
    )
