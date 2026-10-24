# meiva:note
# Fetch one page and have a model answer a prompt about it: read the page through the
# configured reader, judge its text for injection beside the answer, answer from a framed copy
# of it, and scan the answer.

from __future__ import annotations

from base.lm import factory
from base.lm.call import answer_text
from base.lm.effort import ReasoningEffort
from meiva import security
from meiva.sdk_surface.injection_check import begin_check
from meiva.sdk_surface.pure.media.injection_plan import finding_source
from meiva.web import deepseek, transport, wire
from meiva.web.deps import FetchDeps
from meiva.web.errors import FetchError

__all__ = ["fetch_one"]


# meiva:note
# Read the page at `url` as the configured reader returns it, cut to `max_chars`.
def read_page(url: str, max_chars: int, deps: FetchDeps, pacer: transport.RateLimiter) -> wire.Page:
    if deps.reader == "deepseek":
        return deepseek.read_page(url, max_chars, deps, pacer)
    data = transport.send_json(
        wire.jina_request(deps.jina, url),
        timeout_s=deps.jina.timeout_s,
        label="Jina Reader",
        error_type=FetchError,
        provider_json=False,
        pacer=None,
    )
    return wire.jina_page(data, url, max_chars)


# meiva:note
# Have the web-fetch model answer the prompt about the page. A model that cannot be built, a
# typed provider failure that exhausted its retries, or an empty answer raises `FetchError`;
# an unknown invocation error keeps its own type and is not retried.
def _answer(
    prompt: str,
    page: wire.Page,
    effort: str | ReasoningEffort | None,
    deps: FetchDeps,
) -> str:
    fm = deps.fetch_model
    request = wire.plan_answer(fm.model, fm.reasoning, page, prompt, effort)
    return answer_text(
        request.prompt,
        request.material,
        model=request.model,
        effort=request.effort,
        error_type=FetchError,
        desc=request.desc,
        build_error=lambda m, e: FetchError(f"Building model {m!r} for {request.url} failed: {e}"),
        retry_attempts=fm.retry_attempts,
        retry_delay_seconds=fm.retry_delay_seconds,
        retry_max_delay_seconds=fm.retry_max_delay_seconds,
        timeout=fm.timeout_seconds,
        usage_source="web.fetch",
        catalog=fm.catalog,
        keys=fm.keys,
        llm_override=fm.llm_override,
        overrides=fm.overrides,
        build_chat_model=factory.build_chat_model,
    )


# meiva:note
# Record the finding of the injection check, if its verdict reaches the threshold.
def _record_verdict(url: str, probability: float | None, deps: FetchDeps) -> None:
    source = finding_source(url, probability, deps.injection.threshold)
    if source is not None and probability is not None:
        security.deliver_model_finding(deps.delivery, source, probability)


# meiva:note
# Read one page and answer one prompt, the synchronous unit of work the batch schedules in a
# thread. The injection check judges the page text itself, which the scan of the summary never
# sees; it needs only the page, so it runs beside `_answer`, and its finding is kept even when
# the answer fails.
def fetch_one(
    url: str,
    prompt: str,
    max_chars: int,
    effort: str | ReasoningEffort | None,
    deps: FetchDeps,
    pacer: transport.RateLimiter,
) -> str:
    page = read_page(url, max_chars, deps, pacer)
    check = begin_check(url, page.title, page.content, inputs=deps.injection)
    try:
        answer = _answer(prompt, page, effort, deps)
    finally:
        _record_verdict(url, check.finish(), deps)
    return security.scan_to(deps.delivery, answer, "web.fetch")
