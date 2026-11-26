# meiva:note
# Canonical durable usage accounting and billing spans for completed LLM calls.

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from langchain_core.messages import AIMessage
from loguru import logger

from base.api_contracts.agent_contract import harness_billing_of_source
from base.api_contracts.usage import (
    UsageLedger,
    UsageLedgerConflictError,
    UsageLedgerEntry,
    UsageLedgerWriteError,
)
from base.host.boundary.json_values import is_object_dict
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import InferenceSpeed
from base.lm.content import message_addl_kwargs
from base.lm.pricing import CacheTokens, CostQuote, quote, rates_at, tally_tokens
from base.lm.pricing.cache_writes import cache_write_tokens, default_write_ttl
from base.telemetry import ambient_agent_id
from base.telemetry.delivery.reporting import report_sink_failure


def usage_model(msg: AIMessage, requested_model: str, *, catalog: ModelCatalog) -> str:
    # meiva:note
    # Resolve the billable Meiva ID from a Fast call's actual service receipt.
    #
    # Preserve the requested ID for ordinary services. An explicit standard downgrade uses the
    # standard ID; a missing or unknown Fast receipt raises rather than fabricating premium
    # consumption.
    spec = catalog.models.get(requested_model)
    if spec is None or spec.fast_of is None:
        return requested_model
    binding = next(
        b for prefix, b in catalog.bindings.items() if requested_model.startswith(prefix)
    )
    if binding.served_speed is None:
        raise ValueError(f"Fast model {requested_model!r} has no served_speed adapter")
    speed = InferenceSpeed(binding.served_speed(msg.response_metadata))
    return requested_model if speed == InferenceSpeed.FAST else spec.fast_of


def _default_cache_write_ttl(model: str, *, catalog: ModelCatalog) -> Literal["5m", "30m"]:
    return default_write_ttl(vendor_of_model(model, catalog=catalog))


def _metered_cache_read(usage: Mapping[str, object], cached: int) -> int | None:
    # meiva:note
    # Distinguish absent provider metering from an explicitly reported zero.
    details = usage.get("input_token_details")
    keys = details if is_object_dict(details) else {}
    return (
        cached if any(key == "cache_read" or key.endswith("_cache_read") for key in keys) else None
    )


@dataclass(frozen=True, slots=True)
class UsageOptions:
    latency_ms: float | None = None
    decode_ms: float | None = None
    priced_at: datetime | None = None
    source: str | None = None
    for_agent_id: int | None = None
    attribution: str | None = None
    stamp_message: bool = False
    call_id: str | None = None

    @classmethod
    def metered(cls, latency_ms: float | None, *, call_id: str | None = None) -> UsageOptions:
        agent_id = ambient_agent_id()
        return cls(
            latency_ms=latency_ms,
            for_agent_id=agent_id,
            attribution="process" if agent_id is not None else "unattributed",
            stamp_message=True,
            call_id=call_id,
        )


_DEFAULT_USAGE_OPTIONS = UsageOptions()

type UsageReceiptSubmitter = Callable[[UsageLedgerEntry], None]


@dataclass(frozen=True, slots=True)
class UsageFields:
    tok_in: int
    tok_out: int = 0
    tok_cached: int | None = None
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_30m: int = 0
    tok_reasoning: int = 0
    latency_ms: float | None = None
    source: str | None = None
    for_agent_id: int | None = None
    calls: int = 1
    complete: bool = True
    call_id: str | None = None
    parent_call_id: str | None = None
    # meiva:note
    # True when a multi-step batch caller (`log_turn_usage`) has already warned once for this turn's unpriced model so
    # an external turn with hundreds of steps emits one warning while still recording every step's unpriced usage event
    # and billing span.
    unpriced_already_reported: bool = False


