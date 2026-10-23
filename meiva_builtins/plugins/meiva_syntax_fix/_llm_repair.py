# meiva:note
# Step 8 of the syntax-fix pipeline: a fast reasoning model rewrites source that still fails
# compile() after every deterministic step. Each attempt is compiled and, if still broken, its new
# error is fed back for another round. The model is built from the values the hook hands in.
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from loguru import logger

from base.host.env.agent_slices import ModelOverrides
from base.lm.catalog import ModelCatalog

from ._repair_text import (
    REPAIR_MAX_ATTEMPTS,
    REPAIR_SYSTEM,
    extract_text,
    render_syntax_error,
    repair_request,
    retry_request,
    strip_code_fence,
)

_REPAIR_MODEL = "deepseek-flash"
_REPAIR_TIMEOUT = 60.0


@dataclass(frozen=True)
class RepairInputs:
    catalog: ModelCatalog
    overrides: ModelOverrides
    llm_override: str


# meiva:note
# One LLM repair attempt: invoke with a timeout, then flatten and strip the reply. The candidate
# source, or None when the model is unavailable (missing key, timeout, empty output).
async def repair_once(llm: Any, messages: list[Any], *, catalog: ModelCatalog) -> str | None:
    started = time.monotonic()
    try:
        resp = await asyncio.wait_for(llm.ainvoke(messages), timeout=_REPAIR_TIMEOUT)
    except (
        Exception
    ) as exc:  # best-effort: any failure degrades to surfacing the error to the agent
        logger.warning(
            "[syntax_fix] llm_repair unavailable: model={model} elapsed={elapsed:.1f}s err={err}",
            event="syntax_fix",
            model=_REPAIR_MODEL,
            elapsed=time.monotonic() - started,
            err=f"{type(exc).__name__}: {exc}",
        )
        return None
    from base.lm.usage import emit_billing_from_message

    emit_billing_from_message(
        resp,
        model=_REPAIR_MODEL,
        catalog=catalog,
        usage_kind="chat",
        start_time_ns=time.time_ns() - int((time.monotonic() - started) * 1_000_000_000),
    )
    return strip_code_fence(extract_text(resp.content)).strip() or None


# meiva:note
# Fast-model repair for a syntax error the deterministic pipeline could not fix. Single-shot output
# is probabilistic, so each attempt's result is compiled and, if still broken, the new error is fed
# back for up to REPAIR_MAX_ATTEMPTS rounds. Returns source that compiles, or None when every
# attempt fails or the model is unavailable (missing key, timeout, empty output); the caller then
# surfaces the error to the agent.
async def llm_repair_syntax(code: str, rendered_error: str, repair: RepairInputs) -> str | None:
    from base.lm.factory import build_chat_model

    try:
        llm = build_chat_model(
            _REPAIR_MODEL,
            catalog=repair.catalog,
            llm_override=repair.llm_override,
            overrides=repair.overrides,
        )
    except Exception as exc:
        logger.warning(
            "[syntax_fix] llm_repair unavailable: {err}",
            event="syntax_fix",
            err=f"{type(exc).__name__}: {exc}",
        )
        return None

    messages: list[Any] = [
        SystemMessage(content=REPAIR_SYSTEM),
        HumanMessage(content=repair_request(code, rendered_error)),
    ]
    for attempt in range(1, REPAIR_MAX_ATTEMPTS + 1):
        logger.debug(
            "[syntax_fix] llm_repair attempt {attempt}/{max_attempts}: model={model} code_lines={lines}",
            event="syntax_fix",
            attempt=attempt,
            max_attempts=REPAIR_MAX_ATTEMPTS,
            model=_REPAIR_MODEL,
            lines=len(code.splitlines()),
        )
        candidate = await repair_once(llm, messages, catalog=repair.catalog)
        if candidate is None:
            return None
        try:
            compile(candidate, "<agent_code>", "exec")
        except SyntaxError as retry_err:
            messages.append(AIMessage(content=candidate))
            messages.append(
                HumanMessage(content=retry_request(render_syntax_error(retry_err, candidate)))
            )
            continue
        return candidate
    return None
