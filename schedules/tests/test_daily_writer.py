from pathlib import Path
from unittest.mock import Mock

import pytest

import meiva
from base.cluster.machine import MachineIdentity
from base.daemon.schedules.inputs import ScheduleInputs
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry import EventPipeline
from schedules.entry import ScheduleEntryHooks, fresh_schedule_entry, schedule_entry


def test_quiet_schedule_exit_never_builds_a_writer() -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("quiet schedule must not start a writer")

    with fresh_schedule_entry(hooks=ScheduleEntryHooks(pipeline_builder=forbidden)) as inputs:
        assert callable(inputs.producer)
        assert meiva.context.require_machine() is inputs.machine


def test_schedule_closes_constructed_writer_and_preserves_loop_error() -> None:
    primary = RuntimeError("original schedule loop error")

    pipeline: EventPipeline | None = None
    prior = getattr(meiva, "context", None)
    with (
        pytest.raises(RuntimeError) as caught,
        fresh_schedule_entry() as inputs,
    ):
        pipeline = inputs.producer()
        assert inputs.producer() is pipeline
        assert meiva.context.require_machine() is inputs.machine
        raise primary
    assert caught.value is primary
    assert pipeline is not None and pipeline.stopped
    assert getattr(meiva, "context", None) is prior


def test_runner_inputs_are_borrowed_without_rebuilding_or_closing(
    machine_identity: MachineIdentity,
) -> None:
    database = Mock()
    producer = Mock()
    inputs = ScheduleInputs(
        database, producer, LoadedCommit(Path("/entry"), None), machine=machine_identity
    )
    prior = getattr(meiva, "context", None)
    primary = RuntimeError("borrowed loop failed")
    with pytest.raises(RuntimeError) as caught, schedule_entry(inputs) as borrowed:
        assert borrowed is inputs
        assert borrowed.database() is database.return_value
        assert getattr(meiva, "context", None) is prior
        raise primary
    assert caught.value is primary
    producer.assert_not_called()
    database.return_value.close.assert_not_called()