def log_usage_from_message(
    msg: AIMessage,
    model: str,
    *,
    usage_kind: str = "agent",
    options: UsageOptions = _DEFAULT_USAGE_OPTIONS,
    catalog: ModelCatalog,
    now: datetime,
) -> tuple[int, float] | None:
    # meiva:note
    # Log one completed LangChain message's token usage and price snapshot.
    #
    # `options.stamp_message` also writes the logged figures onto `msg.additional_kwargs["meiva_usage"]`
    # (`MeivaUsage`): the one `quote` result feeds the event and the message alike.

    # meiva:note
    # A message carries at most one usage record: the direct-call meter stamps `meiva_usage`
    # before a caller that also accounts for the call gets to log it.
    # meiva:link base/lm/usage/README.md
    if not _accountable(msg):
        return None
    usage_metadata = msg.usage_metadata
    if not usage_metadata:
        return None
    try:
        in_total, out_total, cache_read = tally_tokens([msg])
    except KeyError:
        # meiva:note
        # Preserve main-conversation observability for a malformed provider
        # usage payload, while withholding its incomplete billing span.
        in_total = usage_metadata.get("input_tokens", 0) or 0
        out_total = usage_metadata.get("output_tokens", 0) or 0
        cache_read = (usage_metadata.get("input_token_details") or {}).get("cache_read", 0) or 0
    if in_total is None or out_total is None or cache_read is None:
        return None
    cache_read = _metered_cache_read(usage_metadata, cache_read)
    from base.lm.reasoning import extract_reasoning_tokens

    reasoning = extract_reasoning_tokens(msg.usage_metadata, content=msg.content)  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    write_5m, write_1h, write_30m = cache_write_tokens(
        usage_metadata.get("input_token_details") or {},
        default_ttl=_default_cache_write_ttl(model, catalog=catalog),
    )
    record: dict[str, object] = {}
    complete = "input_tokens" in usage_metadata and "output_tokens" in usage_metadata
    logged = _log_usage(
        _UsageEvent(
            model=usage_model(msg, model, catalog=catalog),
            in_total=in_total,
            out_total=out_total,
            cache_read=cache_read,
            cache_write_5m=write_5m,
            cache_write_1h=write_1h,
            cache_write_30m=write_30m,
            reasoning=reasoning,
            latency_ms=options.latency_ms,
            decode_ms=options.decode_ms,
            priced_at=options.priced_at,
            usage_kind=usage_kind,
            source=options.source,
            for_agent_id=options.for_agent_id,
            attribution=options.attribution,
            emit_billing=complete,
            requested_model=model,
            call_id=options.call_id,
            provider_request_id=_provider_request_id(msg),
            complete=complete,
            catalog=catalog,
            now=now,
        ),
        record_out=record,
    )
    if options.stamp_message:
        message_addl_kwargs(msg)["meiva_usage"] = record
    return logged


def _provider_request_id(msg: AIMessage) -> str | None:
    # meiva:link base/lm/usage/README.md
    for field in ("request_id", "id"):
        receipt = msg.response_metadata.get(field)
        if isinstance(receipt, str) and receipt:
            return receipt
    return None


def log_usage_fields(
    model: str,
    fields: UsageFields,
    *,
    usage_kind: str,
    catalog: ModelCatalog,
    now: datetime,
    submit_receipt: UsageReceiptSubmitter | None = None,
) -> tuple[int, float]:
    # meiva:note
    # Log raw provider usage; absent cache metering stays absent, including on decisions.
    return _log_usage(
        _UsageEvent(
            model=model,
            in_total=fields.tok_in,
            out_total=fields.tok_out,
            cache_read=fields.tok_cached,
            cache_write_5m=fields.cache_write_5m,
            cache_write_1h=fields.cache_write_1h,
            cache_write_30m=fields.cache_write_30m,
            reasoning=fields.tok_reasoning,
            latency_ms=fields.latency_ms,
            usage_kind=usage_kind,
            source=fields.source,
            for_agent_id=fields.for_agent_id,
            calls=fields.calls,
            complete=fields.complete,
            call_id=fields.call_id,
            parent_call_id=fields.parent_call_id,
            unpriced_already_reported=fields.unpriced_already_reported,
            catalog=catalog,
            now=now,
        ),
        submit_receipt=submit_receipt,
    )


def _accountable(msg: AIMessage) -> bool:
    return isinstance(msg, AIMessage) and message_addl_kwargs(msg).get("meiva_usage") is None


def _attribution(for_agent_id: int | None) -> str:
    if for_agent_id is not None:
        return "caller"
    return "process" if ambient_agent_id() is not None else "unattributed"


def _price_snapshot(priced: CostQuote) -> dict[str, float]:
    return {
        "cost_usd": priced.cost_usd,
        "price_miss": priced.rates.cache_miss,
        "price_hit": priced.rates.cache_hit,
        "price_out": priced.rates.output,
        **{
            key: value
            for key, value in (
                ("price_write_5m", priced.rates.cache_write_5m),
                ("price_write_1h", priced.rates.cache_write_1h),
                ("price_write_30m", priced.rates.cache_write_30m),
            )
            if value is not None
        },
    }


