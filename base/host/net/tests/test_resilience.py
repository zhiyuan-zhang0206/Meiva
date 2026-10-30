"""base/host/net/resilience.py — R2-D retry primitives tests.

Covers the four entity contracts: Policy parameterization, the one retry-loop
implementation (retry/aretry), the one classification semantics
(http_classifier + compositional with_ overrides), and the shared jitter /
Retry-After helpers (design-concept.md §4.4, evaluation-record #14).
"""

from __future__ import annotations

import asyncio
import email.message
import inspect
import io
import time
import urllib.error
from dataclasses import FrozenInstanceError

import httpx
import pytest

from base.host.net import resilience
from base.host.net.resilience import (
    ExponentialBackoff,
    Policy,
    aretry,
    extract_retry_after,
    http_classifier,
    jitter_delay,
    jittered,
    retry,
    retry_asleep,
    retry_sleep,
)
from base.host.net.tests.resilience_process import report


class _Flaky:
    """Raises ``error`` for the first ``failures`` calls, then returns ``value``."""

    def __init__(self, error: Exception, failures: int, value: object = "ok") -> None:
        self._error = error
        self._remaining = failures
        self.value = value
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise self._error
        return self.value


def _http_error(status: int, *, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        url="https://example.test/x",
        code=status,
        msg=f"status {status}",
        hdrs=hdrs if retry_after else None,  # type: ignore[arg-type]
        fp=io.BytesIO(b"{}"),
    )


# meiva:note
# Every case here drives the loop through a recorder: a backoff wait lands in `timers.sleeps` and
# nothing sleeps for real. It is local because this package may not reach the clock's manual
# timers (that import closes a host -> clock cycle), and the seam needs nothing more than these two
# methods.
class _RecordingTimers:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    async def asleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


@pytest.fixture
def timers() -> _RecordingTimers:
    return _RecordingTimers()


class TestRetry:
    def test_success_first_attempt(self, timers: _RecordingTimers) -> None:
        f = _Flaky(ValueError("x"), 0)
        assert retry(Policy(), sleep=timers.sleep)(f) == "ok"
        assert f.calls == 1

    def test_transient_then_success(self, timers: _RecordingTimers) -> None:
        f = _Flaky(urllib.error.URLError("boom"), 2)
        assert retry(Policy(max_attempts=4), sleep=timers.sleep)(f) == "ok"
        assert f.calls == 3

    def test_exhausts_raises_last_error(self, timers: _RecordingTimers) -> None:
        f = _Flaky(urllib.error.URLError("boom"), 100)
        with pytest.raises(urllib.error.URLError):
            retry(Policy(max_attempts=3), sleep=timers.sleep)(f)
        assert f.calls == 3

    def test_permanent_status_not_retried(self, timers: _RecordingTimers) -> None:
        f = _Flaky(_http_error(400), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(Policy(max_attempts=3), sleep=timers.sleep)(f)
        assert f.calls == 1

    def test_transient_status_retried(self, timers: _RecordingTimers) -> None:
        f = _Flaky(_http_error(429), 2)
        assert retry(Policy(max_attempts=4), sleep=timers.sleep)(f) == "ok"
        assert f.calls == 3

    def test_non_http_exception_is_permanent(self, timers: _RecordingTimers) -> None:
        f = _Flaky(ValueError("bug"), 100)
        with pytest.raises(ValueError):
            retry(Policy(max_attempts=3), sleep=timers.sleep)(f)
        assert f.calls == 1

    def test_idempotent_false_single_attempt(self, timers: _RecordingTimers) -> None:
        """Non-idempotent: exactly one execution even for retryable errors."""
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(Policy(idempotent=False, on_final_failure=lambda _e: None), sleep=timers.sleep)(f)
        assert f.calls == 1

    def test_on_final_failure_hook_sees_last_error(self, timers: _RecordingTimers) -> None:
        seen: list[Exception] = []
        f = _Flaky(_http_error(500), 100)

        def _hook(exc: Exception) -> None:
            seen.append(exc)

        with pytest.raises(urllib.error.HTTPError):
            retry(Policy(max_attempts=2, on_final_failure=_hook), sleep=timers.sleep)(f)
        assert len(seen) == 1
        assert isinstance(seen[0], urllib.error.HTTPError)

    def test_on_final_failure_can_convert_error_type(self, timers: _RecordingTimers) -> None:
        def _hook(exc: Exception) -> None:
            raise RuntimeError(f"converted: {exc}") from exc

        f = _Flaky(_http_error(502), 100)
        with pytest.raises(RuntimeError, match="converted"):
            retry(Policy(on_final_failure=_hook), sleep=timers.sleep)(f)

    def test_backoff_sequence(self, timers: _RecordingTimers) -> None:
        """Exponential shape: base * factor**attempt, capped."""
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=4,
                    backoff=ExponentialBackoff(1.0, 2.0, 8.0),
                    jitter="none",
                ),
                sleep=timers.sleep,
            )(f)
        assert timers.sleeps == [1.0, 2.0, 4.0]

    def test_backoff_capped(self, timers: _RecordingTimers) -> None:
        f = _Flaky(_http_error(503), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=5,
                    backoff=ExponentialBackoff(1.0, 2.0, 3.0),
                    jitter="none",
                ),
                sleep=timers.sleep,
            )(f)
        assert timers.sleeps == [1.0, 2.0, 3.0, 3.0]

    def test_retry_after_overrides_backoff(self, timers: _RecordingTimers) -> None:
        f = _Flaky(_http_error(429, retry_after="30"), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=2,
                    backoff=ExponentialBackoff(1.0, 1.0, 1.0),
                    jitter="none",
                ),
                sleep=timers.sleep,
            )(f)
        assert timers.sleeps == [30.0]

    def test_respect_retry_after_false(self, timers: _RecordingTimers) -> None:
        f = _Flaky(_http_error(429, retry_after="30"), 100)
        with pytest.raises(urllib.error.HTTPError):
            retry(
                Policy(
                    max_attempts=2,
                    backoff=ExponentialBackoff(1.0, 1.0, 1.0),
                    jitter="none",
                    respect_retry_after=False,
                ),
                sleep=timers.sleep,
            )(f)
        assert timers.sleeps == [1.0]


