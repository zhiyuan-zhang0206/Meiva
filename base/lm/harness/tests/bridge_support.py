# meiva:note
# The old single-turn call, rebuilt on the engine's delivery interface for the bridge's own tests:
# subscribe first, deliver one input, and read events until a turn completes. It also does what the
# graph does on a failure (retire a session that faulted without an outcome or closed), so the
# bridge's lifecycle can be asserted without the graph.
# meiva:link base/lm/harness/docs/delivery.md
from __future__ import annotations

from base.lm.harness import (
    AssistantText,
    HarnessBridge,
    HarnessClosedError,
    HarnessInput,
    HarnessSetupError,
    HarnessTurn,
    HarnessTurnFaultedError,
    TurnCompleted,
    completed_turn,
    fault_of_event,
)


async def run_turn(bridge: HarnessBridge, text: str, *, key: str) -> HarnessTurn:
    try:
        subscription = await bridge.subscribe()
    except (HarnessClosedError, HarnessSetupError):
        await bridge.retire("session unavailable")
        raise
    try:
        try:
            await bridge.deliver([HarnessInput(key=key, text=text)])
        except (HarnessClosedError, HarnessSetupError):
            await bridge.retire("session unavailable")
            raise
        except Exception as exc:
            await bridge.retire("session unavailable")
            raise HarnessSetupError(f"{bridge.harness.value} refused the input: {exc}") from exc
        streamed: dict[str, list[str]] = {}
        while (event := await subscription.get()) is not None:
            if isinstance(event, AssistantText):
                streamed.setdefault(event.turn_id, []).append(event.text)
            fault = fault_of_event(bridge.harness, event)
            if fault is not None:
                if not isinstance(fault, HarnessTurnFaultedError) or fault.outcome is None:
                    await bridge.retire("session unavailable")
                raise fault
            if isinstance(event, TurnCompleted):
                return completed_turn(event, "".join(streamed.get(event.turn_id, [])))
        raise HarnessClosedError("harness event stream ended before the turn completed")
    finally:
        subscription.close()