def _cache_usage(
    cache_read: int | None, in_total: int, write_5m: int, write_1h: int, write_30m: int
) -> tuple[dict[str, int], str]:
    # meiva:note
    # Only measured cache counts enter events or their human-readable usage line.
    fields: dict[str, int] = {}
    text = ""
    if cache_read is not None:
        fields["cache_read"] = cache_read
        text = f" cached={cache_read}"
        if in_total:
            text += f" ({cache_read / in_total * 100:.0f}%)"
    if cache_read is not None or write_5m or write_1h or write_30m:
        fields.update(cache_write_5m=write_5m, cache_write_1h=write_1h)
    if write_30m:
        fields["cache_write_30m"] = write_30m
    return fields, text


@dataclass(frozen=True, slots=True)
class _UsageEvent:
    model: str
    in_total: int
    out_total: int
    cache_read: int | None
    reasoning: int
    latency_ms: float | None
    usage_kind: str
    catalog: ModelCatalog
    now: datetime
    decode_ms: float | None = None
    priced_at: datetime | None = None
    source: str | None = None
    for_agent_id: int | None = None
    emit_billing: bool = True
    requested_model: str | None = None
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_30m: int = 0
    attribution: str | None = None
    calls: int = 1
    complete: bool = True
    call_id: str | None = None
    provider_request_id: str | None = None
    parent_call_id: str | None = None
    unpriced_already_reported: bool = False


def _log_usage(
    event: _UsageEvent,
    *,
    record_out: dict[str, object] | None = None,
    submit_receipt: UsageReceiptSubmitter | None = None,
) -> tuple[int, float]:
    # meiva:note
    # Emit one priced or explicitly unpriced usage event and billing span.
    cache_fields, cache_text = _cache_usage(
        event.cache_read,
        event.in_total,
        event.cache_write_5m,
        event.cache_write_1h,
        event.cache_write_30m,
    )
    reason_pct = f" ({event.reasoning / event.out_total * 100:.0f}%)" if event.out_total else ""
    priced = quote(
        event.model,
        event.in_total,
        event.out_total,
        CacheTokens(
            read=0 if event.cache_read is None else event.cache_read,
            write_5m=event.cache_write_5m,
            write_1h=event.cache_write_1h,
            write_30m=event.cache_write_30m,
        ),
        at=event.priced_at or _utc_usage_time(event.now),
        prices=event.catalog.prices,
    )
    # meiva:note
    # The one figure set both the event and (via `record_out`) the message carry.
    record: dict[str, object] = {
        "model": event.model,
        "in_total": event.in_total,
        "out_total": event.out_total,
        **cache_fields,
        "reasoning": event.reasoning,
        **_usage_snapshot(event, priced),
        "attribution": event.attribution or _attribution(event.for_agent_id),
    }
    vendor = vendor_of_model(event.model, catalog=event.catalog)
    try:
        emit_observation = _book_usage(event, priced, vendor, record, submit_receipt)
    except UsageLedgerConflictError:
        raise
    except UsageLedgerWriteError:
        _emit_usage_observation(event, priced, vendor, record, cache_text, reason_pct)
        raise
    if record_out is not None:
        record_out.update(record)
    if emit_observation:
        _emit_usage_observation(event, priced, vendor, record, cache_text, reason_pct)
    return event.in_total + event.out_total, priced.cost_usd if priced is not None else 0.0


def _usage_snapshot(event: _UsageEvent, priced: CostQuote | None) -> dict[str, float]:
    if priced is not None:
        return _price_snapshot(priced)
    if not event.unpriced_already_reported:
        cause, missing = _unpriced_cause(event)
        logger.warning(
            "[llm usage] model {model!r} is unpriced ({cause}{missing_text}); declare the rate in "
            "base/lm/pricing/pricing_catalog_archive.json and the provider plugin price registry, and the "
            "ledger reprices this call at its own time on the next maintenance pass",
            event="llm_usage_unpriced",
            model=event.model,
            cause=cause,
            missing_classes=missing,
            missing_text=f": {missing}" if missing else "",
        )
    return {"unpriced": 1}


