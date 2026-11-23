from __future__ import annotations

import sys
from pathlib import Path
from time import sleep

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from base.telemetry.otlp.trace_workers import ExecTraceDrain
from base.telemetry.tracing import OtlpJsonHttpSpanExporter


def main(endpoint: str, ready: Path) -> None:
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(
        BatchSpanProcessor(
            OtlpJsonHttpSpanExporter(endpoint, strip_content=True), max_export_batch_size=1
        )
    )
    drain = ExecTraceDrain(provider.shutdown)
    with provider.get_tracer("exec-test").start_as_current_span("probe"):
        pass
    while not ready.exists():
        sleep(0.01)
    drain.close()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))
