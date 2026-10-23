"""Bounded retries of trusted transient provider errors within one LLM node.

Stalls, protocol failures, permanent rejections and unknown exceptions end
this turn. The node never retries a graph or code execution.
"""

from __future__ import annotations

import contextlib
import random

from agent.graph.hooks.compact import CompactionFailedError
from agent.graph.llm_errors import (
    FatalLLMStreamError,
    FatalProviderError,
)
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.errors import is_retryable_provider_error
from base.lm.registry import resolve_setting

from ._backoff import WAIT_JITTER_SPAN_S, RetryPolicy, backoff_wait

RETRY_REMAINING_ATTR = "_meiva_retry_budget_remaining_seconds"
_NEVER_RETRIED = (FatalLLMStreamError, FatalProviderError, CompactionFailedError)


# meiva:note
# The retry bounds of one agent: its lm config, with the try cap resolved for its model.
def retry_policy(agent: AgentSlices, catalog: ModelCatalog, agent_id: int) -> RetryPolicy:
    return RetryPolicy(
        agent_id=agent_id,
        max_attempts=resolve_setting(
            "llm_retry_max_attempts",
            model=agent.brain.llm_model,
            models=catalog.models,
            explicit=agent.read("lm", "llm_retry_max_attempts"),
        ),
        max_total_seconds=agent.read("lm", "llm_retry_max_total_seconds"),
        initial_interval_seconds=agent.read("lm", "llm_retry_initial_interval_seconds"),
        max_interval_seconds=agent.read("lm", "llm_retry_max_interval_seconds"),
    )


def retryable_failure(exc: Exception) -> bool:
    if isinstance(exc, _NEVER_RETRIED):
        return False
    return is_retryable_provider_error(exc)


# meiva:note
# The budget a failed try left rides on its exception to the node's wait decision.
def record_remaining(exc: BaseException, seconds: float) -> None:
    with contextlib.suppress(AttributeError):
        setattr(exc, RETRY_REMAINING_ATTR, seconds)


def remaining_of(exc: BaseException) -> float | None:
    remaining = getattr(exc, RETRY_REMAINING_ATTR, None)
    return remaining if isinstance(remaining, float) else None


# meiva:note
# The one dice roll of the retry loop. Jitter only spreads out agents that failed together; it is not a secret.
def draw_jitter() -> float:
    return random.uniform(0, WAIT_JITTER_SPAN_S)  # noqa: S311


# meiva:note
# Seconds to sleep before the next try after the `failed`-th failed one (1 after the first failure); None ends the node.
def retry_wait(
    exc: Exception, failed: int, *, policy: RetryPolicy, jitter_seconds: float
) -> float | None:
    if not retryable_failure(exc):
        return None
    return backoff_wait(
        failed, policy, remaining_seconds=remaining_of(exc), jitter_seconds=jitter_seconds
    )
