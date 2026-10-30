"""Remote copy and transactional inbound response-loss fault boundaries."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.datastructures import State

from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import (
    NAMESPACE,
    CopyProof,
    ReceiveRequest,
    UploadDeliveryConflictError,
    current_unit,
)
from base.agents.upload_delivery.paths import delivered_upload_dir
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import get_field
from base.config.service_read import ConfigAuthority
from base.daemon.endpoints import ServiceEndpoint, ServiceEndpoints
from base.db.tests.lost_ack import LostAckPool
from base.host.system.tests.fake_commands import fake_host
from base.paths import resolve_meiva_home, run_dir
from gateway.app import cluster_auth_middleware
from gateway.tests.auth.gateway_config import GatewayTestClient, gateway_test_client
from gateway.tests.support.db_faults.insert_fault import failing_insert
from gateway.tests.support.db_faults.update_fault import failing_update
from gateway.tests.support.runner_ops.ops_stub import (
    failed,
    runner_ops,
)
from gateway.tests.upload_delivery.support import (
    receiver_for as receiver_for,
)
from gateway.tests.upload_delivery.support import (
    receiver_op as receiver_op,
)
from gateway.tests.upload_delivery.support import (
    serving_gateway as serving_gateway,
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
from gateway.upload_delivery.router import router as delivery_router
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
# The ready write commits, then the caller sees a failure: the pool lends the batch claim first
# and the ready write second, and loses the acknowledgement of the second. The route answers 500,
# and the next upload of the same batch finds the committed work again.
def test_source_ready_lost_commit_ack_recovers(
    uploaded_agent: tuple[GatewayTestClient, int], db_conn: psycopg.Connection[Any]
):
    client, agent = uploaded_agent
    # meiva:note
    # Assemble the real upload router with its failing ConnectionSource from the start. Borrow
    # the live gateway's native worker and authentication resources; never rewire its pool.
    app = FastAPI()
    app.state = State(
        {
            "db_pool": LostAckPool(
                client.app.state.db_pool, lose_on=2, message="source ready response lost"
            ),
            "gateway_config": client.app.state.gateway_config,
            "gateway_process": client.app.state.gateway_process,
            "machine_token_acceptance": client.app.state.machine_token_acceptance,
            "machine": client.app.state.machine,
            "upload_recovery": client.app.state.upload_recovery,
        }
    )
    app.middleware("http")(cluster_auth_middleware)
    app.include_router(delivery_router)
    with GatewayTestClient(app, raise_server_exceptions=False) as faulted:
        assert post(faulted, agent).status_code == 500
    assert db_conn.execute("SELECT state FROM upload_delivery_batches").fetchone() == ("pending",)
    _recovered_source_ready(client, db_conn, agent)


def _request_and_proof(
    client: GatewayTestClient, agent: int, machine_identity: MachineIdentityReader
) -> tuple[ReceiveRequest, CopyProof]:
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    return request, proof_for(client, request)


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
    machine_identity: MachineIdentityReader,
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
# The inbound commits, then the caller sees a failure once.
def test_inbound_lost_commit_ack_recovers(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    request, proof = _request_and_proof(client, agent, machine_identity)
    lossy = LostAckPool(client.app.state.db_pool, lose_on=1, message="inbound commit ack lost")
    with pytest.raises(psycopg.OperationalError):
        source.complete(lossy, request, proof)
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
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    for model, payload in (
        (ReceiveRequest, request.model_dump()),
        (CopyProof, proof_for(client, request).model_dump()),
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
            home=resolve_meiva_home(),
            receiver=receiver_for(client, request.target),
            agent_host=_agent_host(),
        )


# meiva:note
# The endpoint row a deployed ops daemon hands its dispatch: this unit's agent-host port and pidfile.
def _agent_host() -> ServiceEndpoint:
    home = resolve_meiva_home()
    return ServiceEndpoints.from_fields(get_field, runtime=run_dir(home)).of("agent_host")


@pytest.mark.asyncio
async def test_real_remote_receiver_after_fsync_response_loss_one_inbound(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    source_unit = current_unit(local_machine=machine_identity.name())
    target = source_unit.model_copy(
        update={"machine": "remote", "home": str(tmp_path / "remote-unit")}
    )
    with serving_gateway(client, client.app.state.db_pool, timeout=1.0) as source_url:
        config = replace(
            receiver_for(client, target),
            access=replace(client.app.state.gateway_access, gateway_url=source_url),
        )
        receive = receiver_op(client, config, lose_first_response=True)
        with runner_ops(db_conn, "remote", {"upload-receive-v1": receive}) as runner:
            db_conn.execute(
                "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
                "VALUES ('remote',%s,true,%s)",
                (target.home, runner.url),
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
            assert (
                source.status(client.app.state.db_pool, agent, first["batch_id"]).state == "pending"
            )
            assert db_conn.execute(
                "SELECT count(*) FROM upload_delivery_copies WHERE ready_at IS NOT NULL"
            ).fetchone() == (1,)
            await recovery.copy(request)
            assert (
                source.status(client.app.state.db_pool, agent, first["batch_id"]).state
                == "accepted"
            )
            await recovery.close()
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (1,)
    remote_dir = delivered_upload_dir(
        agent, home=Path(target.home), name=config.upload_dir_name, create=False
    ).resolve()
    assert (
        remote_dir / ".delivered-v1" / request.manifest.batch_id / request.manifest.objects[0].name
    ).read_bytes() == b"bytes"


@pytest.mark.asyncio
async def test_unknown_remote_kind_holds(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    unit = current_unit(local_machine=machine_identity.name())
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
) -> None:
    config = receiver_for(client, request.target)
    proof = receiver.copy_batch(client.app.state.db_pool, request, config)
    assert proof == receiver.copy_batch(client.app.state.db_pool, request, config)
    assert db_conn.execute(
        "SELECT count(*) FROM upload_delivery_copies WHERE ready_at IS NOT NULL"
    ).fetchone() == (1,)


def test_receiver_ready_commit_failure_recovers_without_second_copy(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    config = receiver_for(client, request.target)
    with (
        failing_update(db_conn, "upload_delivery_copies", message="receiver ready rollback"),
        pytest.raises(psycopg.errors.RaiseException),
    ):
        receiver.copy_batch(client.app.state.db_pool, request, config)
    _receive_twice_after_failure(client, db_conn, request)


# meiva:note
# The ready marker commits, then the receiver sees a failure once: the pool lends the quota
# reservation first and the ready marker second.
def test_receiver_ready_lost_commit_ack_recovers_without_second_copy(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    config = receiver_for(client, request.target)
    lossy = LostAckPool(client.app.state.db_pool, lose_on=2, message="receiver ready ack lost")
    with pytest.raises(psycopg.OperationalError):
        receiver.copy_batch(lossy, request, config)
    _receive_twice_after_failure(client, db_conn, request)


def test_concurrent_receivers_and_inbound_acceptance_one_identity(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    from concurrent.futures import ThreadPoolExecutor

    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    config = receiver_for(client, request.target)

    def copied(_index: int) -> CopyProof:
        return receiver.copy_batch(client.app.state.db_pool, request, config)

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


def test_receiver_changed_manifest_and_deleted_source_url_fail_without_false_ready(
    uploaded_agent: tuple[GatewayTestClient, int],
    *,
    config_authority: ConfigAuthority,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    config = receiver_for(client, request.target)
    proof = receiver.copy_batch(
        client.app.state.db_pool,
        request,
        config,
    )
    item = request.manifest.objects[0]
    changed = request.model_copy(
        update={
            "manifest": request.manifest.model_copy(
                update={"objects": [item.model_copy(update={"sha256": "0" * 64})]}
            )
        }
    )

    with pytest.raises(UploadDeliveryConflictError):
        receiver.copy_batch(client.app.state.db_pool, changed, config)
    # meiva:note
    # The gateway loses its staged object and the agent's workspace has no copy: a re-receive
    # has to pull, and must not mark ready anything it could not fetch.
    staged = (
        source.agent_upload_dir(
            agent, create=False, root=client.app.state.gateway_config.upload_staging_root
        )
        / NAMESPACE
        / request.manifest.batch_id
        / item.name
    )
    staged.unlink()
    delivered = Path(proof.directory) / item.name
    delivered.unlink()

    with serving_gateway(client, client.app.state.db_pool, timeout=5.0) as source_url:
        status, result = dispatch_sync(
            "upload-receive-v1",
            request.model_dump(),
            pool=client.app.state.db_pool,
            db=client.app.state.db,
            authority=config_authority,
            image=client.app.state.process_image,
            local_machine=machine_identity,
            host=fake_host(),
            home=resolve_meiva_home(),
            receiver=replace(
                config, access=replace(client.app.state.gateway_access, gateway_url=source_url)
            ),
            agent_host=_agent_host(),
        )
    assert status == OpStatus.FAILED and result["reason"] == "upload-source-unavailable-v1"
    assert not delivered.exists()


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
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    with (
        client.app.state.db.pool(min_size=1, max_size=1) as pool,
        serving_gateway(client, pool, timeout=0.5) as source_url,
    ):
        config = replace(
            receiver_for(client, request.target),
            access=replace(client.app.state.gateway_access, gateway_url=source_url),
        )
        proof = receiver.copy_batch(pool, request, config)
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
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    unit = current_unit(local_machine=machine_identity.name())
    target = unit.model_copy(update={"home": unit.home + "-second"})
    config = replace(receiver_for(client, target), quota_bytes=5)
    receive = receiver_op(client, config)
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