class TestAretry:
    async def test_aretry_transient_then_success(self, timers: _RecordingTimers) -> None:
        f = _Flaky(httpx.ConnectError("boom"), 2)

        async def _call() -> object:
            return f()

        assert await aretry(Policy(max_attempts=4), asleep=timers.asleep)(_call) == "ok"
        assert f.calls == 3

    async def test_aretry_permanent_not_retried(self, timers: _RecordingTimers) -> None:
        f = _Flaky(ValueError("bug"), 100)

        async def _call() -> object:
            return f()

        with pytest.raises(ValueError):
            await aretry(Policy(max_attempts=3), asleep=timers.asleep)(_call)
        assert f.calls == 1

    async def test_aretry_idempotent_false_single_attempt(self, timers: _RecordingTimers) -> None:
        f = _Flaky(_http_error(503), 100)

        async def _call() -> object:
            return f()

        with pytest.raises(urllib.error.HTTPError):
            await aretry(
                Policy(idempotent=False, on_final_failure=lambda _e: None), asleep=timers.asleep
            )(_call)
        assert f.calls == 1


# meiva:note
# `retry` / `aretry` wait through the callable they are handed, not a patched global.
class TestWaitSeam:
    def test_the_defaults_are_the_named_hooks(self) -> None:
        # meiva:note
        # The hook is this module's own function, so a signature that spells `time.sleep` binds a
        # different object and fails here rather than silently missing the seam later.
        assert inspect.signature(retry).parameters["sleep"].default is retry_sleep
        assert inspect.signature(aretry).parameters["asleep"].default is retry_asleep
        assert retry_sleep is not time.sleep
        assert retry_asleep is not asyncio.sleep

    def test_the_hooks_are_exported(self) -> None:
        assert {"retry_sleep", "retry_asleep"} <= set(resilience.__all__)

    def test_retry_sleeps_through_the_callable_it_is_handed(self, timers: _RecordingTimers) -> None:
        # meiva:link base/host/docs/host.meiva.okf.md
        run = retry(
            Policy(max_attempts=3, backoff=ExponentialBackoff(1.0, 2.0, 8.0), jitter="none"),
            sleep=timers.sleep,
        )
        assert run(_Flaky(urllib.error.URLError("boom"), 1)) == "ok"
        assert timers.sleeps == [1.0]

    async def test_aretry_awaits_through_the_callable_it_is_handed(
        self, timers: _RecordingTimers
    ) -> None:
        flaky = _Flaky(urllib.error.URLError("boom"), 1)

        async def call() -> object:
            return flaky()

        assert (
            await aretry(
                Policy(max_attempts=2, backoff=ExponentialBackoff(3.0, 1.0, 3.0), jitter="none"),
                asleep=timers.asleep,
            )(call)
            == "ok"
        )
        assert timers.sleeps == [3.0]

    async def test_one_recorder_captures_sync_and_async_waits_in_call_order(
        self, timers: _RecordingTimers
    ) -> None:
        policy = Policy(max_attempts=2, backoff=ExponentialBackoff(1.0, 1.0, 1.0), jitter="none")
        assert retry(policy, sleep=timers.sleep)(_Flaky(urllib.error.URLError("boom"), 1)) == "ok"
        flaky_async = _Flaky(urllib.error.URLError("boom"), 1)

        async def _call() -> object:
            return flaky_async()

        assert (
            await aretry(
                Policy(max_attempts=2, backoff=ExponentialBackoff(3.0, 1.0, 3.0), jitter="none"),
                asleep=timers.asleep,
            )(_call)
            == "ok"
        )
        assert timers.sleeps == [1.0, 3.0]


