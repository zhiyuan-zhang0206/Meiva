# meiva:note
# What the cluster RPC client decides, from values: how long and how often one dispatch tries,
# how long to wait between tries, what an HTTP status means, and which idempotency key a
# dispatch carries.
#
# `ops.cluster.rpc` owns the transport — the sockets, the credentials, the timers and the
# random draw for the jitter; this module does no I/O.

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "JITTER_HIGH",
    "JITTER_LOW",
    "NON_IDEMPOTENT_KINDS",
    "DispatchBudget",
    "ResponseVerdict",
    "RpcConfig",
    "business_idempotency_key",
    "connect_timeout_s",
    "dispatch_budget",
    "response_verdict",
    "retry_delay_s",
    "worst_case_dispatch_seconds",
]

_RETRY_BASE_DELAY_S = 0.5
_RETRY_MAX_DELAY_S = 4.0
_CONNECT_TIMEOUT_CAP_S = 10.0

# meiva:note
# The retry delay is its backoff scaled by a jitter factor drawn from this range. The factor is
# a value the transport draws once per retry, so the delay itself stays a pure function.
JITTER_LOW = 0.5
JITTER_HIGH = 1.5

# meiva:note
# Operations whose repeat would do the work twice carry an idempotency key across every attempt,
# so the runner can replay the first outcome instead of running the work again.
NON_IDEMPOTENT_KINDS = frozenset({"spawn-launch", "lifecycle"})


# meiva:note
# The configured per-attempt deadline and the number of extra attempts after a transient failure.
@dataclass(frozen=True)
class RpcConfig:
    timeout_seconds: float
    max_retries: int


# meiva:note
# What one dispatch actually does: the per-attempt deadline and the number of extra attempts.
@dataclass(frozen=True)
class DispatchBudget:
    timeout_s: float
    retries: int


# meiva:note
# How the transport reads an HTTP status. ANSWERED: the ops server answered and its body is the
# op envelope. TRANSIENT: a server error, worth another attempt. REFUSED: any other status,
# terminal.
class ResponseVerdict(Enum):
    ANSWERED = "answered"
    TRANSIENT = "transient"
    REFUSED = "refused"


# meiva:note
# A caller's deadline and retry count over the configured ones. The configured retry count denotes
# extra attempts; a negative count still makes one attempt.
def dispatch_budget(
    config: RpcConfig, timeout_s: float | None, retries: int | None
) -> DispatchBudget:
    return DispatchBudget(
        timeout_s=config.timeout_seconds if timeout_s is None else timeout_s,
        retries=max(config.max_retries if retries is None else retries, 0),
    )


def _backoff_s(attempt: int) -> float:
    return min(_RETRY_BASE_DELAY_S * (2**attempt), _RETRY_MAX_DELAY_S)


# meiva:note
# Seconds to wait before retry number `attempt + 1`: bounded exponential backoff
# (`min(0.5 * 2**attempt, 4)`) scaled by `jitter`, a factor in [JITTER_LOW, JITTER_HIGH].
def retry_delay_s(attempt: int, jitter: float) -> float:
    return _backoff_s(attempt) * jitter


# meiva:note
# The longest one dispatch can take: every attempt at the deadline plus the largest jittered
# backoff between them. For callers that own an overall deadline over the client's own budgets.
def worst_case_dispatch_seconds(config: RpcConfig, timeout_s: float | None = None) -> float:
    retries = max(config.max_retries, 0)
    backoff = sum(_backoff_s(attempt) for attempt in range(retries))
    attempt_s = config.timeout_seconds if timeout_s is None else timeout_s
    return (retries + 1) * attempt_s + backoff * JITTER_HIGH


# meiva:note
# The connect deadline inside a per-attempt deadline: a three-second probe cannot spend ten
# seconds connecting.
def connect_timeout_s(timeout_s: float) -> float:
    return min(_CONNECT_TIMEOUT_CAP_S, timeout_s)


# meiva:note
# Business failures, 4xx responses and invalid response envelopes are terminal; a 5xx retries.
def response_verdict(status_code: int) -> ResponseVerdict:
    if status_code == 200:
        return ResponseVerdict.ANSWERED
    if 500 <= status_code < 600:
        return ResponseVerdict.TRANSIENT
    return ResponseVerdict.REFUSED


# meiva:note
# The stable key of an operation that has a business id, or None when this dispatch has none.
#
# A spawn-launch business id (the agent) produces a key scoped to its target and the canonical
# complete payload, so repeated launch dispatches can replay the same outcome. Lifecycle calls and
# payloads without a business id have none: the transport mints a fresh key per dispatch, because
# repeating reconciliation must not replay a previous failed result.
def business_idempotency_key(
    target_machine: str, kind: str, payload: Mapping[str, object]
) -> str | None:
    if kind != "spawn-launch":
        return None
    business_id = payload.get("agent_id")
    if business_id is None:
        return None
    canonical_payload = json.dumps(
        dict(payload), default=str, separators=(",", ":"), sort_keys=True
    )
    fingerprint = hashlib.sha256(canonical_payload.encode()).hexdigest()[:16]
    return f"{kind}:{target_machine}:{business_id}:{fingerprint}"
