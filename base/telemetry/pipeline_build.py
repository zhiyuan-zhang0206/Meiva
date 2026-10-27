"""Compose the event pipeline's writer from its sinks, and close them at process exit.

The emitter (`base.telemetry.emitter`) only enqueues into whatever pipeline a root binds; this
module is where the sinks meet: the JSONL mirror, the `telemetry_events` store, the
observed-metrics projection and the OTLP exporter. It sits above all of them, so none of them
needs to import the emitter's composition.
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from pathlib import Path
from typing import Any

from base.cluster.machine import MachineIdentityReader
from base.paths import logs_path
from base.telemetry.delivery.mirror import append_jsonl
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event
from base.telemetry.delivery.reporting import failure_isolated, report_no_pipeline
from base.telemetry.delivery.state import process_state


def write_batch(
    events: list[Event],
    *,
    database: Callable[[], Any],
    export: Callable[[list[Event]], None],
    target_dir: Path,
) -> None:
    """Mirror first, then the telemetry_events record, the compact metric projection
    and OTLP export.

    Persistence and exporter transport failures retain their sink isolation.
    Required identity defects propagate to the writer's failure receipt. The
    projection cannot turn an observation into a billing proof.
    """
    if not events:
        return
    append_jsonl(events, target_dir=target_dir)
    with failure_isolated("telemetry_events store"):
        from base.telemetry.event_store import store_events

        store_events(database(), events)
    try:
        from base.telemetry.metrics.observed_metrics import project_events

        project_events(database(), events)
    except Exception as exc:
        report_no_pipeline("[observed-metrics] sink unavailable: {err}", err=repr(exc))
    export(events)


# meiva:note
# Own the writer and lazy exporter with the root's database and machine.
#
# `flush_interval_s` is how long the drain thread holds a partial batch before it writes it. The
# mirror and the exporter's loss reports land under `home`, so `home` must be absolute: a relative
# one would put them under whatever directory the process happens to be in. Raises ValueError when
# it is not.
def build_pipeline(
    *,
    database: Callable[[], Any],
    machine: MachineIdentityReader,
    home: Path,
    flush_interval_s: float = 0.5,
) -> EventPipeline:
    if not home.is_absolute():
        raise ValueError(f"the event pipeline needs an absolute home, got {str(home)!r}")
    from base.host.env.registry import process_environment
    from base.telemetry.observability import endpoint_override_is_explicit
    from base.telemetry.otlp.telemetry_otlp import default_exporter, observability_export_allowed
    from base.telemetry.otlp.telemetry_otlp_exporting import ExportingEventPipeline

    # meiva:note
    # The process environment is read once, here: every daemon builds its pipeline at its
    # composition root, after its boot loaded the `.env` it will run with.
    explicit = endpoint_override_is_explicit("MEIVA_TELEMETRY_OTLP_ENDPOINT", process_environment())
    mirror_dir = logs_path(home)
    exporter = default_exporter(
        export_allowed=lambda: observability_export_allowed(
            machine, home=home, endpoint_explicit=explicit
        ),
        mirror_dir=mirror_dir,
    )
    return ExportingEventPipeline(
        writer=lambda events, owned: write_batch(
            events,
            database=database,
            export=owned.export_batch,
            target_dir=mirror_dir,
        ),
        exporter=exporter,
        flush_interval_s=flush_interval_s,
    )


def drain_on_exit() -> None:
    """Close ordinary admission and finitely join its single writer at normal exit.

    The ordered exit seam closes downstream sinks only after the writer finishes.
    Providers leave shutdown to their worker; an unfinished writer is reported;
    process exit may shed its ordinary tail. ``hard_exit`` bypasses this hook.
    """
    pipeline = process_state["pipeline"]
    if pipeline is None:
        return
    result = pipeline.stop()
    if result.status is DrainStatus.UNFINISHED:
        return  # the owned writer still uses these sinks; process exit sheds its tail
    with failure_isolated("close observed-metrics projection"):
        from base.telemetry.metrics.observed_metrics import close_projection

        close_projection()
    with failure_isolated("close telemetry_events store"):
        from base.telemetry.event_store import close_store

        close_store()


atexit.register(drain_on_exit)
