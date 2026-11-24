from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self
from urllib.parse import parse_qs, urlsplit

__all__ = ["LokiStub", "event_line"]


def event_line(
    ts: datetime,
    event_name: str,
    *,
    category: str = "telemetry",
    level: str = "info",
    agent_id: int | None = 7,
    machine: str = "m1",
    trace_id: str | None = None,
    attributes: dict[str, object] | None = None,
) -> tuple[int, str]:
    body = {
        "ts": ts.isoformat(),
        "trace_id": trace_id,
        "span_id": None,
        "agent_id": agent_id,
        "machine": machine,
        "cluster": ".meiva",
        "process": "test",
        "category": category,
        "event_name": event_name,
        "level": level,
        "source": "test",
        "target_agent_id": None,
        "attributes": attributes or {},
        "deployed_sha": "",
    }
    return int(ts.timestamp() * 1_000_000_000), json.dumps(body, separators=(",", ":"))


@dataclass
class _Script:
    lines: list[tuple[int, str]] = field(default_factory=list)
    counts: list[tuple[dict[str, str], int]] = field(default_factory=list)
    status: int = 200
    queries: list[dict[str, str]] = field(default_factory=list)


class LokiStub:
    def __init__(self) -> None:
        script = _Script()
        self.script = script

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _answer(self, status: int, body: dict[str, object]) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                url = urlsplit(self.path)
                params = {key: values[0] for key, values in parse_qs(url.query).items()}
                script.queries.append({"path": url.path, **params})
                if script.status != 200:
                    self._answer(script.status, {"status": "error"})
                    return
                if url.path == "/loki/api/v1/query_range":
                    start, end = int(params["start"]), int(params["end"])
                    inside = [line for line in script.lines if start <= line[0] < end]
                    inside.sort(key=lambda line: line[0], reverse=params["direction"] == "backward")
                    values = [[str(ns), body] for ns, body in inside][: int(params["limit"])]
                    streams: list[dict[str, object]] = (
                        [{"stream": {}, "values": values}] if values else []
                    )
                    self._answer(
                        200,
                        {"status": "success", "data": {"resultType": "streams", "result": streams}},
                    )
                    return
                result = [
                    {"metric": metric, "value": [int(params["time"]) / 1e9, str(count)]}
                    for metric, count in script.counts
                ]
                self._answer(
                    200,
                    {"status": "success", "data": {"resultType": "vector", "result": result}},
                )

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
