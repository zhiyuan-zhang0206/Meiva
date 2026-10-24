# meiva:note
# Observe public SDK calls and inject a real processor failure without replacing tracer lookup.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

from __future__ import annotations

import loguru
from loguru import logger
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor, TracerProvider

from base.cluster.machine import MachineFacts
from base.config import settings
from base.lm.usage import emit_billing_event
from base.paths import meiva_home
from base.telemetry import tracing
from base.telemetry.otlp.trace_config import TraceConfig
from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING
from base.telemetry.tests.capture.armed_tracing import BillingRuntime


def disabled() -> None:
    config = TraceConfig.of(settings.observability, endpoint_explicit=False)
    assert config.enabled is False
    machine = MachineFacts.of(name="billing-child", role=(), description="billing child")
    tracing.initialize_tracing(
        machine=machine, home=meiva_home(), config=config, readiness=PROCESS_RECORDING
    )
    assert not PROCESS_RECORDING.ready
    assert not isinstance(trace.get_tracer_provider(), TracerProvider)
    emit_billing_event(
        vendor="deepseek",
        model="deepseek-v4-pro",
        tok_in=1,
        tok_out=1,
        cost_usd=0.0,
        usage_kind="chat",
    )


class FailingBillingProcessor(SpanProcessor):
    def __init__(self, failure: RuntimeError) -> None:
        self.failure = failure
        self.started: list[str] = []

    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        if span.name == "meiva.billing.call":
            self.started.append(span.name)
            raise self.failure


def failing(tracer: BillingRuntime) -> None:
    failure = RuntimeError("tracing is unavailable")
    processor = FailingBillingProcessor(failure)
    tracer.provider.add_span_processor(processor)
    reported: list[BaseException] = []

    def record(message: loguru.Message) -> None:
        exception = message.record["exception"]
        if exception is not None and exception.value is not None:
            reported.append(exception.value)

    sink = logger.add(record)
    try:
        assert (
            emit_billing_event(
                vendor="deepseek",
                model="deepseek-v4-pro",
                tok_in=1,
                tok_out=1,
                cost_usd=0.0,
                usage_kind="chat",
            )
            is None
        )
        assert processor.started == ["meiva.billing.call"]
        assert any(exception is failure for exception in reported)
        assert tracer.spans == []
    finally:
        logger.remove(sink)
