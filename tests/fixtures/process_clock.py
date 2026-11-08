# meiva:note
# Give every test process an event clock.
#
# An event emitted with no `ts` of its own is stamped with the process clock the boot seam binds
# (`base.telemetry.emitter._process_now`), and a pytest process runs no boot seam: a test that
# stages an audit event before its database write would otherwise fail before it reaches what it
# proves. The fixture binds the real clock for the test and puts the previous binding back
# afterwards, so a test that binds its own clock (`init_telemetry`, `bind_identity`) still wins, and
# a test that asserts the unbound failure clears the slot itself.
#
# It is function-scoped autouse and touches no other slot: identity, pipeline and machine stay what
# each test makes them, and the leak guard compares none of this module state.

from __future__ import annotations

from collections.abc import Iterator

import pytest

__all__ = ["process_event_clock"]


@pytest.fixture(autouse=True)
def process_event_clock() -> Iterator[None]:
    from base import telemetry
    from base.clock.tests.fakes import system_clock

    previous = telemetry.process_state["now"]
    if previous is None:
        telemetry.process_state["now"] = system_clock().now
    try:
        yield
    finally:
        telemetry.process_state["now"] = previous
