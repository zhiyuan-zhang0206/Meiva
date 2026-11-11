from collections.abc import Iterator

import pytest

from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.log.process import release_process_logging
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment
from meiva.tests.fixtures.sdk import sdk_identity as sdk_identity
from meiva.tests.fixtures.sdk import sdk_metering as sdk_metering


# meiva:note
# A schedule round that emits a system event opens the process's log sinks first
# (`schedules.jobs.telemetry_event.emit_system_event` runs `init_gateway_process`), and a schedule is
# its own process: production keeps those sinks until the process exits, so nothing releases them.
# A test that runs a round in-process therefore leaves a stderr sink bound to the stream that
# test's `capsys` replaced, and the process initialization guard stays set. Every later test in the
# worker then writes through a sink on a closed file: loguru reports "I/O operation on closed
# file" on stderr, and any test that reads stderr fails (CI run 42951791313, Loguru handler 1).
# Releasing what the test opened puts the worker back where it was.
@pytest.fixture(autouse=True)
def _release_process_logging() -> Iterator[None]:
    yield
    release_process_logging()
