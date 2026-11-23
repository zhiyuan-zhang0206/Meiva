"""Queue-loss diagnostics that never re-enter a saturated event queue."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

from base.log import logger
from base.telemetry.delivery.mirror import append_jsonl
from base.telemetry.delivery.receipts import Event
from base.telemetry.delivery.state import process_name, process_state, resolve_machine


def _stderr(message: str) -> None:
    """Write one diagnostic line to stderr, the channel that needs no configured sink.

    A closed or detached stderr (`OSError` / `ValueError`) is the one expected
    failure: a daemon whose stderr is gone has no further channel to report on.
    """
    try:
        sys.stderr.write(message + "\n")
    except (OSError, ValueError):
        return


# meiva:note
# Preserve the affected process identity and mark actual data loss as error.
#
# `now` is the instant the loss is reported, handed in by the reporter that observed it: the
# pipeline's own clock for a shed record, the SDK's log record for the exporter's own overflow.
# The module reads no clock of its own: a loss report is a fact about somebody else's batch, and
# the reporter is the one holding both the batch and the time.
def loss_event(
    event: Event, count: int, queue_name: str, *, now: datetime, dropped_at: datetime | None = None
) -> Event:
    return replace(
        event,
        ts=now,
        agent_id=None,
        trace_id=None,
        span_id=None,
        category="telemetry",
        event_name="event_log_drop",
        level="error",
        attributes={
            "n": count,
            "queue": queue_name,
            "last_dropped_at": (dropped_at or now).timestamp(),
        },
    )


def report_loss(
    event: Event,
    count: int,
    queue_name: str,
    *,
    now: datetime,
    record_metric: Callable[[Event], None] | None = None,
) -> Event:
    """File/stderr diagnostic plus an independent metric; neither uses a log queue."""
    report = loss_event(event, count, queue_name, now=now)
    # Always available, even before logging/provider initialization in bare scripts.
    # This exceptional data-loss diagnostic must not depend on any configured sink.
    _stderr(f"ERROR: Telemetry queue {queue_name} full: lost {count} event(s)")
    try:
        logger.bind(_no_emitter=True).error(
            "Telemetry queue {queue} full: lost {n} event(s); event history is incomplete",
            queue=queue_name,
            n=count,
        )
    except Exception as exc:
        _stderr(f"ERROR: Telemetry loss log for queue {queue_name} failed: {exc!r}")
    if record_metric is not None:
        metric_report = report
        if queue_name == "emitter":
            attributes = dict(report.attributes)
            attributes.pop("n", None)
            metric_report = replace(report, attributes=attributes)
        try:
            record_metric(metric_report)
        except Exception as exc:
            _stderr(f"ERROR: Telemetry loss metric for queue {queue_name} failed: {exc!r}")
    return report


class ExporterDropObserver(logging.Filter):
    """Count SDK log evictions on admission and publish one report per export."""

    def __init__(self, *, mirror_dir: Path) -> None:
        super().__init__()
        self._mirror_dir = mirror_dir
        self._lock = Lock()
        self._pending = 0
        self._last_dropped_at = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg != "Queue full, dropping %s." or record.args != ("Log",):
            return True
        with self._lock:
            self._pending += 1
            self._last_dropped_at = max(self._last_dropped_at, record.created)
        return False

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            count, self._pending = self._pending, 0
            now = datetime.fromtimestamp(self._last_dropped_at, UTC)
        try:
            state = process_state
            event = Event(
                ts=now,
                trace_id=None,
                span_id=None,
                agent_id=None,
                machine=state["machine"] or resolve_machine(),
                cluster=state["cluster"] or "",
                process=process_name(),
                category="telemetry",
                event_name="event_log_drop",
                level="error",
                source="system",
                target_agent_id=None,
            )
            pipeline = state["pipeline"]
            report = report_loss(
                event,
                count,
                "otel-sdk",
                now=now,
                record_metric=pipeline.record_export_loss if pipeline is not None else None,
            )
            append_jsonl([report], target_dir=self._mirror_dir, now=now)
        except Exception as exc:
            _stderr(f"ERROR: OTel SDK queue-overflow loss could not be recorded: {exc!r}")


def install_exporter_drop_observer(*, mirror_dir: Path) -> ExporterDropObserver:
    """Attach once before the SDK's duplicate log filter, so every eviction counts."""
    sdk_logger = logging.getLogger("opentelemetry.sdk._shared_internal")
    for item in sdk_logger.filters:
        if isinstance(item, ExporterDropObserver):
            item._mirror_dir = mirror_dir
            return item
    observer = ExporterDropObserver(mirror_dir=mirror_dir)
    sdk_logger.filters.insert(0, observer)
    return observer
