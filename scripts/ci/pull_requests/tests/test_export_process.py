import ast
import inspect
import textwrap
from datetime import UTC, datetime

import pytest

from base.clock.tests.fakes import system_clock
from base.telemetry import Event, EventPipeline
from scripts.ci.pull_requests import export_process


def test_the_writer_is_joined_when_its_body_ends() -> None:
    with export_process.drain_event_pipeline(
        "exporter", EventPipeline(now=system_clock().now, writer=lambda _events: None)
    ) as pipeline:
        assert not pipeline.stopped

    assert pipeline.stopped


def test_body_error_preserves_the_original_writer_error_during_close() -> None:
    primary = ValueError("export failed")
    worker_error = OSError("writer failed")

    def write(_events: list[Event]) -> None:
        raise worker_error

    pipeline = EventPipeline(now=system_clock().now, writer=write, batch_size=1)
    event = Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="exporter",
        category="telemetry",
        event_name="pr_flow_daily",
        level="info",
        source="test",
        target_agent_id=None,
    )
    with (
        pytest.raises(ValueError) as caught,
        export_process.drain_event_pipeline("exporter", pipeline) as owned,
    ):
        owned.enqueue(event)
        raise primary
    assert caught.value is primary and pipeline.stopped
    assert any("writer failed" in note for note in primary.__notes__)
    with pytest.raises(OSError) as worker:
        pipeline.stop(timeout=0)
    assert worker.value is worker_error


# meiva:note
# A shape lock: the writer is built from the home it is given, reads no `meiva_home`, and holds no
# database or image to gate one.
def test_the_owned_writer_takes_the_home_it_is_given_and_reads_none() -> None:
    source = inspect.getsource(export_process.owned_event_pipeline)
    function = ast.parse(textwrap.dedent(source)).body[0]
    assert isinstance(function, ast.FunctionDef)
    names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
    assert "meiva_home" not in names
    homes = [
        keyword.value
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "home"
    ]
    assert len(homes) == 1
    assert all(isinstance(value, ast.Name) and value.id == "home" for value in homes)
    assert [arg.arg for arg in function.args.kwonlyargs] == ["home"]
