# meiva:note
# What rides a batch's first input (the handoff, the compaction reminder) counts as delivered only when that
# input is in the model's context: a refused input is handed over again with it, and an unacknowledged one
# is not followed by a second copy.
# meiva:link base/lm/harness/docs/delivery.md
from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Sequence

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness import (
    HarnessBridge,
    HarnessInput,
    HarnessNotDeliveredError,
    InputRejected,
    TextBlock,
)
from base.lm.harness.registration import SecureRandom
from base.lm.harness.tests.test_bridge import FakeSession, handoff, make_bridge


class RejectingFirstSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.rejected = False

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        if not self.rejected:
            self.rejected = True
            self.submitted.append((inputs[0].text, inputs[0].key, "refused"))
            self._queue.append(InputRejected(inputs[0].key, "no active turn", 1))
            return
        await super().deliver(inputs)


async def _until_events_are_read() -> None:
    await asyncio.sleep(0.05)


async def test_a_refused_first_input_is_handed_over_again_with_the_handoff() -> None:
    session = RejectingFirstSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)
    subscription = await bridge.subscribe()
    try:
        await bridge.deliver([HarnessInput.of("inbound-1", "hello")])
        await _until_events_are_read()
        await bridge.deliver([HarnessInput.of("inbound-1", "hello")])
        await _until_events_are_read()
        await bridge.deliver([HarnessInput.of("inbound-2", "later")])
    finally:
        subscription.close()

    texts = [text for text, _key, _turn in session.submitted]
    rendered = doc.render().strip()
    assert texts[0].startswith(rendered), "the refused delivery carried the handoff"
    assert texts[1].startswith(rendered), "so does the delivery that follows the refusal"
    assert texts[2] == "later", "once the input is in the context it is delivered and not repeated"


async def test_a_second_batch_before_the_first_is_acknowledged_does_not_repeat_the_handoff() -> (
    None
):
    session = FakeSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)

    await bridge.deliver([HarnessInput.of("inbound-1", "one")])
    await bridge.deliver([HarnessInput.of("inbound-2", "two")])

    texts = [text for text, _key, _turn in session.submitted]
    assert texts[0].startswith(doc.render().strip())
    assert texts[1] == "two"


async def test_a_session_that_cannot_start_hands_nothing_over() -> None:
    session = FakeSession()
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda _handle: (_ for _ in ()).throw(RuntimeError("session setup failed")),
        machine="macmini",
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    with pytest.raises(HarnessNotDeliveredError):
        await bridge.deliver([HarnessInput.of("inbound-1", "one")])

    assert session.submitted == []


async def test_the_handoff_goes_before_the_first_block_of_a_multi_block_input() -> None:
    session = RejectingFirstSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)
    subscription = await bridge.subscribe()
    try:
        blocks = (TextBlock("one"), TextBlock("two"))
        await bridge.deliver([HarnessInput("inbound-1", blocks)])
    finally:
        subscription.close()

    [(text, _key, _turn)] = session.submitted
    assert text.startswith(doc.render().strip()) and text.endswith("one\n\ntwo")
