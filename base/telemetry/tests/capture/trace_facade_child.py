from __future__ import annotations

import sys

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import INVALID_SPAN_CONTEXT, NonRecordingSpan

from base.clock.tests.fakes import system_clock
from base.cluster.machine import MachineFacts
from base.paths import meiva_home
from base.telemetry import tracing
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING
from base.telemetry.tests.capture.armed_tracing import BillingRuntime, armed_billing


def inactive(scenario: str) -> None:
    if scenario.endswith("disabled"):
        tracing.initialize_tracing(
            now=system_clock().now,
            machine=MachineFacts.of(name="", role=()),
            home=meiva_home(),
            config=TraceConfig(False, False, "http://127.0.0.1:1", False),
            readiness=PROCESS_RECORDING,
        )
    assert not PROCESS_RECORDING.ready
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    if scenario.startswith("turn"):
        with tracing.turn_span(agent_id=42, turn=3):
            assert not trace.get_current_span().is_recording()
    else:
        with tracing.claim_idle_wait_span():
            assert not trace.get_current_span().is_recording()
    assert not PROCESS_RECORDING.resolved


def root(tracer: BillingRuntime) -> None:
    sdk = tracer.provider.get_tracer("probe")
    with sdk.start_as_current_span("producer"):
        sender = trace.get_current_span().get_span_context()
        carrier = tracing.current_carrier()
    previous = sdk.start_span("previous-turn-node")
    previous.end()
    with trace.use_span(previous):
        with tracing.turn_span(agent_id=42, turn=3):
            current = trace.get_current_span().get_span_context()
            assert current.trace_id != previous.get_span_context().trace_id
            assert [s.name for s in tracer.exporter.get_finished_spans()] == [
                "producer",
                "previous-turn-node",
            ]
            with sdk.start_as_current_span("claim"):
                tracing.link_inbox_input(None)
                tracing.link_inbox_input({"traceparent": "invalid"})
                tracing.link_inbox_input(carrier)
        assert trace.get_current_span() is previous

    *_, claim, root = tracer.exporter.get_finished_spans()
    assert root.name == "meiva-agent-42"
    assert root.parent is None
    assert root.context == current
    assert claim.parent == root.context
    assert root.end_time is not None and claim.end_time is not None
    assert root.end_time >= claim.end_time
    assert dict(root.attributes or {}) == {
        "session.id": "42",
        "meiva.turn": 3,
        "meiva.agent_id": 42,
    }
    [link] = root.links
    assert link.context.trace_id == sender.trace_id
    assert link.context.span_id == sender.span_id
    assert link.context.is_remote


def idle(tracer: BillingRuntime, scenario: str) -> None:
    if scenario == "nonrecording":
        with trace.use_span(NonRecordingSpan(INVALID_SPAN_CONTEXT)), tracing.claim_idle_wait_span():
            assert not trace.get_current_span().is_recording()
        assert tracer.exporter.get_finished_spans() == ()
        return
    name = "execute_task claim" if scenario == "node" else "meiva-agent-42"
    with (
        tracer.provider.get_tracer("langchain-node").start_as_current_span(name) as span,
        tracing.claim_idle_wait_span(),
    ):
        received = tracer.exporter.get_finished_spans()
        if scenario == "node":
            assert [item.name for item in received] == [name]
            assert received[0].end_time is not None
        else:
            assert received == ()
            assert span.is_recording()
    names = [span.name for span in tracer.exporter.get_finished_spans()]
    assert names == ([name, "claim idle-wait"] if scenario == "node" else [name])


def main(scenario: str) -> None:
    if scenario in ("turn-disabled", "turn-uninitialized", "idle-disabled", "idle-uninitialized"):
        inactive(scenario)
        return
    with armed_billing() as tracer:
        if scenario == "resolved":
            tracing.arm_tracing(
                "http://127.0.0.1:1",
                production=False,
                home=meiva_home(),
                strip_content=False,
                readiness=PROCESS_RECORDING,
            )
            assert PROCESS_RECORDING.ready
            assert trace.get_tracer_provider() is tracer.provider
        elif scenario == "root":
            root(tracer)
        elif scenario in ("node", "nonnode", "nonrecording"):
            idle(tracer, scenario)
        else:
            raise ValueError(f"Unknown trace facade scenario: {scenario!r}")


if __name__ == "__main__":
    main(sys.argv[1])
