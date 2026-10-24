# meiva:note
# This process owns the real SDK and a local Gemini HTTP endpoint. The provider's production
# transport, response parsing and retry policy produce the observed billing spans.
from __future__ import annotations

import asyncio
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, cast

import loguru
from opentelemetry.sdk.trace import ReadableSpan

from base.lm.pricing import PriceBook, quote
from base.log import logger
from base.packages.plugin_providers import build_model_catalog
from base.telemetry.tests.capture.armed_tracing import armed_billing
from services.derived.memory_indexer.embeddings.base import EmbeddingAPIError
from services.derived.memory_indexer.embeddings.gemini import (
    DIM,
    EMBED_POLICY,
    MODEL_ID,
    GeminiEmbeddingProvider,
)


class EmbeddingReply(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        server = cast(EmbeddingWire, self.server)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ordinal = len(server.receipts())
        (server.directory / f"{ordinal}.json").write_text(
            json.dumps(
                {
                    "path": self.path,
                    "body": body,
                    "key": self.headers["x-goog-api-key"],
                }
            )
        )
        status = server.statuses[min(ordinal, len(server.statuses) - 1)]
        payload: dict[str, Any] = {"embeddings": [{"values": [1.0] * DIM}]}
        if server.with_usage:
            payload["usageMetadata"] = {"promptTokenCount": 123}
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return None


class EmbeddingWire(HTTPServer):
    def __init__(self, directory: Path, statuses: tuple[int, ...], *, with_usage: bool) -> None:
        self.directory, self.statuses, self.with_usage = directory, statuses, with_usage
        directory.mkdir()
        super().__init__(("127.0.0.1", 0), EmbeddingReply)

    def receipts(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text()) for p in sorted(self.directory.glob("*.json"))]


def exercise_provider(provider: GeminiEmbeddingProvider, name: str) -> None:
    if name == "failed":
        try:
            provider.embed_batch(["hello"])
        except EmbeddingAPIError as exc:
            assert "failed after" in str(exc)
        else:
            raise AssertionError("the failed provider call returned normally")
    elif name == "empty":
        assert provider.embed_batch([]).shape == (0, DIM)
    elif name == "async":
        assert asyncio.run(provider.embed_query_async("hello")).shape == (DIM,)
    else:
        assert provider.embed_batch(["hello"]).shape == (1, DIM)


def verify_receipts(receipts: list[dict[str, Any]], name: str) -> None:
    expected_count = {"empty": 0, "failed": EMBED_POLICY.max_attempts, "retry": 3}.get(name, 1)
    assert len(receipts) == expected_count
    for receipt in receipts:
        assert receipt["path"] == "/embed" and receipt["key"] == "fixture-gemini-key"
        [request] = receipt["body"]["requests"]
        assert request["content"]["parts"] == [{"text": "hello"}]
        assert request["model"] == f"models/{MODEL_ID}"


def verify_billing_span(span: ReadableSpan, name: str, prices: PriceBook) -> None:
    attributes = span.attributes
    assert attributes is not None and span.end_time is not None
    tok_in = 0 if name in ("missing", "retry") else 123
    priced = quote(MODEL_ID, tok_in, 0, 0, prices=prices)
    cost = 0.0 if priced is None else priced.cost_usd
    assert attributes["meiva.billing.vendor"] == "google"
    assert attributes["meiva.billing.model"] == MODEL_ID
    assert attributes["meiva.billing.tokens_in"] == tok_in
    assert attributes["meiva.billing.tokens_out"] == 0
    assert attributes["meiva.billing.usage_kind"] == "embedding"
    assert attributes["meiva.billing.cost"] == round(cost, 6)
    assert (attributes.get("meiva.billing.unpriced") is True) == (name == "unpriced")


def verify_usage_event(messages: list[loguru.Message], name: str, prices: PriceBook) -> None:
    tok_in = 0 if name in ("missing", "retry") else 123
    priced = quote(MODEL_ID, tok_in, 0, 0, prices=prices)
    cost = 0.0 if priced is None else priced.cost_usd
    [event] = [m.record for m in messages if m.record["extra"].get("event") == "llm_usage"]
    assert event["extra"]["usage_kind"] == "embedding"
    assert event["extra"].get("unpriced", 0) == (1 if name == "unpriced" else 0)
    if name != "unpriced":
        assert event["extra"]["cost_usd"] == cost


def scenario(name: str, directory: Path) -> None:
    catalog = build_model_catalog()
    if name == "unpriced":
        catalog = replace(catalog, prices=PriceBook({}, {}))
    statuses = (503, 503, 200) if name == "retry" else ((503,) if name == "failed" else (200,))
    with (
        armed_billing() as tracer,
        EmbeddingWire(directory, statuses, with_usage=name not in ("missing", "retry")) as server,
        ThreadPoolExecutor(1) as pool,
    ):
        serving = pool.submit(server.serve_forever, poll_interval=0.01)
        messages: list[loguru.Message] = []
        sink = logger.add(messages.append)
        try:
            provider = GeminiEmbeddingProvider(
                catalog,
                timeout_s=5,
                api_key="fixture-gemini-key",
                endpoint=f"http://127.0.0.1:{server.server_port}/embed",
            )
            exercise_provider(provider, name)
        finally:
            logger.remove(sink)
            server.shutdown()
            serving.result(timeout=5)
        verify_receipts(server.receipts(), name)
        if name in ("empty", "failed"):
            assert tracer.spans == []
            return
        [span] = tracer.spans
        verify_billing_span(span, name, catalog.prices)
        verify_usage_event(messages, name, catalog.prices)


if __name__ == "__main__":
    name = sys.argv[1]
    if name not in ("priced", "unpriced", "missing", "async", "empty", "retry", "failed"):
        raise ValueError(f"Unknown embedding billing scenario: {name!r}")
    scenario(name, Path(sys.argv[2]))
