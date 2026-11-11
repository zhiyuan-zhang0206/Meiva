# meiva:note
# Fetch one page and have a model answer a prompt about it: read the page through the
# configured reader and answer from a framed copy of it.

from __future__ import annotations

from typing import assert_never

from base.host.net.resilience import Policy
from base.lm import factory
from base.lm.call import answer_text
from base.lm.effort import ReasoningEffort
from meiva.web import deepseek, transport, wire
from meiva.web.deps import FetchDeps, ReaderProvider
from meiva.web.errors import FetchError
from meiva.web.policy import WEB_READ_POLICY

__all__ = ["fetch_one"]


# meiva:note
# Read the page at `url` as the configured reader returns it, cut to `max_chars`.
def read_page(
    url: str,
    max_chars: int,
    deps: FetchDeps,
    pacer: transport.RateLimiter,
    *,
    policy: Policy = WEB_READ_POLICY,
) -> wire.Page:
    match deps.reader:
        case ReaderProvider.DEEPSEEK:
            return deepseek.read_page(url, max_chars, deps, pacer, policy=policy)
        case ReaderProvider.JINA:
            data = transport.send_json(
                wire.jina_request(deps.jina, url),
                timeout_s=deps.jina.timeout_s,
                label="Jina Reader",
                error_type=FetchError,
                provider_json=False,
                pacer=None,
                rng=deps.rng,
                policy=policy,
            )
            return wire.jina_page(data, url, max_chars)
        case _:
            assert_never(deps.reader)


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
        now=fm.now,
        rng=deps.rng,
    )


# meiva:note
# Read one page and answer one prompt, the synchronous unit of work the batch schedules in a
# thread.
def fetch_one(
    url: str,
    prompt: str,
    max_chars: int,
    effort: str | ReasoningEffort | None,
    deps: FetchDeps,
    pacer: transport.RateLimiter,
    *,
    policy: Policy = WEB_READ_POLICY,
) -> str:
    page = read_page(url, max_chars, deps, pacer, policy=policy)
    return _answer(prompt, page, effort, deps)
