from __future__ import annotations

import io
import itertools
import json
import threading
import time
import urllib.error
from typing import Any, cast

import pytest
from pydantic import SecretStr

from base.config import settings
from meiva.tests.namespaces.web._web_helpers import FakeResp, WebHarness
from meiva.tests.namespaces.web._web_helpers import web_harness as web_harness
from meiva.web import FetchError, SearchError, deepseek, wiring

pytestmark = [
    pytest.mark.usefixtures("retry_waits"),
    pytest.mark.usefixtures("sdk_model_owner"),
]


class _CapturingModel:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def invoke(self, messages: list[Any]) -> Any:
        texts: list[str] = []
        for message in messages:
            content = getattr(message, "content", "")
            blocks = cast(list[dict[str, Any]], content) if isinstance(content, list) else []
            for block in blocks:
                if block.get("type") == "text":
                    texts.append(cast(str, block["text"]))
        self._captured["texts"] = texts
        return type("Response", (), {"content": "ANSWER", "response_metadata": {}})()

    def build(self, model: str, **kwargs: Any) -> _CapturingModel:
        return self


def _payload(results: list[dict[str, Any]]) -> bytes:
    return json.dumps({"results": results}).encode()


def _item(**overrides: Any) -> dict[str, Any]:
    base = {
        "rerank_score": 2.3,
        "title": "deepseek title",
        "url": "https://example.com/result",
        "snippet": "deepseek snippet",
        "summary": "deepseek long summary",
        "site_name": "example",
        "date": None,
        "authority": 3,
    }
    return {**base, **overrides}


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.deepseek.com/v1/tools/search",
        code=code,
        msg="error",
        hdrs=cast(Any, None),
        fp=io.BytesIO(body),
    )


def _use_deepseek_search(harness: WebHarness, **changes: Any) -> None:
    harness.set_provider("deepseek")
    harness.set_deepseek_search(api_key=SecretStr("ds-key"), **changes)


def _use_deepseek_fetch(harness: WebHarness, **changes: Any) -> None:
    harness.set_reader("deepseek")
    harness.set_deepseek_fetch(api_key=SecretStr("ds-key"), **changes)


def test_default_provider_is_unchanged():
    assert settings.web.web_search_provider == "brave"
    assert settings.web.web_fetch_reader == "jina"


def test_search_maps_deepseek_results(web_harness: WebHarness):
    _use_deepseek_search(web_harness)
    captured: dict[str, Any] = {}

    def _capture(request: Any, timeout: Any) -> FakeResp:
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.data)
        return FakeResp(_payload([_item(), _item(title="second", url="https://b.com")]))

    with web_harness.urlopen_returns(side_effect=_capture):
        results = web_harness.search(["python asyncio"], count=5)

    assert captured["method"] == "POST"
    assert captured["url"] == settings.web.web_deepseek_search_endpoint
    assert captured["auth"] == "Bearer ds-key"
    assert captured["body"] == {"query": "python asyncio", "count": 5}
    assert [(r.title, r.url, r.snippet, r.kind) for r in results[0]] == [
        ("deepseek title", "https://example.com/result", "deepseek snippet", "web"),
        ("second", "https://b.com", "deepseek snippet", "web"),
    ]


def test_search_falls_back_to_summary_when_snippet_is_empty(web_harness: WebHarness):
    _use_deepseek_search(web_harness)
    with web_harness.urlopen_returns(return_value=FakeResp(_payload([_item(snippet="")]))):
        results = web_harness.search(["q"])
    assert results[0][0].snippet == "deepseek long summary"


def test_search_requires_a_deepseek_key(web_harness: WebHarness):
    web_harness.set_provider("deepseek")
    web_harness.set_deepseek_search(api_key=None)
    with pytest.raises(SearchError, match="DEEPSEEK_API_KEY"):
        web_harness.search(["q"])


def test_search_clamps_count_to_the_provider_cap(web_harness: WebHarness):
    _use_deepseek_search(web_harness, max_count=50)
    captured: dict[str, Any] = {}

    def _capture(request: Any, timeout: Any) -> FakeResp:
        captured["body"] = json.loads(request.data)
        return FakeResp(_payload([]))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.search(["q"], count=100)
    assert captured["body"]["count"] == 50


