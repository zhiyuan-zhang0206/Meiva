# meiva:note
# Closing a takeover attachment. The fence that rejects a call arriving after close began and still
# lets a call admitted before it finish, the ordering of flush, receipt seal, telemetry delivery
# and detach, and which error a caller sees when several of those fail. The SDK call goes to a
# real listener that holds its reply until the test releases it, the receipts are the real rows,
# and the telemetry sink is a real event pipeline whose writer collects the delivered events.
# meiva:link meiva/impersonation/docs/attachment.meiva.okf.md

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from langchain_core.messages import RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

import meiva
from agent.tests.impersonation.external.runtime import Takeover, borrowed_agent_id
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.log.tests.log_records import LogRecords
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event
from base.telemetry.otlp.tests.external_flush import HANDSHAKE_BOUND_S
from base.telemetry.otlp.tests.external_flush import paused_otlp_record as paused_otlp_record
from meiva import impersonation
from meiva.sdk_surface import process_context
from meiva.tests.fixtures.gateway_wire import GatewayStub

SEND = "/api/agents/99/messages"
LAST_MESSAGE = "/api/agents/99/last-message"


# meiva:note
# What an attachment's process talks to: a listener for the gateway and a sink for events.
@dataclass(frozen=True)
class Observed:
    gateway: GatewayStub
    delivered: list[Event]
    pipeline: EventPipeline


@pytest.fixture
def observed(attached_runtime: Takeover, gateway_stub: GatewayStub) -> Iterator[Observed]:
    delivered: list[Event] = []
    pipeline = EventPipeline(writer=delivered.extend, flush_interval_s=3600.0)
    clients = ClientSet(
        gateway_url=gateway_stub.url,
        database=lambda: attached_runtime.cluster.database,
        gateway=lambda url: httpx.Client(base_url=url),
        pipeline_factory=lambda: pipeline,
        factories={
            GatewayTransportInputs: lambda: GatewayTransportInputs(
                max_retries_reader=lambda: 1,
                retry_delay_reader=lambda: 0.0,
                memory_deadline_reader=lambda: 15.0,
            )
        },
    )
    process_context.bind_context(replace(process_context.current_context(), clients=clients))
    try:
        yield Observed(gateway_stub, delivered, pipeline)
    finally:
        pipeline.stop()
        clients.close()


def refused_after_close_began(gateway: GatewayStub) -> None:
    deadline = time.monotonic() + HANDSHAKE_BOUND_S
    while time.monotonic() < deadline:
        try:
            meiva.agents.get_last_message(99)
        except RuntimeError as error:
            assert "closing" in str(error)
            return
        time.sleep(0.01)
    pytest.fail("close never began to refuse new SDK calls")