def _unpriced_cause(event: _UsageEvent) -> tuple[str, str]:
    rates = rates_at(
        event.model,
        event.priced_at or _utc_usage_time(event.now),
        event.in_total,
        prices=event.catalog.prices,
    )
    if rates is None:
        return "no_price", ""
    missing = ",".join(
        name
        for name, count, rate in (
            ("5m", event.cache_write_5m, rates.cache_write_5m),
            ("1h", event.cache_write_1h, rates.cache_write_1h),
            ("30m", event.cache_write_30m, rates.cache_write_30m),
        )
        if count and rate is None
    )
    return "no_cache_write_rate", missing


def _book_usage(
    event: _UsageEvent,
    priced: CostQuote | None,
    vendor: str | None,
    record: dict[str, object],
    submit_receipt: UsageReceiptSubmitter | None,
) -> bool:
    resolve_ledger = event.catalog.usage_ledger
    ledger = resolve_ledger() if resolve_ledger is not None else None
    if ledger is None:
        if not event.complete:
            record["complete"] = False
        return True
    call_id = event.call_id
    if call_id is None and event.provider_request_id is not None:
        call_id = f"api:provider:{vendor or event.model}:{event.provider_request_id}"
    if call_id is None:
        call_id = ledger.new_call_id()
    record["call_id"] = call_id
    record["occurred_at"] = _utc_usage_time(event.now).isoformat()
    if event.provider_request_id is not None:
        record["provider_request_id"] = event.provider_request_id
    if priced is not None and priced.exact_usd is not None:
        record["cost_usd_exact"] = str(priced.exact_usd)
    record["complete"] = event.complete
    entry = _ledger_entry(ledger, call_id, event, priced, vendor)
    if submit_receipt is None:
        return ledger.write(entry)
    submit_receipt(entry)
    return True


def _emit_usage_observation(
    event: _UsageEvent,
    priced: CostQuote | None,
    vendor: str | None,
    record: dict[str, object],
    cache_text: str,
    reason_pct: str,
) -> None:
    _emit_usage_span(event, priced, vendor)
    usage_logger = (
        logger.bind(agent_id=event.for_agent_id) if event.for_agent_id is not None else logger
    )
    usage_logger.info(
        "[llm usage] in={in_total}" + cache_text + "  "
        "out={out_total} reason={reasoning}{reason_pct}",
        event="llm_usage",
        calls=event.calls,
        reason_pct=reason_pct,
        latency_ms=event.latency_ms,
        decode_ms=event.decode_ms,
        usage_kind=event.usage_kind,
        **_optional_fields(event),
        **record,
    )


def _emit_usage_span(event: _UsageEvent, priced: CostQuote | None, vendor: str | None) -> None:
    if vendor is not None and event.emit_billing:
        emit_billing_event(
            BillingSpan(
                vendor=vendor,
                model=event.model,
                tok_in=event.in_total,
                tok_out=event.out_total,
                tok_cached=event.cache_read,
                cache_write_5m=event.cache_write_5m,
                cache_write_1h=event.cache_write_1h,
                cache_write_30m=event.cache_write_30m,
                cost_usd=priced.cost_usd if priced is not None else 0.0,
                usage_kind=event.usage_kind,
                unpriced=priced is None,
                start_time_ns=(_now_ns(event.now) - int(event.latency_ms * 1_000_000))
                if event.latency_ms is not None
                else None,
            ),
            now=event.now,
        )


