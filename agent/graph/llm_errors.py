from __future__ import annotations

from base.host.env.agent_slices import AgentSlices, LlmCallPolicy
from base.lm.catalog import ModelCatalog
from base.lm.errors import ErrorClass, classify_error, emit_provider_error


class LLMStreamError(Exception):
    """Common parent class for all stream-layer aborts — fail-fast errors that
    `_llm_node_impl` raises before final_msg enters state.messages inherit from here.

    `llm_node`'s except block catches BaseException to write turn_end ok=False +
    traceback + publish Error event so the frontend sees it; SQL queries for
    one class of ops can also use the `exception_type` payload key with `LIKE 'LLMStream%'`
    or distinguish by subclass with more granularity.
    """


class FatalLLMStreamError(LLMStreamError):
    """An LLM failure that must leave the node without retrying."""


# meiva:note
# LLM stream inter-chunk timeout — no next chunk within the per-stage timeout
# (TTFT for the first chunk, inter-chunk afterwards); the turn aborts instead of
# hanging on a silent server. It is fatal like every other stream-layer abort,
# and that is load-bearing: the hosted runner routes `FatalLLMStreamError` into
# the settled turn abort (`halted=True` plus the user-visible Error event), while
# a plain `LLMStreamError` escapes into the generic crashed-turn path that only
# logs — the row settles to idling with no message and no error the user can see
# (agent 6933, 2026-10-18: the third gemini-4-argon stream produced no first
# chunk within the 30s TTFT floor and the agent looked silently idle). `stage`
# names WHAT bound expired — "ttft" (no first chunk), "mid-stream" (gap after at
# least one chunk) or "total" (the per-attempt duration ceiling); it rides the
# exception for diagnostics without parsing the message.
# meiva:link agent/graph/llm/docs/failures-and-retry.meiva.okf.md
class LLMStreamStallTimeoutError(FatalLLMStreamError):
    def __init__(self, message: str, *, stage: str = "unknown") -> None:
        super().__init__(message)
        self.stage = stage


class LLMRetryBudgetExceededError(FatalLLMStreamError):
    """The LLM node's configurable retry wall-clock budget expired.

    This is excluded from the retry policy, so it reaches the normal turn
    failure path rather than beginning another provider invocation.
    """


