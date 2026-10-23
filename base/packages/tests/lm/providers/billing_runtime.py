# meiva:note
# Traceloop owns one provider per process; exercise its real arm rather than reset SDK globals.
# meiva:link base/packages/tests/lm/docs/contracts.meiva.okf.md

import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from base.paths import meiva_home
from base.telemetry import tracing


class CollectorHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        assert self.path == "/v1/traces"
        self.rfile.read(int(self.headers["Content-Length"]))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return None


class CollectorServer(HTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        failure = sys.exception()
        assert failure is not None
        raise failure


@dataclass(frozen=True)
class BillingRuntime:
    provider: TracerProvider
    exporter: InMemorySpanExporter

    @property
    def spans(self) -> list[ReadableSpan]:
        return [
            span
            for span in self.exporter.get_finished_spans()
            if span.instrumentation_scope is not None
            and span.instrumentation_scope.name == "meiva.billing"
        ]


def stop_provider(provider: TracerProvider | None) -> None:
    if provider is None:
        return
    try:
        assert provider.force_flush(timeout_millis=5_000)
    finally:
        provider.shutdown()


@contextmanager
def armed_billing() -> Generator[BillingRuntime]:
    with (
        CollectorServer(("127.0.0.1", 0), CollectorHandler) as collector,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="billing-collector") as pool,
    ):
        serving = pool.submit(collector.serve_forever, poll_interval=0.01)
        provider: TracerProvider | None = None
        try:
            tracing.arm_tracing(
                f"http://127.0.0.1:{collector.server_port}",
                production=False,
                home=meiva_home(),
                strip_content=False,
            )
            assert tracing.is_initialized(), "the real tracing arm must succeed"
            active_provider = trace.get_tracer_provider()
            assert isinstance(active_provider, TracerProvider)
            provider = active_provider
            exporter = InMemorySpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(exporter))
            yield BillingRuntime(provider, exporter)
        finally:
            try:
                try:
                    assert tracing.shutdown() == ()
                finally:
                    stop_provider(provider)
            finally:
                collector.shutdown()
                serving.result(timeout=5)
