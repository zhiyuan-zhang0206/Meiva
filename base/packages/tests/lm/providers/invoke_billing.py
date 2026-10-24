from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from base.lm.call import invoke_text
from base.lm.catalog import ModelCatalog
from base.telemetry.tests.capture.armed_tracing import BillingRuntime


class CompletionReply(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert self.path == "/chat/completions"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert body["model"] == "deepseek-v4-pro"
        assert body["messages"][0]["content"] == [{"type": "text", "text": "q"}]
        data = json.dumps(
            {
                "id": "chatcmpl-invoke",
                "object": "chat.completion",
                "created": 1,
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 20},
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return None


def invoke(tracer: BillingRuntime, catalog: ModelCatalog) -> None:
    with HTTPServer(("127.0.0.1", 0), CompletionReply) as server, ThreadPoolExecutor(1) as pool:
        serving = pool.submit(server.serve_forever, poll_interval=0.01)
        try:
            model = ChatOpenAI(
                model="deepseek-v4-pro",
                api_key=SecretStr("fixture-token"),
                base_url=f"http://127.0.0.1:{server.server_port}",
                max_retries=0,
                timeout=5,
            )
            assert (
                invoke_text(
                    model,
                    [{"type": "text", "text": "q"}],
                    desc="local provider",
                    error_type=ValueError,
                    catalog=catalog,
                )
                == "answer"
            )
        finally:
            server.shutdown()
            serving.result(timeout=5)
    [span] = tracer.spans
    attributes = span.attributes
    assert attributes is not None and span.end_time is not None
    assert attributes["meiva.billing.vendor"] == "deepseek"
    assert attributes["meiva.billing.usage_kind"] == "chat"
    assert attributes["meiva.billing.tokens_in"] == 100
    assert attributes["meiva.billing.cache_read_tokens"] == 20
