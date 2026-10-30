"""Native hard-death staging, single wake, bounded copy attempts and actual future draining."""

import asyncio
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.agents.upload_delivery import source, storage
from base.agents.upload_delivery.models import NAMESPACE, UploadDeliveryConflictError
from base.cluster import inbound_channel
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.events.live.bus import EventBus
from base.telemetry.tests.capture.event_capture import capturing_events
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)
from gateway.tests.support.live_events.faults import unpublishable_bus
from gateway.tests.support.runner_ops.ops_stub import refused, runner_ops
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    CLAIM_SECONDS,
    post,
    proof_for,
    recovery_for,
    register_second_home,
    request_for,
)
from gateway.tests.upload_delivery.test_upload_delivery_recovery import (
    uploaded_agent as uploaded_agent,
)
from gateway.upload_delivery.worker import UploadRecovery


@pytest.mark.parametrize("stage", ["before-link", "after-link"])
def test_hard_death_native_temp_not_double_charged_or_cleaned(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    stage: str,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    # meiva:note
    # Publication cannot start: the staging root is a file, so it cannot hold the agent's
    # directory (a fault that binds the root as well, unlike a permission bit). Nothing was
    # uploaded before this test, so no earlier request created the root.
    staging = client.app.state.gateway_config.upload_staging_root
    staging.write_text("not a directory")
    assert post(client, agent).status_code == 500
    staging.unlink()
    row = db_conn.execute("SELECT manifest FROM upload_delivery_batches").fetchone()
    assert row is not None
    manifest = source.Manifest.model_validate(row[0])
    directory = source.agent_upload_dir(
        agent, create=False, root=client.app.state.gateway_config.upload_staging_root
    ).resolve()
    final = storage.object_path(directory, manifest, manifest.objects[0])
    result = subprocess.run(  # noqa: S603 -- fixed interpreter/script and isolated fixture arguments
        [
            sys.executable,
            str(Path(__file__).with_name("child_native_link_crash.py")),
            str(final),
            stage,
        ],
        env=dict(os.environ),
        check=False,
    )
    assert result.returncode == 73
    temps = list(final.parent.glob(".*.tmp"))
    assert len(temps) == 1
    with client.app.state.db_pool.connection() as conn:
        storage.check_quota(
            conn,
            agent,
            directory,
            source.current_unit(local_machine=machine_identity.name()).machine,
            0,
            0,
            5,
            1,
        )
    assert post(client, agent).status_code == 202
    assert final.read_bytes() == b"bytes"
    assert temps[0].exists()  # No orphan cleanup or inferred writer-dead takeover.


def test_hold_is_not_automatically_unsealed_by_late_copy_proof(
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
    source.record_failure(
        client.app.state.db_pool,
        request.manifest.batch_id,
        "unsupported-receiver",
        hold=True,
    )
    with pytest.raises(UploadDeliveryConflictError, match="held"):
        source.complete(client.app.state.db_pool, request, proof_for(client, request))
    assert source.status(client.app.state.db_pool, agent, request.manifest.batch_id).state == "hold"
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


@pytest.mark.parametrize(
    "directory",
    ["relative", "/elsewhere", "/nowhere/workspaces/1/uploads/.delivered-v1/wrong"],
)
def test_unusable_native_directory_never_accepts_inbound(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    directory: str,
    *,
    machine_identity: MachineIdentityReader,
):
    from pydantic import ValidationError

    client, agent = uploaded_agent
    request = request_for(
        client.app.state.db_pool,
        post(client, agent).json()["batch_id"],
        local_machine=machine_identity.name(),
    )
    proof = proof_for(client, request).model_dump()
    proof["directory"] = directory
    with pytest.raises((ValidationError, UploadDeliveryConflictError)):
        source.complete(client.app.state.db_pool, request, source.CopyProof.model_validate(proof))
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)


@pytest.mark.asyncio
async def test_shutdown_drains_native_future_after_cancelled_http_wait(
    uploaded_agent: tuple[GatewayTestClient, int], *, machine_identity: MachineIdentityReader
):
    client, _agent = uploaded_agent
    recovery = recovery_for(client, machine_identity)
    entered, finish = threading.Event(), threading.Event()

    def native_writer():
        entered.set()
        assert finish.wait(5)
        with client.app.state.db_pool.connection() as conn:
            return conn.execute("SELECT 42").fetchone()[0]

    task = asyncio.create_task(recovery.native(native_writer))
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    close = asyncio.create_task(recovery.close())
    try:
        await asyncio.sleep(0.02)
        assert not close.done() and len(recovery.calls) == 1
    finally:
        finish.set()
        await close
    assert not recovery.calls


# meiva:note
# Subscribe to the agent's inbound wake channel; the callable drains what arrived.
@contextmanager
def _inbound_wakes(bus: EventBus, agent: int) -> Generator[Callable[[], list[str]]]:
    redis = bus.sync_redis(decode_responses=True)
    pubsub = redis.pubsub()  # pyright: ignore[reportUnknownMemberType]
    pubsub.subscribe(inbound_channel(agent, bus.channel))
    assert pubsub.get_message(timeout=5)["type"] == "subscribe"  # pyright: ignore[reportUnknownMemberType, reportOptionalSubscript]

    def received() -> list[str]:
        payloads: list[str] = []
        while True:
            # meiva:note
            # The redis-py client and its pubsub are untyped. The annotated shape is the one the untyped call returns.
            message: dict[str, Any] | None = pubsub.get_message(timeout=0.3)  # pyright: ignore[reportUnknownMemberType, reportAssignmentType]
            if message is None:
                return payloads
            if message["type"] == "message":
                payloads.append(str(message["data"]))

    try:
        yield received
    finally:
        pubsub.close()  # pyright: ignore[reportUnknownMemberType]
        redis.close()


@pytest.mark.asyncio
async def test_accepted_upload_wakes_once(
    uploaded_agent: tuple[GatewayTestClient, int], *, machine_identity: MachineIdentityReader
):
    client, agent = uploaded_agent
    batch_id = post(client, agent).json()["batch_id"]
    recovery = recovery_for(client, machine_identity)
    with _inbound_wakes(client.app.state.bus, agent) as received:
        await recovery.round()
        await recovery.round()
        await recovery.round()
        woken = received()
    status = source.status(client.app.state.db_pool, agent, batch_id)
    assert status.state == "accepted" and status.inbound_id is not None
    # meiva:note
    # The accepted chat is a system-sourced notice (the list of files, not the human
    # speaking) — and it still wakes the agent, which is what makes the delivery reach the
    # model.
    # meiva:link gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md
    with client.app.state.db_pool.connection() as conn:
        assert conn.execute(
            "SELECT source FROM inbound_messages WHERE id=%s", (status.inbound_id,)
        ).fetchone() == ("system:upload",)
    # meiva:note
    # One wake at acceptance; the still-pending chat is never re-announced by a scan.
    assert woken == [str(status.inbound_id)]
    await recovery.close()


@pytest.mark.asyncio
async def test_a_lost_wake_is_not_repeated(
    uploaded_agent: tuple[GatewayTestClient, int], *, machine_identity: MachineIdentityReader
):
    client, agent = uploaded_agent
    batch_id = post(client, agent).json()["batch_id"]
    lossy = UploadRecovery(
        client.app.state.db_pool,
        client.app.state.db,
        unpublishable_bus(),
        machine=machine_identity,
        claim_seconds=CLAIM_SECONDS,
        rpc=client.app.state.rpc,
    )
    healthy = recovery_for(client, machine_identity)
    with _inbound_wakes(client.app.state.bus, agent) as received:
        await lossy.round()
        # meiva:note
        # Accepted although the wake never left; no later round, on any worker, announces it.
        await healthy.round()
        await healthy.round()
        woken = received()
    status = source.status(client.app.state.db_pool, agent, batch_id)
    assert status.state == "accepted" and status.inbound_id is not None
    assert woken == []
    await lossy.close()
    await healthy.close()


@pytest.mark.asyncio
async def test_failed_copies_hold_after_the_attempt_budget_and_alert_once(
    uploaded_agent: tuple[GatewayTestClient, int],
    db_conn: psycopg.Connection[Any],
    *,
    machine_identity: MachineIdentityReader,
):
    client, agent = uploaded_agent
    unit = source.current_unit(local_machine=machine_identity.name())
    # meiva:note
    # The receiving runner never answers the receive op.
    with runner_ops(
        db_conn, unit.machine, {"upload-receive-v1": refused(404, "receiver unreachable")}
    ) as runner:
        register_second_home(db_conn, unit, url=runner.url)
        batch_id = post(client, agent).json()["batch_id"]
        recovery = recovery_for(client, machine_identity)
        # Bound here, not as a fixture: the gateway of `uploaded_agent` binds its own pipeline first.
        with capturing_events() as emitted_events:
            for attempt in range(1, source.MAX_COPY_ATTEMPTS + 1):
                with client.app.state.db_pool.connection() as conn:
                    conn.execute("UPDATE upload_delivery_batches SET next_attempt_at=now()")
                await recovery.round()
                status = source.status(client.app.state.db_pool, agent, batch_id)
                assert status.attempts == attempt
                assert status.state == (
                    "hold" if attempt == source.MAX_COPY_ATTEMPTS else "pending"
                )
            assert [(event.event_name, event.attributes) for event in emitted_events] == [
                (
                    "upload_delivery_held",
                    {"agent_id": agent, "batch_id": batch_id, "reason": "ClusterOpUnreachable"},
                )
            ]
        # Held means held: no later round copies it again.
        with client.app.state.db_pool.connection() as conn:
            conn.execute("UPDATE upload_delivery_batches SET next_attempt_at=now()")
        await recovery.round()
        await recovery.close()
    final = source.status(client.app.state.db_pool, agent, batch_id)
    assert (final.state, final.attempts) == ("hold", source.MAX_COPY_ATTEMPTS)
    # meiva:note
    # Held is not lost: the bytes the gateway received are still in staging, so a person's
    # re-upload of the same files finds them and the hold never deletes evidence.
    staged = (
        source.agent_upload_dir(
            agent, create=False, root=client.app.state.gateway_config.upload_staging_root
        )
        / NAMESPACE
        / batch_id
    )
    assert [path.read_bytes() for path in staged.iterdir()] == [b"bytes"]


@pytest.mark.asyncio
async def test_a_stopped_recovery_skips_new_copy_and_db_round(
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
    recovery = recovery_for(client, machine_identity)
    recovery.stopped.set()
    await recovery.copy(request)
    # meiva:note
    # A running worker would copy and accept this batch; a stopped one leaves it pending.
    assert (
        source.status(client.app.state.db_pool, agent, request.manifest.batch_id).state == "pending"
    )
    assert db_conn.execute(
        "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
    ).fetchone() == (0,)
    await recovery.close()


@pytest.mark.asyncio
async def test_cancelled_shutdown_wait_still_drains_actual_native_writer(
    uploaded_agent: tuple[GatewayTestClient, int], *, machine_identity: MachineIdentityReader
):
    client, _agent = uploaded_agent
    recovery = recovery_for(client, machine_identity)
    entered, finish = threading.Event(), threading.Event()

    def native_writer():
        entered.set()
        assert finish.wait(5)
        with client.app.state.db_pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)

    call = asyncio.create_task(recovery.native(native_writer))
    async with asyncio.timeout(2):
        while not entered.is_set():
            await asyncio.sleep(0.01)
    close = asyncio.create_task(recovery.close())
    await asyncio.sleep(0.01)
    close.cancel()
    try:
        await asyncio.sleep(0.02)
        assert not close.done() and len(recovery.calls) == 1
    finally:
        finish.set()
        await call
        with pytest.raises(asyncio.CancelledError):
            await close
    assert not recovery.calls


# meiva:note
# A real gateway lifespan drives its own recovery loop through the runner's `upload-receive-v1`
# op to exactly one accepted chat. The copy is a receiver's copy even on the gateway's own
# machine — staging is not the agent's workspace — so the loop needs a runner that answers, as a
# remote batch does.
def test_actual_gateway_lifespan_recovers_one_chat_through_its_receiver(
    db_conn: psycopg.Connection[Any],
    machine_identity: MachineIdentityReader,
):
    import time

    from gateway.tests.events.test_notices_endpoint import seed_agent
    from gateway.tests.upload_delivery.support import receiver_op, serving_gateway
    from gateway.tests.upload_delivery.test_upload_delivery_recovery import SECRET, receiver_for

    unit = source.current_unit(local_machine=machine_identity.name())
    agent = seed_agent(db_conn)
    db_conn.execute("UPDATE agents_meta SET machine=%s WHERE id=%s", (unit.machine, agent))
    db_conn.commit()
    config = gateway_test_config(cluster_secret=SECRET)
    with (
        gateway_test_client(machine=machine_identity, config=config) as client,
        serving_gateway(client, client.app.state.db_pool) as source_url,
    ):
        client.app.state.gateway_access = replace(
            client.app.state.gateway_access, gateway_url=source_url
        )
        receive = receiver_op(client, receiver_for(client, unit))
        with runner_ops(db_conn, unit.machine, {"upload-receive-v1": receive}) as runner:
            db_conn.execute(
                "INSERT INTO machine_units(machine_name,home,serve_agent_runner,url) "
                "VALUES (%s,%s,true,%s)",
                (unit.machine, unit.home, runner.url),
            )
            db_conn.commit()
            response = post(client, agent)
            assert response.status_code == 202
            deadline = time.monotonic() + 10
            while client.get(response.json()["status_url"]).json()["state"] != "accepted":
                assert time.monotonic() < deadline
                time.sleep(0.02)
            recovery = client.app.state.upload_recovery
            assert recovery.task is not None and not recovery.task.done()
            assert db_conn.execute(
                "SELECT count(*) FROM inbound_messages WHERE agent_id=%s", (agent,)
            ).fetchone() == (1,)
    assert recovery.task.done() and not recovery.calls
    assert client.app.state.db_pool.closed


def test_overlapping_gateway_lifespans_stop_their_original_upload_workers(
    db_conn: psycopg.Connection[Any], *, machine_identity: MachineIdentityReader
) -> None:
    # Both existing HTTP fixture consumers run overlapping gateway lifespans.
    # Each lifespan must drain its own TaskGroup while sharing a borrowed process.
    with gateway_test_client(machine=machine_identity) as first_client:
        process = first_client.app.state.gateway_process
        pipeline = process.event_pipeline()
        original = first_client.app.state.upload_recovery
        assert original.task is not None and not original.stopped.is_set()
        with gateway_test_client(process=process) as second_client:
            assert second_client.app.state.gateway_process is process
            assert second_client.app.state.process_image is process.image
            assert process.event_pipeline() is pipeline
            replacement = second_client.app.state.upload_recovery
            assert replacement is not original
            assert replacement.task is not None and not replacement.stopped.is_set()
        assert not pipeline.stopped
        assert replacement.stopped.is_set() and replacement.task.done()
        assert not original.stopped.is_set() and not original.task.done()
    assert pipeline.stopped
    assert original.stopped.is_set() and original.task.done()
    assert not original.calls and not replacement.calls
