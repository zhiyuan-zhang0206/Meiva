from __future__ import annotations

import json
import sys
from pathlib import Path

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from base.telemetry import process_name
from base.telemetry.otlp.telemetry_otlp_sdk import metric_views
from meiva.boot import bind_sdk_identity
from schedules.jobs.entry import open_schedule_entry


def main(home: Path, process: str | None) -> None:
    with open_schedule_entry(None, home=home, process=process):
        before_sdk = process_name()
        bind_sdk_identity(home)
        after_sdk = process_name()
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader], views=metric_views(after_sdk))
        try:
            provider.get_meter("meiva.telemetry").create_counter(
                "meiva_main_red_watch_round_rounds_total"
            ).add(1)
            exported = reader.get_metrics_data() is not None
        finally:
            provider.shutdown()
    sys.stdout.write(
        json.dumps({"before_sdk": before_sdk, "after_sdk": after_sdk, "exported": exported})
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else None)