class TestHttpClassifier:
    def test_override_inputs_are_snapshots(self) -> None:
        permanent = {429}
        transient = {404}
        classifier = resilience.HttpClassifier(permanent=permanent, transient=transient)
        permanent.clear()
        permanent.add(500)
        transient.clear()
        transient.add(400)
        assert classifier(_http_error(429)) is False
        assert classifier(_http_error(500)) is True
        assert classifier(_http_error(404)) is True
        assert classifier(_http_error(400)) is False

    def test_classifier_fields_are_immutable(self) -> None:
        classifier = http_classifier.with_(transient={404})
        with pytest.raises(FrozenInstanceError):
            classifier.__setattr__("_permanent", frozenset({500}))
        assert classifier(_http_error(500)) is True

    def test_composition_preserves_the_original(self) -> None:
        original = http_classifier.with_(transient={404})
        extended = original.with_(permanent={429})
        assert original(_http_error(429)) is True
        assert extended(_http_error(429)) is False
        assert original(_http_error(404)) is True
        assert extended(_http_error(404)) is True

    def test_transient_statuses(self) -> None:
        for code in (429, 500, 502, 503, 504):
            assert http_classifier(_http_error(code)) is True

    def test_permanent_statuses(self) -> None:
        for code in (400, 401, 403, 404, 422):
            assert http_classifier(_http_error(code)) is False

    def test_httpx_status_error(self) -> None:
        exc = httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "https://e/x"),
            response=httpx.Response(503),
        )
        assert http_classifier(exc) is True
        exc400 = httpx.HTTPStatusError(
            "x",
            request=httpx.Request("GET", "https://e/x"),
            response=httpx.Response(400),
        )
        assert http_classifier(exc400) is False

    def test_transport_errors_retryable(self) -> None:
        assert http_classifier(urllib.error.URLError("dns")) is True
        assert http_classifier(httpx.ConnectError("refused")) is True
        assert http_classifier(httpx.ReadTimeout("slow")) is True

    def test_other_exceptions_permanent(self) -> None:
        assert http_classifier(ValueError("bug")) is False
        assert http_classifier(KeyError("k")) is False

    def test_with_permanent_override(self) -> None:
        c = http_classifier.with_(permanent={429})
        assert c(_http_error(429)) is False  # forced permanent
        assert c(_http_error(500)) is True  # base semantics kept
        assert c(_http_error(400)) is False

    def test_with_transient_override(self) -> None:
        c = http_classifier.with_(transient={404})
        assert c(_http_error(404)) is True  # forced transient
        assert c(_http_error(429)) is True  # base semantics kept
        assert c(_http_error(400)) is False

    def test_with_overrides_accumulate(self) -> None:
        c = http_classifier.with_(permanent={429}).with_(transient={404})
        assert c(_http_error(429)) is False
        assert c(_http_error(404)) is True


