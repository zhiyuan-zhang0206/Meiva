"""Compose the event pipeline's writer from its one sink, the OTLP exporter, and close it at exit.

The emitter (`base.telemetry.emitter`) only enqueues into whatever pipeline a root binds; this
module builds the pipeline a root binds. Loki and Prometheus, reached through the exporter, are the
only stores of a telemetry or log event: nothing is written to Postgres or to a local file.
"""

from __future__ import annotations

import atexit
import random
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from base.clock.ids import new_uuid
from base.cluster.machine import MachineIdentityReader
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.state import process_state


# meiva:note
# Own the writer and lazy exporter with the root's machine.
#
# `flush_interval_s` is how long the drain thread holds a partial batch before it writes it. The
# exporter derives the process's resource identity from `home`, so `home` must be absolute: a
# relative one would name whatever directory the process happens to be in. Raises ValueError when
# it is not.
#
# `now` is the clock this stream stamps with — the loss alarm, and the default timestamp of an
# event whose producer passed none. It is the process clock the root already builds, read once
# there and handed down: the stream has exactly one time source, and a module below this one never
# reaches for the host clock.
def build_pipeline(
    *,
    machine: MachineIdentityReader,
    home: Path,
    now: Callable[[], datetime],
    otlp_enabled: Callable[[], bool],
    otlp_endpoint: Callable[[], str],
    flush_interval_s: float = 0.5,
    rng: random.Random,
) -> EventPipeline:
    if not home.is_absolute():
        raise ValueError(f"the event pipeline needs an absolute home, got {str(home)!r}")
    # meiva:note
    # The `service.instance.id` this process's metrics carry: minted here, once, from the generator
    # the root hands in, and then carried as a value -- a second provider built in this process
    # reuses it rather than acquiring a second identity.
    instance_id = str(new_uuid(rng))
    from base.host.env.registry import process_environment
    from base.telemetry.observability import (
        endpoint_override_is_explicit,
        observability_export_allowed,
    )
    from base.telemetry.otlp.telemetry_otlp import default_exporter
    from base.telemetry.otlp.telemetry_otlp_exporting import ExportingEventPipeline

    # meiva:note
    # The process environment is read once, here: every daemon builds its pipeline at its
    # composition root, after its boot loaded the `.env` it will run with.
    explicit = endpoint_override_is_explicit("MEIVA_TELEMETRY_OTLP_ENDPOINT", process_environment())
    exporter = default_exporter(
        export_allowed=lambda: observability_export_allowed(
            machine, home=home, endpoint_explicit=explicit
        ),
        enabled=otlp_enabled,
        endpoint=otlp_endpoint,
        home=home,
        now=now,
        instance_id=instance_id,
    )
    return ExportingEventPipeline(
        writer=lambda events, owned: owned.export_batch(events),
        exporter=exporter,
        now=now,
        flush_interval_s=flush_interval_s,
    )


def drain_on_exit() -> None:
    """Close ordinary admission and finitely join its single writer at normal exit.

    The pipeline closes the exporter only after the writer finishes. Providers leave shutdown
    to their worker; an unfinished writer is reported; process exit may shed its ordinary
    tail. ``hard_exit`` bypasses this hook.
    """
    pipeline = process_state["pipeline"]
    if pipeline is None:
        return
    pipeline.stop()


atexit.register(drain_on_exit)
