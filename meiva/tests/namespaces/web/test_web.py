# meiva:note
# The fetch path of meiva.web end to end over a real loopback HTTP listener (the Jina Reader
# provider) and a scripted chat model for the answer step. What a provider is sent and how its
# answer is read are asserted on values in test_web_wire.py; these tests cover the shell: the
# page travels over the wire, the model answers, failures surface as the right error, and a batch
# runs concurrently and in order.

from __future__ import annotations

import json
import threading
import time
from typing import Any, cast

import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import BaseMessage
from pydantic import SecretStr

import meiva
from meiva.sdk_surface.pure.plans.batch_plan import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.tests.namespaces.web._web_helpers import Received, Reply, WebHarness
from meiva.tests.namespaces.web._web_helpers import loopback as loopback
from meiva.tests.namespaces.web._web_helpers import web_harness as web_harness
from meiva.web import FetchError, WebError

pytestmark = pytest.mark.usefixtures("retry_waits")


def _page(*, title: str = "", url: str = "", content: str = "", code: int = 200) -> bytes:
    return json.dumps(
        {"code": code, "data": {"title": title, "url": url, "content": content}}
    ).encode()


def test_fetch_requires_list(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"list of .*url, prompt.* pairs"):
        web_harness.fetch("https://example.com", "summarize")  # type: ignore[call-overload]


def test_fetch_rejects_non_tuple_elements(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be a .*url, prompt.* pair"):
        web_harness.fetch(["https://example.com"])  # type: ignore[list-item]


def test_fetch_rejects_wrong_tuple_length(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be a .*url, prompt.* pair"):
        web_harness.fetch([("https://example.com", "prompt", "extra")])  # type: ignore[list-item]


def test_fetch_rejects_non_string_tuple_elements(web_harness: WebHarness) -> None:
    with pytest.raises(TypeError, match=r"must be .*url: str, prompt: str"):
        web_harness.fetch([(123, "prompt")])  # type: ignore[list-item]


def test_fetch_effort_invalid_raises_before_any_request(web_harness: WebHarness) -> None:
    with pytest.raises(ValueError, match="effort"):
        web_harness.fetch([("https://example.com", "summarize")], effort="ultra")
    with pytest.raises(TypeError, match="effort"):
        web_harness.fetch([("https://example.com", "summarize")], effort=cast(Any, 3))
    assert web_harness.loopback.received == []


def test_fetch_reads_the_page_over_the_wire_then_the_model_answers(web_harness: WebHarness) -> None:
    web_harness.set_jina(api_key=SecretStr("jina-secret"))
    web_harness.loopback.answer(
        _page(
            title="PEP 758",
            url="https://peps.python.org/pep-0758/",
            content="Abstract\n--------\nexcept* without parens.",
        )
    )
    model = web_harness.script("FAKE ANSWER")

    results = web_harness.fetch(
        [("https://example.com/search?q=hello world", "what does this PEP propose?")]
    )

    assert results == ["FAKE ANSWER"]
    [request] = web_harness.loopback.received
    assert request.target == "/jina/https://example.com/search?q=hello%20world"
    assert request.headers["Authorization"] == "Bearer jina-secret"
    assert "meiva-sdk" in request.headers["User-Agent"]
    assert model.texts() == [
        "# PEP 758\nhttps://peps.python.org/pep-0758/\n\nAbstract\n--------\nexcept* without parens.",
        "what does this PEP propose?",
    ]


def test_fetch_asks_the_configured_model(web_harness: WebHarness) -> None:
    web_harness.set_jina(api_key=None)
    web_harness.set_model(model="deepseek-v4-pro", reasoning="high")
    web_harness.loopback.answer(_page(content="ok"))
    model = web_harness.script("ANSWER")

    assert web_harness.fetch([("https://example.com", "summarize")]) == ["ANSWER"]
    assert len(model.requests) == 1


def test_fetch_wraps_a_typed_provider_failure_keeping_its_cause(web_harness: WebHarness) -> None:
    web_harness.set_model(retry_attempts=0)
    web_harness.loopback.answer(_page(content="ok"))
    error = ModelRateLimitError("rate limit exceeded")
    model = web_harness.script(error)

    with pytest.raises(FetchError, match="rate limit") as raised:
        web_harness.fetch([("https://example.com", "summarize")])

    assert raised.value.__cause__ is error
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        TypeError("invalid callback input"),
        ValueError("invalid response"),
        RuntimeError("rate limit"),
    ],
)
def test_fetch_unknown_model_error_preserves_identity_without_retry(
    error: Exception, web_harness: WebHarness
) -> None:
    web_harness.loopback.answer(_page(content="ok"))
    model = web_harness.script(error)

    with pytest.raises(type(error)) as raised:
        web_harness.fetch([("https://example.com", "summarize")])

    assert raised.value is error
    assert len(model.requests) == 1


def test_fetch_model_build_error_becomes_a_fetch_error(
    web_harness: WebHarness,
) -> None:
    web_harness.loopback.answer(_page(content="ok"))
    web_harness.scripted_models.refuse(
        web_harness.fetch_deps.fetch_model.model, RuntimeError("DEEPSEEK_API_KEY not set")
    )

    with pytest.raises(FetchError, match="DEEPSEEK_API_KEY"):
        web_harness.fetch([("https://example.com", "summarize")])