def test_search_surfaces_the_provider_error_message(web_harness: WebHarness):
    _use_deepseek_search(web_harness)
    body = b'{"error": {"message": "Authentication Fails, Your api key is invalid"}}'
    with (
        web_harness.urlopen_returns(side_effect=_http_error(401, body)),
        pytest.raises(SearchError, match="Authentication Fails"),
    ):
        web_harness.search(["q"])


def test_transient_429_is_retried(web_harness: WebHarness):
    _use_deepseek_search(web_harness)
    calls = {"n": 0}

    def _flaky(request: Any, timeout: Any) -> FakeResp:
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(429, b'{"error": {"message": "rate limited"}}')
        return FakeResp(_payload([_item()]))

    with web_harness.urlopen_returns(side_effect=_flaky):
        results = web_harness.search(["q"])
    assert calls["n"] == 2
    assert results[0][0].title == "deepseek title"


def test_persistent_429_exhausts_the_retry_budget(web_harness: WebHarness):
    _use_deepseek_search(web_harness)
    calls = {"n": 0}

    def _always(request: Any, timeout: Any) -> FakeResp:
        calls["n"] += 1
        raise _http_error(429, b'{"error": {"message": "rate limited"}}')

    with (
        web_harness.urlopen_returns(side_effect=_always),
        pytest.raises(SearchError, match="429"),
    ):
        web_harness.search(["q"])
    assert calls["n"] == 3


# meiva:note
# The pacer promises a schedule, not an observable spacing: `RateLimiter.acquire()` reserves
# the next slot (`_next_at + interval`) and sleeps until it, and a caller that oversleeps by
# epsilon starts la-epsilon and is followed by the caller that had already reserved the slot
# behind it - so the OBSERVED gap shrinks by epsilon while the slot never moves. Overshooting
# can therefore only make a request later than planned, never earlier, which is the direction
# that matters for the account-level 5 QPS limit. These two cases assert that on a
# deterministic clock; the old `min(gaps) >= 0.15` asserted something the code cannot promise
# and failed 2-3 runs in 5 on a quiet laptop.
class _PacerClock:
    def __init__(self, *, overshoot: float = 0.0) -> None:
        self.now = 1_000.0
        self.overshoot = overshoot
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        # meiva:note
        # The overshoot is applied to the first wait only: it models one late wake-up, and
        # letting every wait overshoot would hide the compression it causes.
        extra = self.overshoot if not self.waits else 0.0
        self.waits.append(seconds)
        self.now += seconds + extra


def _paced_starts(clock: _PacerClock, count: int) -> list[float]:
    pacer = deepseek.RateLimiter(5.0, monotonic=clock.monotonic, sleep=clock.sleep)
    starts: list[float] = []
    for _ in range(count):
        pacer.acquire()
        starts.append(clock.now)
    return starts


def test_the_pacer_reserves_slots_one_interval_apart() -> None:
    clock = _PacerClock()
    starts = _paced_starts(clock, 4)
    assert clock.waits == pytest.approx([0.2, 0.2, 0.2])
    assert starts == sorted(starts)
    assert len(set(starts)) == 4
    assert [later - earlier for earlier, later in itertools.pairwise(starts)] == pytest.approx(
        [0.2, 0.2, 0.2]
    )


def test_an_oversleep_compresses_the_gap_without_moving_a_slot() -> None:
    # meiva:note
    # 50 ms of overshoot on one wait: the next wait is 0.15 instead of 0.20 (the slot it
    # reserved was fixed before this caller woke), so the observed gap compresses by exactly
    # the overshoot while the requested slot sequence stays 0.20 apart, and no start moves
    # earlier than its own slot.
    clock = _PacerClock(overshoot=0.05)
    starts = _paced_starts(clock, 3)
    assert clock.waits == pytest.approx([0.2, 0.15])
    assert starts[1] - starts[0] == pytest.approx(0.25)
    assert starts[2] - starts[1] == pytest.approx(0.15)
    assert starts == sorted(starts)