class FatalProviderError(Exception):
    """Provider permanently rejected the request — a `base.lm.errors.classify_error`
    `PERMANENT` class (HTTP 400 bad request / context length / schema, 401 auth,
    402 billing, 403 forbidden, 404 unknown model, 422 schema) or a configured
    fatal error type (e.g. `engine_overloaded_error`). Retrying within the turn
    cannot succeed, so — like `FatalLLMStreamError` — it is excluded from the
    retry policy and routed to the agent host's idle path. Background wakes
    stop; after correcting the cause, fresh chat or an explicit control can
    request another attempt.

    Carries the structured `(error_class, provider, status)` from the classifier
    so the abort log + Error event are queryable without re-parsing the message.

    Deliberately NOT in the `LLMStreamError` hierarchy: those are mid-stream
    protocol aborts, whereas this is the provider refusing the request outright
    before any stream. So it is not matched by
    the `exception_type` payload key with `LIKE 'LLMStream%'` ops queries.
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: str | None = None,
        provider: str | None = None,
        status: int | None = None,
        context_overflow: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.provider = provider
        self.status = status
        # True when the rejection was the context window being exceeded (the
        # classifier's `context_overflow` predicate). The one permanent
        # failure requiring an explicit compaction; preserve the classification
        # for diagnostics and the suggested operator action.
        self.context_overflow = context_overflow


# Provider error types that are deterministic within a turn — retrying cannot flip
# them. Defaults to {"engine_overloaded_error"} (Kimi K3 overload). Configurable
# via settings.lm.llm_fatal_provider_error_types. An empty set disables the check.


def parse_provider_error_type(exc: BaseException) -> str | None:
    """Read a provider type only through the shared trusted-error classifier."""
    return classify_error(exc).error_type


def is_fatal_provider_error_type(exc: BaseException, llm_policy: LlmCallPolicy) -> bool:
    """Check whether ``exc`` carries a provider error type configured as fatal.

    Returns ``True`` when ``parse_provider_error_type`` extracts a type
    that is in the configured ``llm_fatal_provider_error_types``
    (comma-separated string parsed into a set). On an empty config string
    the check is a fast no-op.
    """
    fatal_csv = llm_policy.llm_fatal_provider_error_types
    if not fatal_csv:
        return False
    fatal_types = {t.strip() for t in fatal_csv.split(",") if t.strip()}
    error_type = parse_provider_error_type(exc)
    return error_type is not None and error_type in fatal_types


def classify_and_log_provider_error(
    exc: Exception, agent: AgentSlices, *, catalog: ModelCatalog
) -> FatalProviderError | None:
    """Classify a provider exception, emit the structured postmortem log, and
    return a `FatalProviderError` to raise when the turn must fail fast — else None
    so the caller re-raises the original for the node's explicit retry predicate.

    Two fail-fast triggers fold together here (see `FatalProviderError`): the
    `classify_error` `PERMANENT` class (400/401/402/403/404/422 — deterministic
    rejection) and a configured fatal error *type* (`llm_fatal_provider_error_types`,
    e.g. Kimi K3's transient-but-in-turn-futile `engine_overloaded_error`). The
    structured log fires for every class, including UNKNOWN errors that propagate
    once, so the `error_class` / `provider` payload keys /
    `->>'status'` are queryable in a postmortem instead of scraping the message.

    `billing` / `vendor` / `model` ride the same log because an out-of-credit key
    is an ops event, not a postmortem detail: the Grafana rule
    `meiva-ops-llm-billing-quota` fires on the first `billing=True` row and its IM
    message has to name what stopped working. `vendor` is the model's provider
    key (deepseek / claude / …) — `classification.provider` is only the SDK
    package that raised, which DeepSeek and Claude share. The exception
    message names both, so the runloop's block / recovery notifications
    identify the same account the event does (task #3916).
    """
    classification = classify_error(exc)
    fatal_type_hit = is_fatal_provider_error_type(exc, agent.llm_policy)
    fatal = classification.error_class is ErrorClass.PERMANENT or fatal_type_hit
    context_overflow = classification.context_overflow
    model = agent.brain.llm_model
    emit_provider_error(
        exc, model=model, fatal=fatal, classification=classification, catalog=catalog
    )
    if not fatal:
        return None
    from base.lm.factory import provider_key_of_model

    vendor = provider_key_of_model(model, catalog=catalog)
    if classification.billing:
        reason = "provider rejected the request for billing: the key is out of credit or quota"
    elif classification.error_class is ErrorClass.PERMANENT:
        reason = "provider permanently rejected the request"
    else:
        reason = f"provider returned configured-fatal error type {classification.error_type!r}"
    return FatalProviderError(
        f"{reason} (HTTP {classification.status}, vendor={vendor}, "
        f"provider={classification.provider}): {exc}. "
        "Retry cannot succeed; aborting the turn — the agent stays alive and idles.",
        error_class=classification.error_class.value,
        provider=classification.provider,
        status=classification.status,
        context_overflow=context_overflow,
    )


# meiva:note
# Exhausted transient provider retries in `llm_node` become a `FatalLLMStreamError` at the
# hosted invocation boundary (`PendingTurnFailure.from_invocation_error`) so `llm_node`
# keeps re-raising the original transient exception while the host settles the turn with
# `halted=True` and structured `(error_class, provider, status)` diagnostics without opening
# the permanent heartbeat circuit breaker.
# meiva:link agent/graph/llm/docs/failures-and-retry.meiva.okf.md
class LLMRetryExhaustedError(FatalLLMStreamError):
    def __init__(
        self,
        message: str,
        *,
        error_class: str | None = None,
        provider: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.provider = provider
        self.status = status


# meiva:link agent/graph/llm/docs/failures-and-retry.meiva.okf.md
def retry_exhausted_error(exc: Exception) -> LLMRetryExhaustedError:
    classification = classify_error(exc)
    error = LLMRetryExhaustedError(
        f"LLM transient provider retries exhausted "
        f"(HTTP {classification.status}, provider={classification.provider}): {exc}",
        error_class=classification.error_class.value,
        provider=classification.provider,
        status=classification.status,
    )
    error.__cause__ = exc
    return error


# meiva:note
# LLM stream accumulated to `final_msg` with incomplete protocol fields (upstream drift):
# - terminal reason missing from `response_metadata` — validator cannot determine whether
#   the model stopped normally and must not fall through as idle.
# - terminal reason claims a tool call while `final_msg.tool_calls` is empty (bug 169).
# - `final_msg.usage_metadata` missing on an otherwise normal terminal frame (agent 113).
#
# Subclasses `FatalLLMStreamError` so hosted invocations settle the turn abort (`halted=True`
# plus the user-visible `Error` event) without re-streaming or crashing the turn.
# Thinking-block drift (#167/#168: `signature_delta` without `thinking_delta`) is repaired in
# place by `sanitize_thinking_blocks` rather than raised here.
# meiva:link agent/graph/llm/docs/failures-and-retry.meiva.okf.md
class LLMStreamCorruptedError(FatalLLMStreamError):
    pass


# meiva:note
# LLM stream finished with a non-normal terminal reason (truncation via subclass
# `LLMStreamTruncatedError`, safety/content_filter/recitation, or provider-specific
# `stop_sequence` / `pause_turn`). Subclasses `FatalLLMStreamError` so the turn settles once
# with `halted=True` and a user-visible `Error` event without automatic re-streaming, while
# exposing `stop_reason` and `output_tokens` directly as attributes.
# meiva:link agent/graph/llm/docs/failures-and-retry.meiva.okf.md
class LLMStreamUnexpectedStopReasonError(FatalLLMStreamError):
    def __init__(
        self,
        message: str,
        *,
        stop_reason: str,
        output_tokens: int,
    ) -> None:
        super().__init__(message)
        self.stop_reason = stop_reason
        self.output_tokens = output_tokens


class LLMStreamTruncatedError(LLMStreamUnexpectedStopReasonError):
    """LLM stream truncated by the server's output budget (anthropic
    `stop_reason='max_tokens'` / openai `finish_reason='length'` / gemini
    `finish_reason='MAX_TOKENS'`; the raw provider value is on `self.stop_reason`).

    Subclassed separately because max_tokens truncation has a deterministic
    client-side fix (bump `max_tokens`), unlike `refusal` / `pause_turn`
    which have no known retry path. SQL
    `WHERE exception_type = 'LLMStreamTruncatedError'` pulls all turns hit
    by client-side cap being too small at a glance.

    This invariant was formed by a max_tokens truncation incident (agent 169).
    """
