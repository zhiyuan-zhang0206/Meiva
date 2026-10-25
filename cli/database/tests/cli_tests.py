# meiva:note
# Shared fixtures for the CLI tests (registered by `tests/fixtures/path_scopes.py`).
#
# Production service and OS job boundaries remain guarded by `cli/conftest.py`
# and the root fixtures.

from __future__ import annotations

from collections.abc import Callable, Generator

import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import as_machine as as_machine
from base.paths import meiva_home
from base.sessions.pty.tests.pty_reaper import pty_reaper as pty_reaper
from base.telemetry import EventPipeline, process_logging
from cli.database import (
    OperatorDatabaseFactory,
    OperatorEventPipeline,
    open_operator_database,
    operator_database_factory,
    operator_event_pipeline,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime


@pytest.fixture
def operator_database(command_runtime: CommandRuntime) -> OperatorDatabaseFactory:
    """Borrow one real operator factory for this case's explicit CLI inputs."""
    return operator_database_factory(
        local_host=command_runtime.machine.host,
        open_database=open_operator_database,
    )


@pytest.fixture
def operator_pipeline(
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
) -> Generator[OperatorEventPipeline]:
    """Borrow one lazy writer for the same command's work and reporter inputs."""
    producer = operator_event_pipeline(
        operator_database, machine=command_runtime.machine, home=meiva_home()
    )
    try:
        yield producer
    finally:
        producer.close()


@pytest.fixture
def installed_machine_identity(command_runtime: CommandRuntime) -> MachineIdentityReader:
    """Give this case's explicit owner the provenance of an installed unit."""
    command_runtime.machine.set(name="unit-test-machine")
    return command_runtime.machine


@pytest.fixture(autouse=True)
def cli_log_sinks(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """In-process dispatch opens no real loguru sinks; the names asked for are recorded."""
    opened: list[str] = []

    def record(
        *, name: str, producer: Callable[[], EventPipeline], machine_reader: Callable[[], str]
    ) -> None:
        opened.append(name)

    monkeypatch.setattr(process_logging, "init_cli_process", record)
    return opened
