from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.tests.support import start


def turn(text: str, *, input_tokens: int, output_tokens: int, thinking: int) -> dict[str, object]:
    return {
        "steps": [
            {
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "agent_response",
                "text_delta": text,
            }
        ],
        "result": {
            "response": text,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "thinking_tokens": thinking,
                "cache_read_tokens": 5,
                "total_tokens": input_tokens + output_tokens + thinking,
            },
        },
    }


async def test_usage_accumulates_across_turns(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        turns=[
            turn("one", input_tokens=10, output_tokens=2, thinking=3),
            turn("two", input_tokens=20, output_tokens=4, thinking=1),
        ],
    )
    try:
        await harness.session.submit("a").outcome()
        await harness.session.submit("b").outcome()
        totals = harness.session.totals
        assert totals.turns == 2
        assert totals.input_tokens == 30
        assert totals.output_tokens == 10
        assert totals.cache_read_input_tokens == 10
        assert totals.cost_usd is None
    finally:
        await harness.stop()
