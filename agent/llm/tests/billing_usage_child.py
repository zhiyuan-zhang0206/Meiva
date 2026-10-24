# meiva:note
# Actual SDK/provider state belongs to this fresh child. Assertions inspect completed SDK spans
# and the real log sink rather than replacing the tracer facade or its initialization outcome.
from __future__ import annotations

import sys
import time
from datetime import UTC, datetime

import loguru
from langchain_core.messages import AIMessage

from agent.llm.usage import log_llm_usage
from base.lm.pricing import quote
from base.log import logger
from base.packages.plugin_providers import build_model_catalog
from base.telemetry.tests.capture.armed_tracing import BillingRuntime, armed_billing


def priced(tracer: BillingRuntime) -> None:
    catalog = build_model_catalog()
    priced_at = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)
    expected = quote("deepseek-v4-pro", 1_000, 100, 800, at=priced_at, prices=catalog.prices)
    assert expected is not None
    message = AIMessage(
        content="",
        usage_metadata={
            "input_tokens": 1_000,
            "output_tokens": 100,
            "total_tokens": 1_100,
            "input_token_details": {"cache_read": 800},
        },
    )
    records: list[loguru.Message] = []
    sink = logger.add(records.append)
    try:
        before_ns = time.time_ns()
        log_llm_usage(
            message,
            agent_id=7,
            model="deepseek-v4-pro",
            latency_ms=1_234.5,
            priced_at=priced_at,
            catalog=catalog,
        )
        after_ns = time.time_ns()
    finally:
        logger.remove(sink)
    [span] = tracer.spans
    assert span.start_time is not None and span.end_time is not None
    assert before_ns <= span.start_time + 1_234_500_000 <= after_ns
    attributes = span.attributes
    assert attributes is not None
    assert attributes["meiva.billing.vendor"] == "deepseek"
    assert attributes["meiva.billing.usage_kind"] == "agent"
    assert attributes["meiva.billing.tokens_in"] == 1_000
    assert attributes["meiva.billing.tokens_out"] == 100
    assert attributes["meiva.billing.cache_read_tokens"] == 800
    assert attributes["meiva.billing.cost"] == round(expected.cost_usd, 6)
    [event] = [m.record for m in records if m.record["extra"].get("event") == "llm_usage"]
    assert event["extra"]["cost_usd"] == expected.cost_usd


# meiva:note
# Pydantic's explicit unvalidated construction represents malformed standardized provider
# metadata. The consumer must retain its input-token observation without inventing a billable
# output total; this is a new message, not mutation of a previously validated one.
def incomplete(tracer: BillingRuntime) -> None:
    message = AIMessage.model_construct(
        content="", usage_metadata={"input_tokens": 100, "total_tokens": 100}
    )
    log_llm_usage(message, agent_id=7, model="deepseek-v4-pro", catalog=build_model_catalog())
    assert tracer.spans == []
    assert message.usage_metadata is not None
    assert message.usage_metadata["input_tokens"] == 100


def main(scenario: str) -> None:
    with armed_billing() as tracer:
        match scenario:
            case "priced":
                priced(tracer)
            case "incomplete":
                incomplete(tracer)
            case _:
                raise ValueError(f"Unknown agent billing scenario: {scenario!r}")


if __name__ == "__main__":
    main(sys.argv[1])
