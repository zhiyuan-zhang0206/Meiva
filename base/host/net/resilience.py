from __future__ import annotations

import asyncio
import os
import random
import time
import urllib.error
from collections.abc import Awaitable, Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Protocol, TypeVar, runtime_checkable

from base.host.boundary.untyped import object_of

__all__ = [
    "ExponentialBackoff",
    "Policy",
    "aretry",
    "aretry_jittered",
    "extract_retry_after",
    "http_classifier",
    "jitter_delay",
    "jittered",
    "retry",
    "retry_asleep",
    "retry_jittered",
    "retry_sleep",
]

T = TypeVar("T")

# HTTP statuses whose failure is transient (rate limit / upstream hiccup). A
# deterministic 4xx is a caller/upstream bug, not a retry gap.
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})

# Longest Retry-After honored at this layer (mirrors the provider-SDK cap);
# a longer header is treated as absent rather than blindly slept.
MAX_RETRY_AFTER_RESPECT_S = 120.0


# meiva:note
# The wait seam: `retry()` waits through its `sleep` keyword and `aretry()` awaits its `asleep`,
# both defaulting to these two functions. The defaults are this module's own functions, never
# `time.sleep` / `asyncio.sleep`: a default binds at definition time, so a caller passes the wait it
# wants and an identity check can notice a signature that stopped naming the hook. Patching these
# globals would not reach a loop that already bound its default.
def retry_sleep(seconds: float) -> None:
    time.sleep(seconds)


async def retry_asleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class Backoff(Protocol):
    """Seconds to wait before retry ``attempt`` (0-based)."""

    def __call__(self, attempt: int) -> float: ...


@dataclass(frozen=True)
class ExponentialBackoff:
    """Bounded exponential backoff: ``base * factor**attempt``, capped.

    The only backoff shape (evaluation-record #14 deleted the fixed-sequence
    variant); a constant schedule is the degenerate ``factor=1.0`` case.
    """

    base: float = 1.0
    factor: float = 2.0
    cap: float = 30.0

    def __call__(self, attempt: int) -> float:
        return min(self.base * (self.factor**attempt), self.cap)


def agent_phase(span: float) -> float:
    """Deterministic phase for this native process, independent of agent identity.

    Agent-specific schedules pass their identity explicitly at their own owner
    (the graph LLM retry policy does so). Generic transport calls spread by pid.
    """
    return span * (os.getpid() % 1000) / 1000.0


# meiva:note
# Jitter exists to spread retries, not to keep a secret, so the non-cryptographic generator is the
# intended one -- and it arrives from the caller: `rng` is the process generator the composition root
# built, so this module draws nothing of its own and a seeded generator fixes the wait a test reads.
def jittered(delay: float, span: float = 1.0, mode: str = "agent", *, rng: random.Random) -> float:
    """``delay`` plus jitter, never negative.

    ``mode``:
    - ``"agent"`` (default) — deterministic per-process phase in [0, span)
      plus random ±span: de-phases a fleet AND spreads within one process.
    - ``"phase"`` — deterministic per-process phase in [0, span) only.
    - ``"relative"`` — multiply ``delay`` by a random factor in [1-span, 1+span].
    - ``"random"`` — random ±span only.
    - ``"none"`` — ``delay`` unchanged (deterministic schedules: probes).

    The result is clamped at 0.0: additive jitter's theoretical lower bound is
    ``delay - span``, and a negative sleep raises ValueError from
    ``time.sleep`` — masking the original exception and killing the retry
    path exactly when the retry is the recovery (audit 2026-08-08 P2; the
    `base/lm/call.py` retry delay comes from a writable cluster setting,
    so ``delay < span`` is reachable in production, not just a broken
    Policy).
    """
    phase = agent_phase(span) if mode not in {"none", "relative", "random"} else 0.0
    noise = rng.uniform(-span, span) if mode not in {"none", "phase"} else 0.0
    return jitter_delay(delay, mode, phase=phase, noise=noise)


def jitter_delay(delay: float, mode: str, *, phase: float, noise: float) -> float:
    if mode == "none":
        return delay
    if mode == "phase":
        return max(0.0, delay + phase)
    if mode == "relative":
        return max(0.0, delay * (1 + noise))
    if mode == "random":
        return max(0.0, delay + noise)
    return max(0.0, delay + phase + noise)


def extract_retry_after(exc: Exception) -> float | None:
    """Seconds the upstream asks us to wait before retrying, or None.

    Duck-typed over any exception shape carrying ``response.headers``
    (provider SDKs, httpx) or a bare ``headers`` attribute (urllib's
    HTTPError). Values above ``MAX_RETRY_AFTER_RESPECT_S`` are ignored
    (treated as absent) rather than blindly slept.
    """
    headers = _headers_of(exc)
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        seconds = _seconds(headers.get(name), scale)
        if seconds is not None and 0 < seconds <= MAX_RETRY_AFTER_RESPECT_S:
            return seconds
    return None