def test_close_waits_for_an_admitted_sdk_call_and_refuses_later_ones(
    attached_runtime: Takeover, observed: Observed
) -> None:
    gateway, release = observed.gateway, threading.Event()
    gateway.hold("POST", SEND, release, {"status": "enqueued"}, status=201)
    gateway.reply("GET", LAST_MESSAGE, {"text": "peer output"})
    attachment = impersonation.attach(attached_runtime.lease_id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sending = pool.submit(meiva.agents.send_message, 99, "already admitted")
        assert gateway.arrived.wait(HANDSHAKE_BOUND_S), (
            "the admitted call never reached the gateway"
        )
        closing = pool.submit(attachment.close)
        refused_after_close_began(gateway)
        before = len(gateway.received)
        with pytest.raises(RuntimeError, match="closing"):
            meiva.agents.get_last_message(99)
        assert len(gateway.received) == before
        assert not closing.done(), "close finished while an admitted call was still in flight"
        release.set()
        sending.result(timeout=HANDSHAKE_BOUND_S)
        closing.result(timeout=HANDSHAKE_BOUND_S)
    posts = [request.body["content"] for request in gateway.received if request.method == "POST"]
    assert posts == ["already admitted"]
    assert attached_runtime.receipts() == ["sealed"]
    assert borrowed_agent_id() is None


def test_close_delivers_queued_telemetry_when_the_plugin_flush_fails(
    attached_runtime: Takeover, observed: Observed
) -> None:
    observed.gateway.reply("POST", SEND, {"status": "enqueued"}, status=201)
    attachment = impersonation.attach(attached_runtime.lease_id)
    meiva.agents.send_message(99, "queued event")
    assert observed.delivered == []
    process_context.set_state_update({"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
    with pytest.raises(ValueError, match="REMOVE_ALL"):
        attachment.close()
    sdk_calls = [event for event in observed.delivered if event.event_name == "sdk_call"]
    assert [event.agent_id for event in sdk_calls] == [attached_runtime.agent_id]
    assert borrowed_agent_id() is None


def test_close_waits_for_a_dequeued_otlp_record_before_force_flush(
    attached_runtime: Takeover,
    paused_otlp_record: tuple[threading.Event, Any],
) -> None:
    release, exporter = paused_otlp_record
    meiva.context.clients.event_pipeline()
    attachment = impersonation.attach(attached_runtime.lease_id)
    timer = threading.Timer(0.1, release.set)
    timer.daemon = True
    timer.start()
    try:
        started = time.monotonic()
        attachment.close()
        assert time.monotonic() - started >= 0.08
        assert len(exporter.get_finished_logs()) == 1
    finally:
        release.set()
        timer.cancel()


def fail_the_open_receipt(takeover: Takeover) -> None:
    (key,) = (
        row[0]
        for row in takeover.cluster.db_conn.execute(
            "SELECT source_key FROM agent_impersonation_event_participants WHERE lease_id=%s",
            (takeover.lease_id,),
        ).fetchall()
    )
    takeover.cluster.db_conn.execute(
        "SELECT seal_impersonation_event_participant(%s,%s,'failed','capture_failed',NULL)",
        (takeover.lease_id, key),
    )
    takeover.cluster.db_conn.commit()


def test_close_preserves_the_flush_error_when_receipt_cleanup_also_fails(
    attached_runtime: Takeover,
) -> None:
    attachment = impersonation.attach(attached_runtime.lease_id)
    fail_the_open_receipt(attached_runtime)
    process_context.set_state_update({"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
    with pytest.raises(ValueError, match="REMOVE_ALL") as raised:
        attachment.close()
    assert any("cannot seal" in note for note in raised.value.__notes__)
    assert borrowed_agent_id() is None


def test_attachment_context_keeps_the_body_error_primary_when_close_fails(
    attached_runtime: Takeover,
) -> None:
    attachment = impersonation.attach(attached_runtime.lease_id)
    primary = LookupError("body failed")
    with pytest.raises(LookupError) as raised, attachment:
        process_context.set_state_update({"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES)]})
        raise primary
    assert raised.value is primary
    assert any("REMOVE_ALL" in note for note in primary.__notes__)
    assert borrowed_agent_id() is None


def test_close_reports_telemetry_delivery_that_did_not_finish(
    attached_runtime: Takeover, loguru_records: LogRecords
) -> None:
    release = threading.Event()

    def hold(batch: list[Event]) -> None:
        del batch
        release.wait(HANDSHAKE_BOUND_S)

    blocked = EventPipeline(writer=hold, flush_interval_s=0.01)
    clients = ClientSet(
        database=lambda: attached_runtime.cluster.database, pipeline_factory=lambda: blocked
    )
    process_context.bind_context(replace(process_context.current_context(), clients=clients))
    try:
        attachment = impersonation.attach(attached_runtime.lease_id)
        meiva.context.clients.event_pipeline().enqueue(
            Event(
                ts=datetime.now(UTC),
                trace_id=None,
                span_id=None,
                agent_id=attached_runtime.agent_id,
                machine="test",
                cluster="test",
                process="external-test",
                category="telemetry",
                event_name="sdk_call",
                level="info",
                source=f"agent:{attached_runtime.agent_id}",
                target_agent_id=None,
                attributes={"fn": "files.read", "duration": 0.01},
            )
        )
        attachment.close()
    finally:
        release.set()
        blocked.stop()
        clients.close()
    assert any(
        "owned telemetry delivery is unfinished" in m for m in loguru_records.messages("WARNING")
    )
    assert borrowed_agent_id() is None