def test_fetch_jina_business_error_raises_before_the_model(web_harness: WebHarness) -> None:
    web_harness.loopback.answer(
        json.dumps({"code": 422, "message": "Unable to fetch: 404 Not Found"}).encode()
    )
    model = web_harness.script("never asked")

    with pytest.raises(FetchError, match="Jina Reader fetch failed"):
        web_harness.fetch([("https://example.com/nonexistent", "summarize")])

    assert model.requests == []


def test_fetch_http_error_raises_with_the_upstream_body(web_harness: WebHarness) -> None:
    web_harness.loopback.answer(b"rate limit exceeded", status=429)

    with pytest.raises(FetchError, match="Jina Reader HTTP 429: rate limit exceeded"):
        web_harness.fetch([("https://example.com", "summarize")])


def test_fetch_network_error_raises(web_harness: WebHarness) -> None:
    web_harness.set_jina(reader_base=f"{web_harness.loopback.refused_url()}/")

    with pytest.raises(FetchError, match="Jina Reader connection failed"):
        web_harness.fetch([("https://example.com", "summarize")])


def test_fetch_malformed_json_raises(web_harness: WebHarness) -> None:
    web_harness.loopback.answer(b"not json")

    with pytest.raises(WebError, match="non-JSON"):
        web_harness.fetch([("https://example.com", "summarize")])


def test_fetch_a_wall_page_never_reaches_the_model(web_harness: WebHarness) -> None:
    web_harness.loopback.answer(
        _page(title="Just a moment...", content="Performing security verification")
    )
    model = web_harness.script("never asked")

    with pytest.raises(FetchError, match="bot-challenge or login wall"):
        web_harness.fetch([("https://example.com/some-article", "summarize")])

    assert model.requests == []


def _echo(messages: list[BaseMessage]) -> str:
    blocks: list[Any] = [block for message in messages for block in message.content]
    return " | ".join(block["text"] for block in blocks)


def test_fetch_batch_returns_results_in_input_order(web_harness: WebHarness) -> None:
    def answer(request: Received) -> Reply:
        site = request.target.split("https://", 1)[1]
        return Reply(_page(title=site, content=f"content of {site}"))

    web_harness.loopback.respond(answer)
    web_harness.script(_echo)

    sent = [("a.com", "summarize a"), ("b.com", "summarize b"), ("c.com", "summarize c")]
    results = web_harness.fetch([(f"https://{site}", prompt) for site, prompt in sent])

    for result, (site, prompt) in zip(results, sent, strict=True):
        assert f"content of {site}" in result
        assert prompt in result


def test_fetch_batch_runs_concurrently(web_harness: WebHarness) -> None:
    barrier = threading.Barrier(3, timeout=5)

    def answer(request: Received) -> Reply:
        barrier.wait()
        return Reply(_page(content="ok"))

    web_harness.loopback.respond(answer)
    web_harness.script("ANSWER")

    started = time.monotonic()
    results = web_harness.fetch(
        [("https://a.com", "p1"), ("https://b.com", "p2"), ("https://c.com", "p3")]
    )

    assert results == ["ANSWER"] * 3
    assert time.monotonic() - started < 3.0


def test_fetch_signature_includes_effort_and_max_concurrent() -> None:
    import inspect

    sig = inspect.signature(meiva.web.fetch)
    assert list(sig.parameters) == ["targets", "max_chars", "effort", "max_concurrent"]
    assert sig.parameters["effort"].default is None
    assert sig.parameters["max_concurrent"].default == DEFAULT_BATCH_MAX_CONCURRENT


def test_fetch_batch_empty_list(web_harness: WebHarness) -> None:
    assert web_harness.fetch([]) == []


def test_fetch_max_concurrent_validation(web_harness: WebHarness) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        web_harness.fetch([("https://example.com", "summarize")], max_concurrent=0)
    with pytest.raises(ValueError, match="at least 1"):
        web_harness.fetch([("https://example.com", "summarize")], max_concurrent=-3)
    with pytest.raises(TypeError, match="int or None"):
        web_harness.fetch(
            [("https://example.com", "summarize")],
            max_concurrent="2",  # type: ignore[arg-type]
        )


def test_fetch_max_concurrent_caps_inflight(web_harness: WebHarness) -> None:
    lock = threading.Lock()
    inflight = 0
    peak = 0

    def answer(request: Received) -> Reply:
        nonlocal inflight, peak
        with lock:
            inflight += 1
            peak = max(peak, inflight)
        time.sleep(0.05)
        with lock:
            inflight -= 1
        return Reply(_page(content="ok"))

    web_harness.loopback.respond(answer)
    web_harness.script("ANSWER")

    results = web_harness.fetch(
        [(f"https://{name}.com", "p") for name in "abcdef"], max_concurrent=2
    )

    assert results == ["ANSWER"] * 6
    assert peak == 2