# meiva:note
# A header value as seconds: `scale` is how many of the header's own unit make a second (1000 for
# milliseconds). A missing or empty value, or one that is not a number, is None (an unparseable header
# is absent, as the provider SDKs parse it, so the caller tries the next form).
def _seconds(raw: object, scale: float) -> float | None:
    if not raw or not isinstance(raw, str | bytes | int | float):
        return None
    try:
        return float(raw) / scale
    except (TypeError, ValueError):
        return None


@runtime_checkable
class _HeaderSource(Protocol):
    def get(self, name: str, /) -> object: ...


# meiva:note
# The headers an exception carries, wherever its type keeps them: `response.headers` (provider SDKs,
# httpx) or a bare `headers` attribute (urllib's `HTTPError`); None when it has neither or the value
# cannot be asked for a header by name.
def _headers_of(exc: Exception) -> _HeaderSource | None:
    response = object_of(getattr, exc, "response", None)
    headers = (
        object_of(getattr, response, "headers", None)
        if response is not None and hasattr(response, "headers")
        else object_of(getattr, exc, "headers", None)
    )
    return headers if isinstance(headers, _HeaderSource) else None


def _status_of(exc: Exception) -> int | None:
    """HTTP status the exception carries, if it is a status-carrying error."""
    response = object_of(getattr, exc, "response", None)
    if response is not None:
        status = object_of(getattr, response, "status_code", None)
        if isinstance(status, str | bytes | int | float):
            return int(status)
    code = object_of(getattr, exc, "code", None)  # urllib.error.HTTPError
    return code if isinstance(code, int) else None


def _is_transport(exc: Exception) -> bool:
    """True for connection-level failures (DNS, refused, timeout) — the
    retryable class that carries no HTTP status."""
    if isinstance(exc, urllib.error.URLError):
        return True
    try:
        import httpx
    except ImportError:
        return False
    return isinstance(exc, httpx.TransportError)


@dataclass(frozen=True, init=False)
class HttpClassifier:
    """The one HTTP error-classification semantics (design D2).

    Retryable: transient statuses (429/5xx) and transport errors. Permanent:
    any other HTTP status (a deterministic caller/upstream bug) and every
    non-HTTP exception (a deterministic programming error). Call sites
    compose local overrides with ``with_`` instead of re-implementing
    classification (only-override, never-rewrite — grep-able). The classifier
    snapshots overrides into immutable sets; composing a new value never
    changes an existing caller's classification policy.
    """

    _permanent: frozenset[int]
    _transient: frozenset[int]

    def __init__(
        self,
        *,
        permanent: AbstractSet[int] = frozenset(),
        transient: AbstractSet[int] = frozenset(),
    ) -> None:
        object.__setattr__(self, "_permanent", frozenset(permanent))
        object.__setattr__(self, "_transient", frozenset(transient))

    def with_(
        self,
        *,
        permanent: AbstractSet[int] = frozenset(),
        transient: AbstractSet[int] = frozenset(),
    ) -> HttpClassifier:
        """Compose: the same base semantics plus the given status overrides."""
        return HttpClassifier(
            permanent=self._permanent | permanent,
            transient=self._transient | transient,
        )

    def __call__(self, exc: Exception) -> bool:
        """True when ``exc`` is a retryable failure."""
        status = _status_of(exc)
        if status is not None:
            if status in self._transient:
                return True
            if status in self._permanent:
                return False
            return status in _TRANSIENT_STATUSES
        return _is_transport(exc)


http_classifier = HttpClassifier()


@dataclass(frozen=True)
class Policy:
    """Immutable retry parameters — the only vocabulary a call site declares.

    - ``max_attempts``: total executions (initial + retries), at least 1.
    - ``backoff``: exponential backoff shape (base / factor / cap).
    - ``jitter`` / ``jitter_span``: sleep spread; see ``jittered``.
    - ``classify``: retryability decision (default ``http_classifier``).
    - ``idempotent``: ``False`` gates retries off entirely — exactly one
      execution, for operations where a blind retry could duplicate an
      effect; the final failure then MUST be visible (``on_final_failure``
      compensation or the raised exception) — silent loss is structurally
      impossible (design D3).
    - ``respect_retry_after``: honor the upstream's Retry-After (capped)
      when it exceeds the backoff.
    - ``on_final_failure``: called with the last exception before it is
      re-raised; may itself raise (e.g. to convert the error type).
    """

    max_attempts: int = 3
    backoff: Backoff = field(default_factory=ExponentialBackoff)
    jitter: str = "agent"
    jitter_span: float = 1.0
    classify: Callable[[Exception], bool] = http_classifier
    idempotent: bool = True
    respect_retry_after: bool = True
    on_final_failure: Callable[[Exception], None] | None = None


# meiva:note
# The modes whose wait is decided without a draw: `none` hands the backoff back unchanged and
# `phase` adds the per-process phase. Every other mode asks for noise, and noise needs a generator.
_DETERMINISTIC_JITTER = frozenset({"none", "phase"})