def _utc_usage_time(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _ledger_entry(
    ledger: UsageLedger,
    call_id: str,
    event: _UsageEvent,
    priced: CostQuote | None,
    vendor: str | None,
) -> UsageLedgerEntry:
    price_snapshot: dict[str, float | str] = {}
    if priced is not None:
        price_snapshot.update(_price_snapshot(priced))
        del price_snapshot["cost_usd"]
        price_snapshot["priced_at"] = (event.priced_at or _utc_usage_time(event.now)).isoformat()
        provenance = event.catalog.prices.provenance(event.model)
        if provenance is not None:
            price_snapshot["source_url"], price_snapshot["source_checked_at"] = provenance
    return UsageLedgerEntry(
        call_id=call_id,
        occurred_at=_utc_usage_time(event.now),
        agent_id=event.for_agent_id if event.for_agent_id is not None else ambient_agent_id(),
        machine=ledger.machine(),
        cluster=ledger.cluster,
        source=event.source,
        billing=harness_billing_of_source(event.source).value,
        usage_kind=event.usage_kind,
        model=event.model,
        requested_model=event.requested_model,
        provider=vendor,
        provider_request_id=event.provider_request_id,
        calls=event.calls,
        complete=event.complete,
        tokens_in=event.in_total,
        tokens_out=event.out_total,
        tokens_reasoning=event.reasoning,
        cache_read=event.cache_read,
        cache_write_5m=event.cache_write_5m or None,
        cache_write_1h=event.cache_write_1h or None,
        cache_write_30m=event.cache_write_30m or None,
        cost_usd=priced.exact_usd if priced is not None else None,
        price_snapshot=price_snapshot,
        price_source="live" if priced is not None else None,
    )


# meiva:note
# The `llm_usage` fields a call carries only when it has them: the model it asked for, the source that stands for a
# shared text caller or an external harness (with the transport provenance), and the call a sub-agent's spend
# belongs to.
def _optional_fields(event: _UsageEvent) -> dict[str, object]:
    fields: dict[str, object] = {}
    if event.requested_model is not None:
        fields["requested_model"] = event.requested_model
    if event.source is not None:
        fields.update(source=event.source, transport_source="system")
    if event.parent_call_id is not None:
        fields["parent_call_id"] = event.parent_call_id
    return fields


MEIVA_BILLING_ATTR_LINE = "meiva.billing.line"
MEIVA_BILLING_ATTR_VENDOR = "meiva.billing.vendor"
MEIVA_BILLING_ATTR_MODEL = "meiva.billing.model"
MEIVA_BILLING_ATTR_TOKENS_IN = "meiva.billing.tokens_in"
MEIVA_BILLING_ATTR_TOKENS_OUT = "meiva.billing.tokens_out"
MEIVA_BILLING_ATTR_COST = "meiva.billing.cost"
MEIVA_BILLING_ATTR_USAGE_KIND = "meiva.billing.usage_kind"
MEIVA_BILLING_ATTR_TS = "meiva.billing.ts"
MEIVA_BILLING_ATTR_CACHE_READ_TOKENS = "meiva.billing.cache_read_tokens"
MEIVA_BILLING_ATTR_CACHE_WRITE_5M_TOKENS = "meiva.billing.cache_write_5m_tokens"
MEIVA_BILLING_ATTR_CACHE_WRITE_1H_TOKENS = "meiva.billing.cache_write_1h_tokens"
MEIVA_BILLING_ATTR_CACHE_WRITE_30M_TOKENS = "meiva.billing.cache_write_30m_tokens"
MEIVA_BILLING_ATTR_UNPRICED = "meiva.billing.unpriced"

_CORE_VENDOR_PREFIXES: tuple[tuple[str, str], ...] = ()


def _is_qwen_family(model: str) -> bool:
    # meiva:note
    # Alibaba family ids nest directly after the stem (``qwen3.8-max``), so a bare ``qwen-`` prefix
    # would miss them. Require a digit after the stem (or an exact ``qwen`` id) so a non-Alibaba
    # ``qwenfoo-*`` id cannot be mis-attributed; plugin registration already rejects such prefixes,
    # this is a second guard.
    return model == "qwen" or (model.startswith("qwen") and len(model) > 4 and model[4].isdigit())


def vendor_of_model(model: str, *, catalog: ModelCatalog) -> str | None:
    # meiva:note
    # Return the registered manufacturer for ``model``, if one is known.
    for prefix, vendor in _CORE_VENDOR_PREFIXES:
        if model.startswith(prefix):
            return vendor
    if _is_qwen_family(model):
        return "alibaba"

    for prefix, binding in catalog.bindings.items():
        if model.startswith(prefix):
            return binding.display_name.lower()
    for prefix, provider in catalog.decision_providers.items():
        if model.startswith(prefix):
            return provider.display_name.lower()
    return None


# meiva:note
# Convert the caller's instant to epoch nanoseconds without float64 rounding drift on the
# sub-second part; a naive datetime is UTC by contract.
def _now_ns(now: datetime) -> int:
    utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return int(utc.timestamp()) * 1_000_000_000 + utc.microsecond * 1_000


def _utc_timestamp(now: datetime) -> str:
    return (
        datetime.fromtimestamp(_now_ns(now) // 1_000_000_000, UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class BillingSpan:
    vendor: str
    model: str
    tok_in: int
    tok_out: int
    cost_usd: float
    usage_kind: str
    line: str = "meiva"
    tok_cached: int | None = None
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_30m: int = 0
    unpriced: bool = False
    start_time_ns: int | None = None


def emit_billing_event(span_data: BillingSpan, *, now: datetime) -> None:
    # meiva:note
    # End one trace-safe billing span for a completed provider call.
    #
    # Billing must never affect the call it observes, including while tracing is initializing or an
    # exporter is unavailable.
    try:
        from base.telemetry.otlp.trace_readiness import PROCESS_RECORDING, TRACE_INIT_WAIT_S

        reading = PROCESS_RECORDING.read(timeout=TRACE_INIT_WAIT_S)
        if reading.first_timeout:
            logger.warning(
                "trace init not resolved within timeout — proceeding without recording",
                event="trace",
                action="init_resolved_timeout",
                timeout_s=TRACE_INIT_WAIT_S,
            )
        if not reading.ready:
            return

        from opentelemetry import trace as otel_trace

        span = otel_trace.get_tracer("meiva.billing").start_span(
            "meiva.billing.call",
            start_time=span_data.start_time_ns,
        )
        span.set_attribute(MEIVA_BILLING_ATTR_LINE, span_data.line)
        span.set_attribute(MEIVA_BILLING_ATTR_VENDOR, span_data.vendor)
        span.set_attribute(MEIVA_BILLING_ATTR_MODEL, span_data.model)
        span.set_attribute(MEIVA_BILLING_ATTR_TOKENS_IN, int(span_data.tok_in))
        span.set_attribute(MEIVA_BILLING_ATTR_TOKENS_OUT, int(span_data.tok_out))
        span.set_attribute(MEIVA_BILLING_ATTR_COST, round(float(span_data.cost_usd), 6))
        span.set_attribute(MEIVA_BILLING_ATTR_USAGE_KIND, span_data.usage_kind)
        span.set_attribute(MEIVA_BILLING_ATTR_TS, _utc_timestamp(now))
        if span_data.tok_cached is not None:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_READ_TOKENS, int(span_data.tok_cached))
        if span_data.cache_write_5m:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_WRITE_5M_TOKENS, span_data.cache_write_5m)
        if span_data.cache_write_1h:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_WRITE_1H_TOKENS, span_data.cache_write_1h)
        if span_data.cache_write_30m:
            span.set_attribute(MEIVA_BILLING_ATTR_CACHE_WRITE_30M_TOKENS, span_data.cache_write_30m)
        if span_data.unpriced:
            span.set_attribute(MEIVA_BILLING_ATTR_UNPRICED, span_data.unpriced)
        span.end()
    except Exception as exc:
        report_sink_failure("billing span emission (the span is dropped)", exc)


