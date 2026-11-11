# meiva:note
# The bridge's part in reconciliation: the generation of the process it runs, the durable record asked
# of the live session or of the one just retired (the record is a file, so no CLI is needed), and the
# adoption of inputs the harness will deliver by itself.
# meiva:link base/lm/harness/docs/delivery.md

from __future__ import annotations

import pytest

from base.lm.harness import HarnessClosedError, Reconciliation, ReconciliationUnavailableError
from base.lm.harness.tests.test_bridge import FakeSession, make_bridge


async def test_the_bridge_reports_the_generation_of_its_live_session() -> None:
    session = FakeSession()
    session.generation = 4
    bridge = make_bridge(session)

    assert bridge.generation == 0, "no process yet"
    _ = await bridge.subscribe()
    assert bridge.generation == 4
    await bridge.close()


async def test_the_record_is_asked_of_the_live_session() -> None:
    bridge = make_bridge(FakeSession())
    _ = await bridge.subscribe()

    assert await bridge.reconcile(["a", "b"]) == dict.fromkeys("ab", Reconciliation.NOT_FOUND)
    await bridge.close()


async def test_the_record_is_still_asked_after_the_session_was_retired() -> None:
    bridge = make_bridge(FakeSession())
    _ = await bridge.subscribe()
    await bridge.retire(reason="the run failed")

    assert bridge.generation == 0
    assert await bridge.reconcile(["a"]) == {"a": Reconciliation.NOT_FOUND}
    await bridge.close()


async def test_a_bridge_that_never_started_a_session_has_no_record_to_ask() -> None:
    bridge = make_bridge(FakeSession())

    with pytest.raises(ReconciliationUnavailableError):
        _ = await bridge.reconcile(["a"])


async def test_adopted_inputs_reach_the_live_session_and_need_one() -> None:
    session = FakeSession()
    bridge = make_bridge(session)
    with pytest.raises(HarnessClosedError):
        bridge.adopt(["a"])

    _ = await bridge.subscribe()
    bridge.adopt(["a", "b"])

    assert session.adopted == ["a", "b"]
    await bridge.close()