# meiva:note
# The one wait decision both loops share: the policy's backoff, the upstream's Retry-After when the
# policy honours it, then the policy's jitter. `rng` is the process generator, or None from the door
# that promises a wait this policy does not draw -- a mode that would draw then refuses instead of
# reading entropy here.
def _wait_seconds(
    policy: Policy, attempt: int, exc: Exception, *, rng: random.Random | None
) -> float:
    delay = policy.backoff(attempt)
    if policy.respect_retry_after:
        retry_after = extract_retry_after(exc)
        if retry_after is not None:
            delay = max(delay, retry_after)
    if rng is not None:
        return jittered(delay, policy.jitter_span, policy.jitter, rng=rng)
    if policy.jitter not in _DETERMINISTIC_JITTER:
        raise ValueError(
            f"this retry policy draws jitter (`jitter={policy.jitter!r}`): build its loop with "
            "`retry_jittered(policy, rng=...)` / `aretry_jittered(...)` and hand it the process "
            "generator, or declare a wait that draws nothing"
        )
    phase = agent_phase(policy.jitter_span) if policy.jitter == "phase" else 0.0
    return jitter_delay(delay, policy.jitter, phase=phase, noise=0.0)


# meiva:note
# A drawing policy must not reach the door that promises no draw: refuse where the loop is built,
# not on the first transient failure, which may be never.
def _require_no_draw(policy: Policy, door: str) -> None:
    if policy.jitter not in _DETERMINISTIC_JITTER:
        raise ValueError(
            f"{door} waits without drawing, but this policy's `jitter={policy.jitter!r}` asks for "
            "noise: use retry_jittered/aretry_jittered with the process generator instead"
        )


def _sync_loop(
    policy: Policy, *, rng: random.Random | None, sleep: Callable[[float], None]
) -> Callable[[Callable[[], T]], T]:
    def _run(fn: Callable[[], T]) -> T:
        attempts = 1 if not policy.idempotent else max(1, policy.max_attempts)
        for attempt in range(attempts):
            try:
                return fn()
            except Exception as exc:
                if attempt < attempts - 1 and policy.classify(exc):
                    sleep(_wait_seconds(policy, attempt, exc, rng=rng))
                    continue
                if policy.on_final_failure is not None:
                    policy.on_final_failure(exc)
                raise
        raise AssertionError("unreachable")

    return _run


def _async_loop(
    policy: Policy, *, rng: random.Random | None, asleep: Callable[[float], Awaitable[None]]
) -> Callable[[Callable[[], Awaitable[T]]], Awaitable[T]]:
    def _run(fn: Callable[[], Awaitable[T]]) -> Awaitable[T]:
        async def _runner() -> T:
            attempts = 1 if not policy.idempotent else max(1, policy.max_attempts)
            for attempt in range(attempts):
                try:
                    return await fn()
                except Exception as exc:
                    if attempt < attempts - 1 and policy.classify(exc):
                        await asleep(_wait_seconds(policy, attempt, exc, rng=rng))
                        continue
                    if policy.on_final_failure is not None:
                        policy.on_final_failure(exc)
                    raise
            raise AssertionError("unreachable")

        return _runner()

    return _run


# meiva:note
# The door that promises a wait which draws nothing: the policy's backoff plus its Retry-After, and
# a policy whose `jitter` mode asks for noise is refused here at build time, because this loop has
# no generator to draw from. `retry_jittered` is the drawing door.
# meiva:link base/host/docs/resilience.meiva.okf.md
def retry(
    policy: Policy, *, sleep: Callable[[float], None] = retry_sleep
) -> Callable[[Callable[[], T]], T]:
    """Wrap a sync ``fn`` in the one retry loop."""
    _require_no_draw(policy, "retry")
    return _sync_loop(policy, rng=None, sleep=sleep)


# meiva:note
# The sync loop for a policy whose wait draws its jitter: `rng` is the process's generator, the one
# `random.Random` the composition root built (a `random.SystemRandom` where a secret rides on the
# draw), and a test hands in a seeded one so the waits it asserts are repeatable.
def retry_jittered(
    policy: Policy, *, rng: random.Random, sleep: Callable[[float], None] = retry_sleep
) -> Callable[[Callable[[], T]], T]:
    return _sync_loop(policy, rng=rng, sleep=sleep)


# meiva:note
# The async twin of `retry`, with the same refusal and the same promise: a drawing policy is built
# with `aretry_jittered` instead.
def aretry(
    policy: Policy, *, asleep: Callable[[float], Awaitable[None]] = retry_asleep
) -> Callable[[Callable[[], Awaitable[T]]], Awaitable[T]]:
    """Wrap an async ``fn`` in the one retry loop (async twin of ``retry``)."""
    _require_no_draw(policy, "aretry")
    return _async_loop(policy, rng=None, asleep=asleep)


# meiva:note
# The async drawing door: the same generator contract as `retry_jittered`, awaited through the
# caller's own `asleep`.
def aretry_jittered(
    policy: Policy, *, rng: random.Random, asleep: Callable[[float], Awaitable[None]] = retry_asleep
) -> Callable[[Callable[[], Awaitable[T]]], Awaitable[T]]:
    return _async_loop(policy, rng=rng, asleep=asleep)
