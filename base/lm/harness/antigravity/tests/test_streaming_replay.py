from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.harness.antigravity.tests.fake_replay import STREAMING
from base.lm.harness.antigravity.tests.support import start
from base.lm.harness.delivery import HarnessInput, TextBlock
from base.lm.harness.events import AssistantText, TextDelta, TurnCompleted


def _recorded_replies(scenario: str) -> list[str]:
    rows = [
        json.loads(line)["row"]
        for line in (STREAMING / scenario / "record.jsonl").read_text().splitlines()
    ]
    return [
        row["content"].strip()
        for row in rows
        if row["type"] == "PLANNER_RESPONSE" and row.get("content")
    ]


@pytest.mark.parametrize("scenario", ["text_only", "tool_call"])
async def test_streamed_chunks_make_up_the_recorded_reply(tmp_path: Path, scenario: str) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.deliver([HarnessInput("one", (TextBlock(f"replay-{scenario}"),))])
        await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        deltas = [event for event in harness.events if isinstance(event, TextDelta)]
        assert deltas
        assert all(event.block is None for event in deltas)
        recorded = _recorded_replies(scenario)
        assert "".join(event.text for event in deltas).strip() == recorded[-1]
        assert [
            event.text for event in harness.events if isinstance(event, AssistantText)
        ] == recorded
    finally:
        await harness.stop()
