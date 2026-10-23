# meiva:note
# Observe public SDK calls and inject a real processor failure without replacing tracer lookup.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

from __future__ import annotations

import sys
from types import FrameType

import loguru
from loguru import logger
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import Span, SpanProcessor

from base.config import settings
from base.lm.usage import emit_billing_event
from base.packages.tests.lm.providers.billing_runtime import BillingRuntime


def disabled(tracer: BillingRuntime) -> None:
    acquisitions: list[str] = []

    def observe(frame: FrameType, event: str, arg: object) -> None:
        if event == "call" and frame.f_code is trace.get_tracer.__code__:
            acquisitions.append(frame.f_locals["instrumenting_module_name"])

    previous = sys.getprofile()
    sys.setprofile(observe)
    try:
        trace.get_tracer("billing-sensor-control")
        assert acquisitions == ["billing-sensor-control"]
        acquisitions.clear()
        assert settings.observability.trace_enabled is False
        emit_billing_event(
            vendor="deepseek",
            model="deepseek-v4-pro",
            tok_in=1,
            tok_out=1,
            cost_usd=0.0,
            usage_kind="chat",
        )
        assert acquisitions == []
        assert tracer.spans == []
    finally:
        sys.setprofile(previous)


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
