# meiva:note
# Real SDK billing assertions run in a fresh, explicitly configured process.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

from __future__ import annotations

import re
import sys
from collections.abc import Mapping
from typing import Literal

import pytest
from langchain_core.messages import AIMessage
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.util.types import AttributeValue

from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.lm.providers.billing_runtime import BillingRuntime, armed_billing
from base.packages.tests.lm.providers.billing_sensors import disabled, failing


def span_attributes(span: ReadableSpan) -> Mapping[str, AttributeValue]:
    attributes = span.attributes
    assert attributes is not None
    return attributes


def schema(tracer: BillingRuntime) -> None:
    # meiva:note
    # A priced cached call is one immediately-ended billing span with the v1 fields.
    #
    # The regression this catches is a billed provider call missing an attribute,
    # losing its original start time, or opening a span without closing it.
    #
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    from base.lm.usage import emit_billing_event

    emit_billing_event(
        vendor="deepseek",
        model="deepseek-v4-pro",
        tok_in=1000,
        tok_out=100,
        tok_cached=800,
        cost_usd=0.0003476,
        usage_kind="agent",
        start_time_ns=123456,
    )
    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.name == "meiva.billing.call"
    assert span.start_time == 123456
    assert span.end_time is not None
    assert {
        key: value for (key, value) in span_attributes(span).items() if key != "meiva.billing.ts"
    } == {
        "meiva.billing.line": "meiva",
        "meiva.billing.vendor": "deepseek",
        "meiva.billing.model": "deepseek-v4-pro",
        "meiva.billing.tokens_in": 1000,
        "meiva.billing.tokens_out": 100,
        "meiva.billing.cost": 0.000348,
        "meiva.billing.usage_kind": "agent",
        "meiva.billing.cache_read_tokens": 800,
    }
    timestamp = span_attributes(span)["meiva.billing.ts"]
    assert isinstance(timestamp, str)
    assert re.fullmatch("\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z", timestamp)


def cache_write_ttls(tracer: BillingRuntime, model_catalog: ModelCatalog, path: str) -> None:
    from base.lm.usage import emit_billing_from_message, log_usage_from_message

    message = AIMessage(
        content="answer",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {
                "cache_read": 200,
                "ephemeral_5m_input_tokens": 300,
                "ephemeral_1h_input_tokens": 400,
            },
        },
    )
    if path == "message":
        emit_billing_from_message(
            message, model="claude-opus-5-5", usage_kind="chat", catalog=model_catalog
        )
    else:
        log_usage_from_message(
            message, model="claude-opus-5-5", usage_kind="agent", catalog=model_catalog
        )
    assert len(tracer.spans) == 1
    attrs = span_attributes(tracer.spans[0])
    assert attrs["meiva.billing.cache_write_5m_tokens"] == 300
    assert attrs["meiva.billing.cache_write_1h_tokens"] == 400
    assert attrs["meiva.billing.cost"] == pytest.approx(0.00614)


def unpriced(tracer: BillingRuntime, model_catalog: ModelCatalog) -> None:
    # meiva:note
    # A known vendor without catalog pricing records an explicit free-unpriced span.
    #
    # The regression this catches is treating an unknown catalog price as a
    # priced $0 call, which makes ledger gaps invisible.
    #
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    from base.lm.usage import emit_billing_from_message

    message = AIMessage(
        content="answer",
        usage_metadata={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
    )
    emit_billing_from_message(
        message, model="deepseek-not-priced", usage_kind="chat", catalog=model_catalog
    )
    assert len(tracer.spans) == 1
    assert span_attributes(tracer.spans[0])["meiva.billing.vendor"] == "deepseek"
    assert span_attributes(tracer.spans[0])["meiva.billing.cost"] == 0.0
    assert span_attributes(tracer.spans[0])["meiva.billing.unpriced"] is True


def missing_usage(tracer: BillingRuntime, model_catalog: ModelCatalog) -> None:
    # meiva:note
    # A response with no standardized token usage produces no billing span.
    #
    # The regression this catches is converting unknown usage into a misleading
    # zero-token ledger event.
    #
    # meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md
    from base.lm.usage import emit_billing_from_message

    emit_billing_from_message(
        AIMessage(content="answer"),
        model="deepseek-v4-pro",
        usage_kind="chat",
        catalog=model_catalog,
    )
    assert tracer.spans == []


def cache_read(
    tracer: BillingRuntime, model_catalog: ModelCatalog, path: str, cached: int | None
) -> None:
    from base.lm.usage import emit_billing_from_message, log_usage_from_message

    message = AIMessage(
        content="answer",
        usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 50,
            "total_tokens": 1050,
            "input_token_details": {} if cached is None else {"cache_read": cached},
        },
    )
    if path == "message":
        emit_billing_from_message(
            message, model="deepseek-flash", usage_kind="chat", catalog=model_catalog
        )
    else:
        log_usage_from_message(
            message, model="deepseek-flash", usage_kind="chat", catalog=model_catalog
        )
    [span] = tracer.spans
    assert span_attributes(span)["meiva.billing.tokens_in"] == 1000
    cost = span_attributes(span)["meiva.billing.cost"]
    assert isinstance(cost, (int, float))
    assert cost > 0
    if cached is None:
        assert "meiva.billing.cache_read_tokens" not in span_attributes(span)
    else:
        assert span_attributes(span)["meiva.billing.cache_read_tokens"] == 0


def main(scenario: str, path: Literal["message", "durable"], cached: int | None) -> None:
    catalog = build_model_catalog()
    if scenario == "disabled":
        disabled()
        return
    with armed_billing() as tracer:
        match scenario:
            case "schema":
                schema(tracer)
            case "cache_write_ttls":
                cache_write_ttls(tracer, catalog, path)
            case "unpriced":
                unpriced(tracer, catalog)
            case "missing_usage":
                missing_usage(tracer, catalog)
            case "cache_read":
                cache_read(tracer, catalog, path, cached)
            case "failure":
                failing(tracer)
            case _:
                raise ValueError(f"Unknown billing scenario: {scenario!r}")


if __name__ == "__main__":
    path = sys.argv[2]
    if path not in ("message", "durable"):
        raise ValueError(f"Unknown billing path: {path!r}")
    main(sys.argv[1], path, None if sys.argv[3] == "none" else int(sys.argv[3]))
