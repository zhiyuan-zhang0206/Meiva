"""The concrete logger adapter feeds its original event writer and filter."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any, cast

import pytest

from base import telemetry
from base.clock.tests.fakes import system_clock
from base.paths import meiva_home
from base.telemetry import process_logging as slog

_AGENT = 8901


_WRITTEN: list[telemetry.Event] = []


@pytest.fixture
def logging_pipeline() -> Iterator[None]:
    """The adapter test explicitly owns its event pipeline, whose writer is a list."""
    _WRITTEN.clear()
    pipeline = telemetry.EventPipeline(
        now=system_clock().now, writer=_WRITTEN.extend, flush_interval_s=0.01
    )
    try:
        telemetry.init_telemetry(home=meiva_home(), process="test-proc", pipeline=pipeline)
        yield
    finally:
        pipeline.stop(timeout=2)


# meiva:note
# `telemetry.flush()` is `Pipeline.sync(timeout=5.0)`: when the drain misses its deadline it
# returns `DrainStatus.UNFINISHED` and reports it, rather than raising. A read that follows a bare
# flush therefore cannot tell "the writer has not seen the event yet" from "the sink never saw the
# record" — the two read as the same zero rows (2026-11-07, #8601: one shard failed this way).
# Every read below proves the drain finished before it counts anything.
def _drained() -> None:
    result = telemetry.flush()
    assert result.status is telemetry.DrainStatus.COMPLETED, result


def _emitted_last(event_name: str, agent: int) -> dict[str, Any]:
    _drained()
    rows = [
        telemetry.event_payload(event)
        for event in list(_WRITTEN)
        if event.event_name == event_name and event.agent_id == agent
    ]
    assert rows, f"expected an event for {event_name}/{agent}, got none"
    return rows[-1]


def test_production_loguru_sink_routes_through_the_emitter() -> None:
    # meiva:note
    # `event_sink` must enqueue into the emitter (which batches), not write
    # straight to the DB per record — the regression that deleted batching before.

    import inspect

    src = inspect.getsource(slog.event_sink)
    assert "emit(" in src
    wiring = inspect.getsource(slog.add_event_sink)
    assert "init_telemetry(" in wiring
    assert "event_sink" in wiring  # the adapter is the loguru handler


def _rec(extra: dict[str, Any]) -> Any:
    """A minimal loguru-Record-shaped object — the filter reads only `extra`."""
    return {"extra": extra}


def test_add_event_sink_registers_at_most_once() -> None:
    """Two `add_event_sink` calls (exec_child's env + request init paths)
    must not double-register the adapter: one loguru record reaches the
    writer exactly once. The 2026-08-24 double registration wrote every
    post-init record twice — byte-identical rows, same surrogate id (task
    #1638)."""
    from base.log import logger as _g

    written: list[telemetry.Event] = []
    pipeline = telemetry.EventPipeline(
        now=system_clock().now, writer=written.extend, flush_interval_s=0.01
    )
    first = slog.add_event_sink(
        home=meiva_home(),
        process="test-dup-guard",
        producer=lambda: pipeline,
        machine_reader=lambda: "test-host",
    )
    second = slog.add_event_sink(
        home=meiva_home(),
        process="test-dup-guard",
        producer=lambda: pipeline,
        machine_reader=lambda: "test-host",
    )
    assert second == first, "repeat registration must return the live sink id"

    marker = f"dup-guard-{time.time_ns()}"
    try:
        _g.configure(extra={"agent_id": "8901"})
        _g.info(f"one record {marker}", event="label_change")
        for index in range(20):
            _g.info("bare info {} {}", marker, index)
        _g.debug("local debug {}", marker)
        _drained()

        rows: list[telemetry.Event] = []
        bare: list[str] = []
        for event in list(written):
            msg = str(event.attributes.get("msg", ""))
            if event.event_name == "label_change" and marker in msg:
                rows.append(event)
            if event.event_name == "log" and marker in msg:
                bare.append(msg)
        assert bare == [f"bare info {marker} {index}" for index in range(20)]
        assert len(rows) == 1, f"expected 1 event, got {len(rows)}"
    finally:
        pipeline.stop(timeout=2)
        _g.remove(first)


def test_event_pipeline_filter_drops_no_emitter_and_node_enter() -> None:
    """The pipeline filter admits ordinary records but drops two families:
    the emitter's own failure reports (`_no_emitter` marker — a failing-sink
    process must not loop failure → warning → emit) and `node_enter` (zero
    event-stream consumers; the row would be pure write amplification — the
    log-file line is the death-analysis source). Anything else — including
    `node_exit`, which agent_inspect reads — passes."""

    f = slog.event_pipeline_filter
    assert f(_rec({"event": "node_exit"}))
    assert f(_rec({"event": "llm_usage"}))
    assert f(_rec({}))
    assert not f(_rec({"event": "node_enter"}))
    assert not f(_rec({"_no_emitter": True}))
    assert not f(
        cast(Any, dict(_rec({}), name="opentelemetry.exporter.otlp.proto.http._log_exporter"))
    )
    # The node_enter drop is by event name only, not by message — a record
    # that merely *mentions* node_enter in its message still passes.
    assert f(_rec({"msg": "node_enter handling"}))
    # The OTel SDK's post-end warning (a deliberate consequence of ending the
    # claim node span early in claim_idle_wait_span) is dropped from the
    # table; a record merely mentioning the text still passes.
    assert not f(cast(Any, dict(_rec({}), message="Setting attribute on ended span.")))
    assert f(cast(Any, dict(_rec({}), message="Setting attribute on ended span. (context)")))


@pytest.mark.usefixtures("logging_pipeline")
def test_loguru_adapter_sets_source_from_extra() -> None:
    """`source=` on a logger call lands in the unified source field (default
    'system')."""
    from loguru import logger as _g

    from base.telemetry.process_logging import event_sink

    sink_id = _g.add(event_sink, level="INFO", enqueue=False, catch=False)
    try:
        _g.configure(extra={"agent_id": str(_AGENT)})
        _g.info("a line from the user path", event="label_change", source="user")
    finally:
        _g.remove(sink_id)
    obj = _emitted_last("label_change", _AGENT)
    assert obj["source"] == "user"
    assert obj["attributes"] == {"msg": "a line from the user path"}
    # loguru stamps record time in the machine's local zone; the emitter must
    # normalize it to UTC so the stream carries one offset (task #1638).
    assert datetime.fromisoformat(obj["ts"]).utcoffset() == timedelta(0)