def test_batch_request_starts_stay_inside_the_configured_rate_window(
    web_harness: WebHarness,
):
    # meiva:note
    # The end-to-end claim the provider cares about, and the one a real clock can carry: four
    # requests occupy at least `(n-1)/qps` because every start waits for its own slot, and
    # sleep overshoot only widens that window. The upper bound is the regression direction the
    # floor cannot see (a pacer ten times slower still satisfies a floor).
    _use_deepseek_search(web_harness)
    starts: list[float] = []
    lock = threading.Lock()

    def _capture(request: Any, timeout: Any) -> FakeResp:
        with lock:
            starts.append(time.monotonic())
        return FakeResp(_payload([_item()]))

    with web_harness.urlopen_returns(side_effect=_capture):
        web_harness.search(["a", "b", "c", "d"], max_concurrent=4)

    assert len(starts) == 4
    window = max(starts) - min(starts)
    assert window >= 3 / 5.0, f"four requests inside {window:.3f}s for 5 QPS"
    assert window <= 1.0, f"four requests spread over {window:.3f}s for 5 QPS"


def test_fetch_reads_the_page_from_the_deepseek_endpoint(web_harness: WebHarness):
    _use_deepseek_fetch(web_harness)
    captured: dict[str, Any] = {}
    web_harness.build_chat_model(_CapturingModel(captured).build)

    def _capture(request: Any, timeout: Any) -> FakeResp:
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return FakeResp(json.dumps({"title": "Page", "content": "the page body"}).encode())

    with web_harness.urlopen_returns(side_effect=_capture):
        answers = web_harness.fetch([("https://example.com/p", "summarize")])

    assert answers == ["ANSWER"]
    assert captured["url"] == settings.web.web_deepseek_fetch_endpoint
    assert captured["body"] == {"url": "https://example.com/p"}
    material = "\n".join(captured["texts"])
    assert "the page body" in material
    assert "Page" in material


def test_fetch_tells_the_model_when_the_deepseek_page_was_cut(web_harness: WebHarness):
    _use_deepseek_fetch(web_harness)
    captured: dict[str, Any] = {}
    web_harness.build_chat_model(_CapturingModel(captured).build)
    with web_harness.urlopen_returns(
        return_value=FakeResp(json.dumps({"title": "t", "content": "x" * 500}).encode())
    ):
        web_harness.fetch([("https://example.com/p", "summarize")], max_chars=100)
    material = "\n".join(captured["texts"])
    assert "cut off" in material
    assert material.count("x" * 100) == 1
    assert material.count("x" * 101) == 0


def test_fetch_reports_an_unreadable_page(web_harness: WebHarness):
    _use_deepseek_fetch(web_harness)
    body = b'{"error": {"message": "failed to fetch URL (request_id: abc)"}}'
    with (
        web_harness.urlopen_returns(side_effect=_http_error(404, body)),
        pytest.raises(FetchError, match="failed to fetch URL"),
    ):
        web_harness.fetch([("https://example.com/p", "summarize")])


def test_fetch_rejects_an_empty_page_body(web_harness: WebHarness):
    _use_deepseek_fetch(web_harness)
    with (
        web_harness.urlopen_returns(
            return_value=FakeResp(json.dumps({"title": "t", "content": ""}).encode())
        ),
        pytest.raises(FetchError, match="no page content"),
    ):
        web_harness.fetch([("https://example.com/p", "summarize")])


def test_fetch_requires_a_deepseek_key_when_the_reader_is_deepseek(web_harness: WebHarness):
    web_harness.set_reader("deepseek")
    web_harness.set_deepseek_fetch(api_key=None)
    with pytest.raises(FetchError, match="DEEPSEEK_API_KEY"):
        web_harness.fetch([("https://example.com/p", "summarize")])


def test_search_deps_reads_provider_settings(web_harness: WebHarness):
    deps = wiring.search_deps()
    assert deps.provider in ("brave", "deepseek")
    assert deps.deepseek.max_count == settings.web.web_deepseek_max_results
    assert deps.deepseek.pacer is not None


def test_fetch_deps_reads_the_reader_setting(web_harness: WebHarness):
    deps = wiring.fetch_deps()
    assert deps.reader in ("jina", "deepseek")
    assert deps.deepseek.endpoint == settings.web.web_deepseek_fetch_endpoint