def emit_billing_from_message(
    msg: AIMessage,
    *,
    model: str,
    usage_kind: str,
    start_time_ns: int | None = None,
    catalog: ModelCatalog,
    now: datetime,
) -> None:
    # meiva:note
    # Price a LangChain message and emit its billing span when usage is known.
    try:
        usage_metadata = msg.usage_metadata
        if not usage_metadata:
            return
        tok_in, tok_out, tok_cached = tally_tokens([msg])
        if tok_in is None or tok_out is None or tok_cached is None:
            return
        tok_cached = _metered_cache_read(usage_metadata, tok_cached)
        model = usage_model(msg, model, catalog=catalog)
        resolved_vendor = vendor_of_model(model, catalog=catalog)
        if resolved_vendor is None:
            return
        write_5m, write_1h, write_30m = cache_write_tokens(
            usage_metadata.get("input_token_details") or {},
            default_ttl=_default_cache_write_ttl(model, catalog=catalog),
        )
        priced = quote(
            model,
            tok_in,
            tok_out,
            CacheTokens(
                read=0 if tok_cached is None else tok_cached,
                write_5m=write_5m,
                write_1h=write_1h,
                write_30m=write_30m,
            ),
            prices=catalog.prices,
        )
        emit_billing_event(
            BillingSpan(
                vendor=resolved_vendor,
                model=model,
                tok_in=tok_in,
                tok_out=tok_out,
                tok_cached=tok_cached,
                cache_write_5m=write_5m,
                cache_write_1h=write_1h,
                cache_write_30m=write_30m,
                cost_usd=priced.cost_usd if priced is not None else 0.0,
                usage_kind=usage_kind,
                unpriced=priced is None,
                start_time_ns=start_time_ns,
            ),
            now=now,
        )
    except Exception as exc:
        report_sink_failure("pricing a provider message for billing (its span is dropped)", exc)
