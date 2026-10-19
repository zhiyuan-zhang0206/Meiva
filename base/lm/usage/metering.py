# meiva:note
# Every provider call a directly built chat model makes writes one llm_usage row, priced by
# the same `log_usage_from_message` the turn loop and the shared request helpers use. A
# worker that built a model and called it directly used to spend money with no row at all
# (task #5327). Attribution is the process identity bound at telemetry startup; a process
# without one records `unattributed` and a NULL agent id rather than guessing an owner.
# meiva:link base/lm/usage/docs/usage.meiva.okf.md

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler, BaseCallbackManager, Callbacks
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import LLMResult

from base.lm.catalog import ModelCatalog
from base.lm.usage import log_usage_from_message
from base.log import logger
from base.telemetry import ambient_agent_id, report_sink_failure

__all__ = ["DIRECT_USAGE_KIND", "LlmUsageMeter", "install_usage_meter", "unmeter"]

DIRECT_USAGE_KIND = "direct"

_FAILURE_REPORT_EVERY = 50

UsageEmitter = Callable[..., Any]


class LlmUsageMeter(BaseCallbackHandler):
    raise_error = False

    def __init__(
        self,
        *,
        model: str,
        catalog: ModelCatalog,
        emit: UsageEmitter = log_usage_from_message,
    ) -> None:
        super().__init__()
        self._model = model
        self._catalog = catalog
        self._emit = emit
        self._started: dict[UUID, float] = {}
        self._lock = threading.Lock()
        self._failures = 0

    def on_chat_model_start(
        self,
        serialized: Any,  # noqa: ARG002
        messages: Any,  # noqa: ARG002
        *,
        run_id: UUID,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        # meiva:note
        # The call start, kept per run id so `on_llm_end` reports `latency_ms` for the one
        # provider call that ended.
        # meiva:link base/lm/usage/docs/usage.meiva.okf.md
        with self._lock:
            self._started[run_id] = time.monotonic()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        **kwargs: Any,  # noqa: ARG002
    ) -> None:
        # meiva:note
        # One completed call can return several generations (batch / generate); each one that
        # carries provider usage becomes its own row, and a message the meter stamps cannot be
        # logged again by its caller.
        # meiva:link base/lm/usage/docs/usage.meiva.okf.md
        with self._lock:
            started = self._started.pop(run_id, None)
        latency_ms = None if started is None else (time.monotonic() - started) * 1_000.0
        for generations in response.generations:
            for generation in generations:
                message: AIMessage | None = getattr(generation, "message", None)
                if message is not None:
                    self._record(message, latency_ms)

    def _record(self, message: AIMessage, latency_ms: float | None) -> None:
        agent_id = ambient_agent_id()
        try:
            self._emit(
                message,
                self._model,
                catalog=self._catalog,
                latency_ms=latency_ms,
                usage_kind=DIRECT_USAGE_KIND,
                for_agent_id=agent_id,
                attribution="process" if agent_id is not None else "unattributed",
                stamp_message=True,
            )
        except Exception as exc:
            self._report_failure(exc)

    def _report_failure(self, exc: BaseException) -> None:
        self._failures += 1
        report_sink_failure("direct model call accounting (the llm_usage row is dropped)", exc)
        if self._failures == 1 or self._failures % _FAILURE_REPORT_EVERY == 0:
            logger.warning(
                "direct model call accounting failed ({failures} time(s) in this process; "
                "the first and every {every}th are reported): {err}",
                event="llm_usage_metering_failed",
                model=self._model,
                error_type=type(exc).__name__,
                err=f"{type(exc).__name__}: {exc}",
                failures=self._failures,
                every=_FAILURE_REPORT_EVERY,
            )


def install_usage_meter(
    model: BaseChatModel,
    *,
    model_name: str,
    catalog: ModelCatalog,
    emit: UsageEmitter = log_usage_from_message,
) -> BaseChatModel:
    if _meter_of(model) is not None:
        return model
    meter = LlmUsageMeter(model=model_name, catalog=catalog, emit=emit)
    callbacks: Callbacks = model.callbacks
    if callbacks is None:
        model.callbacks = [meter]
    elif isinstance(callbacks, list):
        model.callbacks = [*callbacks, meter]
    else:
        callbacks.add_handler(meter, inherit=True)
    return model


def unmeter(model: BaseChatModel) -> BaseChatModel:
    callbacks: Callbacks | None = getattr(model, "callbacks", None)
    if isinstance(callbacks, list):
        kept = [handler for handler in callbacks if not isinstance(handler, LlmUsageMeter)]
        if len(kept) != len(callbacks):
            model.callbacks = kept
    elif isinstance(callbacks, BaseCallbackManager):
        for handler in list(callbacks.handlers):
            if isinstance(handler, LlmUsageMeter):
                callbacks.remove_handler(handler)
    return model


def _meter_of(model: BaseChatModel) -> LlmUsageMeter | None:
    callbacks: Callbacks | None = getattr(model, "callbacks", None)
    if isinstance(callbacks, list):
        return next((h for h in callbacks if isinstance(h, LlmUsageMeter)), None)
    if isinstance(callbacks, BaseCallbackManager):
        return next((h for h in callbacks.handlers if isinstance(h, LlmUsageMeter)), None)
    return None