class TestJitter:
    def test_none_mode_returns_delay(self) -> None:
        assert jittered(2.0, mode="none") == 2.0

    @pytest.mark.parametrize("random_value", [-100.0, 100.0])
    def test_phase_mode_adds_deterministic_phase_only(self, random_value: float) -> None:
        assert jitter_delay(2.0, "phase", phase=1.25, noise=random_value) == 3.25

    def test_phase_mode_bounds(self) -> None:
        for _ in range(100):
            j = jittered(2.0, span=5.0, mode="phase")
            assert 2.0 <= j < 7.0

    @pytest.mark.parametrize("random_value, expected", [(-0.5, 2.0), (0.0, 4.0), (0.5, 6.0)])
    def test_relative_mode_scales_delay(self, random_value: float, expected: float) -> None:
        assert jitter_delay(4.0, "relative", phase=100.0, noise=random_value) == expected

    def test_relative_mode_bounds(self) -> None:
        for _ in range(100):
            j = jittered(4.0, span=0.5, mode="relative")
            assert 2.0 <= j <= 6.0

    def test_relative_mode_never_negative(self) -> None:
        assert jitter_delay(0.5, "relative", phase=100.0, noise=-2.0) == 0.0

    def test_relative_zero_span_returns_delay(self) -> None:
        assert jittered(4.0, span=0.0, mode="relative") == 4.0

    def test_retry_passes_phase_jitter_to_sleep(self) -> None:
        observed = report("phase")
        phase = observed["phase"]
        assert isinstance(phase, (int, float))
        assert observed["result"] == "ok"
        assert observed["waits"] == [1.0 + phase]

    def test_retry_passes_relative_jitter_to_sleep(self) -> None:
        observed = report("relative")
        noise = observed["noise"]
        assert isinstance(noise, (int, float))
        assert observed["result"] == "ok"
        assert observed["waits"] == [1.0 * (1.0 + noise)]

    def test_agent_mode_bounds(self) -> None:
        # delay + phase in [0, span) + uniform(-span, span)
        for _ in range(100):
            j = jittered(2.0, span=1.0, mode="agent")
            assert 1.0 <= j < 4.0

    def test_random_mode_bounds(self) -> None:
        for _ in range(100):
            j = jittered(2.0, span=1.0, mode="random")
            assert 1.0 <= j <= 3.0

    def test_never_negative_when_delay_below_span(self) -> None:
        """`delay < span` must not produce a negative sleep — time.sleep(-x)
        raises ValueError and masks the original exception on the retry path
        (audit 2026-08-08 P2; invoke_text's delay comes from a writable
        cluster setting, so this is production-reachable)."""
        for _ in range(200):
            assert jittered(0.5, span=1.0, mode="agent") >= 0.0
            assert jittered(0.5, span=1.0, mode="random") >= 0.0
        assert jittered(0.0, span=5.0, mode="random") >= 0.0


class TestExtractRetryAfter:
    def test_response_headers(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "5"})
        exc = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://e/x"), response=resp)
        assert extract_retry_after(exc) == 5.0

    def test_retry_after_ms(self) -> None:
        resp = httpx.Response(429, headers={"retry-after-ms": "1500"})
        exc = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://e/x"), response=resp)
        assert extract_retry_after(exc) == 1.5

    def test_urllib_headers_direct(self) -> None:
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "42"
        exc = urllib.error.HTTPError(
            url="https://e/x", code=429, msg="r", hdrs=hdrs, fp=io.BytesIO(b"{}")
        )
        assert extract_retry_after(exc) == 42.0

    def test_over_cap_ignored(self) -> None:
        resp = httpx.Response(429, headers={"Retry-After": "3600"})
        exc = httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://e/x"), response=resp)
        assert extract_retry_after(exc) is None

    def test_no_headers(self) -> None:
        assert extract_retry_after(ValueError("x")) is None
        assert extract_retry_after(_http_error(429)) is None  # hdrs=None


def test_transport_phase_uses_process_not_env_identity() -> None:
    for agent_id in (7, 42):
        observed = report("identity", agent_id=agent_id)
        pid = observed["pid"]
        assert isinstance(pid, int)
        assert observed["phase"] == 10.0 * (pid % 1000) / 1000.0


def test_aretry_awaits_the_timers_it_is_handed_in_a_fresh_process() -> None:
    observed = report("async-waits")
    assert observed == {"attempts": 3, "waits": [1.0, 2.0], "same_error": True}
