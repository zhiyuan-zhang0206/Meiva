from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.lm.harness.claude.mapping import StreamedMessage, map_message
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.events import ReasoningDelta, TextDelta

FIXTURES = Path(__file__).parent / "fixtures" / "streaming"


def _streamed(scenario: str) -> dict[str, str]:
    streamed, blocks = StreamedMessage(), dict[str, str]()
    for line in (FIXTURES / scenario / "stream.jsonl").read_text().splitlines():
        entry = object_of(json.loads, line)
        event = entry.get("event") if is_object_dict(entry) else None
        if not is_object_dict(event):
            continue
        for mapped in map_message(fake.StreamEvent(event=event), turn_id="t1", streamed=streamed):
            assert isinstance(mapped, TextDelta | ReasoningDelta) and mapped.block is not None
            blocks[mapped.block] = blocks.get(mapped.block, "") + mapped.text
    return blocks


def _recorded(scenario: str) -> dict[str, str]:
    blocks = dict[str, str]()
    for raw in (FIXTURES / scenario / "record.jsonl").read_text().splitlines():
        wrapper = object_of(json.loads, raw)
        row = wrapper.get("row") if is_object_dict(wrapper) else None
        message = row.get("message") if is_object_dict(row) else None
        if not (is_object_dict(row) and is_object_dict(message) and row["type"] == "assistant"):
            continue
        content = message["content"]
        assert is_object_list(content) and len(content) == 1
        block = content[0]
        assert is_object_dict(block)
        text = block.get("text", block.get("thinking"))
        if isinstance(text, str):
            blocks[f"{message['id']}:{row['apiBlockIndex']}"] = text
    return blocks


@pytest.mark.parametrize(
    "scenario", ["text_only", "thinking_text_tool", "slow_tool", "interrupt_text"]
)
def test_every_streamed_block_is_a_row_of_the_record_with_the_same_text(scenario: str) -> None:
    streamed, recorded = _streamed(scenario), _recorded(scenario)
    assert streamed and set(streamed) <= set(recorded)
    for block, text in streamed.items():
        assert text.startswith(recorded[block].removesuffix("[truncated]").rstrip())


def test_a_thinking_block_that_was_interrupted_has_no_row() -> None:
    assert _streamed("interrupt_thinking") and not _recorded("interrupt_thinking")
